"""Single-hop transport and public-API navigation coordination.

Protected redirects are never handed back to the browser. Main-frame GET
navigation is resumed outside route callbacks; other redirects fail closed.
Only fixed error codes and sanitized endpoint diagnostics leave this module.
Raw URLs, headers and bodies stay in memory.
"""
from __future__ import annotations

import time
import json
import re
from weakref import WeakSet
from urllib.parse import urljoin, urlsplit, urldefrag
from utils.mutation_fingerprint import safe_endpoint_fingerprint
from utils.localization_request_policy import is_verified_localization_return


MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
REDIRECTS = {301, 302, 303, 307, 308}
SIGNATURE_NAMES = {"signature", "signature-input", "signature-agent"}
STOREFRONT_READ_PATH = re.compile(r"/(?:|search/?|products/[A-Za-z0-9-]+/?|collections/[A-Za-z0-9-]+(?:/products/[A-Za-z0-9-]+)?/?)")


def is_shop_bridge_target(url):
    """The observed Shop anonymous redirect endpoint; never a signing host."""
    try:
        parsed = urlsplit(url)
        return (len(url) <= 16384 and parsed.scheme == "https" and parsed.hostname == "shop.app"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and parsed.path == "/accounts/sur" and not parsed.fragment
                and not any(ord(c) <= 32 or ord(c) == 127 for c in url))
    except (ValueError, TypeError):
        return False


class RequestBoundaryError(RuntimeError):
    category = "REQUEST_BOUNDARY_FAILURE"

    def __init__(self, code, diagnostic=None):
        self.code, self.diagnostic = code, diagnostic
        super().__init__(code + (" " + json.dumps(diagnostic, separators=(",", ":")) if diagnostic else ""))


class _RedirectRequest:
    def __init__(self, original, url, method):
        self.original, self.url, self.method = original, url, method

    def __getattr__(self, name):
        return getattr(self.original, name)


class _DecisionRoute:
    """Run existing mutation classification without sending a request."""
    def __init__(self, request):
        self.request, self.allowed = request, False

    def abort(self):
        self.allowed = False

    def fallback(self):
        self.allowed = True

    continue_ = fallback


class RequestBoundary:
    def __init__(self, context):
        self.context = context
        self.access_policy = None
        self.guards = []
        self.pending = {}
        self.settled = WeakSet()
        self.failures = []
        self.events = []
        self.bridge_events = []
        self.closing = False
        self._handler = self._handle_route
        context.route("**/*", self._handler)

    def register_guard(self, guard):
        if guard not in self.guards:
            self.guards.append(guard)

    def remove_guard(self, guard):
        if guard in self.guards:
            self.guards.remove(guard)

    def assert_clean(self):
        if self.failures:
            diagnostic = next((event for event in self.events if event["gating_failure"]), None)
            raise RequestBoundaryError(self.failures[0], diagnostic)
        if self.pending:
            raise RequestBoundaryError("REDIRECT_NAVIGATION_NOT_COMPLETED")

    def summary(self):
        return {"status": "FAIL" if self.failures or self.pending else "PASS",
                "failure_count": len(self.failures), "pending_navigation_count": len(self.pending),
                "events": [dict(event) for event in self.events],
                "bridge_events": [dict(event) for event in self.bridge_events]}

    def _anonymous_shop_return(self, route, target):
        """Read only the fixed bridge, returning only to the exact source URL.

        No Shop document is rendered or script executed, and no mutation is
        issued. A non-redirect bridge response fails closed. Only the original
        storefront response can update the storefront context's cookie jar.
        """
        request = route.request
        if request.method != "GET" or not request.is_navigation_request():
            return None
        try:
            if request.frame.parent_frame is not None:
                return None
        except Exception:
            return None
        if not STOREFRONT_READ_PATH.fullmatch(urlsplit(request.url).path):
            return None
        deadline = time.monotonic() + 15000 / 1000
        visited = set()
        # Do not copy Origin, Referer, Cookie, authorization, signing headers,
        # request bodies, or arbitrary page/SDK headers to Shop.
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in {"user-agent", "accept", "accept-language"}}
        # APIRequestContext otherwise loads cookies from the shared jar.
        headers["Cookie"] = ""
        # route.fetch merges the browser context's default headers. Use a
        # separate blank context so signing/authorization defaults cannot enter
        # the bridge request. Its cookie jar is never exported to the storefront.
        bridge_context = self._new_bridge_context()
        try:
            for _ in range(3):
                if not is_shop_bridge_target(target) or target in visited or time.monotonic() >= deadline:
                    return None
                visited.add(target)
                event = {"service": "SHOP_APP", "method": "GET", "path": "/accounts/sur",
                         "signature_headers": "OMITTED", "response_status": None}
                if len(self.bridge_events) < 100:
                    self.bridge_events.append(event)
                response = bridge_context.request.get(target, headers=headers,
                                         max_redirects=0, max_retries=0,
                                         timeout=max(1, int((deadline-time.monotonic())*1000)))
                try:
                    event["response_status"] = response.status
                    if response.status not in REDIRECTS:
                        return None
                    location = response.headers.get("location")
                    if not location or any(ord(c) <= 32 or ord(c) == 127 for c in location):
                        return None
                    following = urljoin(target, location)
                    allowed_host = self.access_policy._host_allowed(following)
                    fingerprint = safe_endpoint_fingerprint(following, "GET", self.access_policy._allowed_hosts)
                    event.update(return_host_class=fingerprint["host_class"],
                                 return_path=fingerprint["sanitized_path"],
                                 return_query_present=fingerprint["query_present"],
                                 return_relation="OTHER_ENDPOINT")
                    source_parts, return_parts = urlsplit(request.url), urlsplit(following)
                    if (source_parts.scheme, source_parts.netloc, source_parts.path) == (
                            return_parts.scheme, return_parts.netloc, return_parts.path):
                        event["return_relation"] = "SAME_ENDPOINT_DIFFERENT_QUERY"
                    if allowed_host and urldefrag(following)[0] == urldefrag(request.url)[0]:
                        event["return_relation"] = "EXACT_SOURCE"
                        return following
                    target = following
                finally:
                    response.dispose()
            return None
        finally:
            bridge_context.close()

    def _new_bridge_context(self):
        return self.context.browser.new_context(service_workers="block")

    def _reject(self, route, code, *, target=None, status=None, gating=True):
        if gating:
            self.failures.append(code)
        request = route.request
        if len(self.events) < 100:
            hosts = getattr(self.access_policy, "_allowed_hosts", ())
            source = safe_endpoint_fingerprint(request.url, request.method, hosts)
            destination = safe_endpoint_fingerprint(target, request.method, hosts) if target else {}
            resource = request.resource_type
            try:
                frame_category = "MAIN_FRAME" if request.frame.parent_frame is None else "CHILD_FRAME"
            except Exception:
                frame_category = "UNAVAILABLE"
            self.events.append({
                "code": code, "resource_type": resource if resource in {
                    "document", "stylesheet", "script", "image", "font", "xhr", "fetch", "other",
                    "media", "manifest", "texttrack", "eventsource", "websocket"} else "UNKNOWN",
                "method": request.method if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "UNKNOWN",
                "source_path": source["sanitized_path"],
                "target_path": destination.get("sanitized_path"),
                "source_host_class": source["host_class"],
                "target_host_class": destination.get("host_class"), "http_status": status,
                "gating_failure": gating, "source_query_present": source["query_present"],
                "frame_category": frame_category,
                "target_service": "SHOP_APP" if target and urlsplit(target).hostname == "shop.app" else "OTHER",
            })
        if not gating:
            # Preserve the blocked-response signal. A synthetic 2xx can make
            # localization clients replace the live document with empty HTML.
            # The initial POST's real cookie updates remain in the shared jar.
            route.abort()
            return
        try:
            main_navigation = request.is_navigation_request() and request.frame.parent_frame is None
        except Exception:
            main_navigation = request.is_navigation_request()
        if main_navigation:
            route.fulfill(status=204, body="")
        else:
            route.abort()

    def _handle_route(self, route):
        request = route.request
        policy = self.access_policy
        signed = policy is not None and policy._host_allowed(request.url)
        clean = {k: v for k, v in request.headers.items() if k.lower() not in SIGNATURE_NAMES}
        if policy is not None:
            policy.stats["injected" if signed else "untouched"] += 1
        if not signed and request.method.upper() not in MUTATION_METHODS:
            route.continue_(headers=clean)
            self.settled.add(request)
            return
        # fetch merges context defaults for omitted names. Empty overrides stop
        # stale signing values from reappearing on an unsigned initial POST.
        cleared = {name: "" for name in SIGNATURE_NAMES
                   if any(key.lower() == name for key in request.headers)}
        headers = {**cleared, **clean, **(policy.request_headers(request.url) if signed else {})}
        response = None
        try:
            response = route.fetch(headers=headers, max_redirects=0, max_retries=0, timeout=15000)
            if response.status not in REDIRECTS:
                route.fulfill(response=response)
                return
            location = response.headers.get("location")
            if not location or any(ord(c) < 32 for c in location):
                self._reject(route, "REDIRECT_LOCATION_INVALID", status=response.status)
                return
            target = urljoin(request.url, location)
            parsed = urlsplit(target)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.port == 0):
                self._reject(route, "REDIRECT_LOCATION_INVALID", status=response.status)
                return
            method = request.method.upper()
            if (response.status == 303 and method != "HEAD") or (response.status in {301, 302} and method == "POST"):
                method = "GET"
            redirected = _RedirectRequest(request, target, method)
            allowed = True
            for guard in self.guards:
                decision = _DecisionRoute(redirected)
                guard._handle_route(decision)
                allowed = allowed and decision.allowed
            if not allowed:
                self._reject(route, "REDIRECT_MUTATION_DENIED", target=target, status=response.status)
                return
            if signed and not policy._host_allowed(target):
                returned = self._anonymous_shop_return(route, target) if is_shop_bridge_target(target) else None
                if returned is None:
                    self._reject(route, "REDIRECT_ORIGIN_DENIED", target=target, status=response.status)
                    return
                target = returned
            if (signed and request.resource_type in {"xhr", "fetch"}
                    and not request.is_navigation_request()
                    and is_verified_localization_return(request.url, response.status, target, request)):
                self._reject(route, "LOCALIZATION_RETURN_READ_SUPPRESSED", target=target,
                             status=response.status, gating=False)
                return
            if method not in {"GET", "HEAD"} or not request.is_navigation_request():
                self._reject(route, "REDIRECT_UNSUPPORTED", target=target, status=response.status)
                return
            try:
                frame = request.frame
            except Exception:
                # A popup's first request can precede its public Page/Frame.
                # Associate it with the single new Page after returning 204.
                frame = None
            if frame is not None and frame.parent_frame is not None:
                self._reject(route, "REDIRECT_UNSUPPORTED", target=target, status=response.status)
                return
            page = frame.page if frame is not None else None
            if page in self.pending:
                self._reject(route, "REDIRECT_NAVIGATION_CONFLICT", target=target, status=response.status)
                return
            self.pending[page] = target
            # A 204 ends this navigation without a Chromium error-page race.
            # Cookie updates already live in the shared request/context jar.
            if frame is None:
                # A 204 would cancel an unopened popup. An inert document
                # exposes its public Page; the caller then navigates it.
                route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Navigation</title>",
                              headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'"})
            else:
                route.fulfill(status=204, body="")
        except Exception as exc:
            if not (self.closing and type(exc).__name__ == "TargetClosedError"):
                self.failures.append("REQUEST_TRANSPORT_FAILED")
            try:
                route.abort()
            except Exception:
                pass
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    if not self.closing:
                        self.failures.append("RESPONSE_DISPOSAL_FAILED")
            self.settled.add(request)

    def navigate(self, page, action, *, timeout_ms=45000):
        """Perform one real UI action, then complete validated GET redirects."""
        if self.pending:
            raise RequestBoundaryError("REDIRECT_NAVIGATION_NOT_COMPLETED")
        deadline = time.monotonic() + timeout_ms / 1000
        known_pages = set(self.context.pages)
        visited = set()
        result = None
        for _ in range(11):
            previous_errors = len(self.failures)
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            def navigation_request(request):
                if not request.is_navigation_request():
                    return False
                try:
                    return (request.frame.parent_frame is None
                            and (request.frame.page is page or request.frame.page not in known_pages))
                except Exception:
                    return True
            try:
                # WebKit clicks can finish before their navigation is routed.
                # Subscribe before the gesture and wait for its actual response.
                with self.context.expect_event("request", predicate=navigation_request, timeout=remaining) as started:
                    result = action()
                # Request.response() has no timeout and some WebKit 204 popup
                # navigations never resolve it. Pump events only until this
                # boundary has made its decision, with the operation deadline.
                initial_request = started.value
                while initial_request not in self.settled:
                    if time.monotonic() >= deadline:
                        raise RequestBoundaryError("NAVIGATION_RESPONSE_TIMEOUT")
                    page.wait_for_timeout(20)
            except Exception:
                if not self.pending:
                    if len(self.failures) > previous_errors:
                        raise RequestBoundaryError(self.failures[-1], self.events[-1] if self.events else None) from None
                    raise
            if len(self.failures) > previous_errors:
                raise RequestBoundaryError(self.failures[-1], self.events[-1] if self.events else None)
            candidates = [p for p in self.pending if p is page or p not in known_pages]
            if not candidates:
                return result
            if len(candidates) != 1:
                self.failures.append("REDIRECT_NAVIGATION_CONFLICT")
                raise RequestBoundaryError(self.failures[-1])
            destination_page = candidates[0]
            target = self.pending.pop(destination_page)
            if destination_page is None:
                while not (created := [p for p in self.context.pages if p not in known_pages]):
                    if time.monotonic() >= deadline:
                        self.failures.append("POPUP_NAVIGATION_UNAVAILABLE")
                        raise RequestBoundaryError(self.failures[-1])
                    page.wait_for_timeout(20)
                if len(created) != 1:
                    self.failures.append("REDIRECT_NAVIGATION_CONFLICT")
                    raise RequestBoundaryError(self.failures[-1])
                destination_page = created[0]
            key = (destination_page, urldefrag(target)[0])
            if key in visited or time.monotonic() >= deadline:
                self.failures.append("REDIRECT_LOOP_OR_LIMIT")
                raise RequestBoundaryError(self.failures[-1])
            visited.add(key)
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            action = lambda: destination_page.goto(target, wait_until="domcontentloaded", timeout=remaining)
        self.failures.append("REDIRECT_LOOP_OR_LIMIT")
        raise RequestBoundaryError(self.failures[-1])


def ensure_request_boundary(context):
    boundary = getattr(context, "_shopify_request_boundary", None)
    if not isinstance(boundary, RequestBoundary):
        boundary = RequestBoundary(context)
        context._shopify_request_boundary = boundary
    return boundary


def run_navigation(page, action, *, timeout_ms=45000):
    boundary = getattr(getattr(page, "context", None), "_shopify_request_boundary", None)
    return boundary.navigate(page, action, timeout_ms=timeout_ms) if isinstance(boundary, RequestBoundary) else action()


def assert_request_boundary_clean(context):
    boundary = getattr(context, "_shopify_request_boundary", None)
    if isinstance(boundary, RequestBoundary):
        boundary.assert_clean()

"""Fail-closed routing guard for Readonly storefront runs.

The guard protects only cart mutation endpoints and mutation-capable HTTP
methods. Non-matching requests use Playwright ``route.fallback()`` so a route
registered earlier by the Signed Request policy remains in the chain.
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlsplit


CART_MUTATION_PATHS = frozenset(
    {
        "/cart/add",
        "/cart/add.js",
        "/cart/change",
        "/cart/change.js",
        "/cart/update",
        "/cart/update.js",
        "/cart/clear",
        "/cart/clear.js",
    }
)
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ReadonlyMutationGuard:
    """Block and record cart mutation requests for one BrowserContext."""

    def __init__(self) -> None:
        self._scope = {"viewport": None, "journey": None, "case_id": None}
        self._violations: List[dict] = []
        self._context = None
        self._attached = False
        self._handler = self._handle_route

    @staticmethod
    def normalize_path(url: str) -> str:
        """Return a lower-case path without query, fragment, or trailing slash."""
        try:
            path = urlsplit(str(url or "")).path or "/"
        except ValueError:
            return ""
        path = "/" + path.lstrip("/")
        path = path.lower()
        return path.rstrip("/") or "/"

    @classmethod
    def matches(cls, url: str, method: str = "GET") -> bool:
        """Return whether *method* and *url* identify a cart mutation."""
        normalized_method = str(method or "").upper()
        return (
            normalized_method in MUTATION_METHODS
            and cls.normalize_path(url) in CART_MUTATION_PATHS
        )

    is_cart_mutation = matches

    def set_scope(
        self,
        viewport: Optional[str],
        journey: Optional[str],
        case_id: Optional[str],
    ) -> None:
        """Set the safe attribution scope for subsequently intercepted requests."""
        self._scope = {
            "viewport": viewport,
            "journey": journey,
            "case_id": case_id,
        }

    def _handle_route(self, route) -> None:
        request = route.request
        method = str(getattr(request, "method", "") or "").upper()
        path = self.normalize_path(getattr(request, "url", ""))
        if self.matches(getattr(request, "url", ""), method):
            self._violations.append(
                {
                    "method": method,
                    "path": path,
                    "case_id": self._scope.get("case_id"),
                    "journey": self._scope.get("journey"),
                    "viewport": self._scope.get("viewport"),
                }
            )
            # Record before aborting. There is deliberately no fallback on a
            # match: the request must not reach Shopify.
            route.abort()
            return

        fallback = getattr(route, "fallback", None)
        if callable(fallback):
            fallback()
            return
        # Small fakes and older Playwright adapters may not expose fallback;
        # continue is the safe final fallback when no earlier route exists.
        route.continue_()

    def attach(self, context) -> None:
        """Attach after any existing route so non-matches fall back to it."""
        if self._attached:
            return
        context.route("**/*", self._handler)
        self._context = context
        self._attached = True

    def detach(self) -> None:
        """Remove the handler when the context remains usable."""
        if not self._attached or self._context is None:
            return
        try:
            self._context.unroute("**/*", self._handler)
        except Exception:
            # Context disposal is still owned by BrowserRuntime; a closed
            # context makes unroute unnecessary.
            pass
        finally:
            self._context = None
            self._attached = False

    def violations(self) -> List[dict]:
        """Return copies of safe violation records."""
        return [dict(item) for item in self._violations]

    def violation_count(self) -> int:
        return len(self._violations)

    def violations_since(self, index: int) -> List[dict]:
        """Return violations captured at or after a prior count."""
        return [dict(item) for item in self._violations[max(0, int(index)):]]

    def has_violations(self) -> bool:
        return bool(self._violations)

    def out_of_scope_violations(self) -> List[dict]:
        """Return violations not attributed to a Readonly Case scope."""
        return [item for item in self.violations() if not item.get("case_id")]

    @staticmethod
    def safe_detail(violation: dict) -> str:
        """Render only method and normalized path for user-facing diagnostics."""
        return f"blocked cart mutation: {violation.get('method', '')} {violation.get('path', '')}"

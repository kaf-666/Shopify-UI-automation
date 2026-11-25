from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from utils.request_boundary import RequestBoundary, RequestBoundaryError


class Request:
    url = "https://store.invalid/products/example?opaque=SECRET"
    headers = {}
    method = "GET"
    resource_type = "fetch"

    def is_navigation_request(self):
        return False


def boundary_with_failure(error, closing):
    boundary = RequestBoundary(SimpleNamespace(route=lambda *_: None))
    boundary.closing = closing
    boundary.access_policy = SimpleNamespace(
        _host_allowed=lambda _: True, _allowed_hosts=("store.invalid",),
        stats={"injected":0,"untouched":0}, request_headers=lambda _: {})
    def fail(**_):
        raise error
    route = SimpleNamespace(request=Request(), fetch=fail, abort=lambda: None)
    return boundary, route


@pytest.mark.parametrize("message", ["Route.fetch: Request context disposed.",
                                    "Route.fulfill: Target page, context or browser has been closed",
                                    "Context closed"])
def test_expected_driver_cancellation_only_ignored_during_intentional_shutdown(message):
    boundary, route = boundary_with_failure(PlaywrightError(message), True)
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_cancellation_count"] == 1
    boundary, route = boundary_with_failure(PlaywrightError(message), False)
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


@pytest.mark.parametrize("error", [PlaywrightError("net::ERR_CONNECTION_RESET SECRET"),
                                 RuntimeError("Request context disposed SECRET")])
def test_unknown_transport_errors_still_fail_during_shutdown_without_printing_values(error):
    boundary, route = boundary_with_failure(error, True)
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()
    assert "SECRET" not in str(boundary.summary())
    diagnostic = boundary.summary()["transport_errors"][0]
    assert diagnostic["host_class"] == "FIRST_PARTY"
    assert diagnostic["resource_type"] == "fetch"
    assert diagnostic["phase"] == "FETCH"


def test_shutdown_cancellation_does_not_erase_a_previous_safety_failure():
    boundary, route = boundary_with_failure(PlaywrightError("Request context disposed."), True)
    boundary.failures.append("REDIRECT_ORIGIN_DENIED")
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REDIRECT_ORIGIN_DENIED"):
        boundary.assert_clean()


@pytest.mark.parametrize('resource', ['script','stylesheet','image','font'])
def test_passive_asset_disposal_only_cancelled_after_intentional_close(resource):
    boundary,route=boundary_with_failure(PlaywrightError('opaque driver response disposal SECRET'),True)
    route.request.resource_type=resource
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()['closing_passive_read_cancellation_count'] == 1
    assert 'SECRET' not in str(boundary.summary())
    boundary,route=boundary_with_failure(PlaywrightError('opaque driver response disposal'),False)
    route.request.resource_type=resource
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError,match='REQUEST_TRANSPORT_FAILED'):
        boundary.assert_clean()


@pytest.mark.parametrize('method,resource,navigation', [
    ('POST','script',False),('GET','xhr',False),('GET','fetch',False),('GET','document',True),
])
def test_shutdown_cannot_hide_writing_or_business_request_errors(method,resource,navigation):
    boundary,route=boundary_with_failure(PlaywrightError('opaque driver error'),True)
    route.request.method=method
    route.request.resource_type=resource
    route.request.is_navigation_request=lambda:navigation
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError,match='REQUEST_TRANSPORT_FAILED'):
        boundary.assert_clean()


def test_passive_disposal_does_not_erase_prior_safety_denial():
    boundary,route=boundary_with_failure(PlaywrightError('opaque disposal error'),True)
    route.request.resource_type='script'
    boundary.failures.append('REDIRECT_MUTATION_DENIED')
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError,match='REDIRECT_MUTATION_DENIED'):
        boundary.assert_clean()


MONORAIL_PATH = "/.well-known/shopify/monorail/unstable/produce_batch"


def _monorail_route(error=PlaywrightError("opaque driver error"), *, closing=True,
                    url="https://store.invalid" + MONORAIL_PATH,
                    method="POST", resource_type="fetch", first_party=True):
    boundary, route = boundary_with_failure(error, closing)
    route.request.url = url
    route.request.method = method
    route.request.resource_type = resource_type
    if not first_party:
        boundary.access_policy._host_allowed = lambda _url: False
    return boundary, route


def test_known_shopify_telemetry_post_cancellation_is_non_gating():
    boundary, route = _monorail_route()
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1
    assert boundary.summary()["closing_cancellation_count"] == 1

    boundary, route = _monorail_route(closing=False)
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1

    boundary, route = _monorail_route(
        closing=False, resource_type="document"
    )
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1


@pytest.mark.parametrize("url,method,resource_type", [
    ("https://store.invalid/other", "POST", "fetch"),
    ("https://store.invalid" + MONORAIL_PATH, "GET", "fetch"),
])
def test_telemetry_cancellation_rule_is_narrow(url, method, resource_type):
    boundary, route = _monorail_route(url=url, method=method, resource_type=resource_type)
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


def test_third_party_monorail_is_covered_by_the_generic_telemetry_rule():
    boundary, route = _monorail_route(first_party=False)
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1


def test_third_party_events_cancellation_is_non_gating():
    boundary, route = _monorail_route(
        url="https://analytics.invalid/events", first_party=False
    )
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1

    boundary, route = _monorail_route(
        url="https://analytics.invalid/events", first_party=False, closing=False
    )
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1


def test_third_party_metrics_cancellation_is_also_ignored_during_close():
    boundary, route = _monorail_route(
        url="https://otlp.invalid/v1/metrics", first_party=False
    )
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_telemetry_post_cancellation_count"] == 1


def test_third_party_telemetry_rule_does_not_allow_first_party_or_other_methods():
    boundary, route = _monorail_route(
        url="https://store.invalid/events", first_party=True
    )
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()

    boundary, route = _monorail_route(
        url="https://analytics.invalid/events", first_party=False,
        resource_type="document",
    )
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


def test_telemetry_cancellation_without_policy_remains_gating():
    boundary, route = _monorail_route(url="https://analytics.invalid/events", first_party=False)
    boundary.access_policy = None
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


def test_intentional_page_disposal_cancellation_is_non_gating():
    boundary, route = _monorail_route(
        url="https://store.invalid/api/2026-01/graphql.json",
        method="POST", resource_type="fetch", closing=False,
    )
    page = object()
    route.request.frame = SimpleNamespace(page=page)
    boundary.begin_page_disposal([page])
    try:
        boundary._handle_route(route)
    finally:
        boundary.end_page_disposal([page])
    boundary.assert_clean()
    assert boundary.summary()["page_disposal_cancellation_count"] == 1


def test_first_party_cdn_asset_cancellation_is_passive_during_close():
    boundary, route = _monorail_route(
        url="https://store.invalid/cdn/shopifycloud/checkout-web/assets/c1/x.js",
        method="GET", resource_type="other", closing=True, first_party=True,
    )
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_passive_read_cancellation_count"] == 1


WEB_PIXEL_PATH = (
    "/web-pixels@997a76bdw6b2572e2pb4e93427m632f4b2a/app/"
    "web-pixel-1807647029@f529b794e365d392723141141e99dd15/sandbox/modern/"
    "collections/wedding-guest-dresses/products/example"
)


def test_first_party_web_pixel_sandbox_read_is_passive_during_close():
    boundary, route = _monorail_route(
        url="https://store.invalid" + WEB_PIXEL_PATH,
        method="GET", resource_type="document", closing=True, first_party=True,
    )
    route.request.is_navigation_request = lambda: True
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["closing_passive_read_cancellation_count"] == 1


@pytest.mark.parametrize("closing,first_party,method,path", [
    (False, True, "GET", WEB_PIXEL_PATH),
    (True, True, "POST", WEB_PIXEL_PATH),
    (True, True, "GET", "/collections/wedding-guest-dresses/products/example"),
])
def test_web_pixel_sandbox_rule_is_narrow(closing, first_party, method, path):
    boundary, route = _monorail_route(
        url="https://store.invalid" + path,
        method=method, resource_type="document", closing=closing,
        first_party=first_party,
    )
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


def test_navigation_read_cancellation_is_non_gating_while_marked():
    boundary, route = _monorail_route(
        url="https://store.invalid/products/example-dress?variant=1",
        method="GET", resource_type="fetch", closing=False, first_party=True,
    )
    page = object()
    route.request.frame = SimpleNamespace(page=page)
    boundary.begin_navigation_reads([page])
    try:
        boundary._handle_route(route)
    finally:
        boundary.end_navigation_reads([page])
    boundary.assert_clean()
    assert boundary.summary()["navigation_read_cancellation_count"] == 1


@pytest.mark.parametrize("method,resource_type,navigation", [
    ("POST", "fetch", False),
    ("GET", "document", True),
    ("GET", "fetch", True),
])
def test_navigation_read_rule_is_narrow(method, resource_type, navigation):
    boundary, route = _monorail_route(
        url="https://store.invalid/products/example-dress",
        method=method, resource_type=resource_type, closing=False,
        first_party=True,
    )
    page = object()
    route.request.frame = SimpleNamespace(page=page)
    route.request.is_navigation_request = lambda: navigation
    boundary.begin_navigation_reads([page])
    try:
        boundary._handle_route(route)
    finally:
        boundary.end_navigation_reads([page])
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


def test_finalization_ignores_non_navigation_transport_noise():
    boundary, route = _monorail_route(
        url="https://store.invalid/api/2026-01/graphql.json",
        method="POST", resource_type="fetch", closing=False,
    )
    boundary.begin_finalization()
    boundary._handle_route(route)
    boundary.assert_clean()
    assert boundary.summary()["finalization_cancellation_count"] == 1


def test_finalization_does_not_hide_navigation_failure():
    boundary, route = _monorail_route(
        url="https://store.invalid/products/example",
        method="GET", resource_type="document", closing=False,
    )
    route.request.is_navigation_request = lambda: True
    boundary.begin_finalization()
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REQUEST_TRANSPORT_FAILED"):
        boundary.assert_clean()


def test_telemetry_cancellation_does_not_erase_prior_safety_denial():
    boundary, route = _monorail_route()
    boundary.failures.append("REDIRECT_MUTATION_DENIED")
    boundary._handle_route(route)
    with pytest.raises(RequestBoundaryError, match="REDIRECT_MUTATION_DENIED"):
        boundary.assert_clean()

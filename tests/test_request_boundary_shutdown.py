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

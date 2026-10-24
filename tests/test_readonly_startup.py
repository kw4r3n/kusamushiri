"""Offline API-shaped fixtures; no actual browser, headers, auth or network."""

import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from readonly_startup import TRANSPORT_GUARD, LoopbackReadOnlyPlan, ReadOnlyStartupAdapter

TARGET = "http://127.0.0.1:8765/target/123"
ASSET = "http://127.0.0.1:8765/bootstrap.js"
DETAIL = "http://127.0.0.1:8765/read?target=123"
DELETE = "http://127.0.0.1:8765/write"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Request:
    def __init__(self, page, url=TARGET, method="GET", resource="document", *, target_id=None, frame=None):
        self.url, self.method, self.resource_type = url, method, resource
        if target_id is not None and url.startswith(DETAIL):
            self.url = url.replace("target=123", "target=" + target_id)
        self.body = None
        self.frame = frame or page.main_frame
        self.redirected_from = None

    @property
    def headers(self):
        pytest.fail("Request headers/auth must not be inspected")

    @property
    def post_data(self):
        return self.body  # Real Request.post_data API; GET body must be absent.


class Reply:
    def __init__(self, route):
        self.route = route
        self.url = route.request.url if route.context.response_url is None else route.context.response_url
        self.status = route.context.status
        self.disposed = False

    @property
    def headers(self):
        pytest.fail("Response headers/auth must not be inspected")

    def body(self):
        pytest.fail("Response bodies must not be inspected")

    def json(self):
        pytest.fail("Response payloads must not be inspected")

    def dispose(self):
        self.route.context.fault("dispose")
        self.disposed = True


class Route:
    def __init__(self, context, request):
        self.context, self.request = context, request
        self.index = 0
        self.aborted = self.fulfilled = False
        self.response = None

    def next(self):
        handlers = list(reversed(self.context.handlers))
        if self.index >= len(handlers):
            pytest.fail("Unexpected uncontrolled fallthrough")
        handler = handlers[self.index]
        self.index += 1
        handler(self)

    def fallback(self):
        self.context.order.append("fallback")
        self.next()

    def abort(self, reason):
        assert reason == "blockedbyclient"
        self.context.fault("abort")
        self.aborted = True

    def continue_(self, **kwargs):
        pytest.fail("continue_ can escape redirect routing and must never be called")

    def fetch(self, **kwargs):
        assert kwargs["url"] == self.request.url and kwargs["method"] == "GET"
        assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
        assert 0 < kwargs["timeout"] <= 10000
        assert set(kwargs) == {"url", "method", "max_redirects", "max_retries", "timeout"}
        self.context.fault("fetch")
        self.context.fetches.append((self.request.url, self.request.method))
        self.response = Reply(self)
        return self.response

    def fulfill(self, **kwargs):
        assert set(kwargs) == {"response"} and kwargs["response"] is self.response
        self.context.fault("fulfill")
        self.fulfilled = True


class Page:
    def __init__(self, context):
        self.context = context
        self.main_frame = SimpleNamespace(page=self, url="about:blank")
        self.closed = False
        self.navigations = []
        self.requests = []

    def goto(self, url, *, timeout, wait_until):
        assert wait_until == "commit" and 0 < timeout <= 10000
        self.navigations.append(url)
        self.context.order.append("goto")
        self.requests.append(self.context.dispatch(Request(self, url)))
        self.main_frame.url = url
        for request in self.context.script_requests(self):
            self.requests.append(self.context.dispatch(request))
        self.context.after_goto(self)

    def wait_for_timeout(self, milliseconds):
        self.context.clock.now += milliseconds / 1000
        self.context.on_wait(self)

    def close(self):
        self.context.fault("page_close")
        self.closed = True

    def click(self, *args, **kwargs):
        pytest.fail("Read-only adapter must not click")

    def evaluate(self, *args, **kwargs):
        pytest.fail("Read-only adapter must not run page actions")


class Context:
    def __init__(self, browser):
        self.browser, self.clock = browser, browser.clock
        self.pages = []
        self.handlers = []
        self.callbacks = {}
        self.order = browser.order
        self.fetches = []
        self.routes = []
        self.closed = False
        self.offline = False
        self.status = 200
        self.response_url = None
        self.ws_handler = None
        self.guard_script = None
        self.script_requests = lambda page: [Request(page, ASSET, resource="script"), Request(page, DETAIL, resource="xhr", target_id="123")]
        self.startup_requests = lambda page: []
        self.after_goto = self.on_wait = lambda page: None

    def fault(self, stage):
        if stage in self.browser.faults:
            raise RuntimeError("AUTH_ERROR_CANARY must not appear in diagnostics")

    def route(self, matcher, handler):
        assert matcher == "**/*"
        self.fault("route" + str(len(self.handlers)))
        self.handlers.append(handler)
        self.order.append(handler.__name__)

    def route_web_socket(self, matcher, handler):
        assert matcher == "**/*"
        self.fault("websocket_route")
        self.ws_handler = handler
        self.order.append("websocket_route")

    def add_init_script(self, script):
        self.fault("init_script")
        self.guard_script = script
        self.order.append("init_script")

    def on(self, event, handler):
        self.fault("listener_" + event)
        self.callbacks[event] = handler

    def new_page(self):
        self.fault("new_page")
        assert len(self.handlers) == 2 and self.ws_handler and self.guard_script
        assert set(self.callbacks) == {"page", "serviceworker"}
        self.order.append("new_page")
        page = Page(self)
        self.pages.append(page)
        self.callbacks["page"](page)
        for request in self.startup_requests(page):
            self.dispatch(request)
        return page

    def dispatch(self, request):
        route = Route(self, request)
        self.routes.append(route)
        route.next()
        return route

    def close(self):
        self.fault("context_close")
        # All blocks must still exist until the owned context closes.
        self.order.append("context_close")
        for page in self.pages:
            page.closed = True
        self.closed = True
        self.browser.contexts.remove(self)

    def set_offline(self, value):
        self.fault("offline")
        self.offline = value

    def unroute(self, *args):
        pytest.fail("Never remove routing before context cleanup")


class FixtureBrowser:
    offline_fixture = True

    def __init__(self, clock, *, faults=()):
        self.clock, self.faults = clock, set(faults)
        self.order = []
        self.borrowed = SimpleNamespace(closed=False, history=["untouched"], config={"AUTH_STATE_CANARY": "untouched"})
        self.contexts = [self.borrowed]
        self.owned = None
        self.configure = lambda context: None

    def new_context(self, **kwargs):
        if "new_context" in self.faults:
            raise RuntimeError("AUTH_ERROR_CANARY")
        assert kwargs == {"service_workers": "block", "accept_downloads": False, "java_script_enabled": True}
        self.order.append("new_context")
        self.owned = context = Context(self)
        self.contexts.append(context)
        self.configure(context)
        return context

    def launch_persistent_context(self, *args, **kwargs):
        pytest.fail("Live/persistent profile startup forbidden")

    def close(self):
        pytest.fail("Borrowed browser must not be closed")


def plan():
    return LoopbackReadOnlyPlan(TARGET, frozenset({(TARGET, "document"), (ASSET, "script"), (DETAIL, "xhr")}), True, "loopback-fixture")


def run(configure=lambda context: None, *, fixture_plan=None, faults=()):
    clock = Clock()
    browser = FixtureBrowser(clock, faults=faults)
    browser.configure = configure
    adapter = ReadOnlyStartupAdapter(fixture_plan or plan(), monotonic=clock)
    result = adapter.run(browser)
    assert not browser.borrowed.closed and browser.borrowed.history == ["untouched"]
    assert browser.borrowed.config == {"AUTH_STATE_CANARY": "untouched"}
    assert "AUTH_" not in json.dumps(result.events) and "AUTH_" not in result.stop_reason
    return result, browser, adapter


def test_routes_guards_before_first_dispatch_and_timeout_restores_resources():
    result, browser, adapter = run()
    order = browser.order
    assert order.index("_get_policy") < order.index("_mutation_block") < order.index("websocket_route") < order.index("init_script") < order.index("new_page") < order.index("goto")
    assert result.stop_reason == "time_limit" and result.attempts == result.fetched == 3
    assert result.cleanup_complete and browser.owned.closed and browser.contexts == [browser.borrowed]
    assert all(r.fulfilled and r.response.disposed for r in browser.owned.routes)
    assert adapter.context is adapter.page is None
    assert not hasattr(adapter, "arm_delete")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get"])
def test_independent_blanket_block_precedes_policy_without_dispatch(method):
    def configure(context):
        context.script_requests = lambda page: [Request(page, DELETE, method, "xhr")]
    result, browser, adapter = run(configure)
    assert result.stop_reason == "mutation_or_non_get_blocked"
    assert result.fetched == 1 and browser.owned.routes[-1].aborted
    assert not any(url == DELETE for url, method in browser.owned.fetches)


def test_independent_block_still_holds_when_get_policy_is_removed_in_fixture():
    def configure(context):
        def during_goto(page):
            context.handlers = [context.handlers[-1]]
            route = context.dispatch(Request(page, DELETE, "POST", "xhr"))
            assert route.aborted
        context.script_requests = lambda page: []
        context.after_goto = during_goto
    result, browser, adapter = run(configure)
    assert result.stop_reason == "mutation_or_non_get_blocked" and result.fetched == 1


@pytest.mark.parametrize("variant", ["unknown", "query-hash", "query", "suffix", "foreign", "subframe", "redirect", "target", "wrong-resource", "duplicate", "get-mutation"])
def test_unknown_or_unowned_requests_stop_without_fetching(variant):
    def configure(context):
        def scripts(page):
            request = Request(page, DETAIL, resource="xhr", target_id="123")
            if variant == "unknown":
                request.url = "https://x.com/i/unknown-fixture?token=AUTH_URL_CANARY"
            elif variant == "query-hash":
                request.url = DETAIL.replace("/read", "/unlisted-read")
            elif variant == "query":
                request.url += "?variables=AUTH_QUERY_CANARY"
            elif variant == "suffix":
                request.url += "#"
            elif variant == "foreign":
                request.frame = SimpleNamespace(page=SimpleNamespace(), url=TARGET)
            elif variant == "subframe":
                request.frame = SimpleNamespace(page=page, url=TARGET)
            elif variant == "redirect":
                request.redirected_from = object()
            elif variant == "target":
                request.url = DETAIL.replace("target=123", "target=456")
            elif variant == "wrong-resource":
                request.resource_type = "fetch"
            elif variant == "duplicate":
                return [request, request]
            else:
                request.url = DELETE
            return [request]
        context.script_requests = scripts
    result, browser, adapter = run(configure)
    assert result.cleanup_complete and browser.owned.routes[-1].aborted
    assert result.fetched == (2 if variant == "duplicate" else 1)
    assert all(r.response is None or r.response.disposed for r in browser.owned.routes)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403, 420, 429, 500, 503, "AUTH_STATUS_CANARY"])
def test_redirect_or_http_failure_stops_without_following_or_fabricating(status):
    result, browser, adapter = run(lambda context: setattr(context, "status", status))
    assert result.stop_reason == "http_or_redirect_or_time_block"
    assert len(browser.owned.fetches) == 1 and browser.owned.routes[0].aborted
    assert not browser.owned.routes[0].fulfilled and browser.owned.routes[0].response.disposed
    assert result.cleanup_complete


@pytest.mark.parametrize("stage", ["new_context", "route0", "route1", "websocket_route", "init_script", "listener_page", "listener_serviceworker", "new_page"])
def test_incomplete_startup_guarantee_stops_before_first_get(stage):
    result, browser, adapter = run(faults=[stage])
    assert result.fetched == result.attempts == 0
    assert result.cleanup_complete == (stage != "new_context")
    assert "goto" not in browser.order
    assert browser.contexts == [browser.borrowed]


@pytest.mark.parametrize("stage", ["fetch", "fulfill", "dispose"])
def test_get_control_fault_closes_owned_context_without_retry(stage):
    result, browser, adapter = run(faults=[stage])
    assert result.stop_reason in {"get_control_failed", "response_disposal_failed"}
    assert len(browser.owned.fetches) <= 1 and result.cleanup_complete


def test_abort_failure_quarantines_and_closes_without_fallthrough():
    def configure(context):
        context.script_requests = lambda page: [Request(page, DELETE, "POST", "xhr")]
    result, browser, adapter = run(configure, faults=["abort"])
    assert result.fetched == 1 and result.cleanup_complete and browser.owned.offline
    assert any(e["event"] == "abort_failed" for e in result.events)


def test_context_close_failure_is_reported_and_blocks_remain_armed():
    result, browser, adapter = run(faults=["context_close"])
    assert not result.cleanup_complete and browser.owned.offline
    assert adapter.context is browser.owned and all(p.closed for p in browser.owned.pages)
    before = len(browser.owned.fetches)
    route = browser.owned.dispatch(Request(browser.owned.pages[0], DELETE, "POST", "xhr"))
    assert route.aborted and len(browser.owned.fetches) == before


def test_restored_or_preexisting_page_refused_without_navigation():
    def configure(context):
        context.pages = [Page(context)]
    result, browser, adapter = run(configure)
    assert result.stop_reason == "nonempty_context" and not result.fetched
    assert "new_page" not in browser.order and result.cleanup_complete


def test_unplanned_startup_dispatch_hits_routes_before_navigation():
    def configure(context):
        context.startup_requests = lambda page: [Request(page)]
    result, browser, adapter = run(configure)
    assert result.stop_reason == "unplanned_navigation" and result.attempts == 1 and not result.fetched
    assert "goto" not in browser.order and result.cleanup_complete


def test_request_and_time_budget_stop_before_next_fetch():
    result, browser, adapter = run(fixture_plan=replace(plan(), max_requests=1))
    assert result.stop_reason == "time_or_request_limit" and result.attempts == 2 and result.fetched == 1
    def expire(context):
        context.after_goto = lambda page: setattr(context.clock, "now", 10)
    result, browser, adapter = run(expire)
    assert result.stop_reason == "time_limit" and result.cleanup_complete


@pytest.mark.parametrize("kind", ["popup", "worker", "websocket"])
def test_unowned_resource_or_transport_stops_and_closes_only_owned_context(kind):
    def configure(context):
        def trigger(page):
            if kind == "popup":
                popup = Page(context)
                context.pages.append(popup)
                context.callbacks["page"](popup)
                assert not popup.closed  # No reentrant close in lifecycle callback.
            elif kind == "worker":
                context.callbacks["serviceworker"](object())
            else:
                class Socket:
                    closed = False
                    def close(self, **kwargs):
                        assert kwargs == {"code": 1008, "reason": "read-only observation"}
                        self.closed = True
                    def connect_to_server(self):
                        pytest.fail("Socket must never connect")
                socket = Socket()
                context.ws_handler(socket)
                assert socket.closed
        context.after_goto = trigger
    result, browser, adapter = run(configure)
    assert result.stop_reason in {"unexpected_page", "unexpected_worker", "unsupported_transport"}
    assert result.cleanup_complete and browser.contexts == [browser.borrowed]


@pytest.mark.parametrize("change", [
    {"scope": "live"}, {"experimental_opt_in": False}, {"experimental_opt_in": "yes"},
    {"seconds": 11}, {"seconds": float("nan")}, {"seconds": "AUTH_TOKEN_CANARY"},
    {"max_requests": 101}, {"max_requests": True}, {"gets": frozenset()},
    {"gets": frozenset({(TARGET, "document"), (DELETE, "xhr")})},
    {"gets": frozenset({(TARGET, "document"), (DETAIL + "?token=AUTH_URL_CANARY", "xhr")})},
])
def test_missing_permission_or_non_read_only_plan_never_creates_context(change):
    result, browser, adapter = run(fixture_plan=replace(plan(), **change))
    assert result.stop_reason == "unresolved_or_invalid_read_only_plan"
    assert result.attempts == result.fetched == 0 and browser.owned is None and result.cleanup_complete


def test_adapter_cannot_restart_after_one_observation():
    result, browser, adapter = run()
    with pytest.raises(RuntimeError, match="cannot restart"):
        adapter.run(browser)


def test_read_only_policy_cannot_arm_the_approved_mutation_model():
    from minimum_traffic import OfflineDependencyCapsule
    adapter = ReadOnlyStartupAdapter(OfflineDependencyCapsule())
    browser = FixtureBrowser(Clock())
    result = adapter.run(browser)
    assert result.attempts == result.fetched == 0 and browser.owned is None
    assert not hasattr(adapter, "arm_delete")


def test_init_script_prevents_constructor_start_and_resurrection_in_fixture_realms():
    source = json.dumps(TRANSPORT_GUARD)
    script = """
const vm = require('node:vm');
const names = ['WebTransport','RTCPeerConnection','webkitRTCPeerConnection','Worker','SharedWorker','EventSource'];
for(let frame = 0; frame < 3; frame++) {
  const realm = {};
  for(const name of names) realm[name] = function(){throw Error('Transport must never start');};
  vm.runInNewContext(SOURCE, realm);
  for(const name of names) {
    if(realm[name] !== undefined) throw Error('Unblocked constructor');
    let sealed = false;
    try {Object.defineProperty(realm,name,{value:function(){}});} catch {sealed = true;}
    if(!sealed) throw Error('Constructor could be resurrected');
  }
}
process.stdout.write('three fixture realms: all constructors blocked and sealed');
""".replace("SOURCE", source)
    result = subprocess.run(['node', '--max-old-space-size=64'], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'all constructors blocked and sealed' in result.stdout


def test_get_policy_itself_cannot_dispatch_post_if_blocker_is_removed():
    def configure(context):
        def after(page):
            context.handlers = [context.handlers[0]]
            route = context.dispatch(Request(page, DELETE, "POST", "xhr"))
            assert route.aborted and not route.fulfilled
        context.script_requests = lambda page: []
        context.after_goto = after
    result, browser, adapter = run(configure)
    assert result.fetched == 1 and result.stop_reason == "unknown_or_unowned_request"


def test_setup_exhausting_time_budget_never_navigates():
    def configure(context):
        context.clock.now = 11
    result, browser, adapter = run(configure)
    assert result.stop_reason == "time_limit" and not result.fetched
    assert "goto" not in browser.order and result.cleanup_complete


def test_dispose_or_close_fault_details_never_expose_auth_exception_text():
    result, browser, adapter = run(faults=["context_close", "offline", "page_close"])
    assert not result.cleanup_complete
    assert {e["event"] for e in result.events} >= {"context_close_failed", "offline_control_failed", "page_close_failed"}
    assert "AUTH_" not in json.dumps(result.events)
    assert len(browser.owned.fetches) == 3
    assert browser.owned.dispatch(Request(browser.owned.pages[0], DELETE, "POST", "xhr")).aborted


def test_changed_response_url_stops_without_logging_it_or_following():
    result, browser, adapter = run(lambda context: setattr(context, "response_url", "https://AUTH_URL_CANARY.invalid/private"))
    assert result.stop_reason == "http_or_redirect_or_time_block" and result.fetched == 1
    assert result.cleanup_complete and not browser.owned.routes[0].fulfilled



def test_creation_failure_after_allocation_is_reported_unknown_without_touching_borrowed():
    browser = FixtureBrowser(Clock())
    create = browser.new_context
    def fail_after_allocation(**kwargs):
        create(**kwargs)
        raise RuntimeError("allocation outcome unestablished")
    browser.new_context = fail_after_allocation
    result = ReadOnlyStartupAdapter(plan(), monotonic=browser.clock).run(browser)
    assert result.stop_reason == "startup_or_observation_failed" and not result.cleanup_complete
    assert not browser.borrowed.closed and browser.owned in browser.contexts and not browser.owned.closed
    assert not result.attempts and not result.fetched


def test_installed_request_uses_url_and_post_data_not_fixture_target_attribute():
    from pathlib import Path

    from playwright.sync_api import Request as ActualRequest
    assert "target_id" not in ActualRequest.__dict__ and "post_data" in ActualRequest.__dict__
    source = (Path(__file__).resolve().parents[1] / "scripts/readonly_startup.py").read_text()
    assert 'getattr(request, "target_id"' not in source and 'request.post_data' in source


def test_get_body_is_denied_without_logging_or_fetching_it():
    def configure(context):
        def scripts(page):
            request = Request(page, DETAIL, resource="xhr")
            request.body = "AUTH_BODY_CANARY"
            return [request]
        context.script_requests = scripts
    result, browser, adapter = run(configure)
    assert result.stop_reason == "target_binding_unestablished" and result.fetched == 1
    assert "AUTH_BODY_CANARY" not in json.dumps(result.events)


class NavigationWithoutFrame(Request):
    """Actual Request.frame may throw before a popup navigation frame exists."""

    def __init__(self, page, url, method="GET", resource="document"):
        super().__init__(page, url, method, resource)
        self.frame_reads = 0

    @property
    def frame(self):
        self.frame_reads += 1
        raise RuntimeError("Navigation frame not available yet")

    @frame.setter
    def frame(self, value):
        pass


@pytest.mark.parametrize("url,method", [
    ("http://127.0.0.1:8765/unexpected", "GET"),
    ("http://127.0.0.1:8765/unexpected", "POST"),
    (DETAIL + "&other=1", "GET"),
])
def test_unlisted_navigation_with_absent_frame_is_rejected_before_frame_lookup(url, method):
    requests = []
    def configure(context):
        def scripts(page):
            request = NavigationWithoutFrame(page, url, method)
            requests.append(request)
            return [request]
        context.script_requests = scripts
    result, browser, adapter = run(configure)
    assert result.stop_reason == ("mutation_or_non_get_blocked" if method == "POST" else "unknown_or_unowned_request")
    assert result.cleanup_complete and result.fetched == 1
    assert requests[0].frame_reads == 0
    assert browser.owned.routes[-1].aborted and not browser.owned.routes[-1].fulfilled
    assert not any(destination == url for destination, verb in browser.owned.fetches)


def test_listed_request_without_frame_still_stops_without_dispatch_or_owner_exemption():
    requests = []
    def configure(context):
        def scripts(page):
            request = NavigationWithoutFrame(page, DETAIL, resource="xhr")
            requests.append(request)
            return [request]
        context.script_requests = scripts
    result, browser, adapter = run(configure)
    assert result.stop_reason == "get_control_failed" and result.cleanup_complete
    assert result.fetched == 1 and requests[0].frame_reads == 1
    assert browser.owned.routes[-1].aborted


def test_unexpected_page_callback_never_waits_for_page_close_and_blocks_followups():
    def configure(context):
        def trigger(page):
            popup = Page(context)
            context.pages.append(popup)
            popup.close = lambda: pytest.fail("Reentrant lifecycle callback must not wait on page.close")
            context.callbacks["page"](popup)
            assert not popup.closed
            before = len(context.fetches)
            request = Request(page, DETAIL, resource="xhr", target_id="123")
            assert context.dispatch(request).aborted
            assert len(context.fetches) == before
        context.after_goto = trigger
    result, browser, adapter = run(configure)
    assert result.stop_reason == "unexpected_page" and result.cleanup_complete
    assert browser.owned.closed and all(page.closed for page in browser.owned.pages)


def test_progress_records_blocking_stages_before_calls_and_successful_owned_close():
    clock = Clock()
    browser = FixtureBrowser(clock)
    adapter = ReadOnlyStartupAdapter(plan(), monotonic=clock)
    stages = []
    result = adapter.run(browser, progress=stages.append)
    assert stages == ['context_create', 'routing_setup', 'page_create', 'navigation', 'observe', 'context_close', 'adapter_complete']
    assert result.cleanup_complete and browser.owned.closed


def test_progress_writer_failure_cannot_skip_context_cleanup_or_expose_error_text():
    clock = Clock()
    browser = FixtureBrowser(clock)
    adapter = ReadOnlyStartupAdapter(plan(), monotonic=clock)
    def bad(stage):
        raise OSError('AUTH_PROGRESS_CANARY')
    result = adapter.run(browser, progress=bad)
    assert result.cleanup_complete and browser.owned.closed
    assert any(event['event']=='progress_write_failed' for event in result.events)
    assert 'AUTH_PROGRESS_CANARY' not in json.dumps(result.events)

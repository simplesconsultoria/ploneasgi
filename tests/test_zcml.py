"""Tests for the ``<ploneasgi:route />`` directive.

Zope-free: parsing ZCML and populating a component registry needs
``zope.configuration`` and ``zope.component``, neither of which is Zope. The
permission guard is exercised here with a stubbed ``run_authenticated`` and a fake
context — that it *enforces* against a real PAS is covered by
``tests/plone/test_route_guard.py``.
"""

from . import asgi_scope
from . import receiver
from ploneasgi import zcml
from ploneasgi.context import NotAuthenticated
from ploneasgi.context import SiteNotFound
from ploneasgi.zcml import ASGIRoute
from ploneasgi.zcml import build_route
from ploneasgi.zcml import IASGIRouteRegistration
from ploneasgi.zcml import routes_from_registry
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from zope.configuration import xmlconfig
from zope.configuration.exceptions import ConfigurationError

from types import SimpleNamespace
from typing import ClassVar

import asyncio
import pytest


def endpoint(ctx, request):
    """A stand-in http handler; identity is what the directive tests assert on."""
    return PlainTextResponse("called")


# ZCML resolves `handler` as a dotted name through the import system. The tests
# are a package, so this module is importable by its own name, exactly as an
# add-on's endpoint module would be.
HANDLER = f"{__name__}.endpoint"


TEMPLATE = """
<configure xmlns:ploneasgi="http://namespaces.plone.org/ploneasgi">
  <include package="ploneasgi" file="meta.zcml" />
  {directives}
</configure>
"""


def load(directives):
    """Execute ZCML, registering into the global registry.

    ``TEMPLATE.format`` only substitutes into the template — braces inside
    ``directives`` are left alone, so a ``{site}`` path parameter survives.

    :param directives: The directive snippets to place inside ``<configure>``.
    :returns: The configuration context.
    """
    return xmlconfig.string(TEMPLATE.format(directives=directives))


@pytest.fixture(autouse=True)
def clean_registry():
    """Unregister anything a test left in the global registry.

    ZCML registers into the global site manager, so without this the routes from
    one test leak into the next and ``routes_from_registry`` returns the union.
    """
    from zope.component import getGlobalSiteManager

    yield
    sm = getGlobalSiteManager()
    for name, component in list(sm.getUtilitiesFor(IASGIRouteRegistration)):
        sm.unregisterUtility(component, IASGIRouteRegistration, name=name)


ROUTE = f"""
  <ploneasgi:route
      path="/notify"
      handler="{HANDLER}"
      type="websocket"
      permission="cmf.ManagePortal"
      />
"""


class TestDirective:
    def test_registers_a_route(self):
        load(ROUTE)
        assert len(routes_from_registry()) == 1

    def test_builds_a_websocket_route(self):
        load(ROUTE)
        assert isinstance(routes_from_registry()[0], WebSocketRoute)

    def test_builds_an_http_route_by_default(self):
        load(f"""
          <ploneasgi:route
              path="/x" site="Plone" unguarded="true"
              handler="{HANDLER}" />
        """)
        route = routes_from_registry()[0]
        assert isinstance(route, Route)
        assert route.methods == {"GET", "HEAD"}

    def test_methods_are_parsed(self):
        load(f"""
          <ploneasgi:route
              path="/x" site="Plone" methods="POST PUT" unguarded="true"
              handler="{HANDLER}" />
        """)
        assert routes_from_registry()[0].methods == {"POST", "PUT"}

    def test_rejects_an_unknown_type(self):
        with pytest.raises(ConfigurationError):
            load(f"""
              <ploneasgi:route
                  path="/x" site="Plone" type="carrier-pigeon" unguarded="true"
                  handler="{HANDLER}" />
            """)

    def test_two_routes_on_the_same_path_conflict(self):
        """A silent last-one-wins would be much worse than a startup error."""
        with pytest.raises(ConfigurationError):
            load(ROUTE + ROUTE)

    def test_http_and_websocket_can_share_a_path(self):
        """Starlette dispatches on the scope type, so this is not a conflict.

        Both must survive registration: keying the utility on the path alone
        made the second silently replace the first.
        """
        load(f"""
          <ploneasgi:route path="/x" unguarded="true" handler="{HANDLER}" />
          <ploneasgi:route path="/x" type="websocket" unguarded="true"
              handler="{HANDLER}" />
        """)
        kinds = sorted(type(route).__name__ for route in routes_from_registry())
        assert kinds == ["Route", "WebSocketRoute"]

    def test_two_routes_with_the_same_name_conflict(self):
        """A reused name on two paths must not drop one of them quietly."""
        with pytest.raises(ConfigurationError):
            load(f"""
              <ploneasgi:route path="/a" name="dup" unguarded="true"
                  handler="{HANDLER}" />
              <ploneasgi:route path="/b" name="dup" unguarded="true"
                  handler="{HANDLER}" />
            """)

    def test_ordering_is_deterministic(self):
        load(f"""
          <ploneasgi:route path="/b" site="P" name="b" unguarded="true"
              handler="{HANDLER}" />
          <ploneasgi:route path="/a" site="P" name="a" unguarded="true"
              handler="{HANDLER}" />
        """)
        assert [r.name for r in routes_from_registry()] == ["a", "b"]


class TestSafetyRails:
    """Two mistakes the directive refuses to let you make quietly."""

    def test_a_route_without_a_permission_is_refused(self):
        with pytest.raises(ConfigurationError):
            load(f"""
              <ploneasgi:route path="/x" site="Plone"
                  handler="{HANDLER}" />
            """)

    def test_unguarded_makes_it_explicit(self):
        load(f"""
          <ploneasgi:route path="/x" site="Plone" unguarded="true"
              handler="{HANDLER}" />
        """)
        assert len(routes_from_registry()) == 1

    def test_a_relative_path_is_refused(self):
        """Paths are relative to the reserved prefix, so they must start with /."""
        with pytest.raises(ConfigurationError):
            load(f"""
              <ploneasgi:route path="++asgi++/x" permission="cmf.ManagePortal"
                  handler="{HANDLER}" />
            """)

    def test_a_route_needs_no_site_because_the_rewrite_supplies_it(self):
        load(f"""
          <ploneasgi:route path="/x" permission="cmf.ManagePortal"
              handler="{HANDLER}" />
        """)
        assert len(routes_from_registry()) == 1


class FakePublisherRequest:
    """Stands in for ZPublisher's request; records that its form was parsed."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.processed = False

    def processInputs(self):
        self.processed = True


class TestGuard:
    """The permission check, and the one context the handler runs in."""

    @pytest.fixture
    def calls(self, monkeypatch):
        """Run the work against a fake context, and record what was asked.

        ``_permitted`` itself runs for real: only the permission lookup, which
        needs ``zope.security``, and the check on the context are faked.

        :param monkeypatch: pytest's monkeypatch fixture.
        :returns: The recording dict; set ``allowed`` to steer the verdict.
        """
        recorded = {
            "allowed": True,
            "site_id": None,
            "runs": 0,
            "checked": [],
            "body": None,
            "ctx": None,
        }

        class FakeContext:
            portal = "the portal"

            def __init__(self, body: bytes) -> None:
                self.request = FakePublisherRequest(body)

            def check_permission(self, title, obj):
                recorded["checked"].append((title, obj))
                return recorded["allowed"]

        async def fake_run_authenticated(scope, work, *, site_id, body=None, **kw):
            recorded["runs"] += 1
            recorded["site_id"] = site_id
            recorded["body"] = body
            data = body.read() if body is not None else b""
            recorded["ctx"] = FakeContext(data)
            return work(recorded["ctx"])

        monkeypatch.setattr(zcml, "run_authenticated", fake_run_authenticated)
        monkeypatch.setattr(zcml, "_permission_title", lambda pid: f"title of {pid}")
        return recorded

    def call_http(self, route, scope=None, body=b"", **scope_keys):
        """Drive an http endpoint and return what it produced.

        :param route: The registered route.
        :param scope: A complete scope; built from ``scope_keys`` when omitted.
        :param body: The request body, delivered in one message.
        :param scope_keys: Extra scope keys.
        :returns: The endpoint's response.
        """
        from starlette.requests import Request

        scope = scope or asgi_scope("/++asgi++/x", **scope_keys)
        receive = receiver({"type": "http.request", "body": body})
        return asyncio.run(build_route(route).endpoint(Request(scope, receive)))

    def guarded(self, handler=endpoint, **kwargs):
        """:returns: A route guarded by ``cmf.ManagePortal`` on site ``Plone``."""
        options = {"permission": "cmf.ManagePortal", "site": "Plone", **kwargs}
        return ASGIRoute(path="/x", handler=handler, **options)

    def test_the_handler_gets_the_context_and_the_request(self, calls):
        seen = []

        def handler(ctx, request):
            seen.append((ctx, request.url.path))
            return PlainTextResponse("ok")

        assert self.call_http(self.guarded(handler)).body == b"ok"
        assert seen == [(calls["ctx"], "/++asgi++/x")]

    def test_one_context_per_request(self, calls):
        """The guard and the handler share one call: the point of the contract."""
        self.call_http(self.guarded())
        assert calls["runs"] == 1

    def test_the_permission_is_checked_on_the_portal(self, calls):
        self.call_http(self.guarded())
        assert calls["checked"] == [("title of cmf.ManagePortal", "the portal")]

    def test_returns_403_when_denied(self, calls):
        calls["allowed"] = False
        ran = []

        def handler(ctx, request):  # pragma: no cover - must not run
            ran.append(True)

        assert self.call_http(self.guarded(handler)).status_code == 403
        assert ran == []

    def test_an_unknown_permission_refuses_without_checking(self, calls, monkeypatch):
        """Fail closed: an id that names no permission is never asked about."""
        monkeypatch.setattr(zcml, "_permission_title", lambda pid: None)
        assert self.call_http(self.guarded()).status_code == 403
        assert calls["checked"] == []

    def test_unguarded_runs_the_handler_without_a_check(self, calls):
        result = self.call_http(self.guarded(permission=None))
        assert result.body == b"called"
        assert calls["checked"] == []
        assert calls["runs"] == 1

    def test_site_comes_from_the_declaration(self, calls):
        self.call_http(self.guarded())
        assert calls["site_id"] == "Plone"

    def test_the_rewrite_beats_the_declared_site(self, calls):
        """The rewrite reflects the host the client asked for; site= is a fallback."""
        from ploneasgi.vhm import SITE_KEY

        self.call_http(self.guarded(), **{SITE_KEY: "intranet"})
        assert calls["site_id"] == "intranet"

    def test_no_site_anywhere_is_a_404(self, calls):
        result = self.call_http(self.guarded(site=None))
        assert result.status_code == 404
        assert calls["runs"] == 0

    def test_a_site_that_does_not_exist_is_a_404(self, calls, monkeypatch):
        """Raised inside the context, by the walk to the site."""

        async def missing(scope, work, *, site_id, **kwargs):
            raise SiteNotFound(site_id)

        monkeypatch.setattr(zcml, "run_authenticated", missing)
        assert self.call_http(self.guarded()).status_code == 404

    def test_the_body_reaches_the_context(self, calls):
        """Read on the event loop, handed to the publisher request."""
        self.call_http(self.guarded(methods=("POST",)), body=b"a=1", method="POST")
        assert calls["ctx"].request.body == b"a=1"

    def test_the_form_is_parsed_after_the_guard(self, calls):
        self.call_http(self.guarded())
        assert calls["ctx"].request.processed is True

    def test_a_refused_request_is_not_parsed(self, calls):
        calls["allowed"] = False
        self.call_http(self.guarded())
        assert calls["ctx"].request.processed is False

    def test_the_body_is_closed_after_the_call(self, calls):
        self.call_http(self.guarded())
        assert calls["body"].closed

    def test_a_body_over_the_limit_is_refused_before_any_context(self, calls):
        app = SimpleNamespace(state=SimpleNamespace(max_body_size=4))
        result = self.call_http(self.guarded(), body=b"too long", app=app)
        assert result.status_code == 413
        assert result.headers["connection"] == "close"
        assert calls["runs"] == 0

    def test_a_declared_length_over_the_limit_is_refused_unread(self, calls):
        app = SimpleNamespace(state=SimpleNamespace(max_body_size=4))
        headers = [(b"content-length", b"1000")]
        result = self.call_http(self.guarded(), app=app, headers=headers)
        assert result.status_code == 413

    def test_no_limit_published_falls_back_to_the_adapter_default(self, calls):
        """An app built without create_app still has a limit."""
        app = SimpleNamespace(state=SimpleNamespace())
        assert zcml._max_body_size({"app": app}) == zcml.MAX_BODY_SIZE
        assert zcml._max_body_size({}) == zcml.MAX_BODY_SIZE

    def test_a_client_gone_mid_body_never_reaches_the_handler(self, calls):
        from starlette.requests import Request

        scope = asgi_scope("/++asgi++/x", method="POST")
        receive = receiver(
            {"type": "http.request", "body": b"part", "more_body": True},
            {"type": "http.disconnect"},
        )
        route = self.guarded(methods=("POST",))
        asyncio.run(build_route(route).endpoint(Request(scope, receive)))
        assert calls["runs"] == 0

    def test_not_authenticated_is_a_401(self, calls):
        def handler(ctx, request):
            raise NotAuthenticated

        assert self.call_http(self.guarded(handler)).status_code == 401

    def test_other_errors_propagate(self, calls, monkeypatch):
        """Only Zope's own refusals become responses; a bug stays a 500."""
        monkeypatch.setattr(zcml, "_status_for", lambda exc: None)

        def handler(ctx, request):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            self.call_http(self.guarded(handler))

    def test_a_mapped_error_becomes_its_status(self, calls, monkeypatch):
        monkeypatch.setattr(zcml, "_status_for", lambda exc: 403)

        def handler(ctx, request):
            raise RuntimeError("stands in for Unauthorized")

        result = self.call_http(self.guarded(handler))
        assert result.status_code == 403
        assert result.body == b"Forbidden"

    def test_websocket_is_closed_without_being_accepted_when_denied(self, calls):
        """Accepting first would hand an unauthorized client a live socket."""
        calls["allowed"] = False
        events = []

        class FakeWebSocket:
            scope: ClassVar[dict] = {"type": "websocket", "headers": []}

            async def accept(self):
                events.append("accept")

            async def close(self, code=1000):
                events.append(("close", code))

        route = ASGIRoute(
            path="/x",
            handler=endpoint,
            type="websocket",
            permission="cmf.ManagePortal",
            site="Plone",
        )
        asyncio.run(build_route(route).endpoint(FakeWebSocket()))
        assert events == [("close", zcml.WS_POLICY_VIOLATION)]

    def test_websocket_handler_runs_when_allowed(self, calls):
        seen = []

        async def handler(websocket):
            seen.append("handled")

        class FakeWebSocket:
            scope: ClassVar[dict] = {"type": "websocket", "headers": []}

            async def close(self, code=1000):
                seen.append("closed")

        route = ASGIRoute(
            path="/x",
            handler=handler,
            type="websocket",
            permission="cmf.ManagePortal",
            site="Plone",
        )
        asyncio.run(build_route(route).endpoint(FakeWebSocket()))
        assert seen == ["handled"]
        assert calls["checked"] == [("title of cmf.ManagePortal", "the portal")]

    def test_unguarded_websocket_opens_no_context(self, calls):
        """A websocket's check is its own call; with nothing to check, none."""

        async def handler(websocket):
            pass

        class FakeWebSocket:
            scope: ClassVar[dict] = {"type": "websocket", "headers": []}

        route = ASGIRoute(path="/x", handler=handler, type="websocket", site="Plone")
        asyncio.run(build_route(route).endpoint(FakeWebSocket()))
        assert calls["runs"] == 0

"""Tests for the ``<ploneasgi:route />`` directive.

Zope-free: parsing ZCML and populating a component registry needs
``zope.configuration`` and ``zope.component``, neither of which is Zope. The
permission guard is exercised here with a stubbed ``run_authenticated`` — that it
*enforces* against a real PAS is covered by ``tests/plone/test_route_guard.py``.
"""

from ploneasgi import zcml
from ploneasgi.zcml import ASGIRoute
from ploneasgi.zcml import build_route
from ploneasgi.zcml import IASGIRouteRegistration
from ploneasgi.zcml import routes_from_registry
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from zope.configuration import xmlconfig
from zope.configuration.exceptions import ConfigurationError

from typing import ClassVar

import asyncio
import pytest


async def endpoint(connection):
    """A stand-in endpoint; identity is what the tests assert on."""
    return "called"


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


class TestGuard:
    """The permission check wrapping the handler."""

    @pytest.fixture
    def calls(self, monkeypatch):
        """Record what the guard asks ``run_authenticated`` and control the answer.

        :param monkeypatch: pytest's monkeypatch fixture.
        :returns: The recording dict; set ``allowed`` to steer the verdict.
        """
        recorded = {"allowed": True, "site_id": None, "ran": False}

        async def fake_run_authenticated(scope, work, *, site_id, **kwargs):
            recorded["ran"] = True
            recorded["site_id"] = site_id
            return recorded["allowed"]

        monkeypatch.setattr(zcml, "run_authenticated", fake_run_authenticated)
        return recorded

    def call_http(self, route, scope):
        """Drive an http endpoint and return (status, body_or_None)."""
        from starlette.requests import Request

        built = build_route(route)
        request = Request(scope)
        return asyncio.run(built.endpoint(request))

    def test_allows_when_the_permission_is_granted(self, calls):
        calls["allowed"] = True
        route = ASGIRoute(
            path="/x", handler=endpoint, permission="cmf.ManagePortal", site="Plone"
        )
        result = self.call_http(route, {"type": "http", "headers": [], "method": "GET"})
        assert result == "called"

    def test_returns_403_when_denied(self, calls):
        calls["allowed"] = False
        route = ASGIRoute(
            path="/x", handler=endpoint, permission="cmf.ManagePortal", site="Plone"
        )
        result = self.call_http(route, {"type": "http", "headers": [], "method": "GET"})
        assert result.status_code == 403

    def test_skips_the_check_entirely_when_unguarded(self, calls):
        route = ASGIRoute(path="/x", handler=endpoint, permission=None, site="Plone")
        result = self.call_http(route, {"type": "http", "headers": [], "method": "GET"})
        assert result == "called"
        assert calls["ran"] is False

    def test_site_comes_from_the_declaration(self, calls):
        route = ASGIRoute(
            path="/x", handler=endpoint, permission="cmf.ManagePortal", site="Plone"
        )
        self.call_http(route, {"type": "http", "headers": [], "method": "GET"})
        assert calls["site_id"] == "Plone"

    def test_the_rewrite_beats_the_declared_site(self, calls):
        """The rewrite reflects the host the client asked for; site= is a fallback."""
        from ploneasgi.vhm import SITE_KEY

        route = ASGIRoute(
            path="/x", handler=endpoint, permission="cmf.ManagePortal", site="Plone"
        )
        self.call_http(
            route,
            {"type": "http", "headers": [], "method": "GET", SITE_KEY: "intranet"},
        )
        assert calls["site_id"] == "intranet"

    def test_no_site_anywhere_is_a_404(self, calls):
        route = ASGIRoute(path="/x", handler=endpoint, permission="cmf.ManagePortal")
        result = self.call_http(route, {"type": "http", "headers": [], "method": "GET"})
        assert result.status_code == 404

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
        calls["allowed"] = True
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

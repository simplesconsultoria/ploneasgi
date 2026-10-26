"""A ZCML directive for registering native ASGI routes.

An add-on cannot edit the project's ``server.py``, so there has to be *some* way
for it to contribute a websocket endpoint or a native HTTP route. In this
ecosystem that mechanism is ZCML, and this module supplies one directive::

    <configure xmlns:ploneasgi="http://namespaces.plone.org/ploneasgi">
      <ploneasgi:route
          path="/notify"
          handler=".endpoints.notify"
          type="websocket"
          permission="cmf.ManagePortal"
          />
    </configure>

Paths are relative to a reserved prefix (``/++asgi++`` by default), so that one
lands at ``/++asgi++/notify``. That is not cosmetic: it is what keeps a
deployment to a *single* proxy rule.
:class:`ploneasgi.vhm.VirtualHostMiddleware` recognizes the prefix inside an
ordinary VirtualHostMonster rewrite, so registering a new route needs no
infrastructure change — and the Plone site comes out of the rewrite rather than
being restated in every path.

Two deliberate limits, both consequences of how ZCML actually works:

* **Registrations are process-wide, not per-site.** ZCML executes once at
  startup into the *global* registry; local (persistent) registrations live in
  the ZODB and can change at runtime, so a startup snapshot of them would go
  stale the moment an add-on is installed. Which site a *request* enters is
  resolved per request from the rewrite, so one registration serves every site.
* **Discovery is explicit.** :func:`routes_from_registry` is called by the
  project, not by :func:`ploneasgi.app.create_app`. "Adding a route means passing
  a route" still holds; this just lets an add-on put one in the pile.

The ``permission`` attribute is the reason this is worth having over a plain list
of routes. It is checked against the portal, inside
:func:`ploneasgi.context.run_authenticated`, before the handler is entered — so
an endpoint cannot forget to authorize itself.
"""

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from ploneasgi.context import run_authenticated
from ploneasgi.context import SiteNotFound
from ploneasgi.vhm import DEFAULT_PREFIX
from ploneasgi.vhm import SITE_KEY
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from typing import Any
from zope.configuration.fields import GlobalObject
from zope.interface import Interface
from zope.schema import Bool
from zope.schema import TextLine

import logging


logger = logging.getLogger("ploneasgi.zcml")

HTTP = "http"
WEBSOCKET = "websocket"

#: Websocket close code for a policy violation (RFC 6455). Used when a
#: connection is refused for lack of a permission.
WS_POLICY_VIOLATION = 1008


class IASGIRouteRegistration(Interface):
    """Marker for a registered route, so the registry can be queried for them."""


@dataclass(frozen=True)
class ASGIRoute:
    """One registered route, as declared in ZCML.

    :ivar path: Path pattern, relative to the reserved prefix.
    :ivar handler: The endpoint callable.
    :ivar type: ``http`` or ``websocket``.
    :ivar methods: HTTP methods; ignored for websocket routes.
    :ivar permission: Id of the permission required on the portal, or ``None``.
    :ivar site: Fallback site id for requests arriving with no VHM rewrite.
    :ivar name: Route name.
    """

    path: str
    handler: Callable[..., Any]
    type: str = HTTP
    methods: tuple[str, ...] = field(default_factory=lambda: ("GET",))
    permission: str | None = None
    site: str | None = None
    name: str = ""


def _site_for(scope: Any, route: ASGIRoute) -> str:
    """Determine which Plone site a request is addressed to.

    The proxy's VirtualHostMonster rewrite is authoritative when present: it
    reflects the host the client actually asked for, which is the whole point of
    virtual hosting. ``site=`` on the directive is the fallback for direct
    access with no proxy in front, which in practice means development.

    :param scope: The ASGI connection scope.
    :param route: The registered route.
    :returns: The site id.
    :raises SiteNotFound: When nothing names a site.
    """
    site = scope.get(SITE_KEY) or route.site
    if not site:
        raise SiteNotFound(
            f"no site for {route.path!r}: the request carried no VirtualHostBase "
            'rewrite, the route declares no site="...", and no default site is '
            "configured (PLONE_SITE_ID)"
        )
    return site


def _permission_title(permission_id: str) -> str | None:
    """Translate a ZCML permission id into the title Zope checks against.

    ZCML names permissions by id (``zope2.View``, ``cmf.ManagePortal``), the
    convention of every other directive a Plone developer writes. Zope's
    security machinery knows them only by title (``View``, ``Manage portal``)
    and treats any other string as an unknown permission granted to Manager
    alone — so passing the id straight through refuses Anonymous on
    ``zope2.View`` and Site Administrator on ``plone.app.controlpanel.Overview``,
    while looking correct to the one user who tests it.

    Runs inside the worker thread, where Zope is importable.

    :param permission_id: The permission id from the directive.
    :returns: The permission title, or ``None`` when no such id is registered.
    """
    from zope.component import queryUtility
    from zope.security.interfaces import IPermission

    permission = queryUtility(IPermission, name=permission_id)
    return None if permission is None else permission.title


async def _is_allowed(scope: Any, route: ASGIRoute) -> bool:
    """Check the route's permission against the portal, as the connecting user.

    :param scope: The ASGI connection scope.
    :param route: The registered route.
    :returns: Whether the request may proceed.
    """
    permission = route.permission
    if permission is None:
        return True

    def check(ctx: Any) -> bool:
        title = _permission_title(permission)
        if title is None:
            # Fail closed. Zope would treat the unknown name as Manager-only,
            # which hides a typo for exactly the user most likely to test it.
            logger.error(
                "route %s declares unknown permission %r; refusing every request",
                route.path,
                permission,
            )
            return False
        return ctx.check_permission(title, ctx.portal)

    # Its own connection, closed before the handler runs: the handler is async
    # and opens its own if it needs Plone. Two in sequence, never two at once,
    # and both draw on the application's ZODB limiter (see limiter_for).
    return bool(await run_authenticated(scope, check, site_id=_site_for(scope, route)))


def _guarded_http(route: ASGIRoute) -> Callable[..., Any]:
    """Wrap an HTTP endpoint in its permission check.

    :param route: The registered route.
    :returns: The endpoint Starlette should call.
    """

    async def endpoint(request: Any) -> Any:
        try:
            allowed = await _is_allowed(request.scope, route)
        except SiteNotFound as exc:
            # The site comes from a client-supplied Host header via the
            # rewrite, so an unknown one is a 404, not a traceback.
            logger.info("no such site %s for %s", exc, route.path)
            return PlainTextResponse("Not Found", status_code=404)
        if not allowed:
            return PlainTextResponse("Forbidden", status_code=403)
        return await route.handler(request)

    endpoint.__name__ = getattr(route.handler, "__name__", "endpoint")
    return endpoint


def _guarded_websocket(route: ASGIRoute) -> Callable[..., Any]:
    """Wrap a websocket endpoint in its permission check.

    The connection is closed *without* being accepted when the check fails.
    Accepting first and closing after would leak the fact that the endpoint
    exists and hand an unauthorized client a live socket, however briefly.

    :param route: The registered route.
    :returns: The endpoint Starlette should call.
    """

    async def endpoint(websocket: Any) -> Any:
        try:
            allowed = await _is_allowed(websocket.scope, route)
        except SiteNotFound as exc:
            logger.info("no such site %s for %s", exc, route.path)
            allowed = False
        if not allowed:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return None
        return await route.handler(websocket)

    endpoint.__name__ = getattr(route.handler, "__name__", "endpoint")
    return endpoint


def build_route(route: ASGIRoute, prefix: str = DEFAULT_PREFIX) -> BaseRoute:
    """Turn a registration into a Starlette route.

    Declared paths are relative to the reserved prefix. Mounting every native
    route under one prefix is what lets a deployment keep a single proxy rule:
    :class:`ploneasgi.vhm.VirtualHostMiddleware` recognizes the prefix inside an
    ordinary VirtualHostMonster rewrite, so adding a route needs no proxy change
    at all.

    :param route: The registered route.
    :param prefix: Reserved path prefix.
    :returns: A ``Route`` or ``WebSocketRoute``.
    """
    path = prefix.rstrip("/") + route.path
    name = route.name or route.path
    if route.type == WEBSOCKET:
        return WebSocketRoute(path, _guarded_websocket(route), name=name)
    return Route(path, _guarded_http(route), methods=list(route.methods), name=name)


def routes_from_registry(
    registry: Any = None, prefix: str = DEFAULT_PREFIX
) -> list[BaseRoute]:
    """Collect every ZCML-registered route as a Starlette route.

    Call this *after* Zope has started — ZCML has not executed before that — and
    pass the result to :func:`ploneasgi.app.create_app`::

        wsgi_app = build_wsgi_app(settings)
        app = create_app(settings, routes=routes_from_registry(), wsgi_app=wsgi_app)

    Ordering is by type and path so the route table is deterministic across
    restarts; Starlette matches in list order, and a non-deterministic order
    would make an overlapping pattern win at random.

    :param registry: Component registry to read; defaults to the global one.
    :param prefix: Reserved path prefix to mount the routes under.
    :returns: The routes, ready to pass to ``create_app``.
    """
    from zope.component import getGlobalSiteManager

    site_manager = registry or getGlobalSiteManager()
    registrations = sorted(
        site_manager.getUtilitiesFor(IASGIRouteRegistration), key=lambda pair: pair[0]
    )
    routes = [build_route(route, prefix) for _, route in registrations]
    logger.info("registered %d ASGI route(s) from ZCML under %s", len(routes), prefix)
    return routes


class IASGIRouteDirective(Interface):
    """Schema for the ``<ploneasgi:route />`` directive."""

    path = TextLine(
        title="Path",
        description=(
            "Starlette path pattern, relative to the reserved prefix — declare "
            "/notify and it is served at /++asgi++/notify. Which Plone site it "
            "enters comes from the proxy's VirtualHostMonster rewrite, not from "
            "this path."
        ),
        required=True,
    )

    handler = GlobalObject(
        title="Handler",
        description=(
            "The async endpoint. Called with a Starlette Request for http routes "
            "and a WebSocket for websocket routes. Dotted name, relative names "
            "like .endpoints.notify included — zope.configuration resolves it."
        ),
        required=True,
    )

    type = TextLine(
        title="Type",
        description="Either 'http' or 'websocket'.",
        required=False,
        default=HTTP,
    )

    methods = TextLine(
        title="Methods",
        description="Space-separated HTTP methods. Ignored for websocket routes.",
        required=False,
        default="GET",
    )

    permission = TextLine(
        title="Permission",
        description=(
            "Id of the permission required on the portal (zope2.View, "
            "cmf.ManagePortal), checked before the handler runs. An id that is "
            "not registered refuses every request. Omitting it means the "
            "endpoint is responsible for its own authorization, which is rarely "
            "what you want."
        ),
        required=False,
    )

    site = TextLine(
        title="Site",
        description=(
            "Fallback Plone site id, used only when a request arrives without "
            "a VirtualHostMonster rewrite — direct access in development. Behind "
            "a proxy the rewrite is authoritative and this is ignored."
        ),
        required=False,
    )

    name = TextLine(
        title="Name",
        description="Route name; defaults to the path.",
        required=False,
        default="",
    )

    unguarded = Bool(
        title="Unguarded",
        description=(
            "Acknowledge that a route with no permission is intentional. "
            "Required when permission is omitted, so that unauthenticated "
            "endpoints are always a deliberate choice rather than a typo."
        ),
        required=False,
        default=False,
    )


def routeDirective(
    _context: Any,
    path: str,
    handler: Callable[..., Any],
    type: str = HTTP,  # noqa: A002 - the ZCML attribute is named `type`
    methods: str = "GET",
    permission: str | None = None,
    site: str | None = None,
    name: str = "",
    unguarded: bool = False,
) -> None:
    """Register one ASGI route from ZCML.

    :param _context: The ZCML configuration context.
    :param path: Starlette path pattern.
    :param handler: The endpoint callable, already resolved by ``GlobalObject``.
    :param type: ``http`` or ``websocket``.
    :param methods: Space-separated HTTP methods.
    :param permission: Permission required on the portal.
    :param site: Fallback site id for requests with no VHM rewrite.
    :param name: Route name.
    :param unguarded: Required when ``permission`` is omitted.
    :raises ValueError: On an unknown type, a missing site, or an unacknowledged
        missing permission.
    """
    from zope.component.zcml import handler as registry_handler

    if type not in (HTTP, WEBSOCKET):
        raise ValueError(f"type must be '{HTTP}' or '{WEBSOCKET}', not {type!r}")

    if permission is None and not unguarded:
        # A missing permission is indistinguishable from a forgotten one, and the
        # consequence is an endpoint anyone can reach. Make it say so out loud.
        raise ValueError(
            f"route {path!r} declares no permission. Add one, or set "
            'unguarded="true" to state that it is deliberately open.'
        )

    if not path.startswith("/"):
        raise ValueError(
            f"route path {path!r} must start with '/'. Paths are relative to the "
            "reserved prefix: declare '/notify', not '++asgi++/notify'."
        )

    route = ASGIRoute(
        path=path,
        handler=handler,
        type=type,
        methods=tuple(methods.split()),
        permission=permission,
        site=site,
        name=name,
    )

    # Two add-ons claiming the same path is a genuine conflict, and ZCML's
    # discriminator machinery reports it with both file locations — much better
    # than last-one-wins at request time. The utility name must be exactly as
    # unique as the discriminator: keyed on the path alone, an http and a
    # websocket route on one path passed the conflict check and then the second
    # silently replaced the first in the registry.
    _context.action(
        discriminator=("ploneasgi:route", type, path),
        callable=registry_handler,
        args=(
            "registerUtility",
            route,
            IASGIRouteRegistration,
            f"{type}:{path}",
        ),
    )
    if name:
        # An explicit name is what url_for resolves, so two routes sharing one
        # is a conflict too — not a reason to drop either.
        _context.action(discriminator=("ploneasgi:route-name", name))

"""Compose the ASGI application.

The whole extension story is :func:`create_app`'s keyword arguments. There is
deliberately no registration framework, no entry-point scanning and no
middleware registry: the moment this grows one, PasteDeploy has been reinvented
with extra steps. Adding a route means passing a route.

:mod:`ploneasgi.zcml` lets an add-on *declare* a route — it has no ``server.py``
to edit — but nothing here looks for them. The project calls
:func:`ploneasgi.zcml.routes_from_registry` and passes the result to ``routes``,
so this function still only ever sees a list someone handed it.
"""

from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack
from ploneasgi.accesslog import AccessLogMiddleware
from ploneasgi.settings import Settings
from ploneasgi.settings import setup_logging
from ploneasgi.vhm import VirtualHostMiddleware
from ploneasgi.wsgi import WSGIMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import BaseRoute
from starlette.routing import Mount
from starlette.types import ASGIApp
from typing import Any

import logging


logger = logging.getLogger("ploneasgi.app")

#: Signature of a lifespan factory: called with the app, used as a context
#: manager for the lifetime of the server. What ``@asynccontextmanager``
#: returns — not the bare async generator underneath it.
LifespanFactory = Callable[[Any], AbstractAsyncContextManager[None]]

#: Builds the ASGI app that mounts a WSGI app; see :func:`default_adapter`.
AdapterFactory = Callable[[Callable[..., Iterable[bytes]], Settings], ASGIApp]


def default_adapter(
    wsgi_app: Callable[..., Iterable[bytes]], settings: Settings
) -> WSGIMiddleware:
    """Wrap a WSGI app in this package's adapter.

    :param wsgi_app: The WSGI application to mount.
    :param settings: Settings providing concurrency and chunk size.
    :returns: The configured adapter.
    """
    return WSGIMiddleware(
        wsgi_app, workers=settings.concurrency, chunk_size=settings.chunk_size
    )


def create_app(
    settings: Settings | None = None,
    *,
    routes: Sequence[BaseRoute] = (),
    middleware: Sequence[Middleware] = (),
    lifespans: Sequence[LifespanFactory] = (),
    wsgi_app: Callable[..., Iterable[bytes]] | None = None,
    adapter: AdapterFactory | None = None,
) -> Starlette:
    """Build the Starlette application with Zope mounted at ``/``.

    :param settings: Settings to use; read from the environment when omitted.
    :param routes: Extra routes, matched **before** the catch-all Zope mount.
        This is where websocket endpoints and native HTTP routes go.
    :param middleware: Starlette middleware wrapping the whole application,
        including the routes above. Access logging belongs here.
    :param lifespans: Extra lifespan context managers, entered after Zope's and
        exited before it, so they can rely on the ZODB being open.
    :param wsgi_app: A prebuilt WSGI application; built from ``settings`` when
        omitted. Useful in tests, where the layer already started Zope.
    :param adapter: Builds the ASGI app that wraps ``wsgi_app``; defaults to
        :class:`ploneasgi.wsgi.WSGIMiddleware`. This package's central claim is
        that its adapter beats ``a2wsgi``, so the seam to swap one in and check
        is deliberate — a claim nobody can falsify is not worth much.
    :returns: The composed Starlette application.
    """
    from ploneasgi.zopeapp import build_wsgi_app
    from ploneasgi.zopeapp import check_concurrency_contract
    from ploneasgi.zopeapp import lifespan as zope_lifespan

    settings = settings or Settings.from_env()
    setup_logging(settings)

    if wsgi_app is None:
        wsgi_app = build_wsgi_app(settings)
        check_concurrency_contract(settings)

    all_routes: list[BaseRoute] = list(routes)

    if adapter is None:
        adapter = default_adapter

    # Mount last: it matches everything.
    all_routes.append(Mount("/", adapter(wsgi_app, settings)))

    all_middleware = list(middleware)

    all_middleware.insert(
        0,
        Middleware(
            VirtualHostMiddleware,
            prefix=settings.route_prefix,
            default_site=settings.site_id,
        ),
    )

    if settings.access_log:
        # Outermost, ahead of the virtual-host middleware, for two reasons. It
        # logs the path as it arrived on the socket, rewrite and all, which is
        # the faithful thing for a server access log; the reverse proxy's own
        # log already has the client-facing URL. And the recorded duration then
        # includes everything the caller wrapped the app in, while every route
        # is covered — native routes and websockets included, which a
        # WSGI-layer log cannot see at all.
        all_middleware.insert(0, Middleware(AccessLogMiddleware))

    return Starlette(
        routes=all_routes,
        middleware=all_middleware,
        lifespan=_compose_lifespans([zope_lifespan, *lifespans]),
    )


def _compose_lifespans(factories: Sequence[LifespanFactory]) -> LifespanFactory:
    """Nest several lifespan context managers into one.

    Entered in order and exited in reverse, so a later lifespan may depend on an
    earlier one — which is why Zope's goes first.

    :param factories: The lifespan factories to compose.
    :returns: A single lifespan factory.
    """

    @asynccontextmanager
    async def combined(app: Any) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for factory in factories:
                await stack.enter_async_context(factory(app))
            yield

    return combined


def serve(app: Starlette, settings: Settings | None = None) -> None:
    """Run the application under uvicorn.

    :param app: The application to serve.
    :param settings: Settings providing host, port and log level.
    """
    import uvicorn

    settings = settings or Settings.from_env()
    # Pass the application object rather than an import string: an import string
    # makes uvicorn re-import the caller's module, starting Zope a second time.
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # uvicorn's own access log is on by default, so leaving it alone would
        # log every request even with PLONE_ACCESS_LOG=0, and log twice with it
        # on. :class:`ploneasgi.accesslog.AccessLogMiddleware` is the one access
        # log, and the toggle is the one that decides whether there is one.
        access_log=False,
    )

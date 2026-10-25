"""Starting and stopping Zope without PasteDeploy.

Everything in this module imports Zope lazily, so the rest of ``ploneasgi``
remains importable — and testable — without Plone installed.

Named ``zopeapp`` rather than ``zope`` on purpose: a top-level module called
``zope`` inside this package would be a confusing neighbour to the ``zope``
namespace package.
"""

from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterable
from contextlib import asynccontextmanager
from ploneasgi.settings import Settings

import logging


logger = logging.getLogger("ploneasgi.zopeapp")


def build_wsgi_app(settings: Settings) -> Callable[..., Iterable[bytes]]:
    """Build the Zope WSGI application without PasteDeploy.

    Replaces the ``[pipeline:main]`` section of ``zope.ini``: ``make_wsgi_app``
    is the ``egg:Zope#main`` app factory, and ``HTTPExceptionHandler`` is the
    ``egg:Zope#httpexceptions`` filter — mandatory, since error rendering breaks
    without it.

    Calling this starts Zope: it opens the ZODB and fires ``ProcessStarting``.

    :param settings: Settings providing the ``zope.conf`` path and toggles.
    :returns: The WSGI callable to mount.
    :raises FileNotFoundError: If ``zope.conf`` does not exist.
    """
    if not settings.zope_conf.is_file():
        raise FileNotFoundError(
            f"zope.conf not found at {settings.zope_conf} — generate an instance, "
            "or point PLONE_ZOPE_CONF at an existing zope.conf."
        )

    from Zope2.Startup.run import make_wsgi_app
    from ZPublisher.httpexceptions import HTTPExceptionHandler

    app = make_wsgi_app({}, str(settings.zope_conf))
    app = HTTPExceptionHandler(app)

    # Access logging deliberately does not happen here. A WSGI-layer log sits
    # below the router, so it cannot see native routes or websockets, and it
    # runs before the body is streamed. See :mod:`ploneasgi.accesslog`.
    return app


def check_concurrency_contract(settings: Settings) -> None:
    """Warn when the WSGI worker count can outrun the ZODB connection pool.

    These two numbers must be coordinated or requests block waiting for a
    database connection under load. Body streaming is deliberately *not* part of
    this contract — it uses a separate limiter in
    :class:`ploneasgi.wsgi.WSGIMiddleware`, because pulling bytes off a blob file
    holds no ZODB connection.

    :param settings: Settings providing the configured concurrency.
    """
    import Zope2

    db = Zope2.DB
    if db is None:  # Zope not started yet; nothing to compare against.
        return

    pool_size = db.getPoolSize()
    if settings.concurrency > pool_size:
        logger.warning(
            "PLONE_CONCURRENCY=%s exceeds ZODB pool-size=%s; requests will block "
            "waiting for connections. Raise pool-size in zope.conf.",
            settings.concurrency,
            pool_size,
        )


@asynccontextmanager
async def lifespan(app: object) -> AsyncIterator[None]:
    """Own the process lifecycle that ``runwsgi`` + waitress used to provide.

    Zope startup already happened at application build time (``make_wsgi_app``
    fires ``ProcessStarting``); what is missing under an ASGI server is the
    orderly database close on shutdown.

    :param app: The ASGI application (unused; required by the protocol).
    :yields: Control for the lifetime of the server.
    """
    import Zope2

    logger.info("Plone ASGI server ready")
    try:
        yield
    finally:
        if Zope2.DB is not None:
            logger.info("Closing ZODB")
            Zope2.DB.close()

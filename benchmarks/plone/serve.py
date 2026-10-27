"""Serve a real Plone instance under a chosen ASGI/WSGI adapter.

The Plone-backed counterpart of ``benchmarks/bench_adapter.py``: the same two
arms, but the WSGI app is Zope itself, built from a ``zope.conf`` by
:func:`ploneasgi.zopeapp.build_wsgi_app`, with the ZCML-registered native routes
collected the way a project's ``server.py`` would collect them.

``a2wsgi`` goes in through ``create_app``'s ``adapter`` seam, so the two arms
differ in the adapter alone — same routes, same middleware, same Zope.

One route is passed directly rather than through ZCML:
:func:`endpoints.whoami_two_hops`, the comparison arm of ``run.py routes``.

Usage::

    python benchmarks/plone/serve.py ploneasgi --zope-conf <instance>/etc/zope.conf
"""

from endpoints import whoami_two_hops
from pathlib import Path
from ploneasgi import create_app
from ploneasgi import serve
from ploneasgi import Settings
from ploneasgi.zcml import routes_from_registry
from ploneasgi.zopeapp import build_wsgi_app
from ploneasgi.zopeapp import check_concurrency_contract
from starlette.routing import Route

import argparse


def a2wsgi_adapter(wsgi_app, settings: Settings):
    """Mount the WSGI app under ``a2wsgi``, with the same worker count.

    :param wsgi_app: The Zope WSGI application.
    :param settings: Settings providing the concurrency.
    :returns: The a2wsgi middleware.
    """
    from a2wsgi import WSGIMiddleware

    return WSGIMiddleware(wsgi_app, workers=settings.concurrency)


def main() -> None:
    """Parse arguments, start Zope, and serve until terminated."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("adapter", choices=["ploneasgi", "a2wsgi"])
    parser.add_argument("--zope-conf", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=256 * 1024)
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--access-log", action="store_true")
    args = parser.parse_args()

    settings = Settings(
        host=args.host,
        port=args.port,
        concurrency=args.workers,
        access_log=args.access_log,
        log_level=args.log_level.upper(),
        zope_conf=args.zope_conf,
        chunk_size=args.chunk_size,
    )
    # Built here rather than inside create_app: ZCML runs as Zope starts, and
    # the routes it registers have to be collected before the app is composed.
    wsgi_app = build_wsgi_app(settings)
    check_concurrency_contract(settings)
    app = create_app(
        settings,
        routes=[
            *routes_from_registry(),
            Route(f"{settings.route_prefix}/whoami-two-hops", whoami_two_hops),
        ],
        wsgi_app=wsgi_app,
        adapter=a2wsgi_adapter if args.adapter == "a2wsgi" else None,
    )
    print(f"serving Plone under {args.adapter} on {args.host}:{args.port}", flush=True)
    serve(app, settings)


if __name__ == "__main__":
    main()

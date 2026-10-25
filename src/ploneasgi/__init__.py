"""Run Plone under an ASGI server.

Five things live here, in increasing order of how novel they are:

* :class:`~ploneasgi.settings.Settings` and
  :func:`~ploneasgi.zopeapp.build_wsgi_app` — starting Zope from environment
  variables, with no PasteDeploy ``.ini`` on the serving path.
* :class:`~ploneasgi.wsgi.WSGIMiddleware` — an ASGI/WSGI adapter that streams
  response bodies from the event loop instead of from the worker thread, so a
  slow client never pins a thread.
* :class:`~ploneasgi.accesslog.AccessLogMiddleware` — an access log above the
  router, so every route is covered and render time is separable from transfer
  time.
* :class:`~ploneasgi.vhm.VirtualHostMiddleware` and the
  ``<ploneasgi:route />`` directive — native routes that an add-on can declare
  and that reach Plone through the proxy rule a deployment already has.
* :func:`~ploneasgi.context.run_authenticated` — one tested way for a native
  route to get an authenticated Plone context in a worker thread, so endpoints
  do not each re-derive the security scaffolding.

Typical use, in a project's own ``server.py``::

    from ploneasgi import create_app, serve

    app = create_app(routes=[WebSocketRoute("/ws", my_handler)])

    if __name__ == "__main__":
        serve(app)

Every module except :mod:`ploneasgi.testing` imports without Plone installed:
Zope, Plone and the ZODB are imported inside the functions that need them.
``tests/test_zopeapp.py`` checks that, one module at a time.
"""

from importlib.metadata import version
from ploneasgi.accesslog import AccessLogMiddleware
from ploneasgi.app import create_app
from ploneasgi.app import serve
from ploneasgi.context import PloneContext
from ploneasgi.context import run_authenticated
from ploneasgi.settings import load_env
from ploneasgi.settings import Settings
from ploneasgi.settings import setup_logging
from ploneasgi.wsgi import WSGIMiddleware


#: Read from the installed distribution, so ``pyproject.toml`` is the only
#: place the version is written down.
__version__ = version("ploneasgi")

__all__ = [
    "AccessLogMiddleware",
    "PloneContext",
    "Settings",
    "WSGIMiddleware",
    "__version__",
    "create_app",
    "load_env",
    "run_authenticated",
    "serve",
    "setup_logging",
]

"""Native routes the Plone-backed checks drive over a real socket.

Registered by ``routes.zcml`` exactly as an add-on would register its own, so
the checks exercise the whole path a deployment uses: ZCML directive, route
table, VirtualHostMonster rewrite, permission guard, worker-thread context.

Importable as ``endpoints`` only with this directory on ``PYTHONPATH``, which
the harness sets for every process that loads the instance's ZCML —
``zconsole`` included, since it executes ``site.zcml`` too.
"""

from ploneasgi.context import run_authenticated
from ploneasgi.vhm import SITE_KEY
from starlette.requests import Request
from starlette.responses import JSONResponse


#: Site entered when a request arrives with no rewrite; matches ``site=`` in
#: ``routes.zcml``.
DEFAULT_SITE = "Plone"


async def whoami(request: Request) -> JSONResponse:
    """Report who the request authenticated as, and how it was addressed.

    :param request: The Starlette request.
    :returns: The user, the portal entered, and the URL as the route saw it.
    """

    def work(ctx):
        return {"user": ctx.user.getUserName(), "portal": ctx.portal.getId()}

    site = request.scope.get(SITE_KEY) or DEFAULT_SITE
    data = await run_authenticated(request.scope, work, site_id=site)
    data["url"] = str(request.url)
    return JSONResponse(data)

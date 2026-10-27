"""Native routes the Plone-backed checks drive over a real socket.

Registered by ``routes.zcml`` exactly as an add-on would register its own, so
the checks exercise the whole path a deployment uses: ZCML directive, route
table, VirtualHostMonster rewrite, permission guard, worker-thread context.

:func:`whoami_two_hops` is the exception: ``serve.py`` passes it to
``create_app`` directly. It keeps the request shape native HTTP routes had
before the guard and the handler shared a context, so ``run.py routes`` can
measure what that shape cost, side by side with :func:`whoami`, on one server.

Importable as ``endpoints`` only with this directory on ``PYTHONPATH``, which
the harness sets for every process that loads the instance's ZCML —
``zconsole`` included, since it executes ``site.zcml`` too.
"""

from collections.abc import Iterator
from ploneasgi.context import PloneContext
from ploneasgi.context import run_authenticated
from ploneasgi.vhm import SITE_KEY
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse
from starlette.responses import Response
from starlette.responses import StreamingResponse
from typing import BinaryIO


#: Site entered when a request arrives with no rewrite; matches ``site=`` in
#: ``routes.zcml``.
DEFAULT_SITE = "Plone"

#: Bytes read from the blob per chunk of a native download.
CHUNK_SIZE = 256 * 1024


def _identity(ctx: PloneContext) -> dict:
    """:returns: The user the request authenticated as, and the portal entered."""
    return {"user": ctx.user.getUserName(), "portal": ctx.portal.getId()}


def whoami(ctx: PloneContext, request: Request) -> JSONResponse:
    """Report who the request authenticated as, and how it was addressed.

    :param ctx: The context the guard checked the permission in.
    :param request: The Starlette request.
    :returns: The user, the portal entered, and the URL as the route saw it.
    """
    return JSONResponse({**_identity(ctx), "url": str(request.url)})


async def whoami_two_hops(request: Request) -> Response:
    """:func:`whoami`, as a guarded route used to run it: two contexts.

    The permission check runs in one ``run_authenticated`` call and the work in
    a second, each taking a worker thread, a slot of the ZODB limiter, a
    connection and an authentication. The check asks for ``View`` by title,
    which is what the guard resolved ``zope2.View`` to.

    :param request: The Starlette request.
    :returns: The same body :func:`whoami` returns, or a 403.
    """
    scope = request.scope
    site = scope.get(SITE_KEY) or DEFAULT_SITE

    def check(ctx: PloneContext) -> bool:
        return ctx.check_permission("View", ctx.portal)

    if not await run_authenticated(scope, check, site_id=site):
        return PlainTextResponse("Forbidden", status_code=403)
    data = await run_authenticated(scope, _identity, site_id=site)
    return JSONResponse({**data, "url": str(request.url)})


def _chunks(handle: BinaryIO) -> Iterator[bytes]:
    """Read an open file to its end, then close it.

    :param handle: The file, opened in the worker thread.
    :returns: Its contents, a chunk at a time.
    """
    with handle:
        while chunk := handle.read(CHUNK_SIZE):
            yield chunk


def download(ctx: PloneContext, request: Request) -> Response:
    """Stream a File's blob from a native route.

    The blob is opened here, in the worker thread, with ``openDetached()``: a
    plain file on the committed blob, independent of the connection that is
    closed as soon as this returns. The response then reads it on the event
    loop's side, holding no ZODB connection for the transfer.

    :param ctx: The context the guard checked the permission in.
    :param request: The Starlette request; ``path`` is the File's path in the
        site.
    :returns: The blob's bytes.
    """
    field = ctx.traverse(request.path_params["path"]).file
    return StreamingResponse(
        _chunks(field.openDetached()),
        media_type=field.contentType,
        headers={"Content-Length": str(field.getSize())},
    )

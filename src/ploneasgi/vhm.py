"""Read VirtualHostMonster rewrites at the ASGI layer.

Native routes are siblings of the Zope mount, which puts them on the wrong side
of the proxy: a deployment already rewrites every request into

.. code-block:: text

    /VirtualHostBase/<protocol>/<host:port>/<site-path>/VirtualHostRoot/<rest>

and a route that does not understand that shape needs its own proxy rule to be
reachable — one more rule per route, forever. Parsing the rewrite here instead
means the existing rule serves native routes too, and **no new infrastructure is
needed to add one**.

It also removes a duplication. The site path is already inside the rewrite
(``Plone`` above), so a route does not have to restate it in its path, and the
external scheme and host are recovered as a side effect — without them a native
route behind TLS termination sees ``http`` and the container's address.

The reserved prefix is what makes this unambiguous. Anything whose rewritten path
starts with it belongs to a native route; everything else is Zope's, and its
scope is passed through **untouched** so Zope's own VirtualHostMonster still sees
the URL it expects. Getting that wrong would corrupt every URL Plone generates,
which is why ``tests/test_vhm.py`` spends most of its length on it.

Grammar per ``Products.SiteAccess.VirtualHostMonster``. ``_vh_`` segments — the
virtual-path-prefix feature — are deliberately *not* supported: they change what
the root path means, and guessing wrong would silently mis-route. A request that
uses them against a native route is refused loudly instead.
"""

from dataclasses import dataclass
from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from urllib.parse import quote

import logging


logger = logging.getLogger("ploneasgi.vhm")

#: Path prefix reserved for native ASGI routes. Chosen to match Zope's
#: ``++namespace++`` traversal convention, which content ids cannot collide with.
DEFAULT_PREFIX = "/++asgi++"

VIRTUAL_HOST_BASE = "VirtualHostBase"
VIRTUAL_HOST_ROOT = "VirtualHostRoot"
VH_MARKER = "_vh_"

#: Key under which the path to the virtual root is placed on the ASGI scope.
SITE_KEY = "ploneasgi.site"

#: Characters left unquoted in ``raw_path``: RFC 3986 ``pchar`` minus ``%``,
#: plus the ``/`` separator.
RAW_PATH_SAFE = "/:@!$&'()*+,;="

#: Key marking that a scope was rewritten, so downstream code can tell a
#: proxied request from a direct one.
REWRITTEN_KEY = "ploneasgi.vhm"


@dataclass(frozen=True)
class VirtualHost:
    """A parsed VirtualHostMonster rewrite.

    :ivar protocol: External scheme, e.g. ``https``.
    :ivar host: External host, without the port.
    :ivar port: External port, or ``None`` when the URL carried none.
    :ivar site: Path from the Zope root to the virtual root, e.g. ``Plone`` or
        ``sites/Plone/folder`` — everything before ``VirtualHostRoot``. It
        names an object *inside* a site, not necessarily the site itself;
        :func:`ploneasgi.context.run_authenticated` finds the site by walking
        it.
    :ivar path: The real request path, after ``VirtualHostRoot``.
    :ivar has_vh_segments: Whether ``_vh_`` prefix segments were present.
    """

    protocol: str
    host: str
    port: int | None
    site: str
    path: str
    has_vh_segments: bool = False


def parse_virtual_host(path: str) -> VirtualHost | None:
    """Parse a VirtualHostMonster-rewritten path.

    :param path: The raw request path.
    :returns: The parsed rewrite, or ``None`` when the path is not one.
    """
    segments = [segment for segment in path.split("/") if segment]
    if not segments or segments[0] != VIRTUAL_HOST_BASE:
        return None
    if VIRTUAL_HOST_ROOT not in segments or len(segments) < 3:
        # Malformed: VirtualHostBase with no root marker. Not ours to fix —
        # hand it to Zope, whose VHM will produce its own diagnosis.
        return None

    protocol = segments[1]
    host, _, raw_port = segments[2].partition(":")
    root = segments.index(VIRTUAL_HOST_ROOT)

    # Everything between the host and the marker is the path to the virtual
    # root. It is kept whole: its last segment is the site only in the simplest
    # deployment. A folder published as the virtual root, or a site that lives
    # in a Zope folder, both put something else there.
    site_path = segments[3:root]
    rest = segments[root + 1 :]

    has_vh = False
    while rest and rest[0].startswith(VH_MARKER):
        has_vh = True
        rest.pop(0)

    return VirtualHost(
        protocol=protocol,
        host=host,
        port=int(raw_port) if raw_port.isdigit() else None,
        site="/".join(site_path),
        path="/" + "/".join(rest),
        has_vh_segments=has_vh,
    )


def _rewrite(scope: Scope, virtual_host: VirtualHost) -> Scope:
    """Build a scope describing the request as the client made it.

    :param scope: The original ASGI scope.
    :param virtual_host: The parsed rewrite.
    :returns: A new scope; the original is left untouched.
    """
    rewritten = dict(scope)
    rewritten["path"] = virtual_host.path
    # ``path`` arrives already unquoted, and ``raw_path`` is the bytes on the
    # wire, so it has to be quoted again. Encoding it directly raised on any
    # character outside latin-1. ``+`` stays literal so the prefix stays
    # readable; it is a legal path character.
    rewritten["raw_path"] = quote(virtual_host.path, safe=RAW_PATH_SAFE).encode("ascii")
    rewritten["scheme"] = _external_scheme(scope, virtual_host)
    rewritten[SITE_KEY] = virtual_host.site
    rewritten[REWRITTEN_KEY] = True

    # The Host header drives Starlette's request.url, so leaving the container's
    # address in place would make every generated URL wrong in exactly the way
    # VirtualHostMonster exists to prevent.
    authority = virtual_host.host
    if virtual_host.port:
        authority = f"{virtual_host.host}:{virtual_host.port}"
    rewritten["headers"] = [
        (name, value) for name, value in scope.get("headers", []) if name != b"host"
    ] + [(b"host", authority.encode("latin-1"))]

    if virtual_host.port:
        rewritten["server"] = (virtual_host.host, virtual_host.port)

    return rewritten


def _external_scheme(scope: Scope, virtual_host: VirtualHost) -> str:
    """Map the rewrite's protocol onto the scheme this scope type expects.

    :param scope: The original ASGI scope.
    :param virtual_host: The parsed rewrite.
    :returns: The scheme to report.
    """
    secure = virtual_host.protocol in ("https", "wss")
    if scope["type"] == "websocket":
        return "wss" if secure else "ws"
    return "https" if secure else "http"


class VirtualHostMiddleware:
    """Route VirtualHostMonster-rewritten requests to native ASGI routes.

    :ivar prefix: Reserved path prefix identifying a native route.
    :ivar default_site: Site id used when a request arrives without a rewrite,
        which is the direct-access case in development.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        prefix: str = DEFAULT_PREFIX,
        default_site: str | None = None,
    ) -> None:
        """
        :param app: The ASGI application to wrap.
        :param prefix: Reserved path prefix for native routes.
        :param default_site: Site id assumed when there is no rewrite.
        """
        self.app = app
        self.prefix = prefix
        self.default_site = default_site

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Dispatch one connection.

        :param scope: The ASGI connection scope.
        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        virtual_host = parse_virtual_host(scope.get("path", ""))

        if virtual_host is None:
            # No rewrite: direct access. Only annotate the site, and only for
            # our own prefix — Zope's paths must not acquire scope keys that
            # change how anything downstream reads them.
            if scope.get("path", "").startswith(self.prefix) and self.default_site:
                scope = {**scope, SITE_KEY: self.default_site}
            await self.app(scope, receive, send)
            return

        if not virtual_host.path.startswith(self.prefix):
            # Zope's request. Pass the *original* scope through untouched so its
            # own VirtualHostMonster sees the URL it was sent.
            await self.app(scope, receive, send)
            return

        if virtual_host.has_vh_segments:
            logger.error(
                "_vh_ path segments are not supported for native routes (%s)",
                scope.get("path"),
            )
            await _send_unsupported(scope, send)
            return

        await self.app(_rewrite(scope, virtual_host), receive, send)


async def _send_unsupported(scope: Scope, send: Send) -> None:
    """Refuse a request using an unsupported rewrite feature.

    :param scope: The ASGI connection scope.
    :param send: The ASGI send callable.
    """
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1011})
        return
    body = b"_vh_ path segments are not supported for native ASGI routes"
    await send({
        "type": "http.response.start",
        "status": 501,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})

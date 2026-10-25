"""An authenticated Plone context, acquired from a worker thread.

A native ASGI route runs on the event loop, where there is no ZODB connection, no
site, and no security manager. Anything that wants to touch content therefore has
to establish all three — and doing that badly is how you write a file-disclosure
bug. This module exists so it is written *once*, tested, and reused, instead of
being re-derived per endpoint.

.. code-block:: text

    event loop      await run_authenticated(scope, work)
    worker thread     open connection -> build request -> authenticate ->
                      setSite -> run `work` -> abort -> tear down
    event loop      the return value

Everything Zope touches happens inside the worker thread and is torn down before
it returns. A ZODB connection is not thread-safe and the security manager is
thread-local, so neither may outlive the call — which is why the API takes a
callable rather than handing back an open context.

**What `work` may return.** Plain data. Returning a persistent object hands the
event loop an object whose connection has been closed; read a value out of it
instead. There is no way to enforce this, so it is a rule, not a guarantee.

**Credentials.** Authentication is delegated to ``acl_users.validate()``, never to
parsing a cookie or JWT here. That keeps every PAS plugin working — including
whatever a given site has installed — at the price of having to hand PAS a
request that looks enough like a traversed one. See :func:`_publisher_request`.

**Transactions.** Every call ends in ``transaction.abort()``: a context reads
and commits nothing. Writing is deliberately not enabled here; it needs its own
answer to conflict errors and retries, which a read never has to give.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from ploneasgi.wsgi import build_environ
from starlette.types import Scope
from typing import Any

import anyio
import io
import logging


logger = logging.getLogger("ploneasgi.context")


class NotAuthenticated(Exception):
    """Raised when a route requires a real user and got Anonymous."""


class SiteNotFound(LookupError):
    """Raised when the requested site path enters no Plone site."""


def _authenticate(app: Any, portal: Any, request: Any) -> Any:
    """Authenticate against each acquired user folder, innermost first.

    Mirrors what ZPublisher does while traversing: it walks ``PARENTS`` and asks
    each ``acl_users`` in turn, which is why a Zope *root* user — the emergency
    ``admin`` account created by the instance recipe — can act inside a site.
    Asking only ``portal.acl_users`` looks correct and silently locks that
    account out of every native route.

    :param app: The Zope application root.
    :param portal: The Plone site.
    :param request: The publisher request carrying credentials.
    :returns: The authenticated user, or Anonymous.
    """
    from AccessControl.users import nobody

    for folder in (getattr(portal, "acl_users", None), getattr(app, "acl_users", None)):
        if folder is None:
            continue
        user = folder.validate(request)
        if user is not None:
            return user
    return nobody


@dataclass(frozen=True, slots=True)
class PloneContext:
    """What a unit of work is handed inside the worker thread.

    :ivar portal: The Plone site root, acquisition-wrapped and with a request.
    :ivar user: The authenticated user, or Anonymous.
    :ivar request: The synthesized publisher request.
    """

    portal: Any
    user: Any
    request: Any

    @property
    def is_anonymous(self) -> bool:
        """:returns: ``True`` when no credentials authenticated."""
        return self.user is None or self.user.getUserName() == "Anonymous User"

    def require_user(self) -> Any:
        """Assert that a real user authenticated.

        :returns: The authenticated user.
        :raises NotAuthenticated: When the request is anonymous.
        """
        if self.is_anonymous:
            raise NotAuthenticated("this endpoint requires an authenticated user")
        return self.user

    def traverse(self, path: str) -> Any:
        """Traverse to an object, enforcing the current user's permissions.

        Uses ``restrictedTraverse``, which applies the security machinery at each
        step. Do not replace it with ``unrestrictedTraverse`` plus a permission
        check on the final object: that skips the checks on everything in
        between, which is the shape of most path-traversal disclosure bugs.

        :param path: Path relative to the portal, e.g. ``"folder/document"``.
        :returns: The traversed object.
        :raises Unauthorized: When the user may not reach it.
        :raises KeyError: When it does not exist.
        """
        return self.portal.restrictedTraverse(path.lstrip("/"))

    def check_permission(self, permission: str, obj: Any) -> bool:
        """Check a permission on an object as the current user.

        :param permission: Permission title, e.g. ``"View"``.
        :param obj: The object to check against.
        :returns: Whether the permission is granted.
        """
        from AccessControl.SecurityManagement import getSecurityManager

        return bool(getSecurityManager().checkPermission(permission, obj))


def _publisher_request(scope: Scope) -> Any:
    """Build a publisher request from an ASGI scope.

    PAS reaches into a request that traversal has already filled in, so a
    hand-made one has to carry the same keys. Rather than fabricate a mapping,
    this builds a real :class:`ZPublisher.HTTPRequest.HTTPRequest` from the same
    WSGI environ the adapter would have produced — so header parsing, cookie
    parsing and ``__ac`` handling are Zope's code, not ours.

    Websocket scopes carry no ``method`` and use the ``ws``/``wss`` schemes, so
    they are normalized to a GET over http/https. PAS only reads the headers and
    cookies; the method matters to it not at all.

    :param scope: The ASGI connection scope.
    :returns: An ``HTTPRequest`` with an empty body.
    """
    from ZPublisher.HTTPRequest import HTTPRequest
    from ZPublisher.HTTPResponse import HTTPResponse

    normalized = dict(scope)
    normalized.setdefault("method", "GET")
    normalized.setdefault("http_version", "1.1")
    scheme = normalized.get("scheme", "http")
    normalized["scheme"] = {"ws": "http", "wss": "https"}.get(scheme, scheme)

    body = io.BytesIO(b"")
    environ = build_environ(normalized, body)
    response = HTTPResponse(stdout=io.BytesIO())
    return HTTPRequest(body, environ, response)


def _resolve_site(app: Any, path: str) -> tuple[Any, list[str]]:
    """Find the Plone site a path from the Zope root enters.

    The path comes from the VirtualHostMonster rewrite, so it names the
    *virtual root*: often the site itself (``Plone``), but also a folder
    published as the root (``Plone/folder``) or a site kept in a Zope folder
    (``sites/Plone``). Its last segment is therefore not the site id, and
    looking that segment up at the root sends both of the latter to a 404.

    The path is client-supplied, so whatever it ends on must actually be a
    Plone site: any id at the Zope root — ``acl_users``, say — would otherwise
    become the object the route's permission is checked against.

    Walking with ``__getitem__`` is unrestricted, and deliberately so. It only
    locates the site; nothing the walk passes through reaches the caller, and
    every access to content goes through :meth:`PloneContext.traverse`.

    :param app: The Zope application root, request-wrapped.
    :param path: Path from the root, segments separated by ``/``.
    :returns: The outermost Plone site on the path, and the steps leading to it.
    :raises SiteNotFound: When a segment is missing or no segment is a site.
    """
    from plone.base.interfaces import IPloneSiteRoot

    obj = app
    steps: list[str] = []
    for segment in (segment for segment in path.split("/") if segment):
        try:
            obj = obj[segment]
        except (KeyError, AttributeError, TypeError) as exc:
            raise SiteNotFound(path) from exc
        steps.append(segment)
        if IPloneSiteRoot.providedBy(obj):
            # The outermost site is "the portal". Stopping here also means a
            # missing folder below it cannot 404 a request the site can serve.
            return obj, steps
    raise SiteNotFound(path)


def _run_sync(
    scope: Scope,
    work: Callable[[PloneContext], Any],
    site_id: str,
    db: Any,
) -> Any:
    """Establish the context, run ``work``, and tear everything down.

    Runs in a worker thread. The ``finally`` block is the load-bearing part: a
    leaked security manager would apply to whatever request the thread serves
    next, which is a privilege-escalation bug rather than a leak.

    :param scope: The ASGI connection scope.
    :param work: The callable to run with the established context.
    :param site_id: Path from the Zope root to the site, or to an object in it.
    :param db: The ZODB database to open a connection on.
    :returns: Whatever ``work`` returns.
    :raises SiteNotFound: When the path enters no Plone site.
    """
    from AccessControl.SecurityManagement import newSecurityManager
    from AccessControl.SecurityManagement import noSecurityManager
    from zope.component.hooks import setSite
    from ZPublisher.BaseRequest import RequestContainer

    import transaction

    request = _publisher_request(scope)
    connection = db.open()
    try:
        # Wrapping the root in a RequestContainer is what `makerequest` does: it
        # puts REQUEST in the acquisition chain, which PAS and most Plone code
        # reach for via aq_parent rather than being passed.
        app = connection.root()["Application"].__of__(RequestContainer(REQUEST=request))
        portal, steps = _resolve_site(app, site_id)

        # Traversal never happened on this connection, so fill in what PAS reads
        # out of the request. PUBLISHED is the portal because credential
        # extraction does not depend on the target object — the permission check
        # on the real target happens later, explicitly, via `traverse`.
        request["PARENTS"] = [app]
        request["PUBLISHED"] = portal
        request.steps = steps

        setSite(portal)
        user = _authenticate(app, portal, request)
        newSecurityManager(request, user)

        return work(PloneContext(portal=portal, user=user, request=request))
    finally:
        noSecurityManager()
        setSite(None)
        # Nothing is committed. Aborting also discards anything `work` dirtied
        # by accident, so a read-only endpoint cannot quietly become a writer.
        transaction.abort()
        connection.close()
        request.close()


async def run_authenticated(
    scope: Scope,
    work: Callable[[PloneContext], Any],
    *,
    site_id: str,
    db: Any = None,
    limiter: anyio.CapacityLimiter | None = None,
) -> Any:
    """Run ``work`` in a worker thread with an authenticated Plone context.

    :param scope: The ASGI connection scope; supplies the credentials.
    :param work: Called with a :class:`PloneContext`. Must return plain data —
        a persistent object outlives its connection and is unusable.
    :param site_id: Id of the Plone site to enter, or a path from the Zope root
        to it or to an object inside it (``sites/Plone``, ``Plone/folder``) —
        the shape a VirtualHostMonster rewrite supplies.
    :param db: ZODB database to use; defaults to ``Zope2.DB``. Injectable so
        tests can pass the layer's DemoStorage, which is also how the
        worker-thread-sees-the-test-database question gets answered.
    :param limiter: Bounds concurrent calls. Pass the adapter's ``app_limiter``
        to keep native routes inside the same ZODB pool budget as WSGI requests;
        without it these calls contend for anyio's default pool.
    :returns: Whatever ``work`` returns.
    """
    return await anyio.to_thread.run_sync(
        partial(_run_sync, scope, work, site_id, db or default_db()), limiter=limiter
    )


def default_db() -> Any:
    """The process-wide ZODB set up by Zope's startup.

    A function rather than a direct ``Zope2.DB`` read at the call site so that a
    caller which cannot pass ``db=`` explicitly — a route built by the ZCML
    directive, for instance — still has a single place to redirect in tests.

    :returns: The ZODB database.
    :raises RuntimeError: When Zope has not been started in this process.
    """
    import Zope2

    db = Zope2.DB
    if db is None:
        raise RuntimeError(
            "Zope2.DB is not set — Zope has not been started in this process. "
            "Call ploneasgi.zopeapp.build_wsgi_app() first, or pass db=."
        )
    return db

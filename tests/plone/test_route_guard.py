"""The ``<ploneasgi:route />`` HTTP wrapper, against a real PAS and a real ZODB.

``tests/test_zcml.py`` stubs :func:`ploneasgi.context.run_authenticated`, so it
proves the wrapper *asks*; only a real site proves the answer is right. ZCML
names permissions by id (``zope2.View``) while Zope checks them by title
(``View``), and a guard that skips the translation still looks correct for
Manager: Zope treats an unknown permission as Manager-only. So does
``cmf.ManagePortal``, which *is* Manager-only in Plone. The tests therefore use
guards a non-Manager should pass, and keep the ones they must not pass beside
them.

The rest pins the handler contract: one context per request, Zope's refusals
turned into statuses, the body reaching the publisher request, and a blob
opened in the thread still readable once the context is gone.
"""

from . import basic_auth
from . import PRIVATE_DOCUMENT
from . import SITE_ADMIN
from .. import asgi_scope
from .. import receiver
from plone import api
from plone.app.testing import SITE_OWNER_NAME
from plone.namedfile.file import NamedBlobFile
from ploneasgi import context
from ploneasgi.zcml import ASGIRoute
from ploneasgi.zcml import build_route
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import PlainTextResponse
from starlette.responses import StreamingResponse

import asyncio
import os
import pytest
import transaction


#: The wrapper calls ``run_authenticated`` without ``db=``.
pytestmark = pytest.mark.usefixtures("layer_db")


def handler(ctx, request):
    """The endpoint behind the guard; reaching it is what the tests observe."""
    return PlainTextResponse("handled")


def call(
    route_handler,
    permission: str | None,
    site: str,
    headers=None,
    *,
    method: str = "GET",
    body: bytes = b"",
):
    """Call an HTTP route and return the response it produced.

    :param route_handler: The handler the route declares.
    :param permission: The permission id the route declares; ``None`` for an
        unguarded route.
    :param site: The site the route enters.
    :param headers: Raw request headers.
    :param method: The request method.
    :param body: The request body.
    :returns: The response.
    """
    route = ASGIRoute(
        path="/x",
        handler=route_handler,
        permission=permission,
        site=site,
        methods=(method,),
    )
    endpoint = build_route(route).endpoint
    scope = asgi_scope("/++asgi++/x", headers=headers or [], method=method)
    receive = receiver({"type": "http.request", "body": body})
    return asyncio.run(endpoint(Request(scope, receive)))


def guarded(permission: str, site: str, headers=None) -> int:
    """:returns: The status of a request to a route guarded by ``permission``."""
    return call(handler, permission, site, headers).status_code


def send_body(response) -> bytes:
    """Send a response over raw ASGI and collect the body the client gets.

    :param response: A Starlette response.
    :returns: The concatenated ``http.response.body`` chunks.
    """
    chunks: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    asyncio.run(response(asgi_scope("/"), receiver(), send))
    return b"".join(chunks)


class TestPermissionIds:
    def test_anonymous_passes_a_view_guard(self, site_id):
        """``zope2.View`` is granted to Anonymous on the site root."""
        assert guarded("zope2.View", site_id) == 200

    def test_anonymous_is_refused_by_a_manage_guard(self, site_id):
        assert guarded("cmf.ManagePortal", site_id) == 403

    def test_site_administrator_passes_a_site_setup_guard(self, site_id, site_admin):
        """``Plone Site Setup: Overview`` is granted to Site Administrator.

        Without the id-to-title translation the id is unknown to Zope, which
        falls back to Manager-only and refuses this user.
        """
        guard = "plone.app.controlpanel.Overview"
        assert guarded(guard, site_id, site_admin) == 200

    def test_site_administrator_is_refused_by_a_manage_portal_guard(
        self, site_id, site_admin
    ):
        """The boundary: Plone grants ``Manage portal`` to Manager alone.

        CMFPlone's own ``testSiteAdminRole`` pins the same fact.
        """
        assert guarded("cmf.ManagePortal", site_id, site_admin) == 403

    def test_the_site_owner_passes_a_manage_portal_guard(
        self, site_id, site_owner_name, site_owner_password
    ):
        headers = basic_auth(site_owner_name, site_owner_password)
        assert guarded("cmf.ManagePortal", site_id, headers) == 200

    def test_an_unknown_permission_id_refuses_everyone(
        self, site_id, site_owner_name, site_owner_password
    ):
        """A typo must not fall back to Manager-only; it must fail closed."""
        headers = basic_auth(site_owner_name, site_owner_password)
        assert guarded("cmf.NoSuchPermission", site_id, headers) == 403


@pytest.fixture
def connections(db, monkeypatch) -> list[str]:
    """Count the connections the wrapper opens on the layer's database.

    :param db: The layer's database.
    :param monkeypatch: pytest's patcher.
    :returns: One entry per ``db.open()``.
    """
    opened: list[str] = []

    class CountingDB:
        def open(self, *args, **kwargs):
            opened.append("open")
            return db.open(*args, **kwargs)

    monkeypatch.setattr(context, "default_db", CountingDB)
    return opened


class TestOneContextPerRequest:
    """The guard and the handler share one call: issue #5's whole point."""

    def test_a_guarded_request_opens_one_connection(self, site_id, connections):
        assert guarded("zope2.View", site_id) == 200
        assert connections == ["open"]

    def test_an_unguarded_request_opens_one_connection(self, site_id, connections):
        assert call(handler, None, site_id).status_code == 200
        assert connections == ["open"]

    def test_the_handler_runs_as_the_user_the_guard_checked(self, site_id, site_admin):
        def whoami(ctx, request):
            return JSONResponse({"user": ctx.user.getUserName()})

        response = call(whoami, "plone.app.controlpanel.Overview", site_id, site_admin)
        assert response.body == f'{{"user":"{SITE_ADMIN[0]}"}}'.encode()

    def test_an_unguarded_route_authenticates_too(self, site_id, site_admin):
        def whoami(ctx, request):
            return PlainTextResponse(ctx.user.getUserName())

        assert call(whoami, None, site_id, site_admin).body == SITE_ADMIN[0].encode()


class TestRefusalsFromTheHandler:
    @pytest.mark.portal(content=[PRIVATE_DOCUMENT])
    def test_unauthorized_from_traversal_is_a_403(self, site_id):
        def read(ctx, request):
            return PlainTextResponse(ctx.traverse(PRIVATE_DOCUMENT["id"]).Title())

        assert call(read, "zope2.View", site_id).status_code == 403

    def test_require_user_is_a_401(self, site_id):
        def private(ctx, request):
            ctx.require_user()
            return PlainTextResponse("in")  # pragma: no cover - refused above

        assert call(private, "zope2.View", site_id).status_code == 401

    def test_other_errors_are_not_swallowed(self, site_id):
        def broken(ctx, request):
            raise LookupError("a bug, not a refusal")

        with pytest.raises(LookupError):
            call(broken, "zope2.View", site_id)


class TestRequestBody:
    def test_a_posted_form_reaches_the_publisher_request(self, site_id):
        def echo(ctx, request):
            return PlainTextResponse(ctx.request.form["greeting"])

        headers = [(b"content-type", b"application/x-www-form-urlencoded")]
        response = call(
            echo, "zope2.View", site_id, headers, method="POST", body=b"greeting=hi"
        )
        assert response.body == b"hi"

    def test_a_raw_body_is_readable(self, site_id):
        def echo(ctx, request):
            return PlainTextResponse(ctx.request.get("BODY"))

        headers = [(b"content-type", b"application/json")]
        response = call(
            echo, "zope2.View", site_id, headers, method="POST", body=b'{"a": 1}'
        )
        assert response.body == b'{"a": 1}'

    def test_a_malformed_multipart_body_is_a_400(self, site_id):
        """Parsed after the guard, inside the wrapper's error handling."""
        headers = [(b"content-type", b"multipart/form-data; boundary=xyz")]
        response = call(
            handler, "zope2.View", site_id, headers, method="POST", body=b"garbage"
        )
        assert response.status_code == 400

    def test_the_query_string_reaches_the_form(self, site_id):
        def echo(ctx, request):
            return PlainTextResponse(ctx.request.form["q"])

        route = ASGIRoute(
            path="/x", handler=echo, permission="zope2.View", site=site_id
        )
        scope = asgi_scope("/++asgi++/x", query_string=b"q=plone")
        receive = receiver({"type": "http.request", "body": b""})
        response = asyncio.run(build_route(route).endpoint(Request(scope, receive)))
        assert response.body == b"plone"


@pytest.fixture
def blob_file(portal, site_id) -> tuple[str, bytes]:
    """A File with a blob, committed so the worker's connection sees it.

    Files have no workflow in Plone 6: Anonymous may view one in the site root.

    :param portal: The Plone site.
    :param site_id: The site id.
    :returns: The path of the file in the site, and its bytes.
    """
    data = os.urandom(300 * 1024)
    with api.env.adopt_user(SITE_OWNER_NAME):
        api.content.create(
            container=portal,
            type="File",
            id="blob",
            file=NamedBlobFile(data=data, filename="blob.bin"),
        )
    transaction.commit()
    return "blob", data


class TestBlobDownloads:
    def test_a_blob_opened_in_the_thread_streams_after_the_context(
        self, site_id, blob_file, connections
    ):
        """The handle outlives the connection: the body is sent after teardown.

        By the time ``call`` returns, ``run_authenticated`` has aborted, closed
        the connection and dropped the security manager; only then does the
        response send a byte.
        """
        path, data = blob_file

        def download(ctx, request):
            handle = ctx.traverse(path).file.openDetached()

            def chunks():
                with handle:
                    while chunk := handle.read(64 * 1024):
                        yield chunk

            return StreamingResponse(chunks(), media_type="application/octet-stream")

        response = call(download, "zope2.View", site_id)
        assert connections == ["open"]
        assert send_body(response) == data

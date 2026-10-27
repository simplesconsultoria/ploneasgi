"""The Plone-facing paths, checked against a running instance.

The unit suite was once fully green while native routes refused the Zope root
administrator; only a real server found it. So the paths that touch
authentication, traversal and URL generation are checked here over a socket,
through the whole stack a deployment uses: VirtualHostMonster rewrite, ZCML
route, permission guard, worker-thread context.

The routes come from ``benchmarks/plone/routes.zcml``: ``/whoami`` guarded by
``zope2.View``, ``/whoami-manager`` by ``cmf.ManagePortal``, and
``/download/{path}`` streaming a File's blob. ``/whoami-two-hops`` is passed to
``create_app`` by ``serve.py``.
"""

from . import basic_auth
from . import fetch

import json
import pytest
import socket
import time


pytestmark = pytest.mark.bench_plone

#: How a proxy rewrites https://example.org/... onto the site.
VHB = "/VirtualHostBase/https/example.org:443/{root}/VirtualHostRoot"


def rewritten(root: str, rest: str) -> str:
    """:returns: ``rest`` as a proxy rewrites it onto the virtual root ``root``."""
    return VHB.format(root=root) + rest


class TestZopeTraffic:
    def test_a_rewritten_page_generates_external_urls(self, live, manifest):
        """The rewrite reaches Zope untouched, so its own VHM applies it."""
        base, _ = live
        status, _, body = fetch(base, rewritten(manifest["site"], "/"))
        assert status == 200
        assert b"https://example.org" in body

    def test_a_download_is_streamed_not_materialized(self, live, manifest):
        """The assumption the whole package rests on, observed.

        :mod:`ploneasgi.wsgi` logs each response's body type at DEBUG. A
        ``filestream`` iterator means the adapter is pulling chunks on the
        event loop; ``bytes``, ``list`` or ``tuple`` would mean Plone handed it
        the whole body and the slow-client claim does not apply.
        """
        base, log = live
        path = manifest["download"]["path"] + "/@@download/file"
        status, headers, body = fetch(base, path)
        assert status == 200
        assert len(body) == manifest["download"]["size"]
        assert int(headers["content-length"]) == manifest["download"]["size"]
        types = {
            line.split("body type=")[1].strip()
            for line in log.read_text().splitlines()
            if "body type=" in line
        }
        assert any(name.startswith("filestream") for name in types), types

    def test_a_private_download_is_refused(self, live, manifest):
        """Not followed: the redirect to the login form *is* the refusal."""
        base, _ = live
        status, headers, _ = fetch(
            base, manifest["private"]["path"] + "/@@download/file"
        )
        assert status == 302
        assert "require_login" in headers["location"]

    def test_an_abandoned_download_stops_and_is_logged(self, live, manifest):
        """A client that leaves mid-download ends the stream, visibly.

        Checked over a real socket because the unit tests can only fake the
        part that matters: that uvicorn actually delivers ``http.disconnect``
        while the adapter is streaming. Before the adapter watched for it, this
        line read as a complete 200 for the full 50 MB.
        """
        base, log = live
        host, port = base.removeprefix("http://").split(":")
        path = manifest["download"]["path"] + "/@@download/file"
        with socket.create_connection((host, int(port)), timeout=30) as sock:
            # A small receive buffer, so the server cannot push the whole body
            # into kernel buffers before the close is noticed.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            assert sock.recv(64 * 1024).startswith(b"HTTP/1.1 200")

        deadline = time.monotonic() + 10
        lines: list[str] = []
        while time.monotonic() < deadline:
            lines = [
                line for line in log.read_text().splitlines() if f"GET {path} " in line
            ]
            if any("incomplete=client-gone" in line for line in lines):
                break
            time.sleep(0.2)
        aborted = [line for line in lines if "incomplete=client-gone" in line]
        assert aborted, lines
        sent = int(aborted[-1].split('" 200 ')[1].split()[0])
        assert sent < manifest["download"]["size"]


class TestNativeRoutes:
    def test_anonymous_passes_a_view_guard(self, live):
        base, _ = live
        status, _, body = fetch(base, "/++asgi++/whoami")
        assert status == 200
        assert json.loads(body)["user"] == "Anonymous User"

    def test_anonymous_is_refused_by_a_manage_guard(self, live):
        base, _ = live
        assert fetch(base, "/++asgi++/whoami-manager")[0] == 403

    def test_the_zope_root_admin_passes_through_the_rewrite(self, live, manifest):
        """The regression a real server found first: a root user, behind VHM."""
        base, _ = live
        path = rewritten(manifest["site"], "/++asgi++/whoami-manager")
        status, _, body = fetch(base, path, basic_auth(*manifest["admin"]))
        assert status == 200
        data = json.loads(body)
        assert data["user"] == manifest["admin"][0]
        assert data["portal"] == manifest["site"]
        # Scheme and host come from the rewrite, not from the socket.
        assert data["url"].startswith("https://example.org")

    def test_a_folder_as_virtual_root_enters_its_site(self, live, manifest):
        base, _ = live
        root = manifest["vroot"]["path"].lstrip("/")
        status, _, body = fetch(base, rewritten(root, "/++asgi++/whoami"))
        assert status == 200
        assert json.loads(body)["portal"] == manifest["site"]

    @pytest.mark.parametrize("root", ["no-such-site", "acl_users"])
    def test_a_root_that_is_not_a_site_is_not_found(self, live, root):
        base, _ = live
        assert fetch(base, rewritten(root, "/++asgi++/whoami"))[0] == 404

    def test_the_two_hop_arm_answers_like_the_route(self, live):
        """The comparison arm of ``run.py routes`` must do the same work."""
        base, _ = live
        one = json.loads(fetch(base, "/++asgi++/whoami")[2])
        two = json.loads(fetch(base, "/++asgi++/whoami-two-hops")[2])
        assert {k: v for k, v in one.items() if k != "url"} == {
            k: v for k, v in two.items() if k != "url"
        }

    def test_a_blob_streams_whole_from_a_native_route(self, live, manifest):
        """Opened in the worker thread, sent after it is released."""
        base, _ = live
        site = manifest["site"]
        path = manifest["download"]["path"].removeprefix(f"/{site}/")
        status, headers, body = fetch(base, f"/++asgi++/download/{path}")
        assert status == 200
        assert len(body) == manifest["download"]["size"]
        assert int(headers["content-length"]) == manifest["download"]["size"]

    def test_a_private_blob_is_refused_from_a_native_route(self, live, manifest):
        """Traversal checks every step; the handler's Unauthorized is a 403."""
        base, _ = live
        site = manifest["site"]
        path = manifest["private"]["path"].removeprefix(f"/{site}/")
        assert fetch(base, f"/++asgi++/download/{path}")[0] == 403

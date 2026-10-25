"""Tests for VirtualHostMonster parsing and the routing middleware.

Zope-free. Most of the length is spent on one property: a request that is *not*
for a native route must reach the WSGI mount with its scope byte-for-byte
unchanged. Rewriting Zope's path would corrupt every URL Plone generates, which
is a far worse failure than a native route not matching.
"""

from . import asgi_scope
from ploneasgi.vhm import DEFAULT_PREFIX
from ploneasgi.vhm import parse_virtual_host
from ploneasgi.vhm import REWRITTEN_KEY
from ploneasgi.vhm import SITE_KEY
from ploneasgi.vhm import VirtualHostMiddleware

import asyncio
import pytest


def vhm(site="Plone", rest="/++asgi++/notify", proto="https", host="example.com:443"):
    """Build a VirtualHostMonster-rewritten path.

    :param site: Site path between the host and the marker.
    :param rest: Path after the marker.
    :param proto: External protocol.
    :param host: External host and port.
    :returns: The rewritten path.
    """
    return f"/VirtualHostBase/{proto}/{host}/{site}/VirtualHostRoot{rest}"


def scope_for(path, scope_type="http", headers=None):
    """Build an ASGI scope.

    :param path: Request path.
    :param scope_type: ASGI scope type.
    :param headers: Raw request headers; defaults to the container's Host.
    :returns: The scope mapping.
    """
    return asgi_scope(
        path,
        scope_type=scope_type,
        # What the proxy forwards, before any rewrite: the scheme and Host of
        # the container, not of the client.
        scheme="http",
        headers=headers if headers is not None else [(b"host", b"backend:8080")],
        client=("10.0.0.1", 5555),
    )


def dispatch(scope, **kwargs):
    """Run the middleware and capture what the wrapped app received.

    :param scope: The ASGI connection scope.
    :param kwargs: Passed through to :class:`VirtualHostMiddleware`.
    :returns: Tuple of (scope the app saw or None, messages sent).
    """
    seen = {}
    sent = []

    async def app(inner_scope, receive, send):
        seen["scope"] = inner_scope

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(VirtualHostMiddleware(app, **kwargs)(scope, receive, send))
    return seen.get("scope"), sent


class TestParsing:
    def test_returns_none_for_an_ordinary_path(self):
        assert parse_virtual_host("/Plone/front-page") is None

    def test_extracts_the_site(self):
        assert parse_virtual_host(vhm()).site == "Plone"

    def test_extracts_the_real_path(self):
        assert parse_virtual_host(vhm()).path == "/++asgi++/notify"

    def test_extracts_protocol_host_and_port(self):
        parsed = parse_virtual_host(vhm())
        assert (parsed.protocol, parsed.host, parsed.port) == (
            "https",
            "example.com",
            443,
        )

    def test_port_is_optional(self):
        assert parse_virtual_host(vhm(host="example.com")).port is None

    def test_a_nested_root_keeps_the_whole_site_path(self):
        """A folder published as the virtual root is still inside a Plone site.

        Its last segment names the folder, not the site, so the whole path is
        kept and the site is found by traversing it.
        """
        assert parse_virtual_host(vhm(site="Plone/subfolder")).site == "Plone/subfolder"

    def test_a_site_inside_a_zope_folder_keeps_the_whole_path(self):
        assert parse_virtual_host(vhm(site="sites/Plone")).site == "sites/Plone"

    def test_a_zope_root_rewrite_names_no_site(self):
        assert parse_virtual_host(vhm(site="").replace("//", "/")).site == ""

    def test_root_path_becomes_a_slash(self):
        assert parse_virtual_host(vhm(rest="")).path == "/"

    def test_vh_segments_are_flagged_and_stripped(self):
        parsed = parse_virtual_host(vhm(rest="/_vh_prefix/++asgi++/notify"))
        assert parsed.has_vh_segments is True
        assert parsed.path == "/++asgi++/notify"

    def test_missing_root_marker_is_not_parsed(self):
        """Malformed: leave it for Zope's own VHM to complain about."""
        assert (
            parse_virtual_host("/VirtualHostBase/https/example.com:443/Plone") is None
        )


class TestZopeTrafficIsUntouched:
    """The property that matters most. Breaking it corrupts every Plone URL."""

    def test_rewritten_zope_path_passes_through_identically(self):
        original = scope_for(vhm(rest="/front-page"))
        seen, _ = dispatch(original)
        assert seen["path"] == vhm(rest="/front-page")
        assert seen is original

    def test_plain_zope_path_passes_through_identically(self):
        original = scope_for("/Plone/front-page")
        seen, _ = dispatch(original)
        assert seen is original

    def test_no_scope_keys_are_added_to_zope_traffic(self):
        seen, _ = dispatch(scope_for(vhm(rest="/front-page")), default_site="Plone")
        assert SITE_KEY not in seen
        assert REWRITTEN_KEY not in seen

    def test_host_header_is_left_alone_for_zope(self):
        """Zope does its own VHM; a pre-rewritten Host would double-apply."""
        seen, _ = dispatch(scope_for(vhm(rest="/front-page")))
        assert dict(seen["headers"])[b"host"] == b"backend:8080"

    def test_a_path_merely_containing_the_prefix_is_still_zope(self):
        seen, _ = dispatch(scope_for(vhm(rest="/docs/about-++asgi++-routing")))
        assert SITE_KEY not in seen


class TestNativeRouteRewriting:
    def test_path_is_stripped_to_the_real_one(self):
        seen, _ = dispatch(scope_for(vhm()))
        assert seen["path"] == "/++asgi++/notify"

    def test_site_is_taken_from_the_rewrite(self):
        seen, _ = dispatch(scope_for(vhm(site="intranet")))
        assert seen[SITE_KEY] == "intranet"

    def test_external_scheme_is_recovered(self):
        seen, _ = dispatch(scope_for(vhm(proto="https")))
        assert seen["scheme"] == "https"

    def test_external_host_replaces_the_container_address(self):
        seen, _ = dispatch(scope_for(vhm()))
        assert dict(seen["headers"])[b"host"] == b"example.com:443"

    def test_other_headers_survive(self):
        headers = [(b"host", b"backend:8080"), (b"authorization", b"Basic xyz")]
        seen, _ = dispatch(scope_for(vhm(), headers=headers))
        assert dict(seen["headers"])[b"authorization"] == b"Basic xyz"

    def test_the_scope_is_marked_as_rewritten(self):
        seen, _ = dispatch(scope_for(vhm()))
        assert seen[REWRITTEN_KEY] is True

    def test_the_original_scope_is_not_mutated(self):
        original = scope_for(vhm())
        dispatch(original)
        assert original["path"].startswith("/VirtualHostBase")
        assert SITE_KEY not in original

    def test_raw_path_keeps_the_prefix_readable(self):
        seen, _ = dispatch(scope_for(vhm()))
        assert seen["raw_path"] == b"/++asgi++/notify"

    def test_a_non_latin1_path_is_percent_encoded(self):
        """``raw_path`` is bytes on the wire, so it must be percent-encoded.

        Encoding the decoded path as latin-1 raised ``UnicodeEncodeError`` here,
        which reached the client as a 500.
        """
        seen, _ = dispatch(scope_for(vhm(rest="/++asgi++/日本")))
        assert seen["path"] == "/++asgi++/日本"
        assert seen["raw_path"] == b"/++asgi++/%E6%97%A5%E6%9C%AC"

    def test_websocket_scheme_becomes_wss(self):
        seen, _ = dispatch(scope_for(vhm(proto="https"), scope_type="websocket"))
        assert seen["scheme"] == "wss"

    def test_websocket_scheme_becomes_ws_for_http(self):
        seen, _ = dispatch(scope_for(vhm(proto="http"), scope_type="websocket"))
        assert seen["scheme"] == "ws"


class TestDirectAccess:
    """No proxy in front — the development case."""

    def test_default_site_is_supplied_for_native_paths(self):
        seen, _ = dispatch(scope_for("/++asgi++/notify"), default_site="Plone")
        assert seen[SITE_KEY] == "Plone"

    def test_default_site_is_not_supplied_to_zope_paths(self):
        seen, _ = dispatch(scope_for("/Plone/front-page"), default_site="Plone")
        assert SITE_KEY not in seen

    def test_without_a_default_site_nothing_is_added(self):
        seen, _ = dispatch(scope_for("/++asgi++/notify"))
        assert SITE_KEY not in seen


class TestUnsupportedRewrites:
    def test_vh_segments_are_refused_loudly_for_native_routes(self):
        """Guessing the virtual root would silently mis-route."""
        seen, sent = dispatch(scope_for(vhm(rest="/_vh_x/++asgi++/notify")))
        assert seen is None
        assert sent[0]["status"] == 501

    def test_vh_segments_are_fine_for_zope_traffic(self):
        """Zope supports them; only our own routes do not."""
        original = scope_for(vhm(rest="/_vh_x/front-page"))
        seen, sent = dispatch(original)
        assert seen is original
        assert sent == []

    def test_websocket_is_closed_rather_than_answered(self):
        _, sent = dispatch(scope_for(vhm(rest="/_vh_x/++asgi++/ws"), "websocket"))
        assert sent[0]["type"] == "websocket.close"


class TestPassThrough:
    def test_lifespan_is_forwarded(self):
        seen, _ = dispatch({"type": "lifespan"})
        assert seen["type"] == "lifespan"

    @pytest.mark.parametrize("prefix", ["/++asgi++", "/api", "/_native"])
    def test_the_prefix_is_configurable(self, prefix):
        seen, _ = dispatch(scope_for(vhm(rest=f"{prefix}/notify")), prefix=prefix)
        assert seen[SITE_KEY] == "Plone"

    def test_default_prefix_is_the_documented_one(self):
        assert DEFAULT_PREFIX == "/++asgi++"

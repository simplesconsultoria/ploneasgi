"""Tests for the ASGI-layer access log.

Zope-free, and driven through the raw ASGI protocol like ``test_wsgi``: the
claims worth pinning are all about individual protocol events — that bytes are
counted from ``http.response.body`` rather than from a header, that an aborted
stream is marked incomplete, that a websocket session produces a line at all.
"""

from . import asgi_scope
from ploneasgi.accesslog import AccessLogMiddleware
from ploneasgi.accesslog import USER_KEY

import asyncio
import logging
import pytest


#: A client address distinct from the default, so the tests can tell it from
#: an ``X-Forwarded-For`` value.
CLIENT = ("10.0.0.7", 54321)


def scope_for(
    path: str = "/",
    *,
    method: str = "GET",
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    **extra,
) -> dict:
    """Build an ASGI HTTP scope.

    :param path: Request path.
    :param method: HTTP method.
    :param query: Raw query string.
    :param headers: Raw request headers.
    :param extra: Extra scope keys.
    :returns: The scope mapping.
    """
    return asgi_scope(
        path,
        method=method,
        query_string=query,
        headers=headers or [],
        client=CLIENT,
        **extra,
    )


def collector() -> tuple[logging.Logger, list[str]]:
    """Build an isolated logger that appends formatted messages to a list.

    The level must be set explicitly: an unconfigured logger inherits root's
    ``WARNING`` and would silently drop every ``INFO`` access line.

    :returns: The logger and the list its records land in.
    """
    lines: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    log = logging.getLogger(f"ploneasgi.access.test{id(lines)}")
    log.handlers = [Collector()]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log, lines


def run(app, scope: dict, *, messages: list | None = None, **kwargs) -> list[str]:
    """Drive one connection through the middleware and collect the log lines.

    :param app: The ASGI app to wrap.
    :param scope: The ASGI connection scope.
    :param messages: Messages the client sends.
    :param kwargs: Passed through to :class:`AccessLogMiddleware`.
    :returns: The formatted log lines.
    """
    log, lines = collector()

    inbox = list(
        messages or [{"type": "http.request", "body": b"", "more_body": False}]
    )

    async def receive():
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message):
        pass

    asyncio.run(AccessLogMiddleware(app, log=log, **kwargs)(scope, receive, send))
    return lines


async def hello(scope, receive, send):
    """A minimal ASGI app returning a fixed body."""
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"hello", "more_body": False})


def test_logs_one_line_per_request():
    assert len(run(hello, scope_for())) == 1


def test_logs_method_path_and_status():
    line = run(hello, scope_for("/Plone"))[0]
    assert '"GET /Plone HTTP/1.1" 200' in line


def test_includes_query_string_in_the_path():
    line = run(hello, scope_for("/search", query=b"q=plone&b_start=10"))[0]
    assert "/search?q=plone&b_start=10" in line


def test_logs_the_client_address():
    assert run(hello, scope_for())[0].startswith("10.0.0.7 ")


def test_ignores_x_forwarded_for_by_default():
    """The header is client-supplied, so trusting it by default invites spoofing."""
    scope = scope_for(headers=[(b"x-forwarded-for", b"1.2.3.4")])
    assert run(hello, scope)[0].startswith("10.0.0.7 ")


def test_trusts_x_forwarded_for_when_asked():
    scope = scope_for(headers=[(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")])
    assert run(hello, scope, trust_forwarded=True)[0].startswith("1.2.3.4 ")


def test_logs_referer_and_user_agent():
    scope = scope_for(
        headers=[(b"referer", b"http://example.com/"), (b"user-agent", b"curl/8")]
    )
    line = run(hello, scope)[0]
    assert '"http://example.com/" "curl/8"' in line


def test_missing_headers_become_dashes():
    assert '"-" "-"' in run(hello, scope_for())[0]


def test_user_is_a_dash_without_the_scope_key():
    """Authentication happens inside Zope, below this middleware."""
    assert run(hello, scope_for())[0].startswith("10.0.0.7 - -")


def test_user_is_read_from_the_scope_when_set():
    """The adapter exposes the scope to WSGI, so Zope-side code can fill this in."""
    line = run(hello, scope_for(**{USER_KEY: "admin"}))[0]
    assert line.startswith("10.0.0.7 - admin")


def test_counts_bytes_actually_sent_not_content_length():
    """The regression that motivated replacing translogger.

    ``TransLogger`` reads ``Content-Length`` from the headers and never counts
    what it wrote. Here the header lies and the log must not repeat the lie.
    """

    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"999999")],
        })
        await send({
            "type": "http.response.body",
            "body": b"1234567890",
            "more_body": False,
        })

    line = run(app, scope_for())[0]
    assert " 200 10 " in line
    assert "999999" not in line


def test_counts_bytes_across_multiple_chunks():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(4):
            await send({
                "type": "http.response.body",
                "body": b"x" * 8,
                "more_body": True,
            })
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    assert " 200 32 " in run(app, scope_for())[0]


def test_marks_a_response_the_application_truncated():
    """No final body event means the application stopped emitting early.

    Note what this does *not* cover: a client that walks away mid-download.
    uvicorn discards chunks after a disconnect without raising, so the app keeps
    emitting and the response still looks complete here. Verified against a real
    server 2026-08-03 — an aborted 50 MB download logged as a clean 200.
    """

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({
            "type": "http.response.body",
            "body": b"partial",
            "more_body": True,
        })

    line = run(app, scope_for())[0]
    assert "incomplete=truncated" in line
    assert " 200 7 " in line


def test_completed_requests_carry_no_note():
    assert "incomplete" not in run(hello, scope_for())[0]


def test_logs_and_reraises_when_the_app_fails():
    async def app(scope, receive, send):
        raise RuntimeError("boom")

    log, lines = collector()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    with pytest.raises(RuntimeError):
        asyncio.run(AccessLogMiddleware(app, log=log)(scope_for(), receive, send))

    assert len(lines) == 1
    assert " 500 0 " in lines[0]


def test_logs_a_dash_when_the_app_never_responded():
    async def app(scope, receive, send):
        return

    line = run(app, scope_for())[0]
    assert "incomplete=no-response" in line
    assert '"GET / HTTP/1.1" - 0 ' in line


def test_ttfb_is_separate_from_duration():
    """The distinction the package exists for: render time vs transfer time."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0.05)
        await send({"type": "http.response.body", "body": b"x", "more_body": False})

    # Fields are: … status bytes duration ttfb "referer" "ua"
    fields = run(app, scope_for())[0].split()
    duration = float(fields[-4])
    ttfb = float(fields[-3])
    assert ttfb < 0.02
    assert duration >= 0.05
    assert duration > ttfb


def test_lifespan_passes_through_unlogged():
    """Swallowing lifespan would hang startup, and there is nothing to log."""
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    log, lines = collector()

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    asyncio.run(AccessLogMiddleware(app, log=log)({"type": "lifespan"}, receive, send))
    assert seen == ["lifespan"]
    assert lines == []


def test_logs_a_websocket_session():
    """The reason the log lives at this layer: one format for both protocols."""

    async def app(scope, receive, send):
        await send({"type": "websocket.accept"})
        await receive()
        await send({"type": "websocket.send", "text": "echo"})
        await receive()

    scope = asgi_scope("/ws", scope_type="websocket", client=CLIENT)
    messages = [
        {"type": "websocket.receive", "text": "hi"},
        {"type": "websocket.disconnect", "code": 1001},
    ]

    line = run(app, scope, messages=messages)[0]
    assert "WEBSOCKET /ws" in line
    assert "1001" in line
    assert "messages=1/1" in line


def test_a_newline_in_the_path_cannot_forge_a_line():
    """uvicorn unquotes the path, so ``%0A`` arrives as a real newline.

    Written as is, it ends the line early and lets the client append a forged
    entry of its own choosing.
    """
    forged = '/x\n127.0.0.1 - admin [01/Jan/2026:00:00:00 +0000] "GET / HTTP/1.1" 200'
    line = run(hello, scope_for(forged))[0]
    assert "\n" not in line
    assert "/x\\x0a127.0.0.1" in line


def test_a_quote_cannot_close_the_request_field_early():
    line = run(hello, scope_for('/a" 200 0'))[0]
    assert '"GET /a\\" 200 0 HTTP/1.1"' in line


def test_header_fields_are_escaped_too():
    scope = scope_for(headers=[(b"user-agent", b'evil" "\x1b[31m')])
    line = run(hello, scope)[0]
    assert '"evil\\" \\"\\x1b[31m"' in line


def test_custom_format_is_honoured():
    line = run(hello, scope_for("/x"), fmt="%(method)s %(path)s -> %(status)s")[0]
    assert line == "GET /x -> 200"

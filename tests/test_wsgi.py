"""Tests for the ASGI/WSGI adapter.

Deliberately Zope-free: the adapter is a general WSGI adapter, and every shape it
has to get right — a materialized body, a stream iterator, a raw file object, a
deferred ``start_response`` — is reproducible in a few lines of plain WSGI.
Anything needing Plone belongs in ``tests/plone/``.

The app is driven through the raw ASGI protocol rather than Starlette's
``TestClient``, which would pull in ``httpx``. That is not only cheaper: it
keeps every individual ``http.response.body`` event visible, so the chunking
behaviour — the whole reason this adapter exists — can actually be asserted.
"""

from . import asgi_scope
from ploneasgi.wsgi import WSGIMiddleware

import asyncio
import io
import pytest


class Response:
    """What the adapter sent, decomposed."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []

    @property
    def body(self) -> bytes:
        """:returns: The concatenated response body."""
        return b"".join(self.chunks)


def call(
    app,
    *,
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    **kwargs,
) -> Response:
    """Drive one HTTP request through the adapter.

    :param app: The WSGI application to mount.
    :param method: HTTP method.
    :param path: Request path.
    :param query: Raw query string.
    :param headers: Raw request headers.
    :param body: Request body.
    :param kwargs: Passed through to :class:`WSGIMiddleware`.
    :returns: The collected :class:`Response`.
    """
    middleware = WSGIMiddleware(app, **kwargs)
    scope = asgi_scope(path, method=method, query_string=query, headers=headers or [])

    messages = [{"type": "http.request", "body": body, "more_body": False}]
    response = Response()

    async def receive():
        return messages.pop(0)

    async def send(message):
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = {
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            if message["body"]:
                response.chunks.append(message["body"])

    asyncio.run(middleware(scope, receive, send))
    return response


class StreamIterator:
    """Stands in for ``filestream_iterator`` / ``filestream_range_iterator``.

    What matters to the adapter is the public ``streamsize`` attribute, which it
    raises to its own chunk size, and that iteration honours it.
    """

    def __init__(self, data: bytes, streamsize: int = 1 << 16) -> None:
        """
        :param data: The payload to stream.
        :param streamsize: Initial chunk size, as the Zope classes expose it.
        """
        self._io = io.BytesIO(data)
        self.streamsize = streamsize
        self.closed = False

    def __iter__(self) -> StreamIterator:
        """:returns: Itself."""
        return self

    def __next__(self) -> bytes:
        """:returns: The next chunk.

        :raises StopIteration: When the payload is exhausted.
        """
        data = self._io.read(self.streamsize)
        if not data:
            raise StopIteration
        return data

    def close(self) -> None:
        """Record that the adapter closed the iterable, as WSGI requires."""
        self.closed = True


def test_simple_response():
    """A materialized body is returned verbatim, with its headers."""

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"hello ", b"world"]

    response = call(app)

    assert response.status == 200
    assert response.body == b"hello world"
    assert response.headers["content-type"] == "text/plain"


def test_environ_is_pep3333():
    """The environ carries the request line, query string and headers."""
    captured = {}

    def app(environ, start_response):
        captured.update(environ)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b""]

    call(
        app,
        path="/some/path",
        query=b"a=1&b=2",
        headers=[(b"x-custom", b"value")],
    )

    assert captured["REQUEST_METHOD"] == "GET"
    assert captured["PATH_INFO"] == "/some/path"
    assert captured["QUERY_STRING"] == "a=1&b=2"
    assert captured["HTTP_X_CUSTOM"] == "value"
    assert captured["wsgi.url_scheme"] == "http"
    assert captured["SERVER_PORT"] == "8080"
    assert captured["REMOTE_ADDR"] == "127.0.0.1"


def test_script_name_is_stripped_from_path_info():
    """Under a Starlette ``Mount``, PATH_INFO excludes the mount prefix."""
    captured = {}

    def app(environ, start_response):
        captured.update(environ)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b""]

    middleware = WSGIMiddleware(app)
    scope = asgi_scope("/mounted/inner", root_path="/mounted")
    messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        return None

    asyncio.run(middleware(scope, receive, send))

    assert captured["SCRIPT_NAME"] == "/mounted"
    assert captured["PATH_INFO"] == "/inner"


def test_request_body_is_readable():
    """The request body reaches the app as a seekable file-like object."""

    def app(environ, start_response):
        payload = environ["wsgi.input"].read()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [payload[::-1]]

    response = call(app, method="POST", body=b"abcdef")

    assert response.body == b"fedcba"


def test_stream_iterator_is_rechunked_and_closed():
    """A stream iterator has ``streamsize`` retuned, is chunked, and is closed."""
    payload = b"x" * (300 * 1024)
    iterator = StreamIterator(payload)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/octet-stream")])
        return iterator

    response = call(app, chunk_size=128 * 1024)

    assert response.body == payload
    # The adapter owns the chunk size, not the application.
    assert iterator.streamsize == 128 * 1024
    assert [len(chunk) for chunk in response.chunks] == [
        128 * 1024,
        128 * 1024,
        44 * 1024,
    ]
    assert iterator.closed


def test_file_object_is_read_not_iterated():
    """A raw file object is read in chunks rather than iterated into lines.

    This is the regression that matters for blobs: every ``io.IOBase`` also has
    ``__next__``, and iterating a binary file splits on newlines — so a blob
    would be delivered in chunks of whatever size the data happens to imply.
    """
    payload = b"a\nb\n" * 50_000

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/octet-stream")])
        return io.BytesIO(payload)

    response = call(app, chunk_size=64 * 1024)

    assert response.body == payload
    assert max(len(chunk) for chunk in response.chunks) == 64 * 1024


def test_deferred_start_response():
    """An app may defer ``start_response`` until it yields its first chunk."""

    def app(environ, start_response):
        def generate():
            start_response("201 Created", [("Content-Type", "text/plain")])
            yield b"deferred"

        return generate()

    response = call(app)

    assert response.status == 201
    assert response.body == b"deferred"


def test_write_callable_is_supported():
    """The legacy ``write()`` callable still reaches the client, in order."""

    def app(environ, start_response):
        write = start_response("200 OK", [("Content-Type", "text/plain")])
        write(b"written ")
        return iter([b"returned"])

    response = call(app)

    assert response.body == b"written returned"


def test_repeated_headers_are_joined():
    """Repeated request headers are comma-joined, as PEP 3333 requires."""
    captured = {}

    def app(environ, start_response):
        captured.update(environ)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b""]

    call(app, headers=[(b"x-multi", b"one"), (b"x-multi", b"two")])

    assert captured["HTTP_X_MULTI"] == "one,two"


def test_content_type_and_length_are_unprefixed():
    """``Content-Type`` and ``Content-Length`` must not gain an ``HTTP_`` prefix."""
    captured = {}

    def app(environ, start_response):
        captured.update(environ)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b""]

    call(
        app,
        method="POST",
        body=b"xy",
        headers=[(b"content-type", b"application/json"), (b"content-length", b"2")],
    )

    assert captured["CONTENT_TYPE"] == "application/json"
    assert captured["CONTENT_LENGTH"] == "2"
    assert "HTTP_CONTENT_TYPE" not in captured


def test_application_error_becomes_500():
    """An exception before any header reaches the client yields a bare 500."""

    def app(environ, start_response):
        raise RuntimeError("boom")

    response = call(app)

    assert response.status == 500


def test_status_and_headers_round_trip():
    """Non-200 statuses and multiple response headers survive the adapter."""

    def app(environ, start_response):
        start_response(
            "404 Not Found",
            [("Content-Type", "text/plain"), ("X-Trace", "abc")],
        )
        return [b"nope"]

    response = call(app)

    assert response.status == 404
    assert response.headers["x-trace"] == "abc"
    assert response.body == b"nope"


@pytest.mark.parametrize("size", [0, 1, 64 * 1024, 300 * 1024])
def test_payload_sizes_round_trip(size):
    """Bodies around and across the chunk boundary are delivered intact.

    :param size: Payload size in bytes.
    """
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/octet-stream")])
        return StreamIterator(payload)

    response = call(app, chunk_size=64 * 1024)

    assert response.body == payload


def test_websocket_scope_is_refused():
    """A WSGI app cannot speak websocket; the mount closes rather than hangs."""

    def app(environ, start_response):  # pragma: no cover - must not be called
        raise AssertionError("WSGI app must not see a websocket scope")

    middleware = WSGIMiddleware(app)
    sent = []

    async def receive():  # pragma: no cover - not reached
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "websocket"}, receive, send))

    assert sent == [{"type": "websocket.close", "code": 1000}]

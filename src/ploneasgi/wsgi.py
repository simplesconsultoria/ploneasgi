"""An ASGI↔WSGI adapter that never holds a worker thread waiting for a client.

This replaces :class:`a2wsgi.WSGIMiddleware`. The difference is where the
response body is consumed.

a2wsgi runs the whole WSGI call — including iterating the response body — inside
a worker thread, and pushes each chunk back to the event loop with a blocking
``run_coroutine_threadsafe(...).result()``. Two consequences, both measured:

* Every chunk costs a round trip through the single event loop, which serializes
  workers against each other. Measured 2026-08-02: the translation layer is 66%
  of the ASGI overhead versus waitress, and its cost *grows* with concurrency
  (+395 µs at c=1, +1148 µs at c=4) while native ASGI's does not.
* A worker thread stays blocked in ``send`` until the client accepts the bytes.
  One visitor on bad wifi therefore pins one thread for the length of their
  download. Measured 2026-08-03: with 4 threads, 4 concurrent slow downloads
  stop the site answering page requests entirely.

Here the split is different:

.. code-block:: text

    worker thread   call the WSGI app, return (status, headers, iterable)
    event loop      pull one chunk at a time and send it

The app call is a single thread hop. Producing each chunk is a short hop of its
own — so the thread is held for the *work*, never for the *transfer*. Waiting on
a slow client happens on the event loop, holding nothing.

That holds for every response Zope streams, not only blob downloads.
``ZPublisher`` already closes the ZODB connection before returning its body
iterable, and says so in :class:`ZPublisher.Iterators.IStreamIterator`:

    *"After the application finishes interpreting a request and returns an
    iterator to be processed asynchronously, it closes the ZODB connection."*

Zope has described this architecture since Zope 2. What was missing was a server
that could actually process the iterator asynchronously.

.. note::
   Nothing in this module imports Zope. It is a general WSGI adapter and is
   tested as one.
"""

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from tempfile import SpooledTemporaryFile
from typing import Any

import anyio
import io
import logging
import sys


logger = logging.getLogger("ploneasgi.wsgi")

#: Bytes pulled from a streaming body per thread hop.
#:
#: 64 KB is what ``filestream_iterator`` and ``FileResponse`` default to, but
#: each chunk here costs a thread dispatch, so the default is larger. Measured
#: 2026-08-03 on a 50 MB blob at c=4: 666 MiB/s at 64 KB, 1757 at 256 KB, 3758 at
#: 1 MB. Past 256 KB the gain is measured on a warm page cache over loopback and
#: pays for itself in memory held per in-flight response and coarser
#: backpressure for slow clients — which is the case that matters.
CHUNK_SIZE = 256 * 1024

#: Request bodies larger than this spill to a temporary file instead of RAM.
SPOOL_MAX_SIZE = 512 * 1024

#: Returned by :func:`_next` when an iterator is exhausted. ``None`` cannot be
#: used: a WSGI app is allowed to yield empty chunks.
_EXHAUSTED = object()


class _StartResponse:
    """Captures the WSGI ``start_response`` call.

    A WSGI application may call ``start_response`` at any point up to yielding
    its first non-empty chunk, so the status is not necessarily known when the
    application returns. :meth:`resolved` reports whether it is.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.exc_info: Any = None
        #: Chunks written through the legacy ``write()`` callable, which
        #: predates returning an iterable and which some old code still uses.
        self.written: list[bytes] = []

    def __call__(
        self,
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: Any = None,
    ) -> Callable[[bytes], None]:
        """The WSGI ``start_response`` callable.

        :param status: Status line, e.g. ``"200 OK"``.
        :param response_headers: Header name/value pairs.
        :param exc_info: Exception info when re-starting a failed response.
        :returns: The legacy ``write`` callable.
        """
        self.exc_info = exc_info
        # A second call with exc_info replaces the first, which is exactly what
        # PEP 3333 asks for when the app hits an error before sending anything.
        if self.status is None or exc_info is not None:
            self.status = int(status.split(" ", 1)[0])
            self.headers = [
                (
                    name.strip().lower().encode("latin-1"),
                    value.strip().encode("latin-1"),
                )
                for name, value in response_headers
            ]
        return self.written.append

    @property
    def resolved(self) -> bool:
        """:returns: ``True`` once the status line is known."""
        return self.status is not None


def build_environ(scope: Scope, body: Any) -> dict[str, Any]:
    """Build a PEP 3333 environ from an ASGI HTTP scope.

    :param scope: The ASGI connection scope.
    :param body: A file-like object positioned at the start of the request body.
    :returns: The WSGI environ.
    """
    script_name = scope.get("root_path", "").encode("utf8").decode("latin-1")
    path_info = scope["path"].encode("utf8").decode("latin-1")
    if path_info.startswith(script_name):
        path_info = path_info[len(script_name) :]

    environ: dict[str, Any] = {
        # Kept so downstream code can reach protocol details WSGI cannot express;
        # a2wsgi exposes the same key and some middleware already looks for it.
        "asgi.scope": scope,
        "REQUEST_METHOD": scope["method"],
        "SCRIPT_NAME": script_name,
        "PATH_INFO": path_info,
        "QUERY_STRING": scope["query_string"].decode("ascii"),
        "SERVER_PROTOCOL": f"HTTP/{scope['http_version']}",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": body,
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        # Single process: the ASGI server owns the socket, not a pre-fork pool.
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }

    server_addr, server_port = scope.get("server") or ("localhost", 80)
    environ["SERVER_NAME"] = server_addr
    environ["SERVER_PORT"] = str(server_port or 0)

    client = scope.get("client")
    if client is not None:
        environ["REMOTE_ADDR"] = client[0]
        environ["REMOTE_PORT"] = str(client[1])

    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1")
        if name == "content-length":
            key = "CONTENT_LENGTH"
        elif name == "content-type":
            key = "CONTENT_TYPE"
        else:
            key = f"HTTP_{name}".upper().replace("-", "_")
        value = raw_value.decode("latin-1")
        if key in environ:
            value = f"{environ[key]},{value}"
        environ[key] = value

    return environ


def _next(iterator: Iterator[bytes]) -> Any:
    """Advance an iterator, translating exhaustion into a sentinel.

    ``StopIteration`` cannot cross a thread boundary usefully — it is swallowed
    by the future machinery — so it is converted to :data:`_EXHAUSTED`.

    :param iterator: The body iterator.
    :returns: The next chunk, or :data:`_EXHAUSTED`.
    """
    try:
        return next(iterator)
    except StopIteration:
        return _EXHAUSTED


class WSGIMiddleware:
    """Mount a WSGI application inside an ASGI application.

    :ivar app_limiter: Bounds concurrent WSGI calls. This is the number that
        must be coordinated with the ZODB ``pool-size``: every in-flight call
        may hold a database connection.
    :ivar io_limiter: Bounds concurrent *body* chunk reads. Deliberately separate
        and larger: pulling bytes off a blob file touches no ZODB connection, so
        letting downloads contend for the same slots as page renders would
        reintroduce the starvation this adapter exists to remove.
    """

    def __init__(
        self,
        app: Callable[..., Iterable[bytes]],
        *,
        workers: int = 4,
        io_workers: int | None = None,
        chunk_size: int = CHUNK_SIZE,
        spool_max_size: int = SPOOL_MAX_SIZE,
    ) -> None:
        """
        :param app: The WSGI application to mount.
        :param workers: Maximum concurrent WSGI calls.
        :param io_workers: Maximum concurrent body reads; defaults to
            ``max(4 * workers, 16)``.
        :param chunk_size: Bytes to pull per thread hop from a streaming body.
        :param spool_max_size: Request body size above which to spill to disk.
        """
        self.app = app
        self.chunk_size = chunk_size
        self.spool_max_size = spool_max_size
        self.app_limiter = anyio.CapacityLimiter(workers)
        self.io_limiter = anyio.CapacityLimiter(io_workers or max(4 * workers, 16))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Dispatch one ASGI connection.

        :param scope: The ASGI connection scope.
        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        if scope["type"] == "http":
            await self._handle_http(scope, receive, send)
        elif scope["type"] == "websocket":
            # A WSGI app cannot speak websocket. Refuse rather than hang: a
            # native route should have matched before this mount.
            await send({"type": "websocket.close", "code": 1000})
        elif scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        """Acknowledge lifespan events on behalf of the WSGI app, which has none.

        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _read_body(self, receive: Receive) -> Any:
        """Drain the request body into a seekable file-like object.

        The whole body is read before the WSGI app is called. a2wsgi instead
        streams it lazily, at the cost of a blocking round trip to the event loop
        every time the app reads — the very thing this adapter removes. The
        trade is that a large upload is buffered; it spills to disk past
        ``spool_max_size`` so that memory stays bounded.

        :param receive: The ASGI receive callable.
        :returns: A file-like object positioned at the start of the body.
        """
        # Not a context manager: the file outlives this call as the environ's
        # ``wsgi.input`` and is closed by _handle_http once the app has returned.
        body = SpooledTemporaryFile(max_size=self.spool_max_size)  # noqa: SIM115
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            body.write(message.get("body", b""))
            more_body = message.get("more_body", False)
        body.seek(0)
        return body

    def _call_app(
        self, environ: dict[str, Any], start_response: _StartResponse
    ) -> Iterable[bytes]:
        """Invoke the WSGI application. Runs in a worker thread.

        Deliberately does not consume the returned iterable: doing so here is
        what pins a thread for the length of the client's transfer.

        :param environ: The WSGI environ.
        :param start_response: The ``start_response`` capture object.
        :returns: The application's body iterable.
        """
        return self.app(environ, start_response)

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one HTTP request.

        :param scope: The ASGI connection scope.
        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        body_file = await self._read_body(receive)
        environ = build_environ(scope, body_file)
        start_response = _StartResponse()

        try:
            result = await anyio.to_thread.run_sync(
                self._call_app, environ, start_response, limiter=self.app_limiter
            )
        except Exception:
            logger.exception("WSGI application raised")
            if not start_response.resolved:
                await _send_error(send)
            return
        finally:
            body_file.close()

        try:
            await self._send_response(result, start_response, send)
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                await anyio.to_thread.run_sync(close, limiter=self.io_limiter)

    async def _send_response(
        self, result: Iterable[bytes], start_response: _StartResponse, send: Send
    ) -> None:
        """Stream the application's response to the client.

        :param result: The application's body iterable.
        :param start_response: The ``start_response`` capture object.
        :param send: The ASGI send callable.
        """
        body = _prelude(start_response, result, self.chunk_size)

        # A response already in memory — the common case, since ZPublisher
        # materializes its body — needs no streaming machinery at all.
        if isinstance(body, bytes):
            if not start_response.resolved:
                logger.error("WSGI application never called start_response")
                await _send_error(send)
                return
            await _send_start(send, start_response)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        pending: Any = _EXHAUSTED
        if not start_response.resolved:
            # PEP 3333 lets an app defer start_response until it yields its first
            # non-empty chunk, so pull one to force the call before any header
            # can be sent. It is held back and sent after the start event.
            pending = await anyio.to_thread.run_sync(
                _next, body, limiter=self.io_limiter
            )
            if not start_response.resolved:
                logger.error("WSGI application never called start_response")
                await _send_error(send)
                return

        await _send_start(send, start_response)

        for written in start_response.written:
            await send({
                "type": "http.response.body",
                "body": written,
                "more_body": True,
            })

        chunk = pending
        if chunk is _EXHAUSTED:
            chunk = await anyio.to_thread.run_sync(_next, body, limiter=self.io_limiter)
        while chunk is not _EXHAUSTED:
            if chunk:
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                })
            # One short thread hop per chunk. The thread is released while the
            # client is being waited on, which is the entire point.
            chunk = await anyio.to_thread.run_sync(_next, body, limiter=self.io_limiter)

        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _prelude(
    start_response: _StartResponse, result: Iterable[bytes], chunk_size: int
) -> bytes | Iterator[bytes]:
    """Decide how to consume the application's body iterable.

    Three shapes, in the order they occur in practice:

    * a materialized sequence of chunks — ZPublisher's ordinary case — is joined
      and sent in one go;
    * a stream iterator with a public ``streamsize`` (``filestream_iterator``,
      ``plone.namedfile``'s ``filestream_range_iterator``) has that attribute
      raised to ``chunk_size`` and is then iterated normally; the iterator keeps
      its own file handle open, so a concurrent ZODB pack or blob-cache eviction
      cannot pull the file out from under an in-flight download;
    * a raw file object — what ZPublisher returns when the body is an
      ``IOBase`` and no ``wsgi.file_wrapper`` is offered — is read in chunks.
      It must not be *iterated*: iterating a binary file yields lines.

    :param start_response: The ``start_response`` capture object.
    :param result: The application's body iterable.
    :param chunk_size: Bytes to pull per thread hop.
    :returns: Either the whole body as bytes, or an iterator of chunks.
    """
    # Which branch a response takes is the difference between holding a thread
    # for the client's transfer and not, so make it observable: enable DEBUG on
    # this logger to see whether a given URL is actually being offloaded.
    logger.debug("body type=%s", type(result).__name__)

    if isinstance(result, list | tuple) and not start_response.written:
        return b"".join(result)

    if hasattr(result, "streamsize"):
        # Public on both stream iterator classes, so the chunk size can be tuned
        # without reaching into the file object they wrap. Checked before the
        # IOBase case because ``filestream_iterator`` is both.
        result.streamsize = chunk_size  # type: ignore[attr-defined]
    elif isinstance(result, io.IOBase):
        # Matches ZPublisher's own ``_FILE_TYPES`` check. Testing for ``read``
        # would not do: every IOBase also has ``__next__``, and iterating a
        # binary file yields *lines*, which for a blob means unbounded chunks
        # split on stray newlines.
        return iter(lambda: result.read(chunk_size), b"")

    return iter(result)


async def _send_start(send: Send, start_response: _StartResponse) -> None:
    """Send the response start event.

    :param send: The ASGI send callable.
    :param start_response: The resolved ``start_response`` capture object.
    """
    await send({
        "type": "http.response.start",
        "status": start_response.status,
        "headers": start_response.headers,
    })


async def _send_error(send: Send) -> None:
    """Send a bare 500, for failures before any header reached the client.

    :param send: The ASGI send callable.
    """
    await send({
        "type": "http.response.start",
        "status": 500,
        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
    })
    await send({
        "type": "http.response.body",
        "body": b"Internal Server Error",
        "more_body": False,
    })

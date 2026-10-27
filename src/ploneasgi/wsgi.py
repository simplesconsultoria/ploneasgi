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

#: Largest request body accepted, in bytes; larger ones get a 413. waitress's
#: ``max_request_body_size`` default, so moving off waitress changes nothing.
MAX_BODY_SIZE = 1024 * 1024 * 1024

#: Set to ``True`` on the ASGI scope when the client went away mid-response,
#: so the access log can say so instead of reporting a truncated response.
DISCONNECTED_KEY = "ploneasgi.disconnected"

#: Returned by :func:`_next` when an iterator is exhausted. ``None`` cannot be
#: used: a WSGI app is allowed to yield empty chunks.
_EXHAUSTED = object()


class BodyTooLarge(Exception):
    """The request body is larger than the adapter accepts."""


class ClientGone(Exception):
    """The client disconnected before its request body arrived."""


async def read_body(
    scope: Scope,
    receive: Receive,
    *,
    max_body_size: int | None = MAX_BODY_SIZE,
    spool_max_size: int = SPOOL_MAX_SIZE,
) -> Any:
    """Drain a request body into a seekable file-like object.

    The whole body is read before anything runs in a worker thread. a2wsgi
    instead streams it lazily, at the cost of a blocking round trip to the
    event loop every time the app reads — the very thing this adapter removes.
    The trade is that a large upload is buffered; it spills to disk past
    ``spool_max_size`` so that memory stays bounded, and is refused past
    ``max_body_size`` so that the disk does too.

    Shared with the native HTTP routes, which hand the body to a publisher
    request in a worker thread and so cannot ``await`` it there either.

    :param scope: The ASGI connection scope, for ``Content-Length``.
    :param receive: The ASGI receive callable.
    :param max_body_size: Largest body accepted; ``None`` for no limit.
    :param spool_max_size: Size above which the body spills to disk.
    :returns: A file-like object positioned at the start of the body. The
        caller closes it.
    :raises BodyTooLarge: When the body exceeds ``max_body_size`` — declared,
        or actually received, since chunked uploads declare nothing.
    :raises ClientGone: When the client disconnects mid-body. The app is never
        called with a truncated body: an upload cut short would otherwise be
        processed as if it were complete.
    """
    limit = max_body_size
    if limit is not None and _declared_length(scope) > limit:
        raise BodyTooLarge

    # Not a context manager: the file outlives this call, and the caller closes
    # it once whatever reads it has returned.
    body = SpooledTemporaryFile(max_size=spool_max_size)  # noqa: SIM115
    received = 0
    more_body = True
    try:
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise ClientGone
            chunk = message.get("body", b"")
            received += len(chunk)
            if limit is not None and received > limit:
                raise BodyTooLarge
            body.write(chunk)
            more_body = message.get("more_body", False)
    except BaseException:
        body.close()
        raise
    body.seek(0)
    return body


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
        #: Whether the status line has gone to the client. Set by the adapter;
        #: after this, the status can no longer be replaced.
        self.headers_sent = False

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
        :raises BaseException: The exception in ``exc_info``, when the headers
            have already been sent and the response cannot be replaced.
        """
        if exc_info is not None and self.headers_sent:
            # PEP 3333: "if exc_info is provided, and the HTTP headers have
            # already been sent, start_response must raise an error, and should
            # re-raise using the exc_info tuple." Swallowing it would let the
            # app believe it had replaced a response the client already has.
            raise exc_info[1].with_traceback(exc_info[2])
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
        max_body_size: int | None = MAX_BODY_SIZE,
    ) -> None:
        """
        :param app: The WSGI application to mount.
        :param workers: Maximum concurrent WSGI calls.
        :param io_workers: Maximum concurrent body reads; defaults to
            ``max(4 * workers, 16)``.
        :param chunk_size: Bytes to pull per thread hop from a streaming body.
        :param spool_max_size: Request body size above which to spill to disk.
        :param max_body_size: Largest request body accepted; ``None`` for no
            limit. Larger bodies are refused with a 413 before the app runs.
        """
        self.app = app
        self.chunk_size = chunk_size
        self.spool_max_size = spool_max_size
        self.max_body_size = max_body_size
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
        try:
            # The file outlives the read as the environ's ``wsgi.input`` and is
            # closed below, once the app has returned.
            body_file = await read_body(
                scope,
                receive,
                max_body_size=self.max_body_size,
                spool_max_size=self.spool_max_size,
            )
        except BodyTooLarge:
            await _send_error(send, 413, b"Request Entity Too Large", close=True)
            return
        except ClientGone:
            logger.debug("client disconnected before its request body arrived")
            return

        environ = build_environ(scope, body_file)
        start_response = _StartResponse()

        try:
            result = await anyio.to_thread.run_sync(
                self._call_app, environ, start_response, limiter=self.app_limiter
            )
        except Exception:
            # Nothing has reached the client yet — the start event is sent only
            # after the app returns — so a 500 is always possible here, whether
            # or not the app had called start_response before raising.
            logger.exception("WSGI application raised")
            await _send_error(send)
            return
        finally:
            body_file.close()

        try:
            # The body streams while a watcher waits on `receive`: once the
            # request body is drained, the next message a server delivers is
            # http.disconnect. Without the watcher an abandoned download is read
            # to EOF, a thread hop per chunk, for a client that is gone.
            error: Exception | None = None
            async with anyio.create_task_group() as group:
                watched = group.cancel_scope
                group.start_soon(_cancel_on_disconnect, scope, receive, watched)
                try:
                    await self._send_response(result, start_response, send)
                except Exception as exc:
                    # Held and re-raised below, so it leaves with its own type
                    # rather than wrapped in the task group's ExceptionGroup.
                    error = exc
                group.cancel_scope.cancel()
            if error is not None:
                # Mid-stream: the status is already out, so the only honest
                # signal left is to abort the connection, which the server does
                # when the exception reaches it. A clean final chunk would make
                # a truncated body look complete.
                raise error
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
            try:
                pending = await anyio.to_thread.run_sync(
                    _next, body, limiter=self.io_limiter
                )
            except Exception:
                # Still before the start event, so a 500 is still possible.
                logger.exception("WSGI application raised producing its first chunk")
                await _send_error(send)
                return
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


def _declared_length(scope: Scope) -> int:
    """Read the request's ``Content-Length``.

    :param scope: The ASGI connection scope.
    :returns: The declared length, or ``0`` when absent or malformed — in
        which case the received byte count is what the limit is checked
        against.
    """
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


async def _cancel_on_disconnect(
    scope: Scope, receive: Receive, cancel_scope: anyio.CancelScope
) -> None:
    """Cancel the response being streamed once the client disconnects.

    Started only after the request body has been drained, so ``receive`` has
    nothing left to deliver but the disconnect. Waiting on it holds no thread.

    :param scope: The ASGI connection scope; marked with
        :data:`DISCONNECTED_KEY` for the access log.
    :param receive: The ASGI receive callable.
    :param cancel_scope: The scope the response is streamed in.
    """
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            scope[DISCONNECTED_KEY] = True
            logger.debug("client disconnected; stopped streaming %s", scope["path"])
            cancel_scope.cancel()
            return


async def _send_start(send: Send, start_response: _StartResponse) -> None:
    """Send the response start event.

    :param send: The ASGI send callable.
    :param start_response: The resolved ``start_response`` capture object.
    """
    start_response.headers_sent = True
    await send({
        "type": "http.response.start",
        "status": start_response.status,
        "headers": start_response.headers,
    })


async def _send_error(
    send: Send,
    status: int = 500,
    body: bytes = b"Internal Server Error",
    *,
    close: bool = False,
) -> None:
    """Send a bare error, for failures before any header reached the client.

    :param send: The ASGI send callable.
    :param status: The status code.
    :param body: The plain-text body.
    :param close: Ask for the connection to be closed — after a refused
        upload, whose unread remainder would otherwise be parsed as the next
        request.
    """
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    if close:
        headers.append((b"connection", b"close"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})

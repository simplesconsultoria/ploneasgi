"""An access log that sits at the ASGI layer, above every route.

Replaces ``paste.translogger``. There are three reasons the WSGI layer is the
wrong place for this, and all three are structural rather than cosmetic:

* **It cannot see most of the application.** ``TransLogger`` wraps the WSGI app,
  which this package mounts *below* the router. Websocket routes and anything
  else passed to ``create_app(routes=...)`` never reach it, so they are silently
  absent from the log.
* **It logs before the body exists.** ``TransLogger`` emits its line from inside
  ``replacement_start_response`` — when headers are ready. Under this package the
  body is streamed on the event loop *after* the WSGI call returns, so the entire
  phase :mod:`ploneasgi.wsgi` exists to optimize happens after the log line is
  written. No amount of instrumenting the WSGI layer fixes that; it is on the
  wrong side of the seam.
* **Its byte count is the ``Content-Length`` header, not bytes written.** The
  source says so outright: *"Ideally we would count the bytes going by […] at
  least for now we'll be lazy."* So it is wrong whenever the header and the body
  disagree — range responses, chunked replies, and any body the app truncates.

Wrapping ``send`` instead counts the bytes the application actually emitted, and
splits the timing in two:

``ttfb``
    Time to the ``http.response.start`` event — request parsing, the worker-thread
    hop, and Zope's render. This is what a page-speed number means.
``duration``
    Time to the final body event — ``ttfb`` plus the transfer.

Keeping them apart is the point for this package. Worker-thread starvation shows
up as ``ttfb`` climbing while ``duration`` is dominated by slow clients; a single
merged number cannot distinguish "Plone is slow" from "someone is downloading a
film over a bad connection".

.. warning::
   **``bytes`` is what the application emitted, not what the client received.**
   uvicorn's ``send`` begins ``if self.disconnected: return`` — after a client
   goes away it discards every further chunk without telling the application. So
   an aborted 50 MB download is still logged as 50 MB, and ``duration`` measures
   how long the server spent producing bytes nobody was reading. Measured
   2026-08-03: a rate-limited ``curl`` killed after one second produced a normal,
   complete-looking 200 for the full size.

   Detecting that would mean polling ``receive`` for ``http.disconnect``
   concurrently with the response, which this middleware deliberately does not
   do — the adapter owns ``receive``. ``incomplete=client-gone`` therefore fires
   only when the *application* stops emitting early, which is a real case
   (an exception mid-stream) but not the client-abort case.

.. note::
   uvicorn has its own access log, on by default. :func:`ploneasgi.app.serve`
   turns it off unconditionally, so this is the only access log and
   ``PLONE_ACCESS_LOG`` decides whether there is one at all.

.. note::
   Nothing here imports Zope. It is general ASGI middleware and is tested as one.
"""

from collections.abc import Callable
from collections.abc import MutableMapping
from datetime import datetime
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from typing import Any

import logging
import time


logger = logging.getLogger("ploneasgi.access")

#: Apache combined, plus the two timings and a completeness note. ``bytes`` is
#: bytes the application emitted — see the module warning about what that does
#: and does not tell you.
DEFAULT_FORMAT = (
    '%(client)s - %(user)s [%(time)s] "%(method)s %(path)s %(protocol)s" '
    '%(status)s %(bytes)s %(duration)s %(ttfb)s "%(referer)s" "%(user_agent)s"'
    "%(note)s"
)

#: Key a downstream WSGI middleware may set on the ASGI scope to name the
#: authenticated user. The adapter exposes the scope to WSGI as ``asgi.scope``,
#: so Zope-side code can reach it::
#:
#:     environ["asgi.scope"][USER_KEY] = environ.get("REMOTE_USER")
#:
#: Without it the user column is ``-``: authentication happens inside Zope, and
#: this middleware runs above it.
USER_KEY = "ploneasgi.user"


#: Characters a log field must not carry raw: the quote and backslash that
#: delimit fields, and every C0 control character plus DEL.
_ESCAPES = {ord('"'): '\\"', ord("\\"): "\\\\"} | {
    code: f"\\x{code:02x}" for code in (*range(0x20), 0x7F)
}


def _escape(value: str) -> str:
    """Escape a client-controlled value for one log field, as Apache does.

    The path is the one that matters most: uvicorn unquotes it, so ``%0A`` in
    a request line arrives here as a real newline and would end the line early,
    letting the client append a forged entry of its choosing. A stray ``"``
    does the same job inside a quoted field.

    :param value: The raw field value.
    :returns: The value with quotes, backslashes and control characters escaped.
    """
    return value.translate(_ESCAPES)


def _header(scope: Scope, name: bytes) -> str | None:
    """Read a request header out of an ASGI scope.

    :param scope: The ASGI connection scope.
    :param name: Lowercase header name.
    :returns: The decoded value, or ``None`` when absent.
    """
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == name:
            return raw_value.decode("latin-1")
    return None


def _timestamp() -> str:
    """:returns: The current local time in Apache's log format."""
    return datetime.now().astimezone().strftime("%d/%b/%Y:%H:%M:%S %z")


class AccessLogMiddleware:
    """Log every HTTP request and websocket session in one consistent format.

    Written as raw ASGI middleware rather than a Starlette
    ``BaseHTTPMiddleware``: the latter pumps the response through memory streams,
    which would buffer the very responses :mod:`ploneasgi.wsgi` streams. Logging
    the blob path must not destroy it.

    :ivar trust_forwarded: When true, prefer ``X-Forwarded-For`` over the socket
        address. Off by default — the header is client-supplied and trivially
        spoofed, so it is only meaningful behind a proxy that overwrites it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        fmt: str = DEFAULT_FORMAT,
        trust_forwarded: bool = False,
        level: int = logging.INFO,
        log: logging.Logger | None = None,
    ) -> None:
        """
        :param app: The ASGI application to wrap.
        :param fmt: Percent-style format string; see :data:`DEFAULT_FORMAT` for
            the available field names.
        :param trust_forwarded: Read the client address from ``X-Forwarded-For``.
        :param level: Level to log at.
        :param log: Logger to use; defaults to ``ploneasgi.access``.
        """
        self.app = app
        self.fmt = fmt
        self.trust_forwarded = trust_forwarded
        self.level = level
        self.logger = log or logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Dispatch one ASGI connection.

        :param scope: The ASGI connection scope.
        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            # Lifespan: nothing to log, and swallowing it would hang startup.
            await self.app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve and log one HTTP request.

        :param scope: The ASGI connection scope.
        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        start = time.perf_counter()
        status: int | None = None
        sent = 0
        ttfb: float | None = None
        complete = False

        async def logging_send(message: Message) -> None:
            nonlocal status, sent, ttfb, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                ttfb = time.perf_counter() - start
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
                # Completion of the *application's* output. uvicorn discards
                # chunks after a client disconnect without raising, so this says
                # nothing about whether the bytes were received.
                if not message.get("more_body", False):
                    complete = True
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
        except Exception:
            # The server will turn this into a 500; record what the client got,
            # then let it propagate untouched.
            self._log_http(scope, status or 500, sent, start, ttfb, complete=False)
            raise
        self._log_http(scope, status, sent, start, ttfb, complete=complete)

    def _log_http(
        self,
        scope: Scope,
        status: int | None,
        sent: int,
        start: float,
        ttfb: float | None,
        *,
        complete: bool,
    ) -> None:
        """Emit one HTTP log line.

        :param scope: The ASGI connection scope.
        :param status: Response status, or ``None`` if never started.
        :param sent: Bytes actually written to the client.
        :param start: ``perf_counter`` value taken when the request arrived.
        :param ttfb: Seconds to the response start event, if it happened.
        :param complete: Whether the final body event was sent.
        """
        path = scope["path"]
        if scope.get("query_string"):
            path = f"{path}?{scope['query_string'].decode('latin-1')}"

        note = ""
        if status is None:
            note = " incomplete=no-response"
        elif not complete:
            # The application stopped emitting without a final body event —
            # typically an exception mid-stream. Not the client-abort case,
            # which is invisible here; see the module warning.
            note = " incomplete=truncated"

        self._emit({
            "client": self._client(scope),
            "user": scope.get(USER_KEY) or "-",
            "time": _timestamp(),
            "method": scope.get("method", "-"),
            "path": path,
            "protocol": f"HTTP/{scope.get('http_version', '1.1')}",
            "status": status if status is not None else "-",
            "bytes": sent,
            "duration": f"{time.perf_counter() - start:.3f}",
            "ttfb": f"{ttfb:.3f}" if ttfb is not None else "-",
            "referer": _header(scope, b"referer") or "-",
            "user_agent": _header(scope, b"user-agent") or "-",
            "note": note,
        })

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve and log one websocket session.

        One line per session, written on close — the only layer that can log a
        websocket and an HTTP request in the same format, which is why the log
        belongs here rather than in the WSGI app or in the server.

        :param scope: The ASGI connection scope.
        :param receive: The ASGI receive callable.
        :param send: The ASGI send callable.
        """
        start = time.perf_counter()
        accepted = False
        code: int | None = None
        received = 0
        emitted = 0

        async def logging_receive() -> Message:
            nonlocal received, code
            message = await receive()
            if message["type"] == "websocket.receive":
                received += 1
            elif message["type"] == "websocket.disconnect":
                code = message.get("code", 1005)
            return message

        async def logging_send(message: Message) -> None:
            nonlocal accepted, emitted, code
            if message["type"] == "websocket.accept":
                accepted = True
            elif message["type"] == "websocket.send":
                emitted += 1
            elif message["type"] == "websocket.close":
                code = message.get("code", 1000)
            await send(message)

        try:
            await self.app(scope, logging_receive, logging_send)
        finally:
            path = scope["path"]
            if scope.get("query_string"):
                path = f"{path}?{scope['query_string'].decode('latin-1')}"
            self._emit({
                "client": self._client(scope),
                "user": scope.get(USER_KEY) or "-",
                "time": _timestamp(),
                "method": "WEBSOCKET",
                "path": path,
                "protocol": f"HTTP/{scope.get('http_version', '1.1')}",
                "status": code if code is not None else ("-" if accepted else "403"),
                "bytes": 0,
                "duration": f"{time.perf_counter() - start:.3f}",
                "ttfb": "-",
                "referer": _header(scope, b"referer") or "-",
                "user_agent": _header(scope, b"user-agent") or "-",
                "note": f" messages={received}/{emitted}",
            })

    def _client(self, scope: Scope) -> str:
        """Determine the client address to log.

        :param scope: The ASGI connection scope.
        :returns: The client address, or ``-`` when unavailable.
        """
        if self.trust_forwarded:
            forwarded = _header(scope, b"x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "-"

    def _emit(self, fields: MutableMapping[str, Any]) -> None:
        """Format and write one log record.

        Every string field is escaped here rather than at each call site, so a
        field added later cannot forget to be. That includes ``client``: with
        ``trust_forwarded`` it comes from a request header. The fields this
        module produces itself contain nothing to escape.

        :param fields: The format-string fields.
        """
        escaped = {
            key: _escape(value) if isinstance(value, str) else value
            for key, value in fields.items()
        }
        self.logger.log(self.level, self.fmt, escaped)


#: Type of a middleware factory, for callers composing their own stack.
MiddlewareFactory = Callable[..., AccessLogMiddleware]

"""Tests for ploneasgi.

Two halves, two environments: the modules directly in this package run with
Plone absent, and ``plone/`` needs a real site. ``conftest.py`` skips the latter
when Plone is not installed; ``make test-plone`` asserts it is.

The ASGI scope builder lives here rather than in ``conftest.py``, which is for
fixtures only: the Zope-free tests drive the raw protocol, and each module
starts from the same scope.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import asyncio


def asgi_scope(path: str = "/", *, scope_type: str = "http", **overrides: Any) -> dict:
    """Build an ASGI connection scope with every key the code under test reads.

    Websocket scopes get the ``ws`` scheme and no ``method``, as a server sends
    them.

    :param path: Request path.
    :param scope_type: ``http`` or ``websocket``.
    :param overrides: Scope keys to set or replace.
    :returns: The scope mapping.
    """
    scope: dict[str, Any] = {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws" if scope_type == "websocket" else "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 54321),
    }
    if scope_type == "http":
        scope["method"] = "GET"
    scope.update(overrides)
    return scope


def receiver(*messages: dict) -> Callable[[], Awaitable[dict]]:
    """Build an ASGI ``receive`` that delivers ``messages``, then waits.

    Waiting is what a server does once the request body is drained: the next
    message is ``http.disconnect``, and it arrives only when the client goes.
    A fake that raises or returns early instead would read as a disconnect to
    anything watching for one.

    :param messages: The messages to deliver, in order.
    :returns: The ``receive`` callable.
    """
    inbox = list(messages)

    async def receive() -> dict:
        if inbox:
            return inbox.pop(0)
        await asyncio.Event().wait()  # never set: only cancellation ends it
        raise AssertionError("unreachable")

    return receive

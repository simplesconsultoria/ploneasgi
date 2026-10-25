"""Serve a trivial WSGI app under a chosen ASGI/WSGI adapter.

Isolates adapter cost from Plone's render time. A Plone page takes ~15 ms, which
buries a few hundred microseconds of translation overhead in noise; a
hello-world app makes the fixed per-request cost the whole signal.

Needs no ZODB, so it does not contend for the FileStorage lock and can run while
a real server is up — on a different port.

Two paths, because the interesting question needs both at once:

``/page``
    A cheap response. This is what gets probed.
``/download``
    A file-backed body delivered through a stream iterator, when
    ``--stream-bytes`` is given. This is what the slow clients hold open.

Serving only one of them cannot show starvation at all: the whole effect is a
*download* denying service to a *page*.

This only *serves*. Drive it with ``benchmarks/driver.py`` from another shell —
or let ``make bench-starve`` start and stop both ends for you. Address it as
``127.0.0.1``, never ``localhost``, and alternate the adapters rather than
running all of one and then all of the other.

Usage::

    python benchmarks/bench_adapter.py ploneasgi --port 8091
    python benchmarks/bench_adapter.py a2wsgi    --port 8091

    # add a 50 MB body on /download
    python benchmarks/bench_adapter.py ploneasgi --stream-bytes 52428800
"""

from ploneasgi.wsgi import WSGIMiddleware as PloneASGIMiddleware
from starlette.applications import Starlette
from starlette.routing import Mount

import argparse
import os
import tempfile
import uvicorn


BODY = b"hello world"

#: The only path that streams. Everything else is the cheap page.
DOWNLOAD_PATH = "/download"


def make_wsgi_app(stream_path: str | None, stream_size: int):
    """Build the WSGI application under test.

    :param stream_path: File to stream, or ``None`` to return a small body.
    :param stream_size: Size of that file in bytes.
    :returns: A WSGI application.
    """

    class StreamIterator:
        """A stand-in for ``filestream_iterator``, with the same public shape."""

        def __init__(self, path: str) -> None:
            """
            :param path: File to stream.
            """
            self._io = open(path, "rb")  # noqa: SIM115
            self.streamsize = 1 << 16

        def __iter__(self) -> StreamIterator:
            """:returns: Itself."""
            return self

        def __next__(self) -> bytes:
            """:returns: The next chunk.

            :raises StopIteration: At end of file.
            """
            data = self._io.read(self.streamsize)
            if not data:
                raise StopIteration
            return data

        def close(self) -> None:
            """Close the underlying file."""
            self._io.close()

    def app(environ, start_response):
        # Route on the path so one process serves both the thing being probed
        # and the thing holding threads. Anything that is not the download is a
        # cheap page, so a bare "/" keeps working for throughput runs.
        if environ.get("PATH_INFO", "/") != DOWNLOAD_PATH or stream_path is None:
            start_response(
                "200 OK",
                [("Content-Type", "text/plain"), ("Content-Length", str(len(BODY)))],
            )
            return [BODY]
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(stream_size)),
            ],
        )
        return StreamIterator(stream_path)

    return app


def main() -> None:
    """Parse arguments and serve."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("adapter", choices=["ploneasgi", "a2wsgi"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=256 * 1024)
    parser.add_argument(
        "--stream-bytes",
        type=int,
        default=0,
        help="serve a temp file of this size through a stream iterator",
    )
    args = parser.parse_args()

    stream_path = None
    if args.stream_bytes:
        handle, stream_path = tempfile.mkstemp(prefix="bench-adapter-")
        os.write(handle, os.urandom(args.stream_bytes))
        os.close(handle)

    wsgi_app = make_wsgi_app(stream_path, args.stream_bytes)

    if args.adapter == "a2wsgi":
        from a2wsgi import WSGIMiddleware as A2WSGIMiddleware

        mounted = A2WSGIMiddleware(wsgi_app, workers=args.workers)
    else:
        mounted = PloneASGIMiddleware(
            wsgi_app, workers=args.workers, chunk_size=args.chunk_size
        )

    print(f"serving {args.adapter} on {args.host}:{args.port}", flush=True)
    try:
        uvicorn.run(
            Starlette(routes=[Mount("/", mounted)]),
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    finally:
        if stream_path:
            os.unlink(stream_path)


if __name__ == "__main__":
    main()

"""Start a server, drive load against it, and always tear it down.

``bench_adapter.py`` serves and ``driver.py`` measures; running them by hand
means two shells, a port to pick, and a server that outlives the shell if
anything goes wrong. This is the piece that makes the harness a single command,
which is what lets the same measurement run from a Makefile target and from a
test without the two drifting apart.

The server always runs as a **subprocess**. Hosting it in-process would put
uvicorn's event loop in the same interpreter as the client driving it, so the
loop under measurement would also be the loop generating the load — the one
arrangement guaranteed to misreport exactly the thing this package is about.

Usage::

    python benchmarks/run.py starve     --adapter ploneasgi --slow-clients 16
    python benchmarks/run.py throughput --adapter a2wsgi --reps 3
    python benchmarks/run.py capacity   --adapter ploneasgi --slow-clients 8

Comparing adapters means comparing *processes*, so each invocation covers one
arm. ``make bench-compare`` alternates them.
"""

from contextlib import closing
from pathlib import Path

import argparse
import asyncio
import driver
import json
import socket
import subprocess
import sys
import time


HERE = Path(__file__).parent
SERVER = HERE / "bench_adapter.py"

#: Long enough for an import of uvicorn plus writing the stream file, short
#: enough that a server which will never come up fails the run rather than
#: hanging a CI job.
READY_TIMEOUT = 60.0


def free_port() -> int:
    """Ask the OS for a port that is free right now.

    Inherently racy — nothing stops another process taking it in between — but
    the alternative is a hard-coded port, which collides with a previous run
    that failed to tear down and produces a benchmark of the *wrong server*.
    That failure is silent; this one is not.

    :returns: A port number.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(port: int, process: subprocess.Popen, timeout: float) -> None:
    """Block until the server answers, or fail with what it printed.

    Polls with a real request rather than a bare TCP connect: uvicorn accepts
    connections slightly before the application is mounted, so a connect-only
    check can return while the first request would still 404.

    :param port: Port the server was told to bind.
    :param process: The server process, watched so a crash fails fast.
    :param timeout: Seconds to wait before giving up.
    :raises RuntimeError: If the server exits or never becomes ready.
    """
    target = driver.Target.parse(f"http://127.0.0.1:{port}/page")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"server exited with code {process.returncode} before becoming "
                f"ready:\n{_drain(process)}"
            )
        result = asyncio.run(driver.fetch(target, timeout=2.0))
        if result.status == 200:
            return
        time.sleep(0.1)
    raise RuntimeError(f"server was not ready after {timeout}s:\n{_drain(process)}")


def _drain(process: subprocess.Popen) -> str:
    """Read whatever the server printed, without blocking forever.

    :param process: The server process.
    :returns: Its combined output, or a note that there was none.
    """
    if process.stdout is None:
        return "(no captured output)"
    try:
        return process.stdout.read() or "(no output)"
    except Exception:  # pragma: no cover - best effort on a dying process
        return "(output unreadable)"


class BenchServer:
    """A ``bench_adapter`` process, guaranteed to be cleaned up.

    :ivar adapter: ``ploneasgi`` or ``a2wsgi``.
    :ivar port: The bound port.
    """

    def __init__(
        self,
        adapter: str,
        *,
        workers: int = 4,
        chunk_size: int = 256 * 1024,
        stream_bytes: int = 50 * 1024 * 1024,
        port: int | None = None,
    ) -> None:
        """
        :param adapter: Which adapter to mount.
        :param workers: Concurrent WSGI calls allowed.
        :param chunk_size: Bytes pulled per thread hop when streaming.
        :param stream_bytes: Size of the body served on ``/download``.
        :param port: Port to bind; one is chosen if omitted.
        """
        self.adapter = adapter
        self.workers = workers
        self.chunk_size = chunk_size
        self.stream_bytes = stream_bytes
        self.port = port or free_port()
        self._process: subprocess.Popen | None = None

    @property
    def page_url(self) -> str:
        """:returns: URL of the cheap page that gets probed."""
        return f"http://127.0.0.1:{self.port}/page"

    @property
    def download_url(self) -> str:
        """:returns: URL of the streamed body the slow clients hold open."""
        return f"http://127.0.0.1:{self.port}/download"

    def __enter__(self) -> BenchServer:
        """Start the server and wait for it to answer.

        :returns: Itself.
        """
        self._process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(SERVER),
                self.adapter,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--workers",
                str(self.workers),
                "--chunk-size",
                str(self.chunk_size),
                "--stream-bytes",
                str(self.stream_bytes),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_until_ready(self.port, self._process, READY_TIMEOUT)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Terminate the server, escalating to SIGKILL if it does not go."""
        process = self._process
        if process is None:
            return
        self._process = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # A server still holding slow-client sockets can ignore SIGTERM
                # long enough to outlive the run and squat the port.
                process.kill()
                process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Add the options every mode shares.

    :param parser: The subparser to extend.
    """
    parser.add_argument("--adapter", choices=["ploneasgi", "a2wsgi"], required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=256 * 1024)
    parser.add_argument("--stream-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--json", dest="as_json", action="store_true")


def main() -> int:
    """Parse arguments, run one mode against one adapter.

    :returns: Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    st = sub.add_parser("starve", help="page availability under slow downloads")
    st.add_argument("--slow-clients", type=int, default=16)
    st.add_argument("--throttle-bytes", type=int, default=16 * 1024)
    st.add_argument("--throttle-delay", type=float, default=0.5)
    st.add_argument("--probes", type=int, default=12)
    st.add_argument("--probe-interval", type=float, default=0.25)
    st.add_argument("--probe-timeout", type=float, default=30.0)

    tp = sub.add_parser("throughput", help="full-speed download rate")
    tp.add_argument("--concurrency", type=int, default=4)
    tp.add_argument("--reps", type=int, default=3)

    cap = sub.add_parser("capacity", help="sustained page req/s under downloads")
    cap.add_argument("--slow-clients", type=int, default=8)
    cap.add_argument("--throttle-bytes", type=int, default=16 * 1024)
    cap.add_argument("--throttle-delay", type=float, default=0.5)
    cap.add_argument("--concurrency", type=int, default=4)
    cap.add_argument("--duration", type=float, default=10.0)
    cap.add_argument("--timeout", type=float, default=30.0)

    for subparser in (st, tp, cap):
        _add_common(subparser)

    args = parser.parse_args()

    with BenchServer(
        args.adapter,
        workers=args.workers,
        chunk_size=args.chunk_size,
        stream_bytes=args.stream_bytes,
        port=args.port,
    ) as server:
        if args.mode == "starve":
            rows = asyncio.run(
                driver.run_starve(
                    driver.Target.parse(server.download_url),
                    driver.Target.parse(server.page_url),
                    slow_clients=args.slow_clients,
                    throttle_bytes=args.throttle_bytes,
                    throttle_delay=args.throttle_delay,
                    probes=args.probes,
                    probe_interval=args.probe_interval,
                    probe_timeout=args.probe_timeout,
                )
            )
        elif args.mode == "throughput":
            rows = asyncio.run(
                driver.run_throughput(
                    {args.adapter: driver.Target.parse(server.download_url)},
                    args.concurrency,
                    args.reps,
                )
            )
        else:
            rows = asyncio.run(
                driver.run_capacity(
                    driver.Target.parse(server.page_url),
                    driver.Target.parse(server.download_url),
                    slow_clients=args.slow_clients,
                    throttle_bytes=args.throttle_bytes,
                    throttle_delay=args.throttle_delay,
                    concurrency=args.concurrency,
                    duration=args.duration,
                    timeout=args.timeout,
                )
            )

    for row in rows:
        row["adapter"] = args.adapter
        row["workers"] = args.workers

    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"== {args.adapter}, workers={args.workers} ==")
        driver._print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

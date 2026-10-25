"""Drive load against a running server and report what happened.

The measuring half of the harness. ``bench_adapter.py`` (or a real Plone) only
serves; this is what asks the questions. Three modes, and only the second one
decides anything architectural.

``throughput``
    How fast is a full-speed download? Takes one or more named ``--arm`` URLs
    and runs them *interleaved* and repeated, so machine drift cancels instead
    of aliasing onto the comparison. Comparing adapters means comparing
    *processes*, so the usual invocation is a single arm run once per server
    configuration — see ``make bench-compare``.

``starve``
    With N deliberately slow clients downloading, does the server still answer
    ordinary page requests? This is the package's central claim, and it is
    *categorical*: 12/12 versus 0/12 needs no statistics and survives being
    measured on a laptop, which no MiB/s number does.

``capacity``
    Sustained page throughput with and without downloads in flight. Closed-loop,
    so it reports what the server can take rather than latency at a rate chosen
    in advance.

Stdlib only (raw ``asyncio`` streams), because throttling a client precisely is
easier at the socket than through an HTTP library, and because the ASGI stack
under test should not share an event loop's worth of dependencies with its own
benchmark.

Usage::

    python benchmarks/driver.py starve \\
        --download-url http://127.0.0.1:8091/download \\
        --page-url     http://127.0.0.1:8091/page

    python benchmarks/driver.py throughput \\
        --arm ploneasgi=http://127.0.0.1:8091/download

.. note::
   Always address the server as ``127.0.0.1``, never ``localhost``: uvicorn bound
   to ``0.0.0.0`` is IPv4-only, while waitress on ``localhost`` also binds IPv6.
   Resolving to ``::1`` fails against one arm and not the other — and ``curl``
   hides it by falling back where ``ab`` does not.
"""

from dataclasses import dataclass
from dataclasses import field
from urllib.parse import urlsplit

import argparse
import asyncio
import contextlib
import json
import statistics
import time


@dataclass(frozen=True, slots=True)
class Target:
    """A parsed URL, ready to drive a raw connection."""

    host: str
    port: int
    path: str
    host_header: str

    @classmethod
    def parse(cls, url: str) -> Target:
        """Split a URL into connection parameters.

        :param url: Absolute http URL.
        :returns: A populated :class:`Target`.
        :raises ValueError: If the URL is not http.
        """
        parts = urlsplit(url)
        if parts.scheme != "http":
            raise ValueError(f"only http is supported, got {parts.scheme!r}")
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return cls(
            host=parts.hostname or "127.0.0.1",
            port=port,
            path=path,
            host_header=parts.netloc,
        )


@dataclass(slots=True)
class Result:
    """Outcome of a single request."""

    status: int
    body_bytes: int
    ttfb: float
    elapsed: float
    error: str | None = None


@dataclass(slots=True)
class Sample:
    """Aggregated results for one arm in one repetition."""

    arm: str
    results: list[Result] = field(default_factory=list)

    @property
    def ok(self) -> list[Result]:
        """:returns: Results that completed with a 2xx status."""
        return [r for r in self.results if r.error is None and 200 <= r.status < 300]

    def latencies(self) -> list[float]:
        """:returns: Wall-clock durations of the successful requests, in seconds."""
        return sorted(r.elapsed for r in self.ok)


async def fetch(
    target: Target,
    *,
    throttle_bytes: int = 0,
    throttle_delay: float = 0.0,
    timeout: float = 300.0,
) -> Result:
    """Perform one HTTP GET over a raw connection.

    ``Connection: close`` is used and the body read to EOF, so no chunked or
    keep-alive framing has to be reimplemented here. The extra connection setup
    is paid identically by every arm.

    :param target: Where to connect and what to request.
    :param throttle_bytes: Sleep after each this many bytes; 0 reads at full speed.
    :param throttle_delay: How long to sleep, in seconds.
    :param timeout: Overall deadline for the request.
    :returns: A :class:`Result`.
    """
    started = time.perf_counter()
    ttfb = 0.0
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(target.host, target.port)
            try:
                writer.write(
                    f"GET {target.path} HTTP/1.1\r\n"
                    f"Host: {target.host_header}\r\n"
                    f"User-Agent: ploneasgi-bench\r\n"
                    f"Accept-Encoding: identity\r\n"
                    f"Connection: close\r\n\r\n".encode("latin-1")
                )
                await writer.drain()

                head = await reader.readuntil(b"\r\n\r\n")
                ttfb = time.perf_counter() - started
                status = int(head.split(b" ", 2)[1])

                body_bytes = 0
                since_pause = 0
                read_size = throttle_bytes or 65536
                while True:
                    chunk = await reader.read(read_size)
                    if not chunk:
                        break
                    body_bytes += len(chunk)
                    if throttle_bytes:
                        since_pause += len(chunk)
                        if since_pause >= throttle_bytes:
                            since_pause = 0
                            await asyncio.sleep(throttle_delay)
            finally:
                writer.close()
                # The server may already be gone; closing is best effort.
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
    except Exception as exc:
        return Result(
            status=0,
            body_bytes=0,
            ttfb=ttfb,
            elapsed=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    return Result(
        status=status,
        body_bytes=body_bytes,
        ttfb=ttfb,
        elapsed=time.perf_counter() - started,
    )


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile.

    :param values: Sorted values.
    :param q: Quantile in ``[0, 1]``.
    :returns: The percentile, or ``0.0`` for an empty input.
    """
    if not values:
        return 0.0
    index = min(len(values) - 1, round(q * (len(values) - 1)))
    return values[index]


async def warm_up(target: Target) -> None:
    """Issue one discarded request so the first measurement is not a cold cache.

    :param target: The endpoint to warm.
    """
    await fetch(target)


async def run_throughput(
    arms: dict[str, Target], concurrency: int, reps: int
) -> list[dict]:
    """Measure full-speed download performance, arms interleaved.

    :param arms: Mapping of arm name to target.
    :param concurrency: Simultaneous downloads per measurement.
    :param reps: Repetitions of the whole interleaved cycle.
    :returns: One summary row per arm per repetition.
    """
    for target in arms.values():
        await warm_up(target)

    rows = []
    for rep in range(reps):
        # Interleave: one full cycle of arms per rep, never arm-in-blocks.
        for name, target in arms.items():
            started = time.perf_counter()
            results = await asyncio.gather(*(fetch(target) for _ in range(concurrency)))
            wall = time.perf_counter() - started
            sample = Sample(arm=name, results=list(results))
            total_bytes = sum(r.body_bytes for r in sample.ok)
            latencies = sample.latencies()
            rows.append({
                "mode": "throughput",
                "arm": name,
                "rep": rep,
                "requests": len(sample.results),
                "ok": len(sample.ok),
                "wall_s": round(wall, 4),
                "mib_s": round(total_bytes / wall / 1024 / 1024, 2),
                "bytes_each": sample.ok[0].body_bytes if sample.ok else 0,
                "p50_ms": round(_pct(latencies, 0.50) * 1000, 1),
                "p95_ms": round(_pct(latencies, 0.95) * 1000, 1),
                "errors": [r.error for r in sample.results if r.error][:3],
            })
    return rows


async def _probe_page(
    page: Target, count: int, interval: float, timeout: float
) -> Sample:
    """Poll a cheap page at a fixed rate and record every outcome.

    :param page: The page endpoint.
    :param count: Number of probes.
    :param interval: Seconds between probe starts.
    :param timeout: Per-probe deadline; exceeding it counts as starvation.
    :returns: The collected sample.
    """
    sample = Sample(arm="page")
    tasks = []
    for _ in range(count):
        tasks.append(asyncio.create_task(fetch(page, timeout=timeout)))
        await asyncio.sleep(interval)
    sample.results = list(await asyncio.gather(*tasks))
    return sample


async def run_starve(
    download: Target,
    page: Target,
    *,
    slow_clients: int,
    throttle_bytes: int,
    throttle_delay: float,
    probes: int,
    probe_interval: float,
    probe_timeout: float,
) -> list[dict]:
    """Can the server still serve pages while slow clients download?

    A baseline round with no slow clients runs first, so the loaded numbers have
    something to be compared against on the same machine, seconds apart.

    :param download: The endpoint the slow clients hit.
    :param page: The cheap page endpoint being probed.
    :param slow_clients: How many throttled downloads to hold open.
    :param throttle_bytes: Bytes each slow client reads between sleeps.
    :param throttle_delay: How long each slow client sleeps, in seconds.
    :param probes: Number of page probes per round.
    :param probe_interval: Seconds between probe starts.
    :param probe_timeout: Per-probe deadline.
    :returns: A summary row for the baseline round and one for the loaded round.
    """
    await warm_up(page)
    await warm_up(download)

    rows = []

    baseline = await _probe_page(page, probes, probe_interval, probe_timeout)
    rows.append(_starve_row("baseline", baseline, 0))

    slow = [
        asyncio.create_task(
            fetch(
                download,
                throttle_bytes=throttle_bytes,
                throttle_delay=throttle_delay,
                timeout=3600,
            )
        )
        for _ in range(slow_clients)
    ]
    # Give the slow downloads time to be accepted and start occupying whatever
    # they are going to occupy before probing.
    await asyncio.sleep(2.0)

    loaded = await _probe_page(page, probes, probe_interval, probe_timeout)
    rows.append(_starve_row("loaded", loaded, slow_clients))

    for task in slow:
        task.cancel()
    await asyncio.gather(*slow, return_exceptions=True)

    return rows


async def _drive_page(
    page: Target, concurrency: int, duration: float, timeout: float
) -> tuple[Sample, float]:
    """Hammer a page with a fixed number of concurrent clients for a fixed time.

    Closed-loop, unlike :func:`_probe_page`: each worker issues the next request
    as soon as the previous one finishes. That is what makes the result a
    *capacity* number — offered load adapts to what the server can take — rather
    than a latency-at-a-chosen-rate number.

    :param page: The page endpoint.
    :param concurrency: Simultaneous clients.
    :param duration: How long to keep going, in seconds.
    :param timeout: Per-request deadline.
    :returns: The collected sample and the elapsed wall-clock time.
    """
    sample = Sample(arm="page")
    deadline = time.perf_counter() + duration

    async def worker() -> None:
        while time.perf_counter() < deadline:
            sample.results.append(await fetch(page, timeout=timeout))

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return sample, time.perf_counter() - started


async def run_capacity(
    page: Target,
    download: Target | None,
    *,
    slow_clients: int,
    throttle_bytes: int,
    throttle_delay: float,
    concurrency: int,
    duration: float,
    timeout: float,
) -> list[dict]:
    """Measure sustained page throughput, with and without downloads in flight.

    :param page: The page endpoint to measure.
    :param download: The endpoint the slow clients hit; ``None`` to run the
        baseline round only.
    :param slow_clients: How many throttled downloads to hold open.
    :param throttle_bytes: Bytes each slow client reads between sleeps.
    :param throttle_delay: How long each slow client sleeps, in seconds.
    :param concurrency: Simultaneous page clients.
    :param duration: Seconds to measure per round.
    :param timeout: Per-request deadline.
    :returns: A summary row per round.
    """
    await warm_up(page)
    if download is not None:
        await warm_up(download)

    rows = []

    sample, wall = await _drive_page(page, concurrency, duration, timeout)
    rows.append(_capacity_row("baseline", sample, wall, 0))

    if download is None or not slow_clients:
        return rows

    slow = [
        asyncio.create_task(
            fetch(
                download,
                throttle_bytes=throttle_bytes,
                throttle_delay=throttle_delay,
                timeout=3600,
            )
        )
        for _ in range(slow_clients)
    ]
    # Let the downloads be accepted and start occupying whatever they will.
    await asyncio.sleep(2.0)

    sample, wall = await _drive_page(page, concurrency, duration, timeout)
    rows.append(_capacity_row("loaded", sample, wall, slow_clients))

    for task in slow:
        task.cancel()
    await asyncio.gather(*slow, return_exceptions=True)

    return rows


def _capacity_row(
    round_name: str, sample: Sample, wall: float, slow_clients: int
) -> dict:
    """Summarize one capacity round.

    :param round_name: ``baseline`` or ``loaded``.
    :param sample: The collected page results.
    :param wall: Elapsed wall-clock time for the round.
    :param slow_clients: Slow downloads held open during the round.
    :returns: A summary row.
    """
    latencies = sample.latencies()
    return {
        "mode": "capacity",
        "round": round_name,
        "slow_clients": slow_clients,
        "requests": len(sample.results),
        "ok": len(sample.ok),
        "failed": len(sample.results) - len(sample.ok),
        "req_s": round(len(sample.ok) / wall, 2) if wall else 0.0,
        "p50_ms": round(_pct(latencies, 0.50) * 1000, 1),
        "p95_ms": round(_pct(latencies, 0.95) * 1000, 1),
        "errors": [r.error for r in sample.results if r.error][:3],
    }


def _starve_row(round_name: str, sample: Sample, slow_clients: int) -> dict:
    """Summarize one starvation round.

    :param round_name: ``baseline`` or ``loaded``.
    :param sample: The page probe results.
    :param slow_clients: Slow downloads held open during the round.
    :returns: A summary row.
    """
    latencies = sample.latencies()
    return {
        "mode": "starve",
        "round": round_name,
        "slow_clients": slow_clients,
        "probes": len(sample.results),
        "ok": len(sample.ok),
        "failed": len(sample.results) - len(sample.ok),
        "p50_ms": round(_pct(latencies, 0.50) * 1000, 1),
        "p95_ms": round(_pct(latencies, 0.95) * 1000, 1),
        "max_ms": round(latencies[-1] * 1000, 1) if latencies else 0.0,
        "errors": [r.error for r in sample.results if r.error][:3],
    }


def _print_table(rows: list[dict]) -> None:
    """Print rows as an aligned table.

    :param rows: Summary rows sharing a set of keys.
    """
    if not rows:
        return
    keys = [k for k in rows[0] if k != "errors"]
    widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    print("  ".join(k.ljust(widths[k]) for k in keys))
    print("  ".join("-" * widths[k] for k in keys))
    for row in rows:
        print("  ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys))
    for row in rows:
        if row.get("errors"):
            print(f"  ! {row.get('arm') or row.get('round')}: {row['errors']}")


def main() -> None:
    """Parse arguments and run the selected mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    tp = sub.add_parser("throughput", help="full-speed download A/B, interleaved")
    tp.add_argument(
        "--arm",
        action="append",
        required=True,
        metavar="NAME=URL",
        help="an implementation to measure; repeat for each arm",
    )
    tp.add_argument("--concurrency", type=int, default=4)
    tp.add_argument("--reps", type=int, default=3)

    st = sub.add_parser("starve", help="page latency under slow downloads")
    st.add_argument("--download-url", required=True)
    st.add_argument("--page-url", required=True)
    st.add_argument("--slow-clients", type=int, default=8)
    st.add_argument(
        "--throttle-bytes",
        type=int,
        default=16 * 1024,
        help="bytes a slow client reads between sleeps",
    )
    st.add_argument("--throttle-delay", type=float, default=0.5)
    st.add_argument("--probes", type=int, default=20)
    st.add_argument("--probe-interval", type=float, default=0.25)
    st.add_argument("--probe-timeout", type=float, default=30.0)

    cap = sub.add_parser(
        "capacity", help="sustained page req/s, with and without downloads"
    )
    cap.add_argument("--page-url", required=True)
    cap.add_argument("--download-url")
    cap.add_argument("--slow-clients", type=int, default=8)
    cap.add_argument("--throttle-bytes", type=int, default=16 * 1024)
    cap.add_argument("--throttle-delay", type=float, default=0.5)
    cap.add_argument("--concurrency", type=int, default=4)
    cap.add_argument("--duration", type=float, default=10.0)
    cap.add_argument("--timeout", type=float, default=30.0)

    # Defined per-subparser rather than on the root: an option on the root has to
    # be given *before* the subcommand, which is a trap every caller falls into.
    for subparser in (tp, st, cap):
        subparser.add_argument("--json", dest="as_json", action="store_true")

    args = parser.parse_args()

    if args.mode == "throughput":
        arms = {}
        for spec in args.arm:
            name, _, url = spec.partition("=")
            if not url:
                parser.error(f"--arm expects NAME=URL, got {spec!r}")
            arms[name] = Target.parse(url)
        rows = asyncio.run(run_throughput(arms, args.concurrency, args.reps))
    elif args.mode == "capacity":
        rows = asyncio.run(
            run_capacity(
                Target.parse(args.page_url),
                Target.parse(args.download_url) if args.download_url else None,
                slow_clients=args.slow_clients,
                throttle_bytes=args.throttle_bytes,
                throttle_delay=args.throttle_delay,
                concurrency=args.concurrency,
                duration=args.duration,
                timeout=args.timeout,
            )
        )
    else:
        rows = asyncio.run(
            run_starve(
                Target.parse(args.download_url),
                Target.parse(args.page_url),
                slow_clients=args.slow_clients,
                throttle_bytes=args.throttle_bytes,
                throttle_delay=args.throttle_delay,
                probes=args.probes,
                probe_interval=args.probe_interval,
                probe_timeout=args.probe_timeout,
            )
        )

    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        _print_table(rows)
        if args.mode == "throughput":
            for name in {r["arm"] for r in rows}:
                rates = [r["mib_s"] for r in rows if r["arm"] == name]
                print(
                    f"\n{name}: mean {statistics.mean(rates):.2f} MiB/s "
                    f"over {len(rates)} reps {rates}"
                )


if __name__ == "__main__":
    main()

"""Helpers for the benchmark gate.

The benchmark is driven as a **subprocess**, exactly the way a human runs it.
Importing ``benchmarks/`` instead would need a ``sys.path`` insert — it is not
part of the installed package — and would leave the documented command untested
while a parallel in-process copy was the thing actually verified.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "benchmarks" / "run.py"
PLONE_DIR = ROOT / "benchmarks" / "plone"

#: Zope startup plus ZCML for a classic Plone site: seconds, not minutes.
READY_TIMEOUT = 120.0

#: Generous. A starve round is roughly probes x interval, twice, plus a settle
#: pause and server start — but a *failing* round waits out every probe timeout,
#: which is the case that matters and the slow one.
SUBPROCESS_TIMEOUT = 300.0


def run_bench(mode: str, adapter: str, **options: object) -> list[dict]:
    """Run one benchmark mode against one adapter and return its rows.

    :param mode: ``starve``, ``throughput`` or ``capacity``.
    :param adapter: ``ploneasgi`` or ``a2wsgi``.
    :param options: Extra flags, as ``slow_clients=16`` for ``--slow-clients 16``.
    :returns: The parsed summary rows.
    :raises AssertionError: If the run fails, with the harness's own output —
        a benchmark that did not run must never be reported as a result.
    """
    argv = [sys.executable, str(RUNNER), mode, "--adapter", adapter, "--json"]
    for key, value in options.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]

    result = subprocess.run(  # noqa: S603
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, (
        f"benchmark run failed ({' '.join(argv)}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout)


def round_named(rows: list[dict], name: str) -> dict:
    """Pick one round out of a starve or capacity result.

    :param rows: Rows returned by :func:`run_bench`.
    :param name: ``baseline`` or ``loaded``.
    :returns: That round's summary.
    :raises AssertionError: If it is absent, rather than returning ``None`` and
        failing later on an attribute of it.
    """
    matching = [row for row in rows if row.get("round") == name]
    assert matching, f"no {name!r} round in {rows}"
    return matching[0]


def _plone_env() -> dict[str, str]:
    """:returns: The environment for any process that loads the instance's ZCML.

    ``routes.zcml`` names handlers in ``benchmarks/plone/endpoints.py``, and
    ``zconsole`` executes ``site.zcml`` as well as the server does.
    """
    # Prepended, not replaced: an inherited PYTHONPATH is the caller's to keep.
    inherited = os.environ.get("PYTHONPATH")
    path = os.pathsep.join(p for p in (str(PLONE_DIR), inherited) if p)
    return {**os.environ, "PYTHONPATH": path}


def build_plone_instance(home: Path, *, storage: str = "file", **env: str) -> Path:
    """Generate an instance and create the benchmark site in it.

    Both steps are the documented commands, run as subprocesses:
    ``make_instance.py`` writes the configuration, ``zconsole run`` executes
    ``create_content.py`` inside Zope.

    :param home: Directory for the instance.
    :param storage: Storage backend, as ``make_instance.py --storage`` takes it.
    :param env: Extra environment for both steps, such as a database DSN.
    :returns: ``home``, now holding ``etc/zope.conf`` and ``manifest.json``.
    :raises AssertionError: When either step fails, with its output.
    """
    environ = {**_plone_env(), **env, "BENCH_MANIFEST": str(home / "manifest.json")}
    steps = [
        [
            sys.executable,
            str(PLONE_DIR / "make_instance.py"),
            str(home),
            "--storage",
            storage,
            "--routes-zcml",
            str(PLONE_DIR / "routes.zcml"),
        ],
        [
            str(Path(sys.executable).parent / "zconsole"),
            "run",
            str(home / "etc" / "zope.conf"),
            str(PLONE_DIR / "create_content.py"),
        ],
    ]
    for argv in steps:
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, env=environ, cwd=ROOT, check=False
        )
        assert result.returncode == 0, (
            f"{' '.join(argv)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return home


@contextmanager
def plone_server(home: Path, log: Path, *, adapter: str = "ploneasgi") -> Iterator[str]:
    """Serve an instance for the duration of a block, logging at DEBUG.

    The DEBUG log is part of the evidence: :mod:`ploneasgi.wsgi` logs the body
    type of every response, which is what shows that a Plone download takes the
    streaming branch rather than being materialized.

    :param home: Instance directory.
    :param log: File the server's combined output goes to.
    :param adapter: ``ploneasgi`` or ``a2wsgi``.
    :returns: The server's base URL.
    :raises AssertionError: When the server dies or never answers.
    """
    port = free_port()
    argv = [
        sys.executable,
        str(PLONE_DIR / "serve.py"),
        adapter,
        "--zope-conf",
        str(home / "etc" / "zope.conf"),
        "--port",
        str(port),
        "--log-level",
        "DEBUG",
        "--access-log",
    ]
    with log.open("w") as output:
        process = subprocess.Popen(  # noqa: S603
            argv, stdout=output, stderr=subprocess.STDOUT, env=_plone_env(), cwd=ROOT
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + READY_TIMEOUT
            while True:
                assert process.poll() is None, f"server exited:\n{log.read_text()}"
                assert time.monotonic() < deadline, f"not ready:\n{log.read_text()}"
                try:
                    if fetch(base, "/" + _site(home))[0] == 200:
                        break
                except OSError:
                    pass  # not listening yet
                time.sleep(0.2)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)


def free_port() -> int:
    """Ask the OS for a port that is free right now.

    Racy, like every such helper, but a collision fails loudly: the server
    cannot bind and exits, and :func:`plone_server` reports its output.

    :returns: The port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _site(home: Path) -> str:
    """:returns: The site id recorded in the instance's manifest."""
    return json.loads((home / "manifest.json").read_text())["site"]


def basic_auth(username: str, password: str) -> dict[str, str]:
    """:returns: A Basic ``Authorization`` header."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def fetch(
    base: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Make one GET request, without following redirects.

    Following them would hide the case that matters: an unauthorized download
    answered with a redirect to the login form reads as a 200 once followed.

    :param base: Server base URL.
    :param path: Request path.
    :param headers: Request headers.
    :returns: Status, lowercased response headers, and the body.
    """
    host, port = base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=60)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()

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
import shutil
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


def write_plone_instance(home: Path, *, storage: str = "file", **options: str) -> Path:
    """Generate an instance's configuration, without creating a site in it.

    Separate from :func:`create_site` because a networked storage has to be
    running before Zope can open it, and for ZEO the server's own configuration
    is one of the files this writes.

    :param home: Directory for the instance.
    :param storage: Storage backend, as ``make_instance.py --storage`` takes it.
    :param options: Its flags, as ``dsn="..."`` for ``--dsn ...``.
    :returns: ``home``.
    :raises AssertionError: When the generator fails, with its output.
    """
    argv = [
        sys.executable,
        str(PLONE_DIR / "make_instance.py"),
        str(home),
        "--storage",
        storage,
        "--routes-zcml",
        str(PLONE_DIR / "routes.zcml"),
    ]
    for key, value in options.items():
        argv += [f"--{key.replace('_', '-')}", value]
    _run_step(argv, _plone_env())
    return home


def create_site(home: Path) -> Path:
    """Create the benchmark site in a configured instance.

    The documented command, as a subprocess: ``zconsole run`` executes
    ``create_content.py`` inside Zope.

    :param home: Instance directory, its storage reachable.
    :returns: ``home``, now holding ``manifest.json``.
    :raises AssertionError: When the step fails, with its output.
    """
    environ = {**_plone_env(), "BENCH_MANIFEST": str(home / "manifest.json")}
    _run_step(
        [
            str(Path(sys.executable).parent / "zconsole"),
            "run",
            str(home / "etc" / "zope.conf"),
            str(PLONE_DIR / "create_content.py"),
        ],
        environ,
    )
    return home


def _run_step(argv: list[str], env: dict[str, str]) -> None:
    """Run one setup command, failing with its output.

    :param argv: The command.
    :param env: Its environment.
    :raises AssertionError: When it exits non-zero.
    """
    result = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, env=env, cwd=ROOT, check=False
    )
    assert result.returncode == 0, (
        f"{' '.join(argv)} failed:\n{result.stdout}\n{result.stderr}"
    )


#: The starve gate's shape, shared by every Plone-backed run.
WORKERS = 4
PROBES = 12

#: A rendered Plone page takes tens of milliseconds, not the stand-in's two;
#: this stays far below "held until the download finishes", which is hours.
PROBE_TIMEOUT = 10.0


def starve_plone(adapter: str, slow_clients: int, instance: Path) -> list[dict]:
    """Run one starve round against an instance.

    :param adapter: ``ploneasgi`` or ``a2wsgi``.
    :param slow_clients: Concurrent throttled downloads.
    :param instance: The instance directory.
    :returns: The baseline and loaded rows.
    :raises AssertionError: If the run did not serve Plone.
    """
    rows = run_bench(
        "starve",
        adapter,
        workers=WORKERS,
        slow_clients=slow_clients,
        probes=PROBES,
        probe_timeout=PROBE_TIMEOUT,
        plone_instance=instance,
    )
    assert {row["server"] for row in rows} == {"plone"}, (
        "the run did not serve Plone; this would be the stand-in result again"
    )
    return rows


def drop_blob_caches(home: Path) -> None:
    """Empty the instance's local blob caches, so the next read fetches.

    Creating the site leaves the blob in the cache, because the process that
    commits it writes it there; without this every networked storage would be
    measured as a local file.

    :param home: Instance directory; its server must not be running.
    """
    for cache in ("blobcache", "blobtemp"):
        shutil.rmtree(home / "var" / cache, ignore_errors=True)


def build_plone_instance(home: Path) -> Path:
    """Generate a FileStorage instance and create the benchmark site in it.

    :param home: Directory for the instance.
    :returns: ``home``, now holding ``etc/zope.conf`` and ``manifest.json``.
    :raises AssertionError: When either step fails, with its output.
    """
    return create_site(write_plone_instance(home))


#: Credentials of the throwaway PostgreSQL; it listens on loopback only.
PG_USER = "zodb"
PG_IMAGE = "postgres:17"


@contextmanager
def postgres() -> Iterator[str]:
    """A PostgreSQL server for the duration of a block.

    ``BENCH_PG_DSN`` names an existing server and is used as is. Otherwise a
    throwaway container is started on a free loopback port and removed
    afterwards, whatever happens in between.

    :returns: A DSN for the server's maintenance database, with the right to
        create databases.
    :raises AssertionError: When the container cannot start or never answers.
    """
    import psycopg

    if existing := os.environ.get("BENCH_PG_DSN"):
        yield existing
        return

    docker = shutil.which("docker")
    assert docker, "Docker not found: install it, or set BENCH_PG_DSN to a server"
    port = free_port()
    name = f"ploneasgi-bench-pg-{port}"
    started = subprocess.run(  # noqa: S603
        [
            docker,
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--env",
            f"POSTGRES_USER={PG_USER}",
            "--env",
            f"POSTGRES_PASSWORD={PG_USER}",
            "--publish",
            f"127.0.0.1:{port}:5432",
            PG_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, f"docker run failed:\n{started.stderr}"
    dsn = (
        f"host=127.0.0.1 port={port} user={PG_USER} password={PG_USER} dbname=postgres"
    )
    try:
        # The image's init step runs a temporary server on the Unix socket
        # only, so a TCP connection succeeding means the real one is up.
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            try:
                psycopg.connect(dsn, connect_timeout=2).close()
                break
            except psycopg.OperationalError as exc:
                assert time.monotonic() < deadline, f"postgres never answered: {exc}"
                time.sleep(0.5)
        yield dsn
    finally:
        subprocess.run(  # noqa: S603
            [docker, "rm", "--force", name], capture_output=True, check=False
        )


def fresh_database(admin_dsn: str, name: str) -> str:
    """Drop and recreate one database, so a storage starts empty.

    :param admin_dsn: DSN with the right to create databases.
    :param name: Database name; an identifier, never user input.
    :returns: A DSN for the new database.
    """
    import psycopg

    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return make_conninfo(admin_dsn, dbname=name)


@contextmanager
def zeo_server(home: Path, address: str) -> Iterator[None]:
    """Run the instance's ZEO server for the duration of a block.

    :param home: Instance directory holding ``etc/zeo.conf``.
    :param address: The ``host:port`` that file binds.
    :raises AssertionError: When the server exits or never listens.
    """
    host, port = address.split(":")
    log = home / "var" / "zeo.log"
    with log.open("w") as output:
        process = subprocess.Popen(  # noqa: S603
            [
                str(Path(sys.executable).parent / "runzeo"),
                "-C",
                str(home / "etc" / "zeo.conf"),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
        )
        try:
            deadline = time.monotonic() + READY_TIMEOUT
            while True:
                assert process.poll() is None, f"runzeo exited:\n{log.read_text()}"
                assert time.monotonic() < deadline, f"no ZEO:\n{log.read_text()}"
                try:
                    socket.create_connection((host, int(port)), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.2)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)


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

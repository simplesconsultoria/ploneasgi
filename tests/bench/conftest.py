"""Fixtures for the Plone-backed half of the benchmark gate.

Building a site takes seconds and the instance's FileStorage admits one process
at a time, so the instance is built once per session and servers run against it
one after another, never side by side.
"""

from . import build_plone_instance
from . import create_site
from . import free_port
from . import fresh_database
from . import plone_server
from . import postgres
from . import write_plone_instance
from . import zeo_server
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import json
import pytest


#: The networked storages, each fetching blobs into a local cache on read.
STORAGES = ["zeo", "relstorage", "pgjsonb"]


@pytest.fixture(scope="session")
def plone_instance(tmp_path_factory) -> Path:
    """A generated instance with the benchmark site and its manifest.

    :param tmp_path_factory: pytest's session-scoped temporary directories.
    :returns: The instance directory.
    """
    return build_plone_instance(tmp_path_factory.mktemp("instance"))


@pytest.fixture(scope="session")
def manifest(plone_instance) -> dict:
    """:returns: What ``create_content.py`` created, and where."""
    return json.loads((plone_instance / "manifest.json").read_text())


@pytest.fixture(scope="module")
def live(plone_instance, tmp_path_factory) -> Iterator[tuple[str, Path]]:
    """A ploneasgi server on the instance, for the checks in one module.

    :param plone_instance: The instance directory.
    :param tmp_path_factory: pytest's temporary directories.
    :returns: The base URL and the server's log file.
    """
    log = tmp_path_factory.mktemp("live") / "server.log"
    with plone_server(plone_instance, log) as base:
        yield base, log


@pytest.fixture(scope="session")
def pg_admin() -> Iterator[str]:
    """One PostgreSQL for the session, shared by the storages that need it.

    :returns: A DSN with the right to create databases.
    """
    with postgres() as dsn:
        yield dsn


@pytest.fixture(scope="module", params=STORAGES)
def storage_instance(request, tmp_path_factory) -> Iterator[Path]:
    """An instance on one networked storage, its backend running throughout.

    Module-scoped and parametrized, so pytest runs every test in the module
    against one storage before moving to the next, and a backend is never up
    longer than its tests need. PostgreSQL is requested lazily: the ZEO round
    must not need Docker.

    :param request: Names the storage in ``request.param``.
    :param tmp_path_factory: pytest's temporary directories.
    :returns: The instance directory, holding ``manifest.json``.
    """
    storage = request.param
    home = tmp_path_factory.mktemp(storage)
    with ExitStack() as stack:
        if storage == "zeo":
            address = f"127.0.0.1:{free_port()}"
            write_plone_instance(home, storage=storage, zeo_address=address)
            stack.enter_context(zeo_server(home, address))
        else:
            dsn = fresh_database(request.getfixturevalue("pg_admin"), storage)
            write_plone_instance(home, storage=storage, dsn=dsn)
        yield create_site(home)

"""Fixtures for the Plone-backed half of the benchmark gate.

Building a site takes seconds and the instance's FileStorage admits one process
at a time, so the instance is built once per session and servers run against it
one after another, never side by side.
"""

from . import build_plone_instance
from . import plone_server
from collections.abc import Iterator
from pathlib import Path

import json
import pytest


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

"""The central claim on every networked storage: ZEO, RelStorage, PGJsonb.

FileStorage reads a blob from a local file that already exists. None of these
do: each fetches the blob into a local cache on first read — over the ZEO
protocol, or out of PostgreSQL (a blob table for RelStorage, a ``bytea`` column
for PGJsonb, which has no blob directory at all). Where that fetch runs decides
whether the claim survives the storage, and it has been measured rather than
assumed: ZODB fetches while unghosting the ``Blob``, inside the WSGI call, so
the cost lands on a worker thread and is bounded by the *fetch*, never by the
client's transfer.

Every round starts with the caches emptied, so that fetch is part of it. The
gate stays categorical: 12/12 under ploneasgi with four times its workers in
slow downloads, 0/12 under a2wsgi at its worker count, on each storage.

Marked ``bench_storage`` — it needs Docker for PostgreSQL, or ``BENCH_PG_DSN``
naming a server — and run by ``make bench-storage``.
"""

from . import PROBES
from . import WORKERS
from . import drop_blob_caches
from . import fetch
from . import plone_server
from . import round_named
from . import starve_plone

import json
import pytest


pytestmark = pytest.mark.bench_storage


def test_a_cold_download_is_streamed_whole(storage_instance, tmp_path):
    """The fetch into the cache does not change what the adapter is handed."""
    manifest = json.loads((storage_instance / "manifest.json").read_text())
    drop_blob_caches(storage_instance)
    log = tmp_path / "server.log"
    with plone_server(storage_instance, log) as base:
        status, headers, body = fetch(
            base, manifest["download"]["path"] + "/@@download/file"
        )
    assert status == 200
    assert len(body) == manifest["download"]["size"]
    assert int(headers["content-length"]) == manifest["download"]["size"]
    types = {
        line.split("body type=")[1].strip()
        for line in log.read_text().splitlines()
        if "body type=" in line
    }
    assert any(name.startswith("filestream") for name in types), types


def test_ploneasgi_serves_every_page_under_slow_downloads(storage_instance):
    drop_blob_caches(storage_instance)
    rows = starve_plone("ploneasgi", WORKERS * 4, storage_instance)
    assert round_named(rows, "baseline")["ok"] == PROBES
    loaded = round_named(rows, "loaded")
    assert loaded["ok"] == PROBES, (
        f"{loaded['failed']} of {PROBES} page probes failed under "
        f"{loaded['slow_clients']} slow downloads: {loaded.get('errors')}"
    )


def test_a2wsgi_starves_at_its_worker_count(storage_instance):
    """The falsifier, per storage: without it the test above could pass
    because this storage made downloads fast, not because ploneasgi works."""
    drop_blob_caches(storage_instance)
    rows = starve_plone("a2wsgi", WORKERS, storage_instance)
    assert round_named(rows, "baseline")["ok"] == PROBES
    assert round_named(rows, "loaded")["ok"] == 0

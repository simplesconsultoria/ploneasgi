---
myst:
  html_meta:
    "description": "How ploneasgi behaves on ZEO, RelStorage, and PGJsonb, where a blob is fetched into a local cache on first read."
    "property=og:description": "How ploneasgi behaves on ZEO, RelStorage, and PGJsonb, where a blob is fetched into a local cache on first read."
    "property=og:title": "Blobs on networked storages"
---

(concepts-storages)=

# Blobs on networked storages

<!-- source: tests/bench/test_plone_storages.py, benchmarks/plone/make_instance.py -->

With FileStorage and a local blob directory, a download streams from a file that already exists.
ZEO, RelStorage, and PGJsonb do not have one.
Each of them fetches a blob into a local cache the first time it is read, over the ZEO protocol or out of PostgreSQL.
PGJsonb has no blob directory at all: with no S3 configured, every blob is a row in a `bytea` column.

Where that fetch runs decides whether the claim in {doc}`why-asgi` survives the storage.

## Where the fetch runs

ZODB fetches a blob while it unghosts the `Blob` object, in `Connection.setstate`, which calls the storage's `loadBlob`.
That happens inside the WSGI call, when `@@download` touches the file, so it happens on a worker thread.
By the time the body is streamed, `Blob.committed()` only returns the path of the cached file.

That was measured rather than assumed, by timing `Connection.setstate` for `Blob` objects on each storage.
A cold fetch of a 50 MB blob held a worker for, on one laptop, one run each:

| Storage | Cold fetch, on a worker thread | Warm |
| --- | ---: | ---: |
| FileStorage | none: the file is local | none |
| ZEO, local server | 0.12 s | about 0 |
| RelStorage, PostgreSQL 17 | 3.7 s | about 0 |
| PGJsonb, PostgreSQL 17 | 1.2 s | about 0 |

## What it means

The fetch is *work*.
It is bounded by the storage and the size of the blob, not by the client's connection, and the worker is released before the transfer begins.
The body then streams from the cache file on the event loop, as it does on FileStorage.

The starvation gate confirms it on every storage, starting each round with the caches emptied: ploneasgi answers 12/12 page probes under 16 slow downloads, and a2wsgi answers 0/12 under 4.

The table still has a consequence.
On RelStorage, a burst of first downloads of large, uncached blobs can occupy every worker for seconds.
Size the worker count, and the blob cache, with that in mind.
{doc}`/how-to-guides/tune-concurrency` covers both.

## What was not tested

- PGJsonb's S3 tier: with S3 configured, blobs above `blob-threshold` go there, behind the same local cache.
- Shared blob directories for ZEO and RelStorage.
  An unshared directory is the harder case, since a shared one reduces both to the FileStorage case.

## Related

- {doc}`the-adapter` explains how the stream itself is served.
- {doc}`/how-to-guides/reproduce-the-benchmarks` runs the storage gate.
- [`cookiecutter-zope-instance`](https://github.com/plone/cookiecutter-zope-instance) generates `zope.conf` for each of these storages.

---
myst:
  html_meta:
    "description": "Set the ploneasgi worker count, ZODB pool size, body-read limiter, chunk size, and blob cache for a deployment."
    "property=og:description": "Set the ploneasgi worker count, ZODB pool size, body-read limiter, chunk size, and blob cache for a deployment."
    "property=og:title": "Tune concurrency"
---

(how-to-tune-concurrency)=

# Tune concurrency

<!-- source: src/ploneasgi/settings.py, src/ploneasgi/wsgi.py, src/ploneasgi/zopeapp.py -->

This guide shows you how to size the limiters of a ploneasgi deployment.

## Set the worker count

`PLONE_CONCURRENCY` bounds concurrent WSGI calls and native-route calls together, and each one holds a ZODB connection.
Keep it at or below the ZODB `pool-size` in `zope.conf`, which defaults to `7`:

```shell
PLONE_CONCURRENCY=6
```

To raise the pool, set `pool-size` in the database section of `zope.conf`, or `db_pool_size` when you generate the instance with `cookiecutter-zope-instance`.

{func}`~ploneasgi.zopeapp.check_concurrency_contract` logs a warning at startup when the worker count exceeds the pool.

Downloads do not need workers, so do not raise this number for file traffic.
Raise it for slow pages: renders that wait on a remote service or a slow storage.

## Set the body-read limiter

Concurrent body reads have their own limiter, which defaults to four times the workers, and at least 16.
To change it, pass your own adapter to `create_app`:

```python
from ploneasgi.wsgi import WSGIMiddleware


def adapter(wsgi_app, settings):
    return WSGIMiddleware(
        wsgi_app,
        workers=settings.concurrency,
        io_workers=64,
        chunk_size=settings.chunk_size,
        max_body_size=settings.max_body_size or None,
    )


app = create_app(settings, wsgi_app=wsgi_app, adapter=adapter)
```

Keep `workers=settings.concurrency`: native routes share that limiter.

## Set the chunk size

`PLONE_BLOB_CHUNK_SIZE` is the number of bytes pulled from a file per thread hop, 256 KiB by default.
Raise it for faster transfers of large files; each in-flight download holds one chunk in memory.

```shell
PLONE_BLOB_CHUNK_SIZE=1048576
```

## Size the blob cache on a networked storage

On ZEO, RelStorage, and PGJsonb, the first download of a blob fetches it into a local cache on a worker.
Make the blob cache large enough to hold the files that are downloaded often, so that the fetch happens once.
With `cookiecutter-zope-instance`, the option is `db_blob_cache_size`, or `db_pgjsonb_blob_cache_size` for PGJsonb.

## Check the result

Turn on the access log with `PLONE_ACCESS_LOG=1` and watch `ttfb` under load.
`ttfb` climbing while `duration` stays flat means requests are waiting for a worker.

## Related

- {doc}`/concepts/the-adapter` explains the two limiters.
- {doc}`/concepts/storages` explains the blob fetch.
- {doc}`/reference/settings` lists the variables.
- {doc}`/reference/access-log` lists the log fields.

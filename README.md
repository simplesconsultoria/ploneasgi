# ploneasgi

Run Plone under an ASGI server, with no PasteDeploy `.ini` file, and without spending a worker thread on a client's download.

> **Status: spike.** The API will change.

📖 **Documentation:** https://simplesconsultoria.github.io/ploneasgi/

## Why

Under WSGI, a large download holds a worker thread for the duration of the *client's transfer*, not the duration of the work.
One visitor on a slow connection pins one thread for minutes, and four of them take a four-thread site down.
Measured, not theorized:

| Server | Concurrent slow downloads | Page requests answered |
| ------ | ------------------------: | ---------------------: |
| waitress | 3 | 12 / 12 |
| waitress | **4** (its `threads`) | **0 / 12** |
| uvicorn and a2wsgi | 4 | 0 / 12 |
| uvicorn and ploneasgi | 16 | 12 / 12 |

ploneasgi calls Zope in a worker thread, releases the thread when Zope returns, and streams the body from the event loop.
[Why ASGI](https://simplesconsultoria.github.io/ploneasgi/concepts/why-asgi.html) explains the mechanism.

## What's in the box

- **An ASGI↔WSGI adapter** that holds a thread for the work, never for the transfer.
- **An access log** at the ASGI layer, which sees native routes and websockets, and keeps the time to first byte apart from the transfer.
- **Native routes** beside Plone, declared in ZCML with `<ploneasgi:route />`, guarded by a permission, and authenticated through Plone's own PAS.
- **One proxy rule**: native routes read the VirtualHostMonster rewrite your proxy already sends.
- **Environment-only configuration** in place of `zope.ini`.
- **A test layer**, `ploneasgi.testing`, for projects that build native routes of their own.

## Quick start

```python
from ploneasgi import create_app
from ploneasgi import load_env
from ploneasgi import serve
from ploneasgi import Settings

load_env()
settings = Settings.from_env()
app = create_app(settings)

if __name__ == "__main__":
    serve(app, settings)
```

The [tutorial](https://simplesconsultoria.github.io/ploneasgi/tutorials/serve-a-plone-site.html) builds a site from scratch, and [Migrate from waitress](https://simplesconsultoria.github.io/ploneasgi/how-to-guides/migrate-from-waitress.html) moves an existing one.

## Development

```shell
make install && make test    # the tests that need no Plone
make install-plone && make test
make bench-test              # the starvation claim, as an assertion
make lint
make docs-html
```

[Develop ploneasgi](https://simplesconsultoria.github.io/ploneasgi/how-to-guides/develop-ploneasgi.html) has the details, and `AGENTS.md` the conventions.

## Known gaps

- `build_wsgi_app` is unit-tested only up to its guard clause; the rest runs end to end under `make bench-plone`.
- The access log's `bytes` is an upper bound on what the client received.
- The access log cannot name the authenticated user, because authentication happens inside Zope.
- Request bodies are fully buffered before Zope is called.
- PGJsonb's S3 tier is untested.
- POSIX only, for the unlink-safety argument in the adapter.

## License

GPL-2.0-only

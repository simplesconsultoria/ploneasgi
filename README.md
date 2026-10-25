# ploneasgi

Run Plone under an ASGI server — no PasteDeploy `.ini`, and no worker thread
pinned to a client's download.

> **Status: spike.** The API will change.

## Why

Under WSGI, a large download holds a worker thread for the duration of the
*client's transfer*, not the duration of the work. `plone.namedfile` streams the
blob in chunks and each one blocks until the socket accepts it, so one visitor on
bad wifi pins one of four threads for minutes. Four of them take the site down —
measured, not theorised:

| Server | concurrent slow downloads | page requests answered |
| ------ | ------------------------: | ---------------------: |
| waitress | 3 | 12 / 12 |
| waitress | **4** (= `threads`) | **0 / 12** |
| uvicorn + a2wsgi | 4 | 0 / 12 |
| uvicorn + `ploneasgi.wsgi` | 16 | 12 / 12 |

This is pre-existing Plone behavior, not something ASGI introduced. But ASGI is
the first architecture in which Plone can serve a large file without spending a
thread on it.

## What's in the box

### `ploneasgi.wsgi.WSGIMiddleware`

A replacement for `a2wsgi.WSGIMiddleware`. Same job, different split:

```
a2wsgi        worker thread: call the app AND iterate the body,
                             blocking on the event loop per chunk
ploneasgi     worker thread: call the app, return the iterable
              event loop:    pull one chunk at a time and send it
```

The thread is held for the *work*, never for the *transfer*. Waiting on a slow
client happens on the event loop, holding nothing.

This works because ZPublisher already closes the ZODB connection before returning
its body iterable — and says so, in `ZPublisher.Iterators.IStreamIterator`:

> *"After the application finishes interpreting a request and returns an iterator
> to be processed asynchronously, it closes the ZODB connection."*

Zope has described this architecture since Zope 2. What was missing was a server
that could process the iterator asynchronously.

Two limiters, deliberately separate:

- `workers` bounds concurrent WSGI calls — the number that must be coordinated
  with the ZODB `pool-size`, since each in-flight call may hold a connection.
- `io_workers` bounds concurrent body reads, and defaults to `4 × workers`.
  Pulling bytes off a blob file holds no ZODB connection, so making downloads
  compete for the same slots as page renders would reintroduce the starvation
  this exists to remove.

Nothing in this module imports Zope; it is a general WSGI adapter and is tested
as one.

### `ploneasgi.accesslog.AccessLogMiddleware`

A replacement for `paste.translogger`, at the ASGI layer. The WSGI layer is the
wrong place for an access log here for three structural reasons:

- it sits *below* the router, so websockets and anything passed to
  `create_app(routes=...)` never reach it;
- it logs from inside `start_response` — at headers, before the body exists.
  Under this adapter the body streams *after* the WSGI call returns, so the whole
  phase `ploneasgi.wsgi` exists to optimize happens after the log line is written;
- its byte count is the `Content-Length` header, never what was written ("at
  least for now we'll be lazy" — its own source).

The line is Apache combined plus two timings, kept apart on purpose:

```
127.0.0.1 - - [03/Aug/2026:14:27:54 -0300] "GET /Plone/files/report.pdf/@@download/file HTTP/1.1" 200 52428800 0.303 0.093 "-" "curl/8.7.1"
                                                                                                    bytes    total ttfb
```

`ttfb` is time to `http.response.start` — parsing, the worker hop, Zope's render.
`duration` is that plus the transfer. Worker-thread starvation shows up as `ttfb`
climbing; a slow client shows up in `duration` alone. One merged number cannot
tell "Plone is slow" from "someone is downloading a film over bad wifi".

It is raw ASGI middleware, not a `BaseHTTPMiddleware`: the latter pumps responses
through memory streams, which would buffer the very responses this package
streams. Logging the blob path must not destroy it.

### `ploneasgi.context.run_authenticated`

One tested way for a native route to reach Plone. Runs your callable in a worker
thread with a ZODB connection, `setSite`, and a real security manager, then tears
all three down and aborts the transaction:

```python
def work(ctx):
    return {
        "user": ctx.user.getUserName(),
        "docs": len(ctx.portal.portal_catalog(portal_type="Document")),
    }


data = await run_authenticated(request.scope, work, site_id="Plone")
```

`ctx.traverse()` uses `restrictedTraverse`, so permissions are enforced at every
step rather than only on the target. Return plain data — a persistent object
outlives its connection. Authentication walks the acquired user folders innermost
first, exactly as ZPublisher does, so a Zope *root* user works too.

### `<ploneasgi:route />`

An add-on cannot edit your `server.py`, so routes can also come from ZCML:

```xml
<configure xmlns:ploneasgi="http://namespaces.plone.org/ploneasgi">
  <include package="ploneasgi" file="meta.zcml" />
  <ploneasgi:route
      path="/whoami"
      handler=".endpoints.whoami"
      permission="cmf.ManagePortal"
      />
</configure>
```

`permission` is the reason this beats a plain list: it is checked against the
portal *before* the handler is entered, so an endpoint cannot forget to authorize
itself. It takes a permission *id*, as every other ZCML directive does; an id
that is not registered refuses every request rather than falling back to
Zope's Manager-only default. A route with no permission is a configuration
error unless it says
`unguarded="true"` — a forgotten permission and a deliberately open endpoint
should not look identical. Two routes on one path are a ZCML conflict, reported
at startup with both file locations rather than last-one-wins at request time.

Discovery is explicit — `create_app` does not scan:

```python
wsgi_app = build_wsgi_app(settings)  # ZCML executes here
app = create_app(settings, routes=routes_from_registry(), wsgi_app=wsgi_app)
```

### It costs no new infrastructure

Paths are relative to a reserved prefix, so that route is served at
`/++asgi++/whoami`. This is the part that matters operationally: your proxy
already rewrites everything through VirtualHostMonster, and
`ploneasgi.vhm.VirtualHostMiddleware` reads that rewrite itself.

```
https://example.com/++asgi++/whoami
  → your existing Traefik rule, unchanged
  → /VirtualHostBase/https/example.com:443/Plone/VirtualHostRoot/++asgi++/whoami
  → site=Plone, path=/++asgi++/whoami, scheme=https, host=example.com
```

**Adding a route needs no proxy change at all** — not one rule per route, none.
The reserved prefix is the whole contract, and it is with the app rather than
with the proxy. The Plone site comes out of the rewrite, so one registration
serves every site and no path has to restate it; scheme and host are recovered
too, so a route behind TLS termination does not see `http` and a container
address.

Anything whose rewritten path does *not* start with the prefix is Zope's, and its
scope is passed through untouched so Zope's own VirtualHostMonster still sees the
URL it expects. `_vh_` prefix segments are refused with a 501 for native routes
rather than guessed at; Zope traffic using them is unaffected.

Without a proxy — development — `PLONE_SITE_ID` supplies the site, or `site=` on
the directive does.

Registrations themselves are process-wide, because ZCML executes once at startup
into the global registry; local persistent registrations live in the ZODB and
would go stale the moment an add-on is installed. Which site a *request* enters is
resolved per request, so that is not a limitation in practice.

### `ploneasgi.Settings` / `create_app` / `serve`

Environment-only configuration and a Starlette app with Zope mounted at `/`.

## Usage

```python
from ploneasgi import create_app, load_env, serve, Settings
from starlette.routing import WebSocketRoute

load_env()  # before any Zope import

settings = Settings.from_env()
app = create_app(settings, routes=[WebSocketRoute("/ws", my_handler)])

if __name__ == "__main__":
    serve(app, settings)
```

`create_app` takes `routes`, `middleware` and `lifespans`. It scans nothing and
looks nothing up: adding a route means passing a route, because the moment that
stops being true, PasteDeploy has been reinvented with extra steps.

`<ploneasgi:route />` does not change that. It exists because an add-on has no
`server.py` to edit, and the routes it collects still arrive the same way — you
call `routes_from_registry()` and pass the result in.

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `PLONE_HOST` | `0.0.0.0` | Bind address. Note uvicorn binds IPv4 only. |
| `PLONE_PORT` | `8080` | Bind port. |
| `PLONE_CONCURRENCY` | `4` | Concurrent WSGI calls. Keep ≤ ZODB `pool-size`. |
| `PLONE_ACCESS_LOG` | off | Enable the ASGI-layer access log. uvicorn's own access log is always off, so this is the only one. |
| `PLONE_LOG_LEVEL` | `INFO` | Root log level. |
| `PLONE_ZOPE_CONF` | `instance/etc/zope.conf` | Path to `zope.conf`. |
| `PLONE_BLOB_CHUNK_SIZE` | `262144` | Bytes per thread hop when streaming. |
| `PLONE_ROUTE_PREFIX` | `/++asgi++` | Path prefix reserved for native routes. |
| `PLONE_SITE_ID` | `Plone` | Site entered by native routes when a request arrives with no VirtualHostMonster rewrite. Unused behind a proxy. |

On the chunk size: 64 KB is what `filestream_iterator` and Starlette's
`FileResponse` default to, but here each chunk costs a thread dispatch. Measured
on a 50 MB blob at c=4 — 666 MiB/s at 64 KB, 1757 at 256 KB, 3758 at 1 MB. Past
256 KB the gain is measured on a warm page cache over loopback, and pays for
itself in memory held per in-flight response.

## Testing

The suite has two halves, and they install different things.

```bash
make install && make test        # 112 tests, no Plone in the environment
make test-no-plone               # asserts Plone is absent, then runs those 112
make install-plone && make test  # 153 — the above plus 41 that need a real site
make test-plone                  # asserts Plone is present, then runs those 41
make lint                        # read-only; `make format` is the writing one
```

Most of the package installs **no Plone at all** — `Products`, `Zope2`,
`AccessControl` and `plone` are all absent from the environment those 112 tests
run in. That is the load-bearing check behind the "Zope-free" claim in each
module: it is verified by the absence of Zope, not asserted in a docstring.
`tests/test_zopeapp.py::TestStaysZopeFree` checks the same boundary from inside
a Plone-equipped environment, by importing each module in a subprocess and
inspecting `sys.modules` — the environment check cannot run in a job that needs
Plone for the other half.

`make test-plone` asserts Plone *is* installed before running, which is not
symmetry for its own sake: `tests/` skips `tests/plone/` when Plone is absent,
so without that assertion the Plone half could quietly cover nothing and still
report success.

### `ploneasgi.testing`

The layer behind those 41 tests ships with the package, for projects that build
native routes of their own:

```python
from ploneasgi.testing import FUNCTIONAL_TESTING  # pip install ploneasgi[testing]
```

Use the functional layer for anything that reaches Plone from another thread.
The integration layer aborts rather than commits, so a second connection sees an
empty site.

## Benchmarks

The claim this package exists for is a capacity claim, so it ships the harness
that tries to break it.

```bash
make bench-test                        # the claim, as an assertion
make bench-starve  ADAPTER=a2wsgi      # page availability under slow downloads
make bench-compare REPS=3              # interleaved throughput A/B
```

Three pieces, deliberately separate: `benchmarks/bench_adapter.py` serves a cheap
`/page` and a streamed `/download`, `benchmarks/driver.py` generates the load,
and `benchmarks/run.py` owns the server's lifecycle around one run. The server is
always a subprocess — running it in the same interpreter as the client would put
the event loop under measurement in charge of generating the load.

`make bench-test` asserts only **categorical** outcomes. Both arms are checked,
and the second is not decoration: if a2wsgi ever stops starving, the load is no
longer slow enough or the body is no longer streamed, and the ploneasgi assertion
beside it has become a tautology.

Measured on one laptop, 4 workers, 50 MB body, loopback — a starting point for
your own numbers, not a specification:

| slow clients | ploneasgi | a2wsgi |
| -----------: | --------- | ------ |
| 3            | 12 / 12   | 12 / 12 |
| **4** (= workers) | 12 / 12 | **0 / 12** |
| 16           | 12 / 12   | — |

Throughput on the same body, arms interleaved over 3 cycles: ploneasgi
1306 / 1399 / 1358 MiB/s against a2wsgi 588 / 611 / 583. The ranges do not
overlap, which is the only reason it is worth quoting at all — and it is still a
warm-cache loopback number, not a production profile.

## Known gaps

- **`build_wsgi_app` is tested only up to its guard clause.** Everything past the
  `zope.conf` check needs a generated instance, which this package does not ship.
- **The access log's `bytes` is what the app emitted, not what the client got.**
  uvicorn's `send` begins `if self.disconnected: return`, discarding chunks after
  a disconnect without telling the application — so an aborted 50 MB download is
  logged as a clean, complete 200. Catching that needs a concurrent poll of
  `receive` for `http.disconnect`, which the middleware does not do because the
  adapter owns `receive`. Behind a proxy, the proxy's log is authoritative on
  delivery.
- **The access log cannot name the authenticated user** — authentication happens
  inside Zope, below the middleware. It logs `-` unless something sets
  `ploneasgi.user` on the ASGI scope, which the adapter exposes to WSGI as
  `environ["asgi.scope"]`.
- **Request bodies are fully buffered** before the WSGI app is called, spilling to
  disk past 512 KB. a2wsgi streams them instead, at the cost of a blocking round
  trip per read.
- **Only local blobstorage tested.** ZEO and RelStorage without a shared blob dir
  fetch into a local cache first; that fetch lands in the worker thread.
  PGJsonb keeps blobs in PostgreSQL or S3 with no local blob file at all, and
  how it behaves here has not been measured yet.
- **POSIX only** for the unlink-safety argument.
- **`run_authenticated(limiter=...)` is plumbed but unused.** Native routes draw
  on anyio's default thread pool rather than the ZODB pool budget.

## License

GPL-2.0-only

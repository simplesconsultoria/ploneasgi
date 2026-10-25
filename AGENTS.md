# AGENTS.md — ploneasgi

Guidance for anyone — human or agent — working on **ploneasgi**.

---

## Project overview

**ploneasgi** runs Plone under an ASGI server with no PasteDeploy `.ini` file, and
without spending a worker thread on a client's download.

The central claim, and the thing every change is measured against:

> Under WSGI a large download holds a worker thread for the duration of the
> *client's transfer*, not the duration of the work. Four slow clients take a
> four-thread site down. ASGI is the first architecture in which Plone can serve
> a large file without spending a thread on it.

This is a **spike**. The API will change. It was extracted from a proof of
concept and its numbers come from one laptop — see "Evidence discipline".

---

## Tech stack

| Concern | Technology |
|---|---|
| Runtime | Python 3.14 only |
| ASGI framework | Starlette |
| Server | uvicorn (+ `httptools`, `websockets`; deliberately not `uvicorn[standard]`, which pulls uvloop) |
| Threading | anyio capacity limiters |
| ZCML | `zope.configuration`, `zope.component`, `zope.schema` |
| Packaging | uv, `uv_build` |
| Tests | pytest |
| Lint/format | ruff |
| Changelog | towncrier |

---

## Project structure

```
ploneasgi/
├── src/ploneasgi/
│   ├── wsgi.py        # ASGI↔WSGI adapter — replaces a2wsgi
│   ├── accesslog.py   # ASGI-layer access log — replaces paste.translogger
│   ├── vhm.py         # VirtualHostMonster parsing + route dispatch
│   ├── zcml.py        # <ploneasgi:route /> directive + routes_from_registry
│   ├── context.py     # authenticated Plone context in a worker thread
│   ├── zopeapp.py     # Zope startup, lifespan, concurrency contract
│   ├── testing.py     # PloneSandboxLayer; needs the `testing` extra
│   ├── settings.py    # environment-only configuration
│   ├── app.py         # create_app / serve
│   └── meta.zcml      # directive declaration
├── tests/             # 112 tests, none of which need Plone
│   ├── plone/         # 41 more that do — skipped when Plone is absent
│   └── bench/         # 3 that need a live server — deselected by default
├── benchmarks/
│   ├── bench_adapter.py  # serves /page and /download under either adapter
│   ├── driver.py         # generates load: starve / throughput / capacity
│   └── run.py            # starts the server, runs a mode, always tears down
└── news/              # towncrier fragments
```

---

## The rule that shapes everything: stay Zope-free

`wsgi.py`, `accesslog.py` and `vhm.py` **must never import Zope, Plone or the
ZODB** — not at module level, not lazily. They are general ASGI middleware and
are tested as such.

`context.py` and `zopeapp.py` are the only modules that touch Plone, and they
import it **inside functions**, never at module scope. `testing.py` is the one
exception and imports Plone at module scope — it is an optional import path
behind the `testing` extra, which is why nothing else in the package may import
it. `zcml.py` sits in between:
it needs `zope.configuration`, which is *not* Zope — a small pure-Python closure
with no ZODB. Its permission guard resolves ids through `zope.security`, and
imports it inside the function that runs in the worker thread, where Plone is
present by definition.

The check is mechanical, and CI should keep running it:

```bash
make test-no-plone   # asserts Products, Zope2, AccessControl, plone are all absent
```

If that target ever needs Plone installed to pass, something has moved to the
wrong side of the line.

---

## Development workflow

```bash
make install         # package + test dependencies
make install-plone   # adds Plone and the test layer's harness
make test            # whatever the venv supports: 112, or 153 with Plone
make test-no-plone   # asserts Plone is absent, then runs the 112
make test-plone      # asserts Plone is present, then runs the 41
make bench-test      # asserts the starvation claim; needs a live server
make bench-compare   # interleaved ploneasgi vs a2wsgi throughput
make lint            # read-only — safe for CI
make format          # the writing one
make changelog       # preview the towncrier draft
make build           # sdist + wheel
```

`make lint` passes `--no-fix` on purpose: the ruff config sets `fix = true`, so
plain `ruff check` rewrites the tree. A check target must never modify files.

---

## Evidence discipline

This package's whole pitch is a performance and capacity claim, so the standard
for asserting one is higher than usual.

- **Interleave and repeat benchmarks.** Never run all of arm A then all of arm B
  — the machine drifts between blocks and the drift lands on the comparison. Use
  at least 3 repetitions and report the spread, not just a mean. A single run is
  noise. `make bench-compare` does the alternation for you.
- **Benchmark `127.0.0.1`, never `localhost`.** uvicorn on `0.0.0.0` is IPv4-only;
  `ab` resolves `localhost` to `::1` and fails, while `curl` silently falls back
  and hides it.
- **The harness is three pieces, and they are separate on purpose.**
  `bench_adapter.py` serves, `driver.py` measures, `run.py` owns the server's
  lifecycle around a run. The server is always a *subprocess*: hosting it
  in-process would put uvicorn's event loop in the same interpreter as the client
  driving it, which is the one arrangement guaranteed to misreport this package's
  central claim.
- **Assert categorical results only.** `make bench-test` gates on 12/12 versus
  0/12 page probes, which survives being measured on whatever machine CI
  allocated. Never assert a MiB/s threshold — put those numbers in a commit
  message, with the spread and the machine.
- **The a2wsgi arm must keep failing.** `TestA2wsgiStarves` pins the incumbent
  behaviour; if it ever stops starving, the load has stopped being slow enough or
  the body has stopped streaming, and every ploneasgi assertion beside it has
  quietly become a tautology.
- **Read the installed library's source before attributing a cost.** The venv is
  right there and it is cheaper than a wrong hypothesis. The a2wsgi overhead was
  first blamed on `h11`; reading `a2wsgi/wsgi.py` showed the real mechanism was a
  blocking cross-thread round trip per chunk.
- **Say where a number came from.** Throughput measured on a warm page cache over
  loopback is not a production profile. Starvation results are categorical and
  travel much better than MiB/s.

---

## Testing conventions

- Tests are driven through the **raw ASGI protocol**, not Starlette's
  `TestClient` — that keeps every `http.response.body` event visible, which is
  the behaviour this package exists to control.
- **Mutation-test anything security-related.** A passing security test proves
  nothing until you have watched it fail: swap `restrictedTraverse` for
  `unrestrictedTraverse`, or delete `noSecurityManager()` from a teardown, and
  confirm the intended test breaks. A test that survives the mutation is
  decoration.
- **Unit tests are not enough for the Plone-facing paths.** The suite was fully
  green while native routes rejected the Zope *root* administrator, because the
  code asked only `portal.acl_users`. Only a real server found it. Anything
  touching authentication or traversal gets checked against a running instance.
- Plone-dependent tests need the **functional** layer, not integration. Anything
  that commits so a second connection can see it will hit
  `plone.testing.zope.TestIsolationBroken` under the integration layer — loudly,
  which is the good case. The bad case is the test that *doesn't* commit: it
  passes under either layer while proving nothing about cross-thread visibility.
  Verified by mutation: swapping the `portal` fixture to `integration` errors 5
  of 7 authorization tests and leaves 2 passing vacuously.
- The Zope-free rule is checked **two** ways. `make test-no-plone` removes Plone
  from the environment; `tests/test_zopeapp.py::TestStaysZopeFree` imports each
  module in a subprocess and inspects `sys.modules`. Only the second one still
  works in a job that has Plone installed — which is every job running the other
  half of the suite.

---

## Invariants that must not regress

- **Zope traffic passes through untouched.** In `vhm.py`, any request whose
  rewritten path does not start with the reserved prefix must reach the WSGI
  mount with its scope *identical* — same object, no added keys, `Host` header
  unmodified. Rewriting Zope's path corrupts every URL Plone generates, which is
  worse than any routing bug. `TestZopeTrafficIsUntouched` exists for this alone.
- **The adapter must not buffer streamed responses.** Never wrap the app in
  Starlette's `BaseHTTPMiddleware`; it pumps responses through memory streams and
  silently undoes the streaming.
- **A route with no permission is a configuration error.** `<ploneasgi:route />`
  refuses to register one unless it says `unguarded="true"`. A forgotten
  permission and a deliberately open endpoint must never look identical.
- **Discovery stays explicit.** `create_app` scans nothing and looks nothing up.
  ZCML-registered routes reach it because the project calls
  `routes_from_registry()` and passes the result in.

---

## Coding conventions

- Type hints on new code.
- Docstrings in **reStructuredText** — `:param:`, `:returns:`, `:raises:`. Never
  Google or NumPy style.
- Comments explain *why*, especially where the obvious implementation is wrong.
  Several modules encode hard-won traps this way; do not strip them as noise.
- Imports follow the ruff isort config: single line, no sections, `from` first.
- Concrete return types (`list`, `dict`) rather than abstract ones, unless the
  function genuinely branches.

---

## What NOT to do

- Do not import Zope or Plone from `wsgi.py`, `accesslog.py` or `vhm.py`.
- Do not add a registration framework, entry-point scanning or a middleware
  registry to `create_app`. The moment one appears, PasteDeploy has been
  reinvented with extra steps.
- Do not quote a performance delta from a single, non-interleaved run.
- Do not claim the access log's `bytes` is what the client received — uvicorn
  discards chunks after a disconnect without telling the application.
- Do not use `unrestrictedTraverse` plus a permission check on the target. That
  skips the checks on every intermediate step, which is the shape of most
  path-traversal disclosure bugs.
- Do not widen `requires-python` without a CI matrix that actually exercises the
  added versions.

---

## Known gaps

- CI (`.github/workflows/ci.yml`) has not run yet: it runs for the first
  time on the first push, which is where defects in a never-run workflow show
  up.
- **`build_wsgi_app` is only tested up to its guard clause.** Everything past the
  `zope.conf` check needs a generated instance, which the package does not ship.
- `run_authenticated(limiter=...)` is plumbed but nothing passes it, so native
  routes draw on anyio's default thread pool rather than the ZODB pool budget.
- ZEO, RelStorage and PGJsonb untested; only local blobstorage. PGJsonb is the
  one that matters most: its blobs live in PostgreSQL or S3 behind a cache, so
  there is no local blob file to stream from.
- The route guard and site resolution are tested against the functional layer
  only, not yet against a running instance.
- POSIX only, for the unlink-safety argument in the adapter.

---

## Changelog

- Every feature or bugfix **must** include a news fragment in `news/`.
- With an issue: `news/<issue-number>.<type>`. Without: `news/+<slug>.<type>`.
- Types: `breaking`, `feature`, `bugfix`, `documentation`, `internal`, `tests`.
- Past tense, user-oriented, ending with `@github_username`.
- Run `make changelog` to preview before committing.

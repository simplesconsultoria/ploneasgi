---
myst:
  html_meta:
    "description": "Replace runwsgi, waitress, and zope.ini with ploneasgi, uvicorn, and environment variables."
    "property=og:description": "Replace runwsgi, waitress, and zope.ini with ploneasgi, uvicorn, and environment variables."
    "property=og:title": "Migrate from waitress"
---

(how-to-migrate-from-waitress)=

# Migrate from waitress

<!-- source: src/ploneasgi/settings.py, src/ploneasgi/zopeapp.py, src/ploneasgi/app.py -->

This guide shows you how to move an existing Plone instance from `runwsgi` and waitress to ploneasgi.
Your `zope.conf`, database, and add-ons stay as they are; only `zope.ini` and the start command change.

## Install ploneasgi

Add ploneasgi to the project that holds your Plone dependencies:

```shell
uv add "ploneasgi[plone] @ git+https://github.com/simplesconsultoria/ploneasgi"
```

ploneasgi requires Python 3.14.

## Write a server script

Create `server.py` at the root of the project:

```python
from ploneasgi import create_app
from ploneasgi import load_env
from ploneasgi import serve
from ploneasgi import Settings
from ploneasgi.zcml import routes_from_registry
from ploneasgi.zopeapp import build_wsgi_app
from ploneasgi.zopeapp import check_concurrency_contract

load_env()
settings = Settings.from_env()
# Starting Zope executes the ZCML, which registers the routes collected below.
wsgi_app = build_wsgi_app(settings)
check_concurrency_contract(settings)
app = create_app(
    settings,
    routes=routes_from_registry(prefix=settings.route_prefix),
    wsgi_app=wsgi_app,
)

if __name__ == "__main__":
    serve(app, settings)
```

If you register no native routes, `create_app(settings)` alone builds the same application.

## Translate `zope.ini`

`zope.ini` is no longer read.
Move each value you changed from its default into an environment variable, or into a `.env` file next to `server.py`:

| `zope.ini` | ploneasgi |
| --- | --- |
| `[app:zope]` `zope_conf` | `PLONE_ZOPE_CONF`, default `instance/etc/zope.conf` |
| `[server:main]` `listen = localhost:8080` | `PLONE_HOST` and `PLONE_PORT`, default `0.0.0.0` and `8080` |
| `[server:main]` `threads` | `PLONE_CONCURRENCY`, default `4` |
| `[server:main]` `max_request_body_size` | `PLONE_MAX_REQUEST_BODY_SIZE`, default 1 GiB |
| `[filter:translogger]` in the pipeline | `PLONE_ACCESS_LOG=1` |
| `[logger_root]` `level` | `PLONE_LOG_LEVEL`, default `INFO` |
| `egg:Zope#httpexceptions` in the pipeline | always applied by {func}`~ploneasgi.zopeapp.build_wsgi_app` |

The log format matches `formatter_generic` from the generated `zope.ini`, so existing log parsing keeps working.
The access log line has a different format; see {doc}`/reference/access-log`.

```{important}
waitress's `localhost` binds both IPv4 and IPv6.
ploneasgi's `0.0.0.0` binds IPv4 only, so check health probes and proxies with `127.0.0.1`, not `localhost`.
```

## Start the server

Replace `runwsgi instance/etc/zope.ini` with:

```shell
uv run python server.py
```

Check that Plone answers as before:

```shell
curl -I http://127.0.0.1:8080/Plone
```

## Related

- {doc}`/reference/settings` lists every variable.
- {doc}`run-behind-a-proxy` keeps your proxy rules working.
- {doc}`tune-concurrency` sets the worker count against the ZODB pool.

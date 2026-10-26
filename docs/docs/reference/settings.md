---
myst:
  html_meta:
    "description": "Every environment variable ploneasgi reads, with its default and meaning."
    "property=og:description": "Every environment variable ploneasgi reads, with its default and meaning."
    "property=og:title": "Settings"
---

(reference-settings)=

# Settings

<!-- source: src/ploneasgi/settings.py -->

ploneasgi reads its configuration from environment variables only.
{func}`ploneasgi.settings.load_env` loads a `.env` file first, if one exists, without overriding variables already set.
{meth}`ploneasgi.settings.Settings.from_env` then builds a {class}`~ploneasgi.settings.Settings` instance from the environment.

## Environment variables

| Variable | `Settings` field | Default | Meaning |
| --- | --- | --- | --- |
| `PLONE_HOST` | `host` | `0.0.0.0` | Interface to bind. uvicorn binds IPv4 only for this value. |
| `PLONE_PORT` | `port` | `8080` | Port to bind. |
| `PLONE_CONCURRENCY` | `concurrency` | `4` | Maximum concurrent WSGI calls, and the size of the limiter native routes share. It must not exceed the ZODB `pool-size`. |
| `PLONE_ACCESS_LOG` | `access_log` | off | Enables the access log. Accepts `1`, `true`, `on` or `yes`, in any case. |
| `PLONE_LOG_LEVEL` | `log_level` | `INFO` | Root log level. |
| `PLONE_ZOPE_CONF` | `zope_conf` | `instance/etc/zope.conf` | Path to `zope.conf`. A relative default resolves against the current directory. |
| `PLONE_BLOB_CHUNK_SIZE` | `chunk_size` | `262144` | Bytes pulled from a streamed body per thread hop. |
| `PLONE_ROUTE_PREFIX` | `route_prefix` | `/++asgi++` | Path prefix reserved for native routes. |
| `PLONE_SITE_ID` | `site_id` | `Plone` | Site entered by native routes when a request carries no VirtualHostMonster rewrite. |
| `PLONE_MAX_REQUEST_BODY_SIZE` | `max_body_size` | `1073741824` | Largest request body accepted, in bytes. A larger body gets a `413` before Zope runs. `0` disables the limit. |

## Constraints

- `PLONE_CONCURRENCY` must be less than or equal to the ZODB `pool-size` in `zope.conf`.
  {func}`ploneasgi.zopeapp.check_concurrency_contract` logs a warning when it is not.
- `PLONE_HOST` must be an IPv4 address for uvicorn to accept connections on it.
  Benchmark and probe `127.0.0.1`, never `localhost`, which can resolve to `::1`.
- uvicorn's own access log is always off.
  `PLONE_ACCESS_LOG` decides whether there is an access log at all.

## Related

- {doc}`/how-to-guides/tune-concurrency` sets these values for a deployment.
- {doc}`/how-to-guides/migrate-from-waitress` maps the `zope.ini` options they replace.
- {ref}`reference-api-settings` documents the class and functions.

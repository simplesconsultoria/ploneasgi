---
myst:
  html_meta:
    "description": "The fields, format, and notes of the ploneasgi access log."
    "property=og:description": "The fields, format, and notes of the ploneasgi access log."
    "property=og:title": "Access log format"
---

(reference-access-log)=

# Access log format

<!-- source: src/ploneasgi/accesslog.py -->

{class}`~ploneasgi.accesslog.AccessLogMiddleware` writes one line per HTTP request and one line per websocket session to the `ploneasgi.access` logger, at level `INFO`.
`PLONE_ACCESS_LOG` enables it.

## Format

The default format is Apache combined, plus two timings and a note:

```text
%(client)s - %(user)s [%(time)s] "%(method)s %(path)s %(protocol)s" %(status)s %(bytes)s %(duration)s %(ttfb)s "%(referer)s" "%(user_agent)s"%(note)s
```

An example line:

```text
127.0.0.1 - - [26/Oct/2026:11:28:59 -0300] "GET /Plone HTTP/1.1" 200 8790 0.733 0.732 "-" "curl/8.7.1"
```

## Fields

| Field | HTTP request | Websocket session |
| --- | --- | --- |
| `client` | Socket address of the client. With `trust_forwarded`, the first address in `X-Forwarded-For`. | Same. |
| `user` | The `ploneasgi.user` scope key, or `-`. | Same. |
| `time` | Local time when the line is written, Apache format. | Same. |
| `method` | Request method. | `WEBSOCKET`. |
| `path` | Path as it arrived, rewrite included, with the query string. | Same. |
| `protocol` | `HTTP/` and the HTTP version. | Same. |
| `status` | Response status, or `-` when no response started. | The close code, from whichever side closed. With no close code recorded: `-` if the session was accepted, `403` if it never was. |
| `bytes` | Body bytes the application emitted. | `0`. |
| `duration` | Seconds from arrival to the final body event. | Seconds from arrival to close. |
| `ttfb` | Seconds from arrival to the response start event. | `-`. |
| `referer` | `Referer` header, or `-`. | Same. |
| `user_agent` | `User-Agent` header, or `-`. | Same. |
| `note` | See {ref}`reference-access-log-notes`. | ` messages=<received>/<sent>`. |

Every string field is escaped as Apache escapes it: `"` becomes `\"`, `\` becomes `\\`, and each control character becomes `\xhh`.

(reference-access-log-notes)=

## Notes

| Note | Meaning |
| --- | --- |
| *(none)* | The final body event was sent. |
| ` incomplete=no-response` | No response started. |
| ` incomplete=client-gone` | The client disconnected while the body was streaming, and the adapter stopped. |
| ` incomplete=truncated` | The application stopped emitting before its final body event, typically an exception mid-stream. |

```{important}
`bytes` is an upper bound on what the client received.
Bytes already handed to the server may never have left the socket buffer.
Behind a proxy, the proxy's log is authoritative on delivery.
```

## Constructor options

| Option | Default | Meaning |
| --- | --- | --- |
| `fmt` | `DEFAULT_FORMAT` | Percent-style format string over the fields above. |
| `trust_forwarded` | `False` | Read `client` from `X-Forwarded-For`. Enable only behind a proxy that overwrites the header. |
| `level` | `logging.INFO` | Level to log at. |
| `log` | `ploneasgi.access` | Logger to write to. |

{func}`~ploneasgi.app.create_app` adds the middleware with its defaults.
To change an option, leave `PLONE_ACCESS_LOG` off and pass the middleware yourself in `middleware`.

## Related

- {doc}`/concepts/the-adapter` explains why `ttfb` and `duration` are kept apart.
- {ref}`reference-api-accesslog` documents the module.

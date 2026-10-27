---
myst:
  html_meta:
    "description": "Read Plone content from a native route as the requesting user, from the context the route's handler runs in."
    "property=og:description": "Read Plone content from a native route as the requesting user, from the context the route's handler runs in."
    "property=og:title": "Read content from a route"
---

(how-to-read-content-from-a-route)=

# Read content from a route

<!-- source: src/ploneasgi/context.py, src/ploneasgi/zcml.py -->

This guide shows you how to read Plone content from a native route, with the permissions of the user who made the request.

## Use the context

An HTTP handler registered with `<ploneasgi:route />` receives a {class}`~ploneasgi.context.PloneContext` as its first argument:

```python
from starlette.responses import JSONResponse


def title(ctx, request):
    obj = ctx.traverse(request.query_params.get("path", ""))
    return JSONResponse({"title": obj.Title(), "url": obj.absolute_url()})
```

The handler runs in a worker thread, with a ZODB connection, the site set, and the request's user authenticated.
`ctx.portal` is the site the request is addressed to, from the proxy rewrite or from `PLONE_SITE_ID`.

## Return a response built from plain data

Build the response from strings, numbers, lists, and dictionaries, never from a content object.
The connection is closed when the handler returns, and a persistent object is unusable after that.

## Let traversal check permissions

`ctx.traverse(path)` raises `Unauthorized` when the user may not reach the object, and `KeyError` when it does not exist.
An uncaught `Unauthorized` becomes a `403`.
Catch `KeyError` and answer `404` yourself; uncaught, it becomes a `500`:

```python
from starlette.responses import PlainTextResponse


def title(ctx, request):
    try:
        obj = ctx.traverse(request.query_params.get("path", ""))
    except KeyError:
        return PlainTextResponse("Not Found", status_code=404)
    return JSONResponse({"title": obj.Title()})
```

Never use `unrestrictedTraverse` to reach content in a handler.

## Require a user

To refuse anonymous requests, call `ctx.require_user()`.
It raises {class}`~ploneasgi.context.NotAuthenticated`, which becomes a `401`.
To check another permission on an object, call `ctx.check_permission("Modify portal content", obj)` with the permission's title.

## Read the request body

The body has already been read when the handler runs, and `request.body()` cannot be awaited from it.
Read it from the publisher request instead:

- `ctx.request.form` holds the query string and a form-encoded or multipart body, parsed as Zope parses them;
- `ctx.request.get("BODY")` returns any other body, a JSON document for instance, as bytes.

A malformed multipart body is answered with `400` before the handler runs.

## Stream a blob

Open the blob in the handler with `openDetached()`, and stream the file it returns:

```python
from starlette.responses import StreamingResponse


def download(ctx, request):
    field = ctx.traverse(request.path_params["path"]).file
    handle = field.openDetached()

    def chunks():
        with handle:
            while chunk := handle.read(256 * 1024):
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type=field.contentType,
        headers={"Content-Length": str(field.getSize())},
    )
```

`openDetached()` returns a plain file on the committed blob, which stays readable after the connection closes.
The response sends it after the worker thread is released, so a slow download holds no ZODB connection.
Register the route with a `path` such as `/download/{path:path}` to receive the content path.

## Do not write

Every request ends in an abort, so anything the handler changes is discarded.
Native routes are read-only.

## From a route you pass to `create_app`

A route passed to {func}`~ploneasgi.app.create_app` directly, or a websocket handler, gets no context.
Put the Plone work in a function that takes one, and hand it to {func}`~ploneasgi.context.run_authenticated`:

```python
from ploneasgi import run_authenticated
from ploneasgi.vhm import SITE_KEY


async def title(request):
    path = request.query_params.get("path", "")

    def work(ctx):
        return {"title": ctx.traverse(path).Title()}

    data = await run_authenticated(request.scope, work, site_id=request.scope[SITE_KEY])
    return JSONResponse(data)
```

`work` must return plain data, for the same reason.
Exceptions from `work` propagate out of `run_authenticated` unchanged, so catch `Unauthorized`, `KeyError`, and `NotAuthenticated` around it.

## Related

- {doc}`/concepts/security` explains what the context sets up and tears down.
- {doc}`add-a-native-route` registers the endpoint.
- {doc}`test-native-routes` tests it against a real site.
- {ref}`reference-api-context` documents the module.

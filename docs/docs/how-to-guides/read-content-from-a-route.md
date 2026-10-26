---
myst:
  html_meta:
    "description": "Read Plone content from a native route as the requesting user, with run_authenticated."
    "property=og:description": "Read Plone content from a native route as the requesting user, with run_authenticated."
    "property=og:title": "Read content from a route"
---

(how-to-read-content-from-a-route)=

# Read content from a route

<!-- source: src/ploneasgi/context.py -->

This guide shows you how to read Plone content from a native route, with the permissions of the user who made the request.

## Run the work in a context

Put everything that touches Plone in a function that takes a {class}`~ploneasgi.context.PloneContext`, and hand it to {func}`~ploneasgi.context.run_authenticated`:

```python
from ploneasgi import run_authenticated
from ploneasgi.vhm import SITE_KEY
from starlette.responses import JSONResponse


async def title(request):
    path = request.query_params.get("path", "")

    def work(ctx):
        obj = ctx.traverse(path)
        return {"title": obj.Title(), "url": obj.absolute_url()}

    data = await run_authenticated(request.scope, work, site_id=request.scope[SITE_KEY])
    return JSONResponse(data)
```

`work` runs in a worker thread, with a ZODB connection, the site set, and the request's user authenticated.
`request.scope[SITE_KEY]` is the site the request is addressed to, from the proxy rewrite or from `PLONE_SITE_ID`.

## Return plain data

Return strings, numbers, lists, and dictionaries from `work`, never a content object.
The connection is closed when `work` returns, and a persistent object is unusable after that.

## Let traversal check permissions

`ctx.traverse(path)` raises `Unauthorized` when the user may not reach the object, and `KeyError` when it does not exist.
Both propagate out of `run_authenticated`, so catch them in `work` and turn them into data:

```python
from zExceptions import Unauthorized


def work(ctx):
    try:
        obj = ctx.traverse(path)
    except Unauthorized, KeyError:
        return None
    return {"title": obj.Title()}
```

The endpoint can then answer `404` when `work` returns `None`.

Never use `unrestrictedTraverse` to reach content in `work`.

## Require a user

To refuse anonymous requests, call `ctx.require_user()`, which raises {class}`~ploneasgi.context.NotAuthenticated`.
Catch it around the `run_authenticated` call and answer `401`; uncaught, it becomes a `500`.
To check another permission on an object, call `ctx.check_permission("Modify portal content", obj)` with the permission's title.

## Do not write

Every call ends in an abort, so anything `work` changes is discarded.
Native routes are read-only.

## Related

- {doc}`/concepts/security` explains what the context sets up and tears down.
- {doc}`add-a-native-route` registers the endpoint.
- {doc}`test-native-routes` tests it against a real site.
- {ref}`reference-api-context` documents the module.

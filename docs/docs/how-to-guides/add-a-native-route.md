---
myst:
  html_meta:
    "description": "Register an HTTP or websocket endpoint beside Plone, from ZCML or from server.py."
    "property=og:description": "Register an HTTP or websocket endpoint beside Plone, from ZCML or from server.py."
    "property=og:title": "Add a native route"
---

(how-to-add-a-native-route)=

# Add a native route

<!-- source: src/ploneasgi/zcml.py, src/ploneasgi/app.py -->

This guide shows you how to serve an ASGI endpoint from the same process as Plone.
Register it in ZCML when it belongs to an add-on, or pass it to `create_app` when it belongs to the project.

## Write the endpoint

An HTTP endpoint registered in ZCML is a sync function.
It takes a {class}`~ploneasgi.context.PloneContext` and a Starlette `Request`, and returns a Starlette `Response`:

```python
from starlette.responses import JSONResponse


def status(ctx, request):
    return JSONResponse({"ok": True, "site": ctx.portal.getId()})
```

It runs in a worker thread, after the permission check and in the same context.
To read Plone content from it, see {doc}`read-content-from-a-route`.

A websocket endpoint is async, and takes a `WebSocket`:

```python
async def echo(websocket):
    await websocket.accept()
    async for message in websocket.iter_text():
        await websocket.send_text(message)
```

## Register it in ZCML

Declare the route in a ZCML file that Zope loads, for example your add-on's `configure.zcml`:

```xml
<configure
    xmlns="http://namespaces.zope.org/zope"
    xmlns:ploneasgi="http://namespaces.plone.org/ploneasgi"
    >

  <include package="ploneasgi" file="meta.zcml" />

  <ploneasgi:route
      path="/status"
      handler=".endpoints.status"
      permission="zope2.View"
      />

  <ploneasgi:route
      path="/echo"
      handler=".endpoints.echo"
      type="websocket"
      permission="cmf.ManagePortal"
      />

</configure>
```

Give every route a permission id.
For an endpoint meant to be open to anyone, write `unguarded="true"` instead.

Then make sure the project's `server.py` collects ZCML routes after Zope starts:

```python
wsgi_app = build_wsgi_app(settings)
app = create_app(
    settings,
    routes=routes_from_registry(prefix=settings.route_prefix),
    wsgi_app=wsgi_app,
)
```

ZCML changes take effect when you restart the server.

## Pass it to `create_app`

A project can pass Starlette routes directly.
These are plain Starlette endpoints, async, with no context of their own:

```python
async def ping(request):
    return JSONResponse({"ok": True})
```

Mount them under the reserved prefix, so the proxy rule serves them:

```python
from starlette.routing import Route
from starlette.routing import WebSocketRoute

prefix = settings.route_prefix
app = create_app(
    settings,
    routes=[
        Route(f"{prefix}/ping", ping),
        WebSocketRoute(f"{prefix}/echo", echo),
    ],
)
```

A route passed this way has no permission guard.
Check authorization in the endpoint, with {func}`~ploneasgi.context.run_authenticated`.
Each call is a thread hop and a ZODB connection, so do the check and the work in one call.

## Check it

Without a proxy, request the route under the prefix:

```shell
curl http://127.0.0.1:8080/++asgi++/status
```

A ZCML route that answers `403` is working: the request did not have the permission.
Send credentials that do, for example with `curl -u admin:<password>`.

## Related

- {doc}`/reference/route-directive` lists every attribute.
- {doc}`/concepts/native-routes` explains the prefix and site resolution.
- {doc}`/concepts/security` explains the permission guard.
- {doc}`run-behind-a-proxy` serves the route through your proxy.

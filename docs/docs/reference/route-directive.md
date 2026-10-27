---
myst:
  html_meta:
    "description": "Attributes and behavior of the ploneasgi:route ZCML directive."
    "property=og:description": "Attributes and behavior of the ploneasgi:route ZCML directive."
    "property=og:title": "The route directive"
---

(reference-route-directive)=

# The route directive

<!-- source: src/ploneasgi/zcml.py, src/ploneasgi/meta.zcml, src/ploneasgi/app.py -->

`<ploneasgi:route />` registers a native ASGI route from ZCML.

## Namespace and declaration

| Item | Value |
| --- | --- |
| Namespace | `http://namespaces.plone.org/ploneasgi` |
| Directive | `route` |
| Declared in | `ploneasgi/meta.zcml` |
| Handler | {func}`ploneasgi.zcml.routeDirective` |

Include the declaration before any file that uses the directive:

```xml
<include package="ploneasgi" file="meta.zcml" />
```

## Attributes

| Attribute | Required | Default | Meaning |
| --- | --- | --- | --- |
| `path` | yes | | Starlette path pattern, relative to the reserved prefix. It must start with `/`. |
| `handler` | yes | | Dotted name of the endpoint: a sync function for `http`, an async function for `websocket`. Relative names such as `.endpoints.notify` resolve against the package of the ZCML file. |
| `type` | no | `http` | `http` or `websocket`. |
| `methods` | no | `GET` | Space-separated HTTP methods. Ignored for websocket routes. |
| `permission` | no | | Id of the permission required on the portal, for example `zope2.View` or `cmf.ManagePortal`. |
| `site` | no | | Site id used when a request carries no VirtualHostMonster rewrite. |
| `name` | no | the path | Route name, as `url_for` resolves it. |
| `unguarded` | no | `false` | Must be `true` when `permission` is omitted. |

## Behavior

Mounting
:   A route declared with `path="/notify"` is served at the reserved prefix plus the path: `/++asgi++/notify` by default.

Handler call
:   An `http` handler is called as `handler(ctx, request)` in a worker thread, where `ctx` is a {class}`~ploneasgi.context.PloneContext` and `request` the Starlette `Request`.
    It returns a Starlette `Response`.
    The permission check and the handler share one {func}`~ploneasgi.context.run_authenticated` call: one ZODB connection, one authentication, and one snapshot per request.
    A `websocket` handler is called as `await handler(websocket)` on the event loop, after the check.

Response
:   The response is sent on the event loop after the worker thread and the ZODB connection are released.
    A `StreamingResponse` or `FileResponse` therefore holds no thread for the transfer.

Request body
:   The body is read on the event loop before the thread hop, and handed to `ctx.request`.
    It is held to the application's `max_body_size`, published by {func}`~ploneasgi.app.create_app` as `app.state.max_body_size`; a larger body gets `413 Request Entity Too Large` and the connection is closed.
    After the permission check, `ctx.request.processInputs()` fills `ctx.request.form` from the query string and the body.
    The Starlette `request.body()` cannot be awaited from the handler.

Permission check
:   The permission is checked against the portal, as the connecting user, before the handler runs.
    The id is resolved to its title through the `IPermission` utility.
    An `unguarded` route skips the check, but its handler still runs in an authenticated context.

Unknown permission
:   An id with no registered `IPermission` utility refuses every request, and the refusal is logged as an error.

Refusal
:   An `http` request that fails the check gets `403 Forbidden`.
    A websocket that fails the check is closed with code `1008` without being accepted.

Errors from the handler
:   {class}`~ploneasgi.context.NotAuthenticated` becomes `401 Unauthorized`.
    Zope's `Unauthorized`, raised by {meth}`~ploneasgi.context.PloneContext.traverse`, becomes `403 Forbidden`.
    Zope's `BadRequest`, raised by form parsing on a malformed body, becomes `400 Bad Request`.
    Any other exception propagates, and the server answers `500 Internal Server Error`.

Unknown site
:   A site path that enters no Plone site gets `404 Not Found` on `http`, and is refused on websocket.

## Configuration errors

ZCML execution fails at startup when:

- `type` is neither `http` nor `websocket`;
- `permission` is omitted and `unguarded` is not `true`;
- `path` does not start with `/`;
- two routes of the same `type` declare the same `path`;
- two routes declare the same `name`.

The conflict report names both file locations.

## Collection

Registrations go into the global component registry.
{func}`ploneasgi.zcml.routes_from_registry` returns them as Starlette routes, sorted by type and path.
{func}`ploneasgi.app.create_app` never calls it: the project must pass the result in `routes`.

## Related

- {doc}`/how-to-guides/add-a-native-route` registers one.
- {doc}`/concepts/native-routes` explains the prefix and the rewrite.
- {doc}`/concepts/security` explains the permission check.
- {ref}`reference-api-zcml` documents the module.

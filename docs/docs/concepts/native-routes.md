---
myst:
  html_meta:
    "description": "How native ASGI routes sit beside Zope: the reserved prefix, the VirtualHostMonster rewrite, site resolution, and explicit discovery."
    "property=og:description": "How native ASGI routes sit beside Zope: the reserved prefix, the VirtualHostMonster rewrite, site resolution, and explicit discovery."
    "property=og:title": "Native routes"
---

(concepts-native-routes)=

# Native routes

<!-- source: src/ploneasgi/vhm.py, src/ploneasgi/zcml.py, src/ploneasgi/context.py, src/ploneasgi/app.py -->

A native route is an ASGI endpoint served by the same process as Plone, beside the Zope mount rather than inside it.
Websockets are the obvious use, since a WSGI application cannot speak them at all.
Long-polling, server-sent events, and any endpoint that waits on something other than the database are the others.

## Beside Zope, not inside it

{func}`~ploneasgi.app.create_app` builds a Starlette application whose last route mounts Zope at `/`.
Every route you pass is matched first, and anything that matches none of them is Zope's.

That position has a cost.
A native route does not go through ZPublisher, so it gets no traversal, no site, no authentication, and no ZODB connection for free.
{func}`~ploneasgi.context.run_authenticated` exists to supply all four, in one tested place, so each endpoint does not derive its own.

## Two kinds of handler

An HTTP route registered in ZCML has a sync handler.
It runs in a worker thread, inside the context the route's permission was checked in, and returns a Starlette response.
The request does its Plone work in one thread hop, and the response, streamed or not, is sent on the event loop after the thread is released.

A websocket handler is async, and runs on the event loop.
A session lasts as long as the client stays connected, which no worker thread should wait out.
It reaches Plone, when it needs to, with its own calls to `run_authenticated`.

A route passed to `create_app` directly is a plain Starlette endpoint, async, and does the same.

## The proxy problem

A deployment already sends every request through a proxy rule that rewrites it for VirtualHostMonster:

```text
https://www.example.org/++api++/whatever
  -> /VirtualHostBase/https/www.example.org:443/Plone/++api++/VirtualHostRoot/whatever
```

A native route that did not understand that shape would need a proxy rule of its own, and every new route would need another.
Infrastructure would grow with the code, forever.

{class}`~ploneasgi.vhm.VirtualHostMiddleware` parses the rewrite itself instead.
It recovers the external scheme and host, the path to the virtual root, and the path the client asked for.
Your existing rule then serves native routes too, and adding one needs no proxy change at all.

## The reserved prefix

The rewrite alone cannot tell a native route from a Zope path.
The reserved prefix, `/++asgi++` by default, makes that unambiguous.

A request whose rewritten path starts with the prefix belongs to a native route.
The middleware rewrites its scope to describe the request as the client made it: the real path, the external scheme, the external host in the `Host` header, and the virtual root's path under the `ploneasgi.site` scope key.

Everything else is Zope's, and passes through **untouched**: the same scope object, with no key added and the `Host` header unchanged.
Zope's own VirtualHostMonster must see the path it was sent.
Rewriting Zope's path would corrupt every link Plone generates, which is worse than any routing bug.

The prefix follows Zope's `++namespace++` traversal convention, so no content id can collide with it.

## Which site a request enters

Registrations are process-wide, but the site a request enters is resolved per request, from the rewrite.
One registration therefore serves every site the process hosts, and no route path restates a site id.

The rewrite names the *virtual root*, which is not always the site:

- in the simplest deployment it is the site itself, `Plone`;
- a Volto deployment's API rule adds a traversal step, `Plone/++api++`;
- a folder published as the root makes it `Plone/folder`;
- a site kept in a Zope folder makes it `sites/Plone`.

{func}`~ploneasgi.context.run_authenticated` walks the path from the Zope root and enters the first Plone site it meets.
A path that enters no site, like `acl_users`, is refused rather than used as one.

Without a proxy, in development, there is no rewrite.
The `site` attribute of the route, or `PLONE_SITE_ID`, supplies the site instead.

`_vh_` segments, VirtualHostMonster's virtual path prefix, change what the root path means.
Native routes refuse them with `501` rather than guess.
A classic UI rule that adds `_vh_ClassicUI` therefore cannot serve native routes; an API rule without `_vh_` can.

## Explicit discovery

An add-on cannot edit the project's `server.py`, so routes can be declared in ZCML with `<ploneasgi:route />`.
Nothing scans for them.
The project calls {func}`~ploneasgi.zcml.routes_from_registry` and passes the result to `create_app`, so `create_app` still only ever sees a list someone handed it.

That is deliberate.
The moment `create_app` grows a registration framework, entry-point scanning, or a middleware registry, PasteDeploy has been reinvented with extra steps.

## Related

- {doc}`security` explains the permission check and the context a route runs in.
- {doc}`/how-to-guides/add-a-native-route` declares one.
- {doc}`/how-to-guides/run-behind-a-proxy` shows the proxy rule.
- {doc}`/reference/route-directive` lists the directive's attributes.

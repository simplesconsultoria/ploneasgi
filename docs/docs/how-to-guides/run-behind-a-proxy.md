---
myst:
  html_meta:
    "description": "Serve Plone and ploneasgi native routes behind Traefik or another reverse proxy with a VirtualHostMonster rewrite."
    "property=og:description": "Serve Plone and ploneasgi native routes behind Traefik or another reverse proxy with a VirtualHostMonster rewrite."
    "property=og:title": "Run behind a proxy"
---

(how-to-run-behind-a-proxy)=

# Run behind a proxy

<!-- source: src/ploneasgi/vhm.py -->

This guide shows you how to put ploneasgi behind a reverse proxy, so that Plone and native routes share one rule.

## Keep the rewrite you have

A proxy for Plone rewrites each request into a VirtualHostMonster path.
ploneasgi reads the same rewrite, so the rule that already serves Plone also serves native routes.
You need no rule per route.

For a Volto deployment, the API rule in Traefik labels looks like this:

```yaml
labels:
  - traefik.http.services.svc-backend.loadbalancer.server.port=8080
  - "traefik.http.middlewares.mw-backend-vhm-api.replacepathregex.regex=^/\\+\\+api\\+\\+($$|/.*)"
  - "traefik.http.middlewares.mw-backend-vhm-api.replacepathregex.replacement=/VirtualHostBase/https/www.example.org/Plone/++api++/VirtualHostRoot$$1"
  - traefik.http.routers.rt-backend-api.rule=Host(`www.example.org`) && PathPrefix(`/++api++`)
  - traefik.http.routers.rt-backend-api.service=svc-backend
  - traefik.http.routers.rt-backend-api.middlewares=mw-backend-vhm-api
```

With it, a route registered as `/hello` is served at:

```text
https://www.example.org/++api++/++asgi++/hello
```

The route enters the `Plone` site, which ploneasgi takes from the rewrite.

## Serve a site without `++api++`

If your proxy maps the site root directly, a rewrite such as this one serves native routes at `https://www.example.org/++asgi++/hello`:

```text
/VirtualHostBase/https/www.example.org:443/Plone/VirtualHostRoot/<path>
```

## Avoid `_vh_` rules for native routes

A rule that adds a `_vh_` segment, such as the classic UI rule `/VirtualHostRoot/_vh_ClassicUI`, answers native routes with `501 Not Implemented`.
Reach native routes through a rule without `_vh_`, such as the API rule above.

## Proxy websockets

Traefik forwards the websocket upgrade on an ordinary HTTP router, so a websocket route needs no extra rule.
nginx forwards it only when the `location` sets the `Upgrade` and `Connection` headers with `proxy_set_header`.
A proxy with a short idle timeout closes quiet sockets; raise it for long-lived connections.

## Check the result

Request a route through the proxy, and check that it answers:

```shell
curl https://www.example.org/++api++/++asgi++/hello
```

A `404` that names no site means the rewrite names a path that enters no Plone site.
Check the site id in the replacement.

## Related

- {doc}`/concepts/native-routes` explains how the rewrite is parsed.
- {doc}`add-a-native-route` registers the route used above.
- {ref}`reference-api-vhm` documents the middleware.

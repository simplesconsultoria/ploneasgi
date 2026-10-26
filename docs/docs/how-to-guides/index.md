---
myst:
  html_meta:
    "description": "How-to guides for ploneasgi: migrating from waitress, proxies, native routes, concurrency, testing, and benchmarks."
    "property=og:description": "How-to guides for ploneasgi: migrating from waitress, proxies, native routes, concurrency, testing, and benchmarks."
    "property=og:title": "How-to guides"
    "keywords": "Plone, ploneasgi, ASGI, how-to, uvicorn, waitress"
---

(how-to-guides-index)=

# How-to guides

These guides solve one task each, for someone who already runs Plone.

```{toctree}
:maxdepth: 1
:hidden: true

migrate-from-waitress
run-behind-a-proxy
add-a-native-route
read-content-from-a-route
tune-concurrency
test-native-routes
reproduce-the-benchmarks
develop-ploneasgi
```

## Deploy

`````{grid} 1 1 3 3
:gutter: 3

````{grid-item-card} 🚚 Migrate from waitress
:link: migrate-from-waitress
:link-type: doc

Replace `runwsgi` and `zope.ini` with a server script and environment variables.
````

````{grid-item-card} 🌐 Run behind a proxy
:link: run-behind-a-proxy
:link-type: doc

Serve Plone and native routes through one VirtualHostMonster rule.
````

````{grid-item-card} 🎛️ Tune concurrency
:link: tune-concurrency
:link-type: doc

Size workers, the ZODB pool, the body-read limiter, and the blob cache.
````
`````

## Build

`````{grid} 1 1 3 3
:gutter: 3

````{grid-item-card} ➕ Add a native route
:link: add-a-native-route
:link-type: doc

Register an HTTP or websocket endpoint, from ZCML or from `server.py`.
````

````{grid-item-card} 📄 Read content from a route
:link: read-content-from-a-route
:link-type: doc

Reach Plone content as the requesting user.
````

````{grid-item-card} 🧪 Test native routes
:link: test-native-routes
:link-type: doc

Test routes against a real site with the functional layer.
````
`````

## Contribute

`````{grid} 1 1 2 2
:gutter: 3

````{grid-item-card} 📊 Reproduce the benchmarks
:link: reproduce-the-benchmarks
:link-type: doc

Run the starvation gate, the throughput comparison, and the storage matrix.
````

````{grid-item-card} 🧰 Develop ploneasgi
:link: develop-ploneasgi
:link-type: doc

Set up a checkout, run the checks, build the docs, and add a changelog entry.
````
`````

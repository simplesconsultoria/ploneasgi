---
myst:
  html_meta:
    "description": "Run Plone under an ASGI server, without spending a worker thread on a client's download."
    "property=og:description": "Run Plone under an ASGI server, without spending a worker thread on a client's download."
    "property=og:title": "ploneasgi"
    "keywords": "Plone, ploneasgi, ASGI, WSGI, uvicorn, Starlette, waitress, streaming"
---

# ploneasgi

ploneasgi runs Plone under an ASGI server, with no PasteDeploy `.ini` file, and without spending a worker thread on a client's download.

Under WSGI, a large download holds a worker thread for the duration of the client's transfer, not the duration of the work.
Four slow clients take a four-thread site down.
ploneasgi releases the thread when Zope returns, and streams the file from the event loop.

| Server | Concurrent slow downloads | Page requests answered |
| --- | ---: | ---: |
| waitress | 4 | 0 / 12 |
| uvicorn and ploneasgi | 16 | 12 / 12 |

**Status:** a spike, and the API will change.
It requires Python 3.14 and is tested with Plone 6.2.2, on FileStorage, ZEO, RelStorage, and PGJsonb.

```{warning}
This documentation was written with Claude Opus 5.5, following the [Plone documentation style skill](https://github.com/plone/plone-doc-style-skill), and reviewed by a human being.
```

## Where to start

`````{grid} 1 1 2 2
:gutter: 3

````{grid-item-card} 🎓 Tutorial
:link: tutorials/serve-a-plone-site
:link-type: doc

Create a site, serve it under uvicorn, and add a native route.
+++
Start here if the package is new to you.
````

````{grid-item-card} 🛠️ How-to guides
:link: how-to-guides/index
:link-type: doc

Migrate from waitress, run behind a proxy, add routes, tune concurrency.
+++
Directions toward a result.
````

````{grid-item-card} 💡 Concepts
:link: concepts/index
:link-type: doc

Why ASGI, how the adapter splits work from transfer, how routes stay secure.
+++
Understanding, away from the keyboard.
````

````{grid-item-card} 📖 Reference
:link: reference/index
:link-type: doc

Settings, the route directive, the access log format, and the Python API.
+++
Technical description of the machinery.
````
`````

## What it gives you

**Downloads that hold no thread.**
A file streams from the event loop after Zope returns, so slow clients wait on the network, not on a worker.

**No `zope.ini`.**
A short `server.py` and environment variables replace PasteDeploy, waitress, and `runwsgi`.

**Native routes beside Plone.**
Websockets and other ASGI endpoints run in the same process, authenticated through Plone's own PAS, and guarded by a permission.

**One proxy rule.**
Native routes understand the VirtualHostMonster rewrite your proxy already sends, so a new route needs no infrastructure change.

**An access log that sees everything.**
Native routes and websockets included, with the time to first byte kept apart from the transfer.

## What is not in scope

Writing to the database from native routes, Windows, and any registration framework for discovering routes.
{doc}`concepts/native-routes` explains why discovery stays explicit.

```{toctree}
:maxdepth: 2
:hidden: true

tutorials/index
how-to-guides/index
concepts/index
reference/index
```

```{toctree}
:caption: Appendices
:maxdepth: 2
:hidden: true

glossary
genindex
```

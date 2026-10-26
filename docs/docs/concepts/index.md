---
myst:
  html_meta:
    "description": "Explanation of ploneasgi: why Plone needs ASGI, how the adapter works, native routes, their security, and networked storages."
    "property=og:description": "Explanation of ploneasgi: why Plone needs ASGI, how the adapter works, native routes, their security, and networked storages."
    "property=og:title": "Concepts"
    "keywords": "Plone, ploneasgi, ASGI, WSGI, concepts, explanation"
---

(concepts-index)=

# Concepts

This part of the documentation explains why ploneasgi is built the way it is.
Read it away from the keyboard; nothing here asks you to type anything.

```{toctree}
:maxdepth: 1
:hidden: true

why-asgi
the-adapter
native-routes
security
storages
```

`````{grid} 1 1 2 2
:gutter: 3

````{grid-item-card} 🐢 Why ASGI
:link: why-asgi
:link-type: doc

Why a download holds a WSGI thread for the client's whole transfer, and what changes under ASGI.
````

````{grid-item-card} 🔀 The adapter
:link: the-adapter
:link-type: doc

How work and transfer are split: two limiters, streamed bodies, disconnects, and buffered uploads.
````

````{grid-item-card} 🛣️ Native routes
:link: native-routes
:link-type: doc

Endpoints beside Zope: the reserved prefix, the VirtualHostMonster rewrite, and which site a request enters.
````

````{grid-item-card} 🔐 Security of native routes
:link: security
:link-type: doc

Permission ids, authentication through PAS, restricted traversal, and what is torn down after each call.
````

````{grid-item-card} 🗄️ Blobs on networked storages
:link: storages
:link-type: doc

Where a blob fetch runs on ZEO, RelStorage, and PGJsonb, and what it costs a worker.
````
`````

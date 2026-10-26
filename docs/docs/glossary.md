---
myst:
  html_meta:
    "description": "Terms used throughout the ploneasgi documentation."
    "property=og:description": "Terms used throughout the ploneasgi documentation."
    "property=og:title": "Glossary"
---

(glossary-label)=

# Glossary

```{glossary}
:sorted: true

ASGI
    The Asynchronous Server Gateway Interface, the [interface](https://asgi.readthedocs.io/en/latest/) between async Python web servers and applications.
    uvicorn is an ASGI server; Starlette is an ASGI framework.

WSGI
    The Web Server Gateway Interface, defined in [PEP 3333](https://peps.python.org/pep-3333/).
    A WSGI application is a blocking call that returns an iterable of bytes.
    Zope is a WSGI application.

waitress
    The WSGI server Plone uses by default, started by `runwsgi` from `zope.ini`.

PasteDeploy
    The library that reads `zope.ini` and builds the WSGI pipeline from it.
    ploneasgi replaces it with a server script and environment variables.

VirtualHostMonster
VHM
    The Zope object that rewrites URLs for a public host.
    A proxy sends it a path such as `/VirtualHostBase/https/www.example.org:443/Plone/VirtualHostRoot/`.

native route
    An ASGI endpoint served beside Zope, under the reserved prefix, rather than published by ZPublisher.

reserved prefix
    The path prefix, `/++asgi++` by default, under which native routes are served.

stream iterator
    An object providing `ZPublisher.Iterators.IStreamIterator`, such as `filestream_iterator`.
    Zope returns one for a file download, and closes the ZODB connection before it is read.

limiter
    An anyio capacity limiter: a bound on how many calls run in worker threads at once.
    ploneasgi keeps one for WSGI calls and native routes, and another for body reads.

worker thread
    A thread from a limiter's pool, in which blocking work runs off the event loop.

event loop
    The single thread that an ASGI server uses to wait on every socket at once.

PAS
    The Pluggable Authentication Service, Plone's user folder, `acl_users`.

ZODB
    The Zope Object Database, where Plone keeps its content.

blob
    A file stored outside the ZODB's object records, read from a blob directory or a blob cache.

Plone
    [Plone](https://plone.org/) is an open-source content management system.
```

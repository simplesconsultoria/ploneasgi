---
myst:
  html_meta:
    "description": "Technical reference for ploneasgi: settings, the route directive, the access log format, and the Python API."
    "property=og:description": "Technical reference for ploneasgi: settings, the route directive, the access log format, and the Python API."
    "property=og:title": "Reference"
    "keywords": "Plone, ploneasgi, ASGI, reference, API, settings, ZCML"
---

(reference-index)=

# Reference

This part of the documentation describes the machinery: what each setting, attribute, field, and function is, and nothing else.

```{toctree}
:maxdepth: 1
:hidden: true

settings
route-directive
access-log
api/app
api/settings
api/wsgi
api/accesslog
api/vhm
api/zcml
api/context
api/zopeapp
api/testing
```

## Configuration and formats

`````{grid} 1 1 3 3
:gutter: 3

````{grid-item-card} ⚙️ Settings
:link: settings
:link-type: doc

Every environment variable, its default, and its meaning.
````

````{grid-item-card} 🧭 The route directive
:link: route-directive
:link-type: doc

Attributes, behavior, and configuration errors of `<ploneasgi:route />`.
````

````{grid-item-card} 📜 Access log format
:link: access-log
:link-type: doc

Fields, notes, and options of the access log.
````
`````

## Python API

Every module but {mod}`ploneasgi.testing` imports without Plone installed.

### Composing and serving

```{eval-rst}
.. currentmodule:: ploneasgi

.. autosummary::

   app.create_app
   app.serve
   app.default_adapter
   settings.Settings
   settings.load_env
   settings.setup_logging
   zopeapp.build_wsgi_app
   zopeapp.check_concurrency_contract
   zopeapp.lifespan
```

### Serving Zope

```{eval-rst}
.. currentmodule:: ploneasgi

.. autosummary::

   wsgi.WSGIMiddleware
   wsgi.build_environ
   accesslog.AccessLogMiddleware
```

### Native routes

```{eval-rst}
.. currentmodule:: ploneasgi

.. autosummary::

   vhm.VirtualHostMiddleware
   vhm.parse_virtual_host
   vhm.VirtualHost
   zcml.routes_from_registry
   zcml.build_route
   zcml.ASGIRoute
   context.run_authenticated
   context.limiter_for
   context.PloneContext
   context.default_db
```

### Testing

```{eval-rst}
.. currentmodule:: ploneasgi

.. autosummary::

   testing.FUNCTIONAL_TESTING
   testing.INTEGRATION_TESTING
   testing.PLONEASGI_FIXTURE
```

The module pages document every public name, one page per module:
{ref}`reference-api-app`, {ref}`reference-api-settings`, {ref}`reference-api-wsgi`, {ref}`reference-api-accesslog`, {ref}`reference-api-vhm`, {ref}`reference-api-zcml`, {ref}`reference-api-context`, {ref}`reference-api-zopeapp`, and {ref}`reference-api-testing`.

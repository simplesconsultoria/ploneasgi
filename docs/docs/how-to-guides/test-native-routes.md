---
myst:
  html_meta:
    "description": "Test native routes and code that uses run_authenticated against a real Plone site with the ploneasgi test layers."
    "property=og:description": "Test native routes and code that uses run_authenticated against a real Plone site with the ploneasgi test layers."
    "property=og:title": "Test native routes"
---

(how-to-test-native-routes)=

# Test native routes

<!-- source: src/ploneasgi/testing.py, tests/plone/ -->

This guide shows you how to test native routes against a real Plone site, with pytest and `pytest-plone`.

## Install the test layer

Add the `testing` extra to your test dependencies:

```shell
uv add --group test "ploneasgi[testing] @ git+https://github.com/simplesconsultoria/ploneasgi" pytest-plone
```

## Use the functional layer

A native route reaches Plone from a worker thread, on a second ZODB connection.
That connection sees only what has been committed, so use {data}`~ploneasgi.testing.FUNCTIONAL_TESTING`, and commit in the fixture:

```python
from ploneasgi import context
from ploneasgi.testing import FUNCTIONAL_TESTING
from pytest_plone import fixtures_factory

import pytest
import transaction


globals().update(fixtures_factory(((FUNCTIONAL_TESTING, "functional"),)))


@pytest.fixture
def portal(functional_portal):
    transaction.commit()
    yield functional_portal


@pytest.fixture
def layer_db(portal, monkeypatch):
    monkeypatch.setattr(context, "default_db", lambda: portal._p_jar.db())
```

`layer_db` points {func}`~ploneasgi.context.run_authenticated` at the test database.
Without it, the worker opens `Zope2.DB`, which is not the layer's.

To base your own layer on ploneasgi's, use {data}`~ploneasgi.testing.PLONEASGI_FIXTURE` in its `defaultBases`.

## Call the route

Build the route from a registration and call its endpoint with a raw ASGI scope:

```python
from ploneasgi.zcml import ASGIRoute
from ploneasgi.zcml import build_route
from starlette.requests import Request

import asyncio


async def receive():
    return {"type": "http.request", "body": b""}


def scope(path, headers=()):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 50000),
    }


@pytest.mark.usefixtures("layer_db")
def test_anonymous_is_refused(portal):
    route = ASGIRoute(
        path="/status",
        handler=status,
        permission="cmf.ManagePortal",
        site=portal.getId(),
    )
    endpoint = build_route(route).endpoint
    response = asyncio.run(endpoint(Request(scope("/++asgi++/status"), receive)))
    assert response.status_code == 403
```

The endpoint reads the request body before it calls the handler, so the `Request` needs a `receive`; this one delivers an empty body.
Send a Basic `authorization` header in `headers` to test as a user.

## Test both sides of a permission

A guard that refuses everyone passes every test that expects a refusal.
For each route, test a user who must pass, and a user who must not.

## Check against a running server

Unit tests do not cover everything a running server does.
Before you rely on a route that touches authentication or traversal, request it from a running instance with real credentials.

## Related

- {doc}`/concepts/security` explains what the guard checks.
- {doc}`read-content-from-a-route` writes the code under test.
- {ref}`reference-api-testing` documents the layers.

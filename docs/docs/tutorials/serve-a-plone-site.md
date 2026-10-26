---
myst:
  html_meta:
    "description": "Create a Plone site, serve it under uvicorn with ploneasgi, and add a first native route."
    "property=og:description": "Create a Plone site, serve it under uvicorn with ploneasgi, and add a first native route."
    "property=og:title": "Serve a Plone site with ploneasgi"
---

(tutorials-serve-a-site)=

# Serve a Plone site with ploneasgi

In this tutorial, you will create a Plone site, serve it under uvicorn with ploneasgi, and add a native route that tells you who is asking.
At the end, the same process will answer both a Plone page and a JSON endpoint that knows the logged-in user.

You need Python 3.14, [uv](https://docs.astral.sh/uv/), and `curl`.
The steps take about ten minutes, most of it installing Plone.

## Create the project

Create a new Python package and move into it:

```shell
uv init --package --python 3.14 my-site
cd my-site
```

Add ploneasgi and Plone's default content types:

```shell
uv add "ploneasgi[plone] @ git+https://github.com/simplesconsultoria/ploneasgi" plone.app.contenttypes
```

This downloads Plone, so it takes a few minutes.

## Write the route

Create `src/my_site/endpoints.py`:

```python
from ploneasgi import run_authenticated
from ploneasgi.vhm import SITE_KEY
from starlette.responses import JSONResponse


async def hello(request):
    def work(ctx):
        return {
            "user": ctx.user.getUserName(),
            "site": ctx.portal.Title(),
        }

    data = await run_authenticated(request.scope, work, site_id=request.scope[SITE_KEY])
    return JSONResponse(data)
```

Create `src/my_site/routes.zcml` to register it:

```xml
<configure xmlns:ploneasgi="http://namespaces.plone.org/ploneasgi">

  <ploneasgi:route
      path="/hello"
      handler="my_site.endpoints.hello"
      permission="zope2.View"
      />

</configure>
```

## Generate the instance

Generate a Zope instance that loads ploneasgi's directive and the routes:

```shell
uvx cookiecutter --no-input gh:plone/cookiecutter-zope-instance \
  target=instance \
  initial_user_name=admin \
  initial_user_password=letmein-plone \
  zcml_package_metas=ploneasgi \
  zcml_include_file_location=src/my_site/routes.zcml
```

You now have an `instance` folder, with the configuration in `instance/etc/zope.conf`.

## Create the site

Create `create_site.py`:

```python
from Products.CMFPlone.factory import addPloneSite
from Testing.makerequest import makerequest

import transaction


app = makerequest(app)  # noqa: F821 - zconsole binds `app`

if "Plone" not in app.objectIds():
    addPloneSite(
        app,
        "Plone",
        title="My site",
        extension_ids=("plone.app.contenttypes:default",),
        setup_content=True,
    )
    transaction.commit()
    print("Created the site at /Plone")
```

Run it:

```shell
uv run zconsole run instance/etc/zope.conf create_site.py
```

The terminal should print `Created the site at /Plone`.

## Write the server

Create `server.py`:

```python
from ploneasgi import create_app
from ploneasgi import load_env
from ploneasgi import serve
from ploneasgi import Settings
from ploneasgi.zcml import routes_from_registry
from ploneasgi.zopeapp import build_wsgi_app
from ploneasgi.zopeapp import check_concurrency_contract


load_env()
settings = Settings.from_env()

# Starting Zope executes the ZCML, which registers the routes collected below.
wsgi_app = build_wsgi_app(settings)
check_concurrency_contract(settings)

app = create_app(
    settings,
    routes=routes_from_registry(prefix=settings.route_prefix),
    wsgi_app=wsgi_app,
)

if __name__ == "__main__":
    serve(app, settings)
```

Start it, with the access log on:

```shell
PLONE_ACCESS_LOG=1 uv run python server.py
```

Notice the line `Uvicorn running on http://0.0.0.0:8080`.
Leave the server running, and open a second terminal in the same folder.

## Visit the site

Open `http://127.0.0.1:8080/Plone` in your browser.
You should see the front page of the site, titled {guilabel}`My site`, served by Plone under uvicorn.

Look at the first terminal.
Each request has written one access log line, with two timings after the status and size: the total duration, then the time to the first byte.

## Call the route

In the second terminal, call the route:

```shell
curl http://127.0.0.1:8080/++asgi++/hello
```

The answer is:

```json
{"user":"Anonymous User","site":"My site"}
```

The route entered the site and read its title, as an anonymous visitor.

Now call it as the administrator:

```shell
curl -u admin:letmein-plone http://127.0.0.1:8080/++asgi++/hello
```

This time the answer names `admin`.
The route authenticated the request through Plone, exactly as a Plone page would.

## Stop the server

Press {kbd}`Ctrl+C` in the first terminal.
The last lines of the log say `Closing ZODB`: the database closed cleanly.

You have served a Plone site under an ASGI server, and added an endpoint beside it that knows who is asking.

## Next steps

- {doc}`/concepts/why-asgi` explains why serving Plone this way matters.
- {doc}`/how-to-guides/add-a-native-route` adds websocket routes and other options.
- {doc}`/how-to-guides/run-behind-a-proxy` serves the site and the route on a public host.

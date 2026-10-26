---
myst:
  html_meta:
    "description": "Set up a ploneasgi checkout, run its tests and checks, build its documentation, and add a changelog entry."
    "property=og:description": "Set up a ploneasgi checkout, run its tests and checks, build its documentation, and add a changelog entry."
    "property=og:title": "Develop ploneasgi"
---

(how-to-develop-ploneasgi)=

# Develop ploneasgi

<!-- source: Makefile, docs/Makefile, AGENTS.md -->

This guide shows you how to work on ploneasgi itself.
You need Python 3.14 and uv.

## Set up the checkout

```shell
git clone https://github.com/simplesconsultoria/ploneasgi.git
cd ploneasgi
make install
```

`make install` installs the package and the test dependencies, without Plone.
To run the tests that need a Plone site, add it:

```shell
make install-plone
```

## Run the tests

```shell
make test
```

`make test` runs whatever the environment supports.
To run each half on its own, and assert the environment matches:

```shell
make test-no-plone
make test-plone
```

`make test-no-plone` fails if Plone is installed.
It proves that the modules that must stay free of Zope still import without it.

## Format and check

```shell
make format
make lint
```

`make format` rewrites files.
`make lint` only reads them, and is the target CI runs.

## Build the documentation

The documentation is its own uv project, in `docs/`, with its own environment:

```shell
make docs-install
make docs-html
make docs-vale
make docs-linkcheck
```

The pages are written to `docs/_build/html`.
To rebuild on every change and serve the result locally, run `make docs-livehtml`.

## Add a changelog entry

Every change that users notice needs a news fragment in `news/`:

```text
news/<issue-number>.<type>
news/+<slug>.<type>
```

Use `breaking`, `feature`, `bugfix`, `documentation`, `internal`, or `tests` as the type.
Write in the past tense, and end with your GitHub handle:

```text
Added a websocket example to the documentation. @your-handle
```

Preview the result with `make changelog`.

## Related

- {doc}`reproduce-the-benchmarks` runs the live-server gates.
- {doc}`test-native-routes` describes the test layers.

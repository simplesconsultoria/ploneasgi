# ploneasgi documentation

The source of the ploneasgi documentation, published at https://simplesconsultoria.github.io/ploneasgi/.

It is a separate uv project, built with Sphinx, MyST, and the [Plone Sphinx Theme](https://github.com/plone/plone-sphinx-theme), and organized with the [Diátaxis](https://diataxis.fr/) framework.
It installs `ploneasgi[testing]` from the checkout, Plone included, because the API pages import every module.

## Build it

From the repository root:

```shell
make docs-install
make docs-html
```

The pages are written to `docs/_build/html`.
To rebuild on every change and serve the result locally:

```shell
make docs-livehtml
```

## Check it

CI runs the same checks on every pull request:

```shell
make docs-vale
make docs-linkcheck
```

`docs-vale` fails on warnings and errors, not on suggestions.
Add project terms to `styles/config/vocabularies/Base/accept.txt`, one per line.

## Write it

- Sources live in `docs/`, one directory per Diátaxis quadrant: `tutorials`, `how-to-guides`, `concepts`, and `reference`.
- Write one sentence per line.
- Start each page with `html_meta` front matter, a label, and a single H1.
- End each page with a "Related" section.
- Reference pages describe only; they carry a `<!-- source: … -->` comment naming the code they describe.

`docs/conf.py` holds the Sphinx configuration.

## Publish it

`.github/workflows/docs.yml` builds the documentation on every pull request and push, and deploys `main` to GitHub Pages.
The repository's Pages source must be set to "GitHub Actions".

Generated from the `documentation_starter` template of [Cookieplone](https://github.com/plone/cookieplone).

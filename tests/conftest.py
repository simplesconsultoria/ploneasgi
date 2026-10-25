"""Collection rules for the two halves of the suite.

Everything directly under ``tests/`` runs with Plone absent — that is the
package's central structural claim and ``make test-no-plone`` enforces it.
``tests/plone/`` is the other half and needs a real site, so it is skipped
entirely when Plone is not installed rather than erroring at import time.

A silently skipped suite is the failure mode this package cares most about, so
``make test-plone`` asserts Plone *is* present before running. Neither half can
quietly cover nothing.
"""

from importlib.util import find_spec


def _installed(name: str) -> bool:
    """Whether a dotted module name can be located without importing it.

    ``find_spec`` imports the *parent* packages to find a submodule, so on a
    missing ``plone`` it raises rather than returning ``None`` — which is the
    whole point of asking, and would otherwise crash collection in exactly the
    Zope-free environment this guard exists to support.

    :param name: Dotted module name.
    :returns: ``True`` when the module can be located.
    """
    try:
        return find_spec(name) is not None
    except ModuleNotFoundError:
        return False


collect_ignore = [] if _installed("plone.app.testing") else ["plone"]

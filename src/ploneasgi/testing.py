"""Test layers for code that runs Plone under ASGI.

``ploneasgi`` is not an add-on: it ships no GenericSetup profile and installs
nothing into a site. The layer therefore does no ``applyProfile`` — it exists to
provide a *committed* Plone site that a worker thread can open a second
connection onto, which is the one thing this package's Plone-facing code needs
and cannot fake.

``plone.app.contenttypes`` is a base rather than a convenience: the security
assertions need real content to be denied access to, and Document is the
cheapest real content there is.

.. warning::

    Use :data:`FUNCTIONAL_TESTING` for anything that opens a connection from
    another thread. The integration layer aborts between tests instead of
    committing, so a second connection sees an *empty* site.

    A fixture that commits to work around this raises
    ``plone.testing.zope.TestIsolationBroken`` — loudly, which is the safe
    failure. The dangerous one is the test that needs no fixture: it passes
    under either layer while proving nothing about cross-thread visibility.

    :data:`INTEGRATION_TESTING` is kept for single-threaded tests, where it is
    considerably faster.

This module imports Plone at module scope, which the rest of ``ploneasgi``
deliberately does not. That is fine — it is an optional import path, installed
via the ``testing`` extra — but it means nothing in the package may import it.
"""

from plone.app.contenttypes.testing import PLONE_APP_CONTENTTYPES_FIXTURE
from plone.app.testing import FunctionalTesting
from plone.app.testing import IntegrationTesting
from plone.app.testing import PloneSandboxLayer
from plone.testing.zope import WSGI_SERVER_FIXTURE

import ploneasgi


class Layer(PloneSandboxLayer):
    """A plain Plone site with the ``<ploneasgi:route />`` directive available."""

    defaultBases = (PLONE_APP_CONTENTTYPES_FIXTURE,)

    def setUpZope(self, app, configurationContext):
        """Make the package's ZCML directive resolvable.

        Only ``meta.zcml`` — the package has no ``configure.zcml`` to load, and
        registering routes is the consuming project's job, not the layer's.

        :param app: The Zope application root.
        :param configurationContext: The ZCML configuration context.
        """
        self.loadZCML("meta.zcml", package=ploneasgi)


#: The shared fixture: a Plone site with ``plone.app.contenttypes`` and the
#: route directive's ``meta.zcml``. Base your own layers on it.
PLONEASGI_FIXTURE = Layer()


#: Single-threaded tests only. Aborts between tests, so a connection opened
#: from another thread sees an empty site.
INTEGRATION_TESTING = IntegrationTesting(
    bases=(PLONEASGI_FIXTURE,),
    name="PloneasgiLayer:IntegrationTesting",
)


# WSGI_SERVER_FIXTURE is included because the Plone-facing paths here have
# already been caught passing under unit tests while failing against a real
# server; a layer that cannot be driven over HTTP cannot check that.
#: Anything that reaches Plone from another thread: native routes,
#: :func:`~ploneasgi.context.run_authenticated`, a server driven over HTTP.
FUNCTIONAL_TESTING = FunctionalTesting(
    bases=(PLONEASGI_FIXTURE, WSGI_SERVER_FIXTURE),
    name="PloneasgiLayer:FunctionalTesting",
)

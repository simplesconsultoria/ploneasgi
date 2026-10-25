"""Fixtures for the half of the suite that needs a real Plone site.

One layer for the whole package: the *functional* one. Everything here opens a
second ZODB connection from a worker thread, and only a layer that commits lets
that connection see anything — see the warning in :mod:`ploneasgi.testing`.
Exposing the integration layer too would invite the mixed-scope teardown
failure the test pattern warns about, for no test that needs it.

Content and roles come from ``@pytest.mark.portal``, which pytest-plone applies
to ``functional_portal``. The marker does not commit, so :func:`portal` does.
"""

from . import basic_auth
from . import SITE_ADMIN
from collections.abc import Generator
from plone import api
from plone.app.testing import SITE_OWNER_NAME
from plone.app.testing import TEST_USER_NAME
from plone.app.testing import TEST_USER_PASSWORD
from ploneasgi import context
from ploneasgi.testing import FUNCTIONAL_TESTING
from pytest_plone import fixtures_factory

import pytest
import transaction


globals().update(fixtures_factory(((FUNCTIONAL_TESTING, "functional"),)))


@pytest.fixture
def portal(functional_portal) -> Generator:
    """The functional portal, with whatever the marker created *committed*.

    Function-scoped on purpose: these tests commit, and a class-scoped portal
    would carry one test's commits into the next.

    Checked rather than assumed, on the fixtures this one replaced (2026-08):
    binding the integration layer instead errored 5 of the 7 authorization
    tests with ``TestIsolationBroken`` — that layer aborts between tests, so
    committing is a violation — and left 2 passing vacuously.

    :param functional_portal: pytest-plone's portal, marker applied.
    :returns: The Plone site.
    """
    transaction.commit()
    yield functional_portal


@pytest.fixture
def db(portal):
    """The layer's database, as seen from the test thread.

    Passing this explicitly is what makes the cross-thread question testable at
    all: the worker thread must reach *this* storage, not a fresh one.

    :param portal: The Plone site.
    :returns: The ZODB database.
    """
    return portal._p_jar.db()


@pytest.fixture
def site_id(portal) -> str:
    """:returns: The id of the test portal."""
    return portal.getId()


@pytest.fixture
def layer_db(db, monkeypatch) -> None:
    """Make :func:`ploneasgi.context.default_db` return the layer's database.

    For code that calls ``run_authenticated`` without ``db=`` — the route
    guard does — this is the seam the function exists to provide.

    :param db: The layer's database.
    :param monkeypatch: pytest's patcher.
    """
    monkeypatch.setattr(context, "default_db", lambda: db)


@pytest.fixture
def test_user_auth() -> list[tuple[bytes, bytes]]:
    """Basic auth for the layer's test user.

    What that user may do is set by ``roles=`` on ``@pytest.mark.portal``.

    :returns: The header list.
    """
    return basic_auth(TEST_USER_NAME, TEST_USER_PASSWORD)


@pytest.fixture
def site_admin(portal) -> list[tuple[bytes, bytes]]:
    """A Site Administrator, committed so the worker's connection sees it.

    :param portal: The Plone site.
    :returns: Basic auth headers for that user.
    """
    username, password = SITE_ADMIN
    # Granting a role the caller lacks is refused, and the layer's user is a
    # Member. adopt_roles is not enough: the check reads the real user's roles.
    with api.env.adopt_user(SITE_OWNER_NAME):
        api.user.create(
            email="siteadmin@example.org",
            username=username,
            password=password,
            roles=["Site Administrator"],
        )
    transaction.commit()
    return basic_auth(username, password)

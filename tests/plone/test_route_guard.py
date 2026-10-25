"""The ``<ploneasgi:route />`` permission guard, against a real PAS.

``tests/test_zcml.py`` stubs :func:`ploneasgi.context.run_authenticated`, so it
proves the guard *asks*; only a real site proves the answer is right. ZCML names
permissions by id (``zope2.View``) while Zope checks them by title (``View``),
and a guard that skips the translation still looks correct for Manager: Zope
treats an unknown permission as Manager-only. So does ``cmf.ManagePortal``,
which *is* Manager-only in Plone. The tests therefore use guards a non-Manager
should pass, and keep the ones they must not pass beside them.
"""

from . import basic_auth
from .. import asgi_scope
from ploneasgi.zcml import ASGIRoute
from ploneasgi.zcml import build_route
from starlette.requests import Request

import asyncio
import pytest


#: The guard calls ``run_authenticated`` without ``db=``.
pytestmark = pytest.mark.usefixtures("layer_db")


async def handler(request):
    """The endpoint behind the guard; reaching it is what the tests observe."""
    return "handled"


def guarded(permission: str, site: str, headers=None):
    """Call a guarded HTTP route and return what the endpoint produced.

    :param permission: The permission id the route declares.
    :param site: The site the route enters.
    :param headers: Raw request headers.
    :returns: ``"handled"``, or the refusal response.
    """
    route = ASGIRoute(path="/x", handler=handler, permission=permission, site=site)
    endpoint = build_route(route).endpoint
    scope = asgi_scope("/++asgi++/x", headers=headers or [])
    return asyncio.run(endpoint(Request(scope)))


class TestPermissionIds:
    def test_anonymous_passes_a_view_guard(self, site_id):
        """``zope2.View`` is granted to Anonymous on the site root."""
        assert guarded("zope2.View", site_id) == "handled"

    def test_anonymous_is_refused_by_a_manage_guard(self, site_id):
        assert guarded("cmf.ManagePortal", site_id).status_code == 403

    def test_site_administrator_passes_a_site_setup_guard(self, site_id, site_admin):
        """``Plone Site Setup: Overview`` is granted to Site Administrator.

        Without the id-to-title translation the id is unknown to Zope, which
        falls back to Manager-only and refuses this user.
        """
        guard = "plone.app.controlpanel.Overview"
        assert guarded(guard, site_id, site_admin) == "handled"

    def test_site_administrator_is_refused_by_a_manage_portal_guard(
        self, site_id, site_admin
    ):
        """The boundary: Plone grants ``Manage portal`` to Manager alone.

        CMFPlone's own ``testSiteAdminRole`` pins the same fact.
        """
        assert guarded("cmf.ManagePortal", site_id, site_admin).status_code == 403

    def test_the_site_owner_passes_a_manage_portal_guard(
        self, site_id, site_owner_name, site_owner_password
    ):
        headers = basic_auth(site_owner_name, site_owner_password)
        assert guarded("cmf.ManagePortal", site_id, headers) == "handled"

    def test_an_unknown_permission_id_refuses_everyone(
        self, site_id, site_owner_name, site_owner_password
    ):
        """A typo must not fall back to Manager-only; it must fail closed."""
        headers = basic_auth(site_owner_name, site_owner_password)
        result = guarded("cmf.NoSuchPermission", site_id, headers)
        assert result.status_code == 403

"""An authenticated Plone context, acquired from a worker thread.

These need a real Plone — PAS, a portal, a security manager — which is why they
sit under ``tests/plone/`` rather than in the Zope-free suite next door.

The security assertions are the point. The deleted ``blobs.py`` had equivalent
code and never got these tests; that is a large part of why it was deleted
rather than kept.
"""

from . import basic_auth
from . import PRIVATE_DOCUMENT
from .. import asgi_scope
from AccessControl import Unauthorized
from plone.app.testing import TEST_USER_NAME
from ploneasgi.context import NotAuthenticated
from ploneasgi.context import PloneContext
from ploneasgi.context import run_authenticated
from ploneasgi.context import SiteNotFound

import asyncio
import pytest


SECRET = PRIVATE_DOCUMENT["id"]


def ws_scope(headers=None) -> dict:
    """A websocket scope — the shape a native route most often receives.

    :param headers: Raw request headers.
    :returns: The scope mapping.
    """
    return asgi_scope("/ws", scope_type="websocket", headers=headers or [])


def run(scope, work, *, db, site_id):
    """Drive one context acquisition from a synchronous test.

    :param scope: The ASGI connection scope.
    :param work: The callable to run inside the context.
    :param db: The ZODB database to open.
    :param site_id: Path to the Plone site.
    :returns: Whatever ``work`` returns.
    """
    return asyncio.run(run_authenticated(scope, work, site_id=site_id, db=db))


class TestWorkerThreadSeesTheTestDatabase:
    """The open question the layer exists to answer: same DemoStorage or not?"""

    def test_reaches_the_same_portal(self, db, site_id):
        result = run(ws_scope(), lambda ctx: ctx.portal.getId(), db=db, site_id=site_id)
        assert result == site_id

    @pytest.mark.portal(content=[PRIVATE_DOCUMENT])
    def test_sees_content_committed_by_the_test_thread(self, db, site_id):
        """If this fails, the worker opened a different storage."""
        result = run(
            ws_scope(),
            lambda ctx: ctx.portal.unrestrictedTraverse(SECRET).Title(),
            db=db,
            site_id=site_id,
        )
        assert result == PRIVATE_DOCUMENT["title"]

    def test_runs_off_the_calling_thread(self, db, site_id):
        import threading

        caller = threading.get_ident()
        worker = run(
            ws_scope(), lambda ctx: threading.get_ident(), db=db, site_id=site_id
        )
        assert worker != caller


class TestAuthentication:
    """Credentials come from the scope and go through PAS, not through us."""

    def test_no_credentials_is_anonymous(self, db, site_id):
        assert run(ws_scope(), lambda ctx: ctx.is_anonymous, db=db, site_id=site_id)

    def test_basic_auth_authenticates(self, db, site_id, test_user_auth):
        name = run(
            ws_scope(test_user_auth),
            lambda ctx: ctx.user.getUserName(),
            db=db,
            site_id=site_id,
        )
        assert name == TEST_USER_NAME

    def test_a_zope_root_user_authenticates(
        self, db, site_id, site_owner_name, site_owner_password
    ):
        """Regression: the root admin is not in ``portal.acl_users``.

        The instance recipe creates ``admin`` in the *Zope root* user folder.
        Asking only the portal's PAS looks right and silently locks that account
        out of every native route — found by hand against a running server,
        because no test covered it.
        """
        name = run(
            ws_scope(basic_auth(site_owner_name, site_owner_password)),
            lambda ctx: ctx.user.getUserName(),
            db=db,
            site_id=site_id,
        )
        assert name == site_owner_name

    def test_bad_password_does_not_authenticate(self, db, site_id):
        assert run(
            ws_scope(basic_auth(TEST_USER_NAME, "wrong")),
            lambda ctx: ctx.is_anonymous,
            db=db,
            site_id=site_id,
        )

    def test_require_user_raises_for_anonymous(self, db, site_id):
        with pytest.raises(NotAuthenticated):
            run(ws_scope(), lambda ctx: ctx.require_user(), db=db, site_id=site_id)

    def test_require_user_returns_the_user(self, db, site_id, test_user_auth):
        user = run(
            ws_scope(test_user_auth),
            lambda ctx: ctx.require_user().getUserName(),
            db=db,
            site_id=site_id,
        )
        assert user == TEST_USER_NAME

    def test_works_for_http_scopes_too(self, db, site_id, test_user_auth):
        """The helper is not websocket-only; an http scope must behave the same."""
        name = run(
            asgi_scope("/x", headers=test_user_auth),
            lambda ctx: ctx.user.getUserName(),
            db=db,
            site_id=site_id,
        )
        assert name == TEST_USER_NAME


class TestSiteResolution:
    def test_an_unknown_site_raises_site_not_found(self, db):
        """The site path is client-supplied, so this is a 404 upstream."""
        with pytest.raises(SiteNotFound):
            run(ws_scope(), lambda ctx: None, db=db, site_id="no-such-site")

    def test_a_root_object_that_is_not_a_site_is_refused(self, db):
        """The site comes from the client-supplied rewrite path.

        Any id at the Zope root resolves, so ``acl_users`` must not be accepted
        as the "site" a route's permission is checked against.
        """
        with pytest.raises(SiteNotFound):
            run(ws_scope(), lambda ctx: None, db=db, site_id="acl_users")

    @pytest.mark.portal(content=[{"type": "Folder", "id": "vroot", "title": "V"}])
    def test_a_folder_inside_the_site_resolves_to_the_site(self, db, site_id):
        """A folder published as the virtual root still enters its Plone site."""
        result = run(
            ws_scope(),
            lambda ctx: ctx.portal.getId(),
            db=db,
            site_id=f"{site_id}/vroot",
        )
        assert result == site_id

    def test_segments_below_the_site_do_not_matter(self, db, site_id):
        """The walk stops at the site; what follows is the client's business.

        Native route paths are relative to the portal, so a virtual root below
        it changes nothing they can reach.
        """
        result = run(
            ws_scope(),
            lambda ctx: ctx.portal.getId(),
            db=db,
            site_id=f"{site_id}/nope",
        )
        assert result == site_id

    def test_a_missing_segment_above_the_site_is_not_found(self, db, site_id):
        with pytest.raises(SiteNotFound):
            run(ws_scope(), lambda ctx: None, db=db, site_id=f"sites/{site_id}")


@pytest.mark.portal(content=[PRIVATE_DOCUMENT], roles=["Manager"])
class TestAuthorization:
    """The assertions that make this safe to build endpoints on.

    The test user is a Manager here, so the only thing separating the two sides
    of each pair is whether the request carries credentials.
    """

    def test_anonymous_cannot_traverse_to_private_content(self, db, site_id):
        """The file-disclosure test. If this ever passes, stop shipping."""
        with pytest.raises(Unauthorized):
            run(
                ws_scope(),
                lambda ctx: ctx.traverse(SECRET).Title(),
                db=db,
                site_id=site_id,
            )

    def test_authenticated_manager_can_traverse_to_private_content(
        self, db, site_id, test_user_auth
    ):
        title = run(
            ws_scope(test_user_auth),
            lambda ctx: ctx.traverse(SECRET).Title(),
            db=db,
            site_id=site_id,
        )
        assert title == PRIVATE_DOCUMENT["title"]

    def test_check_permission_is_false_for_anonymous(self, db, site_id):
        allowed = run(
            ws_scope(),
            lambda ctx: ctx.check_permission(
                "View", ctx.portal.unrestrictedTraverse(SECRET)
            ),
            db=db,
            site_id=site_id,
        )
        assert allowed is False

    def test_check_permission_is_true_for_manager(self, db, site_id, test_user_auth):
        allowed = run(
            ws_scope(test_user_auth),
            lambda ctx: ctx.check_permission(
                "View", ctx.portal.unrestrictedTraverse(SECRET)
            ),
            db=db,
            site_id=site_id,
        )
        assert allowed is True


@pytest.mark.portal(content=[PRIVATE_DOCUMENT], roles=["Manager"])
class TestCatalogRead:
    """One authenticated catalog read that commits nothing."""

    def test_catalog_search_respects_the_users_permissions(
        self, db, site_id, test_user_auth
    ):
        def search(ctx):
            return len(ctx.portal.portal_catalog(portal_type="Document", id=SECRET))

        as_anonymous = run(ws_scope(), search, db=db, site_id=site_id)
        as_manager = run(ws_scope(test_user_auth), search, db=db, site_id=site_id)
        assert as_anonymous == 0
        assert as_manager == 1


def state_after(scope, work, *, db, site_id):
    """Run ``_run_sync`` in a dedicated thread and report that thread's state.

    Teardown has to be observed *in the worker thread*: both the current site
    (``zope.component.hooks``) and the security manager
    (``AccessControl.SecurityManagement``) are stored in thread-locals, so
    checking them from the test thread reads unrelated state — the layer's own
    site and logged-in user — and says nothing about whether the call cleaned up.

    Thread-locality bounds the blast radius of a leak: it cannot reach a
    concurrent request. It still has to be cleaned, because anyio *reuses* pool
    threads, so leftovers would land on the next unit of work scheduled there.

    :param scope: The ASGI connection scope.
    :param work: The callable to run inside the context.
    :param db: The ZODB database to open.
    :param site_id: Path to the Plone site.
    :returns: Mapping with the post-call ``site``, ``user`` and any ``error``.
    """
    from AccessControl.SecurityManagement import getSecurityManager
    from ploneasgi.context import _run_sync
    from zope.component.hooks import getSite

    import threading

    captured: dict = {}

    def target():
        try:
            _run_sync(scope, work, site_id, db)
        except Exception as exc:
            captured["error"] = exc
        captured["site"] = getSite()
        captured["user"] = getSecurityManager().getUser().getUserName()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    return captured


class TestTeardown:
    """A leaked security manager is a privilege-escalation bug, not a leak."""

    def test_security_manager_is_reset_in_the_worker_thread(
        self, db, site_id, test_user_auth
    ):
        state = state_after(
            ws_scope(test_user_auth),
            lambda ctx: ctx.user.getUserName(),
            db=db,
            site_id=site_id,
        )
        assert state["user"] != TEST_USER_NAME

    def test_site_is_cleared_in_the_worker_thread(self, db, site_id):
        state = state_after(ws_scope(), lambda ctx: None, db=db, site_id=site_id)
        assert state["site"] is None

    def test_exceptions_propagate_and_still_tear_down(self, db, site_id):
        def boom(ctx):
            raise ValueError("boom")

        # Propagation through the async wrapper.
        with pytest.raises(ValueError):
            run(ws_scope(), boom, db=db, site_id=site_id)

        # And the finally block still ran, observed where it happened.
        state = state_after(ws_scope(), boom, db=db, site_id=site_id)
        assert isinstance(state["error"], ValueError)
        assert state["site"] is None
        assert state["user"] != TEST_USER_NAME

    def test_a_reused_thread_does_not_inherit_the_previous_user(
        self, db, site_id, test_user_auth
    ):
        """anyio reuses pool threads, so this is the leak that would matter."""
        run(ws_scope(test_user_auth), lambda ctx: None, db=db, site_id=site_id)
        name = run(
            ws_scope(), lambda ctx: ctx.user.getUserName(), db=db, site_id=site_id
        )
        assert name != TEST_USER_NAME

    def test_writes_are_aborted(self, db, site_id, portal, test_user_auth):
        """A read-only endpoint must not become a writer by accident."""

        def mutate(ctx):
            ctx.portal.title = "Mutated by the worker"
            return True

        run(ws_scope(test_user_auth), mutate, db=db, site_id=site_id)
        portal._p_jar.sync()
        assert portal.title != "Mutated by the worker"


class TestContextObject:
    def test_is_frozen(self, db, site_id):
        def mutate(ctx):
            # frozen dataclass: rebinding the portal mid-request must not be a
            # way to smuggle a different site past the security setup.
            with pytest.raises(AttributeError):
                ctx.portal = None
            return True

        assert run(ws_scope(), mutate, db=db, site_id=site_id) is True

    def test_carries_the_request(self, db, site_id):
        method = run(
            ws_scope(),
            lambda ctx: ctx.request["REQUEST_METHOD"],
            db=db,
            site_id=site_id,
        )
        assert method == "GET"

    def test_is_a_plone_context(self, db, site_id):
        assert run(
            ws_scope(),
            lambda ctx: isinstance(ctx, PloneContext),
            db=db,
            site_id=site_id,
        )

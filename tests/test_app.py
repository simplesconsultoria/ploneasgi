"""Tests for application composition and the settings that feed it.

Zope-free: ``create_app`` is given a prebuilt WSGI app, so it never starts
Zope, and nothing here enters the lifespan.
"""

from ploneasgi import app as app_module
from ploneasgi.app import create_app
from ploneasgi.context import limiter_for
from ploneasgi.settings import Settings
from ploneasgi.wsgi import WSGIMiddleware
from starlette.routing import Mount
from types import SimpleNamespace

import anyio
import pytest


def wsgi_app(environ, start_response):  # pragma: no cover - never called
    start_response("200 OK", [])
    return [b""]


@pytest.fixture
def quiet(monkeypatch):
    """Keep ``create_app`` from reconfiguring logging under pytest.

    ``setup_logging`` uses ``force=True``, which would remove pytest's own
    capture handlers from the root logger.
    """
    monkeypatch.setattr(app_module, "setup_logging", lambda settings: None)


def mounted(app) -> object:
    """:returns: The ASGI app mounted at ``/`` — the WSGI adapter."""
    mounts = [route for route in app.routes if isinstance(route, Mount)]
    return mounts[-1].app


class TestZodbLimiter:
    def test_native_routes_share_the_adapters_limiter(self, quiet):
        """One budget for every thread that may hold a ZODB connection."""
        app = create_app(Settings(concurrency=3), wsgi_app=wsgi_app)

        adapter = mounted(app)
        assert isinstance(adapter, WSGIMiddleware)
        assert app.state.zodb_limiter is adapter.app_limiter
        assert app.state.zodb_limiter.total_tokens == 3

    def test_an_adapter_without_one_publishes_none(self, quiet):
        """a2wsgi has no app_limiter; native routes then use anyio's pool."""

        async def plain(scope, receive, send):  # pragma: no cover - never called
            return None

        app = create_app(
            Settings(), wsgi_app=wsgi_app, adapter=lambda wsgi, settings: plain
        )

        assert app.state.zodb_limiter is None

    def test_limiter_for_reads_it_off_the_scope(self):
        limiter = anyio.CapacityLimiter(2)
        app = SimpleNamespace(state=SimpleNamespace(zodb_limiter=limiter))

        assert limiter_for({"app": app}) is limiter

    @pytest.mark.parametrize(
        "scope",
        [{}, {"app": object()}, {"app": SimpleNamespace(state=SimpleNamespace())}],
        ids=["no-app", "no-state", "no-limiter"],
    )
    def test_limiter_for_is_none_when_absent(self, scope):
        assert limiter_for(scope) is None


class TestMaxBodySize:
    def test_defaults_to_waitress_limit(self, monkeypatch):
        monkeypatch.delenv("PLONE_MAX_REQUEST_BODY_SIZE", raising=False)
        assert Settings.from_env().max_body_size == 1024 * 1024 * 1024

    def test_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PLONE_MAX_REQUEST_BODY_SIZE", "2048")
        assert Settings.from_env().max_body_size == 2048

    def test_reaches_the_adapter(self, quiet):
        app = create_app(Settings(max_body_size=2048), wsgi_app=wsgi_app)
        assert mounted(app).max_body_size == 2048

    def test_zero_disables_it(self, quiet):
        app = create_app(Settings(max_body_size=0), wsgi_app=wsgi_app)
        assert mounted(app).max_body_size is None

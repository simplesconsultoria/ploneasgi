"""The Zope-facing half of :mod:`ploneasgi.zopeapp`.

These need Zope importable but not a running site, so they take no layer — they
substitute a stub for ``Zope2.DB``. That is deliberate rather than lazy:
:func:`ploneasgi.zopeapp.lifespan` *closes* the database, and closing the
layer's would take every other test in this directory with it.
"""

from ploneasgi.settings import Settings
from ploneasgi.zopeapp import check_concurrency_contract
from ploneasgi.zopeapp import lifespan

import asyncio
import logging
import pytest


class FakeDB:
    """Stands in for ``Zope2.DB``.

    :ivar pool_size: What :meth:`getPoolSize` reports.
    :ivar closed: Whether :meth:`close` was called.
    """

    def __init__(self, pool_size=7):
        self.pool_size = pool_size
        self.closed = False

    def getPoolSize(self):
        """The ZODB spelling, which is what the code under test calls."""
        return self.pool_size

    def close(self):
        self.closed = True


@pytest.fixture
def zope2(monkeypatch):
    """``Zope2`` with a stub database, restored on teardown.

    :param monkeypatch: pytest's patcher.
    :returns: The ``Zope2`` module.
    """
    import Zope2

    monkeypatch.setattr(Zope2, "DB", FakeDB(), raising=False)
    return Zope2


class TestConcurrencyContract:
    """More workers than ZODB connections means requests block, silently."""

    def test_warns_when_concurrency_exceeds_the_pool(self, zope2, caplog):
        zope2.DB.pool_size = 4

        with caplog.at_level(logging.WARNING, logger="ploneasgi.zopeapp"):
            check_concurrency_contract(Settings(concurrency=8))

        assert "exceeds ZODB pool-size" in caplog.text
        # The numbers have to be in the message: a warning that does not say
        # which two values disagree cannot be acted on.
        assert "8" in caplog.text
        assert "4" in caplog.text

    def test_silent_when_concurrency_fits(self, zope2, caplog):
        zope2.DB.pool_size = 8

        with caplog.at_level(logging.WARNING, logger="ploneasgi.zopeapp"):
            check_concurrency_contract(Settings(concurrency=4))

        assert caplog.text == ""

    def test_equal_is_within_contract(self, zope2, caplog):
        """The boundary. ``>`` not ``>=`` — a pool exactly the size of the
        worker count is the intended configuration, not a warning."""
        zope2.DB.pool_size = 4

        with caplog.at_level(logging.WARNING, logger="ploneasgi.zopeapp"):
            check_concurrency_contract(Settings(concurrency=4))

        assert caplog.text == ""

    def test_no_database_yet_is_not_an_error(self, zope2, caplog, monkeypatch):
        """Called before startup there is nothing to compare, and saying so
        with a traceback would be worse than saying nothing."""
        monkeypatch.setattr(zope2, "DB", None)

        with caplog.at_level(logging.WARNING, logger="ploneasgi.zopeapp"):
            check_concurrency_contract(Settings(concurrency=999))

        assert caplog.text == ""


class TestLifespan:
    """What ``runwsgi`` + waitress used to own: the orderly shutdown."""

    def test_closes_the_database_on_shutdown(self, zope2):
        async def cycle():
            async with lifespan(object()):
                assert zope2.DB.closed is False

        asyncio.run(cycle())
        assert zope2.DB.closed is True

    def test_closes_the_database_even_when_the_server_raises(self, zope2):
        """The close lives in a ``finally``; a crashing server must not leak
        the storage."""

        async def cycle():
            async with lifespan(object()):
                raise RuntimeError("server died")

        with pytest.raises(RuntimeError, match="server died"):
            asyncio.run(cycle())

        assert zope2.DB.closed is True

    def test_no_database_is_not_an_error(self, zope2, monkeypatch):
        monkeypatch.setattr(zope2, "DB", None)

        async def cycle():
            async with lifespan(object()):
                pass

        asyncio.run(cycle())  # must not raise

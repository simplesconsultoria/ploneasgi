"""The central claim against real Plone, not a stand-in.

``test_starvation.py`` proves the adapter on a hello-world app shaped like
Zope's stream iterators. That leaves one assumption untested: that Plone's own
``@@download`` actually hands the adapter an iterator, with the ZODB connection
already closed. These run the same categorical gate with Zope as the app — a
generated instance, a 50 MB blob, the page probe on the site root.

Marked ``bench_plone`` rather than ``bench``: they need Plone installed, which
``make bench-test`` does not, and building the site costs seconds per session.
``make bench-plone`` runs them.
"""

from . import PROBES
from . import WORKERS
from . import round_named
from . import starve_plone

import pytest


pytestmark = pytest.mark.bench_plone


class TestPloneasgiSurvivesSlowPloneDownloads:
    def test_serves_every_page_under_four_times_its_workers(self, plone_instance):
        rows = starve_plone("ploneasgi", WORKERS * 4, plone_instance)
        assert round_named(rows, "baseline")["ok"] == PROBES
        loaded = round_named(rows, "loaded")
        assert loaded["ok"] == PROBES, (
            f"{loaded['failed']} of {PROBES} Plone page probes failed under "
            f"{loaded['slow_clients']} slow downloads: {loaded.get('errors')}"
        )


class TestA2wsgiStarvesOnPlone:
    """The falsifier, on Plone. If this passes, the downloads are not slow
    enough to hold a worker, or Plone stopped streaming them — and the test
    above proves nothing."""

    def test_stops_answering_at_its_worker_count(self, plone_instance):
        rows = starve_plone("a2wsgi", WORKERS, plone_instance)
        assert round_named(rows, "baseline")["ok"] == PROBES, (
            "a2wsgi could not serve Plone pages even unloaded"
        )
        assert round_named(rows, "loaded")["ok"] == 0

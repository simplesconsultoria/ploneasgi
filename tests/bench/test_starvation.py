"""The package's central claim, as a test that can fail.

> Under WSGI a large download holds a worker thread for the duration of the
> *client's transfer*, not the duration of the work. Four slow clients take a
> four-thread site down.

Everything here is **categorical** — 12/12 versus 0/12 — which is the only kind
of performance assertion that belongs in a gate. Throughput is deliberately not
asserted anywhere: a MiB/s figure measured on whatever machine CI allocated is
noise dressed as a threshold. Use ``make bench-throughput`` for those, read the
spread, and put the number in a commit message rather than in an `assert`.

Two things are checked, and the second matters as much as the first:

1. ploneasgi keeps answering under four times its worker count in slow clients.
2. a2wsgi does **not**. If that ever stops failing, the comparison has become
   vacuous — the load is no longer slow enough, the body no longer large enough,
   or the app no longer streaming — and test 1 proves nothing.

Not asserted: where exactly a2wsgi's cliff falls. It was measured at precisely
the worker count (3 slow clients → 12/12, 4 → 0/12) and that is worth knowing,
but near-cliff behaviour on a shared CI runner is a flaky test, not a finding.

These need a live server on a real port and take tens of seconds, so they are
marked ``bench`` and deselected by default. ``make bench-test`` runs them.
"""

from . import round_named
from . import run_bench

import pytest


pytestmark = pytest.mark.bench

#: Concurrent WSGI calls the server allows. The number the claim is relative to.
WORKERS = 4

#: Every probe must answer within this. Well above the ~2 ms a healthy round
#: shows, and well below the "held until the download finishes" case, which at
#: 16 KB per 500 ms would take hours.
PROBE_TIMEOUT = 10.0

PROBES = 12


class TestPloneasgiSurvivesSlowClients:
    """Threads are released before the client's transfer, so pages still land."""

    def test_serves_every_page_under_four_times_its_workers(self):
        rows = run_bench(
            "starve",
            "ploneasgi",
            workers=WORKERS,
            slow_clients=WORKERS * 4,
            probes=PROBES,
            probe_timeout=PROBE_TIMEOUT,
        )
        loaded = round_named(rows, "loaded")

        assert loaded["slow_clients"] == WORKERS * 4
        assert loaded["ok"] == PROBES, (
            f"{loaded['failed']} of {PROBES} page probes failed under "
            f"{loaded['slow_clients']} slow clients: {loaded.get('errors')}"
        )

    def test_the_baseline_round_is_healthy(self):
        """Guards the guard.

        If the unloaded round were failing too, the loaded round would be
        measuring a broken server rather than starvation — and the a2wsgi test
        below would 'pass' for entirely the wrong reason.
        """
        rows = run_bench(
            "starve",
            "ploneasgi",
            workers=WORKERS,
            slow_clients=WORKERS,
            probes=PROBES,
            probe_timeout=PROBE_TIMEOUT,
        )
        baseline = round_named(rows, "baseline")

        assert baseline["slow_clients"] == 0
        assert baseline["ok"] == PROBES


class TestA2wsgiStarves:
    """The falsifier.

    a2wsgi iterates the response body *in the worker thread*, so a slow client
    holds one for its whole download. This is not a defect being mocked — it is
    the incumbent behaviour the package claims to fix, and pinning it here is
    what stops the test above from quietly becoming a tautology.
    """

    def test_stops_answering_at_its_worker_count(self):
        rows = run_bench(
            "starve",
            "a2wsgi",
            workers=WORKERS,
            slow_clients=WORKERS,
            probes=PROBES,
            probe_timeout=PROBE_TIMEOUT,
        )
        loaded = round_named(rows, "loaded")

        assert round_named(rows, "baseline")["ok"] == PROBES, (
            "a2wsgi could not serve pages even unloaded; this run measured "
            "something other than starvation"
        )
        assert loaded["ok"] == 0, (
            f"a2wsgi answered {loaded['ok']} of {PROBES} probes under "
            f"{WORKERS} slow clients. Either the load is no longer slow enough "
            f"to hold a worker thread, or the body is no longer streamed — "
            f"either way the ploneasgi assertions above are now vacuous."
        )

"""Native-route request shapes, measured against a running instance.

``run.py routes`` serves the same work behind two request shapes — one context
per request, as the ``<ploneasgi:route />`` wrapper runs a guarded handler, and
two, as it did before guard and handler shared a context — and drives both,
interleaved, on their own and beside page renders.

Only the categorical parts are asserted: every request of every arm and round
succeeded, and every round was measured. Which shape is faster, and by how
much, is a number from one machine; it belongs in a commit message with its
spread, never in an assertion.
"""

from . import run_bench
from . import WORKERS

import pytest


pytestmark = pytest.mark.bench_plone

ARMS = {"one-hop", "two-hops"}
ROUNDS = {"alone", "with-pages"}


@pytest.fixture(scope="module")
def rows(plone_instance) -> list[dict]:
    """One short routes run: two repetitions, so both arm orders occur.

    :param plone_instance: The instance directory.
    :returns: The summary rows.
    """
    return run_bench(
        "routes",
        "ploneasgi",
        workers=WORKERS,
        duration=2,
        reps=2,
        plone_instance=plone_instance,
    )


class TestRouteShapes:
    def test_every_arm_was_measured_in_every_round(self, rows):
        measured = [(row["arm"], row["round"], row["rep"]) for row in rows]
        expected = [(a, r, rep) for a in ARMS for r in ROUNDS for rep in (0, 1)]
        assert sorted(measured) == sorted(expected)

    def test_the_rows_come_from_plone(self, rows):
        assert {row["server"] for row in rows} == {"plone"}

    def test_every_route_request_succeeded(self, rows):
        failures = [
            (row["arm"], row["round"], row["errors"]) for row in rows if row["failed"]
        ]
        assert not failures
        assert all(row["ok"] > 0 for row in rows)

    def test_pages_kept_rendering_beside_the_routes(self, rows):
        beside = [row for row in rows if row["round"] == "with-pages"]
        assert all(row["page_req_s"] > 0 for row in beside)
        assert all(row["page_failed"] == 0 for row in beside)

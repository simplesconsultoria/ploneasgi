"""Helpers for the benchmark gate.

The benchmark is driven as a **subprocess**, exactly the way a human runs it.
Importing ``benchmarks/`` instead would need a ``sys.path`` insert — it is not
part of the installed package — and would leave the documented command untested
while a parallel in-process copy was the thing actually verified.
"""

from pathlib import Path

import json
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "benchmarks" / "run.py"

#: Generous. A starve round is roughly probes x interval, twice, plus a settle
#: pause and server start — but a *failing* round waits out every probe timeout,
#: which is the case that matters and the slow one.
SUBPROCESS_TIMEOUT = 300.0


def run_bench(mode: str, adapter: str, **options: object) -> list[dict]:
    """Run one benchmark mode against one adapter and return its rows.

    :param mode: ``starve``, ``throughput`` or ``capacity``.
    :param adapter: ``ploneasgi`` or ``a2wsgi``.
    :param options: Extra flags, as ``slow_clients=16`` for ``--slow-clients 16``.
    :returns: The parsed summary rows.
    :raises AssertionError: If the run fails, with the harness's own output —
        a benchmark that did not run must never be reported as a result.
    """
    argv = [sys.executable, str(RUNNER), mode, "--adapter", adapter, "--json"]
    for key, value in options.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]

    result = subprocess.run(  # noqa: S603
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, (
        f"benchmark run failed ({' '.join(argv)}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout)


def round_named(rows: list[dict], name: str) -> dict:
    """Pick one round out of a starve or capacity result.

    :param rows: Rows returned by :func:`run_bench`.
    :param name: ``baseline`` or ``loaded``.
    :returns: That round's summary.
    :raises AssertionError: If it is absent, rather than returning ``None`` and
        failing later on an attribute of it.
    """
    matching = [row for row in rows if row.get("round") == name]
    assert matching, f"no {name!r} round in {rows}"
    return matching[0]

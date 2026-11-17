"""Shared in-process relative-timing helpers for performance guard tests.

Timing is compared inside one process (growth between two input sizes, or
against a linear control), never against an absolute number of seconds: a
hosted runner or coverage tracing slows both sides alike, so the ratio stays
put (#104, #114).
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable

import pytest

# Doubling the input costs ~2x when linear and ~4x when quadratic; 3.0 sits
# between the two. A quadratic term trips it once it costs about twice the
# linear work at the larger size.
DOUBLING_MAX_RATIO = 3.0
_REPEATS = 5


def report(request: pytest.FixtureRequest, message: str) -> None:
    """Print once into the run output even when pytest captures stdout (E4)."""
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message)
    else:
        print(message)


def _seconds(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def time_ratio(
    subject: Callable[[], object],
    baseline: Callable[[], object],
) -> float:
    """Best *subject* time over best *baseline* time.

    The two sides are timed alternately, so a slow stretch on a shared runner
    hits both; each side's minimum filters the remaining scheduler noise.
    Collecting once up front and pausing the collector for the whole loop (as
    ``timeit`` does) keeps a full collection of the pytest session's objects
    from landing inside one side of the ratio, without paying a full
    collection per measurement.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        best_subject = best_baseline = float("inf")
        for _ in range(_REPEATS):
            best_subject = min(best_subject, _seconds(subject))
            best_baseline = min(best_baseline, _seconds(baseline))
    finally:
        if was_enabled:
            gc.enable()
    return best_subject / max(best_baseline, 1e-9)

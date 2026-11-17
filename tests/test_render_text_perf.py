"""#235, #289 (L3): CSS cleanup in render_text is linear on bodies & selector runs."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

import pytest

from mailattic.query import render_text
from tests._timing import DOUBLING_MAX_RATIO, report, time_ratio

_SENTENCE = "Please quote the price and shipment date. "


def _best_time(text: str, repeats: int = 3) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = perf_counter()
        render_text(text)
        best = min(best, perf_counter() - start)
    return best


def test_render_text_cost_is_not_quadratic_on_long_english_body():
    # Sizes big enough that the linear path takes milliseconds, not microseconds:
    # under xdist load a ~1 ms baseline makes the ratio jitter.
    small = (_SENTENCE * 3200)[:32_000]
    large = (_SENTENCE * 12800)[:128_000]
    ratio = _best_time(large, 5) / max(_best_time(small, 5), 1e-6)
    # 4x input: linear ~4x, quadratic ~16x. Generous margin against noise.
    assert ratio < 10, f"4x input cost {ratio:.1f}x (quadratic would be ~16x)"


# At N=8000, linear cases took 1-7 ms, where xdist scheduler jitter swamps a
# ratio. Enlarging to 40k vs 80k puts every baseline at 11-148 ms; measured
# locally across all 9 shapes: 1.91x-2.62x. DOUBLING_MAX_RATIO (3.0) sits
# between linear (~2x) and quadratic (~4x).
_SELECTOR_BASELINE_N = 40_000
_SELECTOR_LARGE_N = 80_000


def _not_css_block(n: int) -> str:
    return "{" + ("x " * (n * 2))


@pytest.mark.parametrize(
    "build_text",
    [
        pytest.param(
            lambda n: ("p.a, " * n) + _not_css_block(n), id="comma-run-unclosed"
        ),
        pytest.param(lambda n: ("p " * n) + _not_css_block(n), id="space-run-unclosed"),
        pytest.param(
            lambda n: ("p " * n) + _not_css_block(n) + "}",
            id="space-run-closed-not-css",
        ),
        pytest.param(
            lambda n: ("p.a, " * n) + "p {color:red;} x", id="comma-run-real-rule"
        ),
        # The run does not reach the brace: every start inside it used to be retried.
        pytest.param(
            lambda n: ("p " * n) + "zzz {color:red;} x", id="space-run-then-word"
        ),
        pytest.param(
            lambda n: ("p.a, " * n) + "zzz {color:red;} x", id="comma-run-then-word"
        ),
        pytest.param(
            lambda n: (":a " * n) + "zzz {color:red;} x", id="pseudo-run-then-word"
        ),
        pytest.param(
            lambda n: ("p[ " * (n * 4)) + "{color:red;} x",
            id="unclosed-attr-brackets",
        ),
        pytest.param(lambda n: "@media " * (n * 4), id="media-without-block"),
    ],
)
def test_render_text_stays_fast_on_long_selector_runs(
    request: pytest.FixtureRequest, build_text: Callable[[int], str]
) -> None:
    # #289 (L3): each of these took seconds to minutes when the selector grammar
    # was matched by a regex that could restart inside a run (quadratic).
    small = build_text(_SELECTOR_BASELINE_N)
    large = build_text(_SELECTOR_LARGE_N)
    ratio = time_ratio(lambda: render_text(large), lambda: render_text(small))
    case_id = request.node.callspec.id
    report(
        request,
        f"render_text selector ({case_id}): 80k / 40k time ratio = {ratio:.2f}",
    )
    assert ratio <= DOUBLING_MAX_RATIO

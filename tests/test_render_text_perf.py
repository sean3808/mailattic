"""#235, #289 (L3): CSS cleanup in render_text is linear on bodies & selector runs."""

from time import perf_counter

import pytest

from mailattic.query import render_text

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


_N = 8000
_NOT_CSS_BLOCK = "{" + ("x " * 20000)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(("p.a, " * _N) + _NOT_CSS_BLOCK, id="comma-run-unclosed"),
        pytest.param(("p " * _N) + _NOT_CSS_BLOCK, id="space-run-unclosed"),
        pytest.param(("p " * _N) + _NOT_CSS_BLOCK + "}", id="space-run-closed-not-css"),
        pytest.param(("p.a, " * _N) + "p {color:red;} x", id="comma-run-real-rule"),
        # The run does not reach the brace: every start inside it used to be retried.
        pytest.param(("p " * _N) + "zzz {color:red;} x", id="space-run-then-word"),
        pytest.param(("p.a, " * _N) + "zzz {color:red;} x", id="comma-run-then-word"),
        pytest.param((":a " * _N) + "zzz {color:red;} x", id="pseudo-run-then-word"),
        pytest.param(("p[ " * _N * 4) + "{color:red;} x", id="unclosed-attr-brackets"),
        pytest.param("@media " * _N * 4, id="media-without-block"),
    ],
)
def test_render_text_stays_fast_on_long_selector_runs(text: str):
    # #289 (L3): each of these took seconds to minutes when the selector grammar
    # was matched by a regex that could restart inside a run (quadratic). Linear
    # cost is a few milliseconds, so an absolute bound separates the two without
    # the jitter a ratio of millisecond timings has under xdist.
    elapsed = _best_time(text, 2)
    assert elapsed < 0.5, f"took {elapsed:.3f}s (must be < 0.5s)"

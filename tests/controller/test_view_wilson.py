"""view_data's engagement CI is stats.wilson_lower/upper (default z 1.96): same numbers as the old local helper."""
import math

import pytest

from controller import stats


def _old_wilson(x, n, z=1.96):
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    d = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


@pytest.mark.parametrize("x,n", [(0, 0), (0, 40), (40, 40), (1, 3), (13, 500), (250.0, 500.0), (7, 7000)])
def test_stats_wilson_matches_the_previous_view_helper(x, n):
    lo, hi = _old_wilson(x, n)
    assert stats.wilson_lower(x, n) == pytest.approx(lo, abs=1e-12)
    assert stats.wilson_upper(x, n) == pytest.approx(hi, abs=1e-12)

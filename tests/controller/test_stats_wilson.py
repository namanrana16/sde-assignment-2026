"""wilson_upper / wilson_lower stay bit-identical to the original closed forms."""
import math

import pytest

from controller import stats


def _old_upper(x, n, z):
    if n <= 0:
        return 1.0
    p = x / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + spread) / denom)


def _old_lower(x, n, z):
    if n <= 0:
        return 0.0
    p = x / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / denom)


GRID = [(x, n, z) for n in (0, 1, 2, 7, 50, 300, 10_000) for x in sorted({0, 1, n // 2, n}) if x <= n
        for z in (0.0, 1.0, 1.645, 1.96, 3.0902)]


@pytest.mark.parametrize("x,n,z", GRID)
def test_bounds_match_original_formulas(x, n, z):
    assert stats.wilson_upper(x, n, z) == _old_upper(x, n, z)
    assert stats.wilson_lower(x, n, z) == _old_lower(x, n, z)


def test_default_z_and_empty_sample():
    assert stats.wilson_upper(3, 100) == _old_upper(3, 100, 1.96)
    assert stats.wilson_upper(0, 0) == 1.0 and stats.wilson_lower(0, 0) == 0.0

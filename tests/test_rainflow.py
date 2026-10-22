import numpy as np
import pytest

from bess_augment.rainflow import count_cycles, reversals


def _as_pairs(cycles):
    return sorted((round(float(d), 9), float(c)) for d, c in zip(cycles["depth"], cycles["count"]))


def test_astm_example():
    # ASTM E1049-85 example load history; known result: ranges 3 (0.5), 4 (1.5), 6 (0.5), 8 (1.0), 9 (0.5)
    series = [-2, 1, -3, 5, -1, 3, -4, 4, -2]
    c = count_cycles(series)
    out = {}
    for d, n in _as_pairs(c):
        out[d] = out.get(d, 0) + n
    assert out == {3.0: 0.5, 4.0: 1.5, 6.0: 0.5, 8.0: 1.0, 9.0: 0.5}


def test_ten_daily_cycles():
    # equal ranges in a row are closed as half cycles by the three-point method; throughput is what matters
    soc = [0.5] + [1.0, 0.0] * 10 + [0.5]
    c = count_cycles(soc)
    deep = c[np.isclose(c["depth"], 1.0)]
    assert deep["count"].sum() == pytest.approx(9.5)
    assert (c["depth"] * c["count"]).sum() == pytest.approx(10.0)


def test_full_cycle_inside_a_larger_one():
    c = count_cycles([0.0, 1.0, 0.3, 0.9, 0.1, 0.9, 0.0])
    full = c[c["count"] == 1.0]
    assert _as_pairs(full) == [(0.6, 1.0), (0.8, 1.0)]


def test_nested_small_cycle():
    c = count_cycles([0.0, 1.0, 0.6, 0.8, 0.0])
    assert _as_pairs(c) == [(0.2, 1.0), (1.0, 0.5), (1.0, 0.5)]


def test_reversals_drop_plateaus_and_noise():
    idx, v = reversals([0, 0, 1, 1, 1, 0.99, 1, 0])
    assert list(v) == [0, 1, 0.99, 1, 0]
    idx, v = reversals([0, 0, 1, 1, 1, 0.99, 1, 0], min_range=0.05)
    assert list(v) == [0, 1, 0]

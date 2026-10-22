import numpy as np
import pandas as pd
import pytest

from bess_augment.aging import profile_stress
from bess_augment.dispatch import ArbitrageParams, optimize_day, reserve_profile, run_arbitrage


def test_buys_low_sells_high():
    prices = np.r_[np.full(48, 100.0), np.full(48, 600.0)]
    p = ArbitrageParams(energy_mwh=2.0, rte=0.81, wear_cost=0.0)
    revenue, discharged, soc = optimize_day(prices, p)
    assert soc[47] == pytest.approx(1.0) and soc[-1] == pytest.approx(0.5)
    # charge 1 MWh stored (0.5 -> 1.0) at 100 and empty to 0.5 again: discharged = 1 MWh * 0.9 at 600
    assert discharged == pytest.approx(0.9)
    assert revenue == pytest.approx(0.9 * 600 - (1.0 / 0.9) * 100)


def test_cycle_cap_and_window():
    prices = np.tile(np.r_[np.full(12, 50.0), np.full(12, 500.0)], 4)
    free = optimize_day(prices, ArbitrageParams(energy_mwh=2.0))
    capped = optimize_day(prices, ArbitrageParams(energy_mwh=2.0, max_cycles=1.0))
    window = optimize_day(prices, ArbitrageParams(energy_mwh=2.0, soc_lo=0.2, soc_hi=0.8))
    assert capped[1] <= 2.0 + 1e-6 < free[1]
    assert window[2].min() >= 0.2 - 1e-6 and window[2].max() <= 0.8 + 1e-6
    assert capped[0] < free[0] and window[0] < free[0]


def test_negative_prices_are_used():
    prices = np.r_[np.full(48, -200.0), np.full(48, 0.0)]
    revenue, _, _ = optimize_day(prices, ArbitrageParams(energy_mwh=2.0))
    assert revenue > 0                                  # paid to charge, burns energy in losses


def test_run_arbitrage_chains_days():
    days = pd.date_range("2026-01-01", periods=3, freq="D").date
    rows = []
    for d in days:
        rows += [{"business_date": d, "price": 100.0 if i < 48 else 400.0} for i in range(96)]
    prices = pd.DataFrame(rows)
    res = run_arbitrage(prices, ArbitrageParams(energy_mwh=1.0), jobs=1)
    assert len(res.soc) == 3 * 96 + 1
    assert len(res.daily) == 3
    assert res.revenue_per_year == pytest.approx(res.daily["revenue_pln"].sum() * 365 / 3)


def test_reserve_profile_is_shallow():
    r = reserve_profile(energy_mwh=2.0, days=30, seed=3)
    s = profile_stress(r.soc, r.dt_hours)
    assert 0.0 <= r.soc.min() and r.soc.max() <= 1.0
    assert abs(s.mean_soc - 0.5) < 0.1
    assert s.depth_hist["efc"].iloc[:3].sum() > 0.5 * s.fec_per_year   # most throughput in cycles < 20 %

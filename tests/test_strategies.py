import numpy as np
import pandas as pd
import pytest

from bess_augment.economics import Economics, cashflows
from bess_augment.fleet import Case, Event, Plan, simulate
from bess_augment.strategies import bridge_plan, search


def econ(years=15, decline=0.03, rate=0.06, fixed=0.0):
    return Economics(power_mw=1.0, e_op=2.0, obligation_mw=0.5, capacity_price=200_000, shortfall_penalty=0.0,
                     energy_cost=500_000 * (1 - decline) ** np.arange(years + 1), power_cost=300_000,
                     augment_premium=0.2, augment_fixed=fixed, module_share=0.6, replace_extra=20_000,
                     replace_fixed=fixed, om_per_mw=10_000, om_per_mwh=5_000, discount_rate=rate)


@pytest.fixture
def case(model, daily_cycle_stress):
    return Case(model, daily_cycle_stress, 25.0, e_op=2.0, e_req=2.0, years=15)


def test_npv_of_constant_flows():
    e = econ(years=3, decline=0.0, rate=0.1)
    table = pd.DataFrame({"year": [1, 2, 3], "installed_mwh": 2.0, "added_mwh": 0.0, "replaced_mwh": 0.0,
                          "avail_start_mwh": 2.0, "avail_end_mwh": 2.0, "shortfall_frac": 0.0})
    curve = pd.Series({1.0: 100_000.0, 2.0: 200_000.0})
    cf, pv = cashflows(Plan("x", 2.0), table, e, curve)
    annual = 0.5 * 200_000 + 200_000 - 10_000 - 2 * 5_000
    annuity = sum(1.1 ** -t for t in (1, 2, 3))
    assert pv["npv"] == pytest.approx(annual * annuity - 2.0 * 500_000 - 300_000)


def test_shortfall_loses_capacity_payment_and_revenue():
    e = econ(years=1, decline=0.0)
    full = pd.DataFrame({"year": [1], "installed_mwh": 2.0, "added_mwh": 0.0, "replaced_mwh": 0.0,
                         "avail_start_mwh": 2.0, "avail_end_mwh": 2.0, "shortfall_frac": 0.0})
    short = full.assign(avail_start_mwh=1.6, avail_end_mwh=1.6, shortfall_frac=0.2)
    curve = pd.Series({1.0: 100_000.0, 2.0: 200_000.0})
    a = cashflows(Plan("x", 2.0), full, e, curve)[1]
    b = cashflows(Plan("x", 2.0), short, e, curve)[1]
    assert a["pv_capacity"] - b["pv_capacity"] == pytest.approx(0.2 * 0.5 * 200_000 / 1.06)
    assert a["pv_arbitrage"] - b["pv_arbitrage"] == pytest.approx(40_000 / 1.06)
    assert b["shortfall_mw_years"] == pytest.approx(0.1)


def test_augmentation_cost_uses_price_of_its_year():
    e = econ(years=5)
    table = pd.DataFrame({"year": [1, 2], "installed_mwh": [2.0, 2.5], "added_mwh": [0.0, 0.5],
                          "replaced_mwh": 0.0, "avail_start_mwh": 2.0, "avail_end_mwh": 2.0, "shortfall_frac": 0.0})
    _, pv = cashflows(Plan("x", 2.0, (Event(2, "augment", 0.5),)), table, e)
    assert pv["pv_augment"] == pytest.approx(0.5 * e.energy_cost[1] * 1.2 / 1.06)


def test_search_matches_brute_force_single_event(case):
    e = econ()

    def npv(p):
        return cashflows(p, simulate(p, case), e)[1]["npv"]

    ranked = search(case, npv, "augment", max_events=1)
    brute = [bridge_plan(case, [y]) for y in range(2, case.years + 1)] + [bridge_plan(case, [])]
    best = max(npv(p) for p in brute if p is not None)
    assert ranked[0][0] == pytest.approx(best, rel=1e-6)
    assert len(ranked) == len([p for p in brute if p is not None])


def test_searched_plans_stay_compliant(case):
    e = econ()
    ranked = search(case, lambda p: cashflows(p, simulate(p, case), e)[1]["npv"], "augment", max_events=2)
    for _, p in ranked[:10]:
        t = simulate(p, case)
        assert (t["avail_end_mwh"] >= case.e_req * (1 - 1e-6)).all()


def test_augmentation_beats_overbuild_when_prices_fall(case):
    e = econ(decline=0.06)
    ranked = search(case, lambda p: cashflows(p, simulate(p, case), e)[1]["npv"], "augment", max_events=2)
    over = next(v for v, p in ranked if not p.events)
    assert ranked[0][1].events and ranked[0][0] > over


def test_fixed_cost_per_event_reduces_events(case):
    cheap = econ(fixed=0.0)
    dear = econ(fixed=400_000)
    def n_events(e):
        r = search(case, lambda p: cashflows(p, simulate(p, case), e)[1]["npv"], "augment", max_events=2)
        return len(r[0][1].events)
    assert n_events(dear) <= n_events(cheap)

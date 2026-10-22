import numpy as np
import pytest

from bess_augment.fleet import Case, Event, Fleet, Plan, advance, bridge, simulate


@pytest.fixture
def case(model, daily_cycle_stress):
    return Case(model, daily_cycle_stress, 25.0, e_op=2.0, e_req=2.0, years=15)


def test_soh_decreases_monotonically(case):
    t = simulate(Plan("none", 2.0), case)
    e = np.r_[t["avail_start_mwh"].iloc[0], t["avail_end_mwh"].to_numpy()]
    assert (np.diff(e) < 0).all()
    assert (np.diff(t["soh_initial_modules"]) < 0).all()
    assert t["shortfall_frac"].iloc[0] > 0                 # no headroom: short from year 1


def test_hotter_ages_faster(model, daily_cycle_stress):
    cool = simulate(Plan("x", 2.0), Case(model, daily_cycle_stress, 20.0, 2.0, 2.0, 10))
    hot = simulate(Plan("x", 2.0), Case(model, daily_cycle_stress, 35.0, 2.0, 2.0, 10))
    assert (hot["avail_end_mwh"] < cool["avail_end_mwh"]).all()


def test_headroom_slows_cell_ageing(case):
    lean = simulate(Plan("x", 2.0), case)
    big = simulate(Plan("x", 3.0), case)
    assert (big["soh_initial_modules"] > lean["soh_initial_modules"]).all()


def test_augmentation_restores_energy(case):
    t = simulate(Plan("aug", 2.4, (Event(6, "augment", 0.5),)), case)
    before = t.loc[t["year"] == 5, "avail_end_mwh"].item()
    after = t.loc[t["year"] == 6, "avail_start_mwh"].item()
    assert after == pytest.approx(before + 0.5)
    assert t.loc[t["year"] == 6, "installed_mwh"].item() == pytest.approx(2.9)


def test_replacement_swaps_modules(case):
    t = simulate(Plan("rep", 2.4, (Event(6, "replace", 0.5),)), case)
    soh = simulate(Plan("x", 2.4), case).loc[4, "soh_initial_modules"]
    before = t.loc[t["year"] == 5, "avail_end_mwh"].item()
    after = t.loc[t["year"] == 6, "avail_start_mwh"].item()
    assert after == pytest.approx(before + 0.5 * 2.4 * (1 - soh))
    assert t.loc[t["year"] == 6, "installed_mwh"].item() == pytest.approx(2.4)


def test_bridge_is_minimal(case):
    size, _ = bridge(None, case, "augment", 1, 8, 2.0, 6.0)
    s = float(size[0])
    ok = simulate(Plan("x", s), case)
    assert (ok.loc[ok["year"] < 8, "avail_end_mwh"] >= case.e_req * (1 - 1e-6)).all()
    short = simulate(Plan("x", s - 0.01), case)
    assert (short.loc[short["year"] < 8, "avail_end_mwh"] < case.e_req).any()


def test_bridge_reports_infeasible(case):
    size, _ = bridge(None, case, "augment", 1, 16, 2.0, 2.1)
    assert np.isinf(size[0])


def test_batch_equals_single(case):
    single = Fleet.build(2.5)
    batch = Fleet.build([2.5, 2.5, 3.0])
    a = advance(single, case, 5)
    b = advance(batch, case, 5)
    assert np.allclose(b[:, 0], a[:, 0]) and np.allclose(b[:, 1], a[:, 0])
    assert (b[:, 2] > b[:, 0]).all()


def test_per_sample_parameters(model, daily_cycle_stress):
    fast = model.with_(k_cal=np.array([1.6e-3, 3.2e-3]), k_cyc=np.array([2.1e-5, 2.1e-5]))
    c = Case(fast, daily_cycle_stress, np.array([25.0, 25.0]), 2.0, 2.0, 5)
    e = advance(Fleet.build([2.0, 2.0]), c, 5)
    single = advance(Fleet.build(2.0), Case(model, daily_cycle_stress, 25.0, 2.0, 2.0, 5), 5)
    assert np.allclose(e[:, 0], single[:, 0])
    assert (e[:, 1] < e[:, 0]).all()

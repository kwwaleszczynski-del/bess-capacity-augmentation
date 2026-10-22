import numpy as np
import pandas as pd
import pytest

from bess_augment.aging import AgingModel, arrhenius, fit_cells, load_cells, profile_stress


def test_arrhenius_reference_and_direction():
    assert arrhenius(25.0, 30_000) == pytest.approx(1.0)
    assert arrhenius(35.0, 30_000) > 1.0 > arrhenius(15.0, 30_000)
    assert arrhenius(35.0, 0.0) == pytest.approx(1.0)


def test_calendar_power_law(model):
    one = model.calendar_loss(365, 0.5, 25)
    four = model.calendar_loss(4 * 365, 0.5, 25)
    assert four == pytest.approx(2 * one)            # sqrt of time


def test_stress_of_daily_full_cycle(daily_cycle_stress):
    s = daily_cycle_stress
    assert s.fec_per_year == pytest.approx(365, rel=0.01)
    assert s.mean_soc == pytest.approx(0.5, abs=0.01)
    assert s.weights.sum() == pytest.approx(1.0)
    assert s.n_eq_per_year(1.0) == pytest.approx(s.fec_per_year)
    assert s.n_eq_per_year(1.5) == pytest.approx(s.fec_per_year, rel=0.01)   # full-depth cycles: beta irrelevant


def test_scaled_stress_is_shallower(daily_cycle_stress):
    half = daily_cycle_stress.scaled(0.5)
    assert half.fec_per_year == pytest.approx(daily_cycle_stress.fec_per_year / 2)
    assert half.levels.min() >= 0.25 - 1e-9 and half.levels.max() <= 0.75 + 1e-9
    # with beta > 1 shallow cycles are cheaper per unit of throughput
    assert half.n_eq_per_year(1.5) < half.n_eq_per_year(1.0)


def test_shallow_cycles_profile():
    t = np.arange(0, 365 * 96)
    soc = 0.5 + 0.04 * np.sin(2 * np.pi * t / 8)       # 12 cycles a day of 8 % depth
    s = profile_stress(soc, 0.25)
    assert s.fec_per_year == pytest.approx(365 * 12 * 0.08, rel=0.02)
    assert s.depth_hist["efc"].iloc[:2].sum() == pytest.approx(s.fec_per_year, rel=0.02)


def test_fit_recovers_synthetic_parameters():
    true = AgingModel(k_cal=2.0e-3, ea_cal=35_000, gamma=0.5, k_cyc=3.0e-5, ea_cyc=28_000)
    cells = load_cells()
    rng = np.random.default_rng(0)
    rows = []
    for cell, g in cells.groupby("cell"):
        c0 = 190 + rng.uniform(-3, 3)
        for _, r in g.iterrows():
            if r["mode"] == "calendar":
                loss = true.calendar_loss(r["days"], r["storage_soc"] / 100, r["chamber_temp_c"])
            else:
                socs = np.linspace(0, 1, 21)
                k = np.mean(true.k_cal_at(socs, r["cell_temp_c"]) ** 2) ** 0.5
                loss = k * r["days"] ** 0.5 + true.cycle_loss(r["checkup"], r["cell_temp_c"])
            rows.append(dict(r, q_dis_ah=c0 * (1 - loss) * (1 + rng.normal(0, 5e-4))))
    fit = fit_cells(pd.DataFrame(rows))
    m = fit["model"]
    assert m.k_cal == pytest.approx(true.k_cal, rel=0.1)
    assert m.ea_cal == pytest.approx(true.ea_cal, rel=0.1)
    assert m.k_cyc == pytest.approx(true.k_cyc, rel=0.15)
    assert fit["calendar"].cov.shape == (3, 3)


def test_bundled_cell_data():
    cells = load_cells()
    assert set(cells["mode"]) == {"calendar", "cycling"}
    assert cells["cell"].nunique() == 12
    assert (cells.groupby("cell")["q_dis_ah"].last() < cells.groupby("cell")["q_dis_ah"].max()).all()

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from bess_augment import config as C
from bess_augment.aging import AgingModel
from bess_augment.dispatch import ProfileResult
from bess_augment.economics import cashflows
from bess_augment.fleet import Plan, simulate
from bess_augment.pipeline import (capture_sensitivity, compliance_years, curve_for, make_case, profile_summary,
                                   stresses)
from bess_augment.strategies import bridge_plan


def _profile(revenue, energy=2.0, n=96 * 30):
    soc = 0.5 + 0.45 * np.sin(np.linspace(0, 2 * np.pi * 30, n + 1))
    daily = pd.DataFrame({"revenue_pln": revenue / 365, "discharged_mwh": energy}, index=range(30))
    return ProfileResult(soc=soc, dt_hours=0.25, daily=daily, revenue_per_year=revenue,
                         discharged_per_year=energy * 365)


@pytest.fixture
def cfg():
    return C.load()


@pytest.fixture
def profiles():
    out = {("rce_opt", d): _profile(100_000 * d) for d in (1.0, 2.0, 4.0)}
    out[("rce_w10", 2.0)] = _profile(160_000)
    out[("rce_w10", 4.0)] = _profile(300_000)
    return out


def test_window_profile_uses_parent_curve(cfg, profiles):
    own, scale = curve_for(cfg, profiles, "rce_opt", 2.0)
    assert scale == 1.0 and list(own.index) == [1.0, 2.0, 4.0]
    parent, scale = curve_for(cfg, profiles, "rce_w10", 2.0)
    assert list(parent.index) == [1.0, 2.0, 4.0]                # revenue below 2 h comes from the parent
    assert scale == pytest.approx(0.8)
    assert curve_for(cfg, profiles, "rce_w10", 4.0)[1] == pytest.approx(0.75)


def test_compliance_years_grow_with_margin(cfg, profiles):
    st = stresses(profiles)
    case = make_case(cfg, AgingModel(), st[("rce_opt", 2.0)], 2.0, 25.0)
    rows = compliance_years(case, [0.0, 0.1, 0.3])
    years = [r["first_short_year"] or 99 for r in rows]
    assert years[0] == 1 and years[0] <= years[1] <= years[2]


def test_profile_summary_columns(cfg, profiles):
    table = profile_summary(cfg, profiles, stresses(profiles))
    assert {"cycles_per_day", "rainflow_fec_per_year", "mean_soc"} <= set(table.columns)
    assert len(table) == len(profiles)


def test_capture_sensitivity_matches_cashflows(cfg, model, daily_cycle_stress):
    case = make_case(cfg, model, daily_cycle_stress, 2.0, cfg["analysis"]["temp_c"])
    econ = C.economics_for(cfg, 1.0, 2.0, case.years)
    curve = pd.Series({1.0: 200_000.0, 2.0: 400_000.0})
    plans = {"none": Plan("none", 2.0), "overbuild": bridge_plan(case, []), "augment_opt": bridge_plan(case, [8])}
    key = dict(duration_h=2.0, profile=cfg["analysis"]["base_profile"], temp_c=cfg["analysis"]["temp_c"],
               scenario="base")
    pv = {k: cashflows(p, simulate(p, case), econ, curve)[1] for k, p in plans.items()}
    assert pv["overbuild"]["pv_arbitrage"] == pytest.approx(pv["augment_opt"]["pv_arbitrage"], rel=1e-6)
    assert pv["none"]["pv_arbitrage"] < pv["overbuild"]["pv_arbitrage"]          # a faded battery earns less
    out = capture_sensitivity(cfg, pd.DataFrame([dict(key, strategy=k, plan=plans[k].label(), **v)
                                                 for k, v in pv.items()]))
    assert len(out) == len(plans) * len(cfg["economics"]["capture_rates"])
    for c, g in out.groupby("capture_rate"):
        e = replace(econ, arbitrage_capture=c)
        for k, p in plans.items():
            r = g[g["strategy"] == k].iloc[0]
            assert r["npv"] == pytest.approx(cashflows(p, simulate(p, case), e, curve)[1]["npv"])
        assert g.loc[g["strategy"] == "augment_opt", "npv_vs_augment_opt"].iloc[0] == 0
        assert g["npv_vs_best"].max() == 0

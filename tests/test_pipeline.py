import numpy as np
import pandas as pd
import pytest

from bess_augment import config as C
from bess_augment.aging import AgingModel
from bess_augment.dispatch import ProfileResult
from bess_augment.pipeline import compliance_years, curve_for, make_case, profile_summary, stresses


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

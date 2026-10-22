import numpy as np
import pytest

from bess_augment import config as C
from bess_augment.aging import fit_cells
from bess_augment.fleet import Case, Event, Plan, simulate
from bess_augment.risk import required_bol, run_adaptive, run_fixed, sample_case, summarize
from bess_augment.warranty import Warranty, check

W = Warranty(years=15, fec_per_year=365, fec_total=5475, retention=0.6, max_temp_c=30, max_c_rate=0.5)


def test_warranty_annual_limit_and_throughput():
    ok = check(W, discharged_mwh_per_year=700, bol_mwh=2.0, power_mw=1.0, temp_c=25, soh_initial=np.full(15, 0.9))
    assert ok["break_year"] == 0 and ok["throughput_used_up_year"] == 0
    heavy = check(W, discharged_mwh_per_year=1400, bol_mwh=2.0, power_mw=1.0, temp_c=25, soh_initial=np.full(15, 0.9))
    assert not heavy["annual_limit_ok"] and heavy["break_year"] == 1
    assert heavy["throughput_used_up_year"] == 8          # 700 FEC/yr: 5475 used up in year 8
    hot = check(W, 500, 2.0, 1.0, temp_c=35, soh_initial=np.full(15, 0.9))
    assert hot["break_year"] == 1
    soc = check(W, 500, 2.0, 1.0, 25, np.full(15, 0.9), mean_soc=0.55)
    assert soc["break_year"] == 0                       # W has no SoC limit
    w50 = Warranty(15, 365, 5475, 0.6, 30, 0.5, max_mean_soc=0.5)
    assert check(w50, 500, 2.0, 1.0, 25, np.full(15, 0.9), mean_soc=0.55)["break_year"] == 1


def test_warranty_retention_at_end():
    assert check(W, 500, 2.0, 1.0, 25, np.linspace(0.97, 0.5, 15))["below_retention_at_end"]
    assert not check(W, 500, 2.0, 1.0, 25, np.linspace(0.97, 0.75, 15))["below_retention_at_end"]


def test_config_loads_and_is_consistent():
    cfg = C.load()
    assert 0 < C.kwd(cfg) <= 1.0
    path = C.cost_path(cfg, cfg["capacity_market"]["contract_years"])
    assert (np.diff(path) <= 1e-9).all() and path[0] > 0
    e = C.economics_for(cfg, 1.0, 2.0, cfg["capacity_market"]["contract_years"])
    assert e.obligation_mw == pytest.approx(0.5 * C.kwd(cfg))         # 2 h battery: 0.5 MW for 4 h


def test_capacity_market_requirements():
    cfg = C.load()
    assert C.net_power(cfg, 1.0, 2.0) == pytest.approx(0.5)
    assert C.net_power(cfg, 1.0, 6.0) == pytest.approx(1.0)
    assert C.requirement_mwh(cfg, 1.0, 2.0, mode="four_hour_rule") == pytest.approx(2.0)
    assert C.requirement_mwh(cfg, 1.0, 6.0, mode="four_hour_rule") == pytest.approx(4.0)
    call = C.requirement_mwh(cfg, 1.0, 4.0, "ag2028", mode="call")
    assert call == pytest.approx(0.95 * cfg["capacity_market"]["design_call_mwh_per_mw"])
    obl = C.obligation_mw(cfg, 1.0, 4.0, "ag2028")
    assert C.call_penalty(cfg, 10.0, obl, "ag2028") == 0.0
    cm = cfg["capacity_market"]
    empty = obl * sum(cm["calls_mwh_per_mw"]) * cm["penalty_pln_per_mwh"]
    assert C.call_penalty(cfg, 0.0, obl, "ag2028") == pytest.approx(empty)
    cfg["capacity_market"]["penalty_pln_per_mwh"] = 1e6                  # the annual cap binds
    cap = 2 * obl * cm["vintages"]["ag2028"]["price_pln_per_kw_year"] * 1000
    assert C.call_penalty(cfg, 0.0, obl, "ag2028") == pytest.approx(cap)


@pytest.fixture
def mc(model, daily_cycle_stress):
    fit = fit_cells()
    case = Case(fit["model"], daily_cycle_stress, 25.0, e_op=2.0, e_req=2.0, years=12)
    return case, sample_case(case, fit, n=64, temp_sd=2.0, seed=4)


def test_monte_carlo_spread_and_adaptive_sizing(mc):
    case, sampled = mc
    plan = Plan("aug", 2.4, (Event(6, "augment", 0.4),))
    fixed = run_fixed(plan, sampled)
    assert fixed["avail_end"].shape == (12, 64)
    assert np.std(fixed["avail_end"][-1]) > 0
    adapt, sizes = run_adaptive(plan, sampled)
    assert sizes.shape == (1, 64) and np.std(sizes) > 0
    late = adapt["short"][5:]                           # after the adaptive action every sample is compliant
    assert (late < 1e-6).all()
    assert summarize(adapt)["exp_shortfall_years"] <= summarize(fixed)["exp_shortfall_years"]


def test_required_bol_quantiles(mc):
    case, sampled = mc
    need = required_bol(sampled, case.years + 1, 2.0, 8.0)
    central = simulate(Plan("x", float(np.median(need))), case)
    assert need.shape == (64,) and np.isfinite(need).all()
    assert np.quantile(need, 0.9) > np.quantile(need, 0.5)
    assert central["avail_end_mwh"].iloc[-1] == pytest.approx(2.0, rel=0.1)

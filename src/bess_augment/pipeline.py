"""Analysis steps: operating profiles, ageing fit, compliance years, strategies, risk, sensitivity and warranty.

Usage:
    from bess_augment import pipeline
    profiles = pipeline.build_profiles(cfg, prices, Path("data"), jobs=8)
    res = pipeline.analyse(cfg, profiles, fit, jobs=8)
"""
from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from bess_augment import config as C
from bess_augment.aging import AgingModel, Stress, profile_stress
from bess_augment.dispatch import ArbitrageParams, ProfileResult, ReserveParams, reserve_profile, run_arbitrage
from bess_augment.economics import Economics, cashflows
from bess_augment.fleet import Case, Event, Plan, simulate
from bess_augment.risk import required_bol, run_adaptive, run_fixed, sample_case, sample_table, summarize
from bess_augment.strategies import bridge_plan, search
from bess_augment.warranty import check

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------------------------------
# Operating profiles
# ---------------------------------------------------------------------------------------------------------------------

def _profile_path(data_dir: Path, name: str, d: float) -> Path:
    return data_dir / "profiles" / f"{name}_{d:g}h.npz"


def _save(path: Path, r: ProfileResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, soc=r.soc.astype(np.float32), dt=r.dt_hours, rev=r.daily["revenue_pln"].to_numpy(),
                        dis=r.daily["discharged_mwh"].to_numpy(), revenue=r.revenue_per_year,
                        discharged=r.discharged_per_year)


def _load(path: Path) -> ProfileResult:
    z = np.load(path)
    daily = pd.DataFrame({"revenue_pln": z["rev"], "discharged_mwh": z["dis"]})
    return ProfileResult(soc=z["soc"].astype(float), dt_hours=float(z["dt"]), daily=daily,
                         revenue_per_year=float(z["revenue"]), discharged_per_year=float(z["discharged"]))


def build_profiles(cfg: dict, prices: pd.DataFrame | None, data_dir: Path, jobs: int = 1,
                   refresh: bool = False) -> dict[tuple[str, float], ProfileResult]:
    """Every operating profile of ``cfg['profiles']`` at each of its durations (cached in data/profiles)."""
    b = cfg["battery"]
    out = {}
    for name, spec in cfg["profiles"].items():
        for d in spec["durations"]:
            path = _profile_path(data_dir, name, d)
            if path.exists() and not refresh:
                out[(name, d)] = _load(path)
                continue
            if spec["kind"] == "arbitrage":
                if prices is None:
                    raise RuntimeError(f"profile {name} {d} h not cached and no prices given")
                lo, hi = spec.get("window", [0.0, 1.0])
                p = ArbitrageParams(power_mw=b["power_mw"], energy_mwh=b["power_mw"] * d, rte=b["rte"], soc_lo=lo,
                                    soc_hi=hi, max_cycles=spec.get("max_cycles", np.inf),
                                    wear_cost=spec.get("wear_cost", 1.0))
                r = run_arbitrage(prices, p, jobs=jobs)
            else:
                r = reserve_profile(b["power_mw"] * d, b["power_mw"], days=365, seed=spec.get("seed", 1),
                                    p=ReserveParams(rte=b["rte"], **spec.get("params", {})))
            _save(path, r)
            log.info("profile %s %g h: %.0f PLN/MW-yr, %.0f MWh discharged/yr", name, d, r.revenue_per_year,
                     r.discharged_per_year)
            out[(name, d)] = r
    return out


def stresses(profiles: dict) -> dict[tuple[str, float], Stress]:
    return {k: profile_stress(r.soc, r.dt_hours) for k, r in profiles.items()}


def profile_summary(cfg: dict, profiles: dict, stress: dict) -> pd.DataFrame:
    rows = []
    for (name, d), r in profiles.items():
        s = stress[(name, d)]
        h = s.depth_hist
        rows.append({"profile": name, "label": cfg["profiles"][name]["label"], "duration_h": d,
                     "revenue_pln_per_mw_year": r.revenue_per_year, "discharged_mwh_per_year": r.discharged_per_year,
                     "cycles_per_day": r.discharged_per_year / 365 / (cfg["battery"]["power_mw"] * d),
                     "rainflow_fec_per_year": s.fec_per_year, "mean_soc": s.mean_soc,
                     "efc_share_depth_below_20pct": float(h["efc"].iloc[:3].sum() / max(h["efc"].sum(), 1e-12)),
                     "efc_share_depth_above_80pct": float(h["efc"].iloc[-1] / max(h["efc"].sum(), 1e-12))})
    return pd.DataFrame(rows)


def revenue_curve(profiles: dict, name: str) -> pd.Series:
    pts = {d: r.revenue_per_year for (n, d), r in profiles.items() if n == name}
    return pd.Series(pts).sort_index()


def curve_for(cfg: dict, profiles: dict, name: str, duration: float) -> tuple[pd.Series | None, float]:
    """Revenue curve of a profile by usable duration, and the scale applied to it (window profiles use the curve of
    their parent profile, scaled by their revenue at the nominal duration)."""
    spec = cfg["profiles"][name]
    if spec["kind"] != "arbitrage":
        return None, 1.0
    own = revenue_curve(profiles, name)
    if "parent" not in spec:
        return own, 1.0
    parent = revenue_curve(profiles, spec["parent"])
    return parent, float(own.loc[duration] / parent.loc[duration])


# ---------------------------------------------------------------------------------------------------------------------
# One battery, profile and temperature: compliance years and strategies
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class Task:
    duration: float
    profile: str
    temp_c: float
    model: AgingModel
    stress: Stress
    curve: pd.Series | None
    revenue_scale: float
    discharged_per_year: float
    cfg: dict
    scenarios: dict                 # label -> Economics (first = base)
    full: bool = True               # search the schedules (False: compliance years only)


def make_case(cfg: dict, model: AgingModel, stress: Stress, duration: float, temp_c: float) -> Case:
    p = cfg["battery"]["power_mw"]
    return Case(model, stress, temp_c, e_op=p * duration, e_req=C.requirement_mwh(cfg, p, duration),
                years=cfg["capacity_market"]["contract_years"], steps_per_year=cfg["analysis"]["steps_per_year"])


def compliance_years(case: Case, margins: list[float]) -> list[dict]:
    rows = []
    for m in margins:
        t = simulate(Plan("none", case.e_op * (1 + m)), case)
        short = t.loc[t["shortfall_frac"] > 1e-9, "year"]
        rows.append({"bol_margin": m, "first_short_year": int(short.iloc[0]) if len(short) else 0,
                     "energy_end_mwh": float(t["avail_end_mwh"].iloc[-1]),
                     "soh_end": float(t["soh_initial_modules"].iloc[-1])})
    return rows


def run_task(task: Task) -> dict:
    cfg, a = task.cfg, task.cfg["analysis"]
    case = make_case(cfg, task.model, task.stress, task.duration, task.temp_c)
    key = {"duration_h": task.duration, "profile": task.profile, "temp_c": task.temp_c}
    base = Plan("none", case.e_op)
    path0 = simulate(base, case).assign(**key)
    comp = [dict(key, **r) for r in compliance_years(case, a["bol_margins"])]
    out = {"soh_path": path0, "compliance": pd.DataFrame(comp)}
    if not task.full:
        return out

    tables: dict[Plan, pd.DataFrame] = {}

    def table(plan: Plan) -> pd.DataFrame:
        if plan not in tables:
            tables[plan] = simulate(plan, case)
        return tables[plan]

    base_econ = next(iter(task.scenarios.values()))

    def npv(plan: Plan, econ: Economics = base_econ) -> float:
        return cashflows(plan, table(plan), econ, task.curve, task.revenue_scale)[1]["npv"]

    aug = [p for _, p in search(case, npv, "augment", a["max_events"], min_bol=case.e_op)]
    rep = [p for _, p in search(case, npv, "replace", a["max_replacements"], min_bol=case.e_op)]
    fixed = bridge_plan(case, a["fixed_augment_years"], "augment")
    candidates = {"none": [base], "overbuild": [p for p in aug if not p.events][:1],
                  "augment_fix": [fixed] if fixed else [], "augment_opt": [p for p in aug if p.events],
                  "replace_opt": [p for p in rep if p.events]}
    w = C.warranty_from(cfg)
    rows, paths, best_plans = [], [], {}
    for label, econ in task.scenarios.items():
        for strat, plans in candidates.items():
            if not plans:
                continue
            vals = [cashflows(p, table(p), econ, task.curve, task.revenue_scale) for p in plans]
            i = int(np.argmax([v[1]["npv"] for v in vals]))
            plan, (cf, pv) = plans[i], vals[i]
            wc = check(w, task.discharged_per_year, plan.bol_mwh, cfg["battery"]["power_mw"], task.temp_c,
                       table(plan)["soh_initial_modules"], task.stress.mean_soc)
            rows.append(dict(key, scenario=label, strategy=strat, plan=plan.label(), bol_mwh=plan.bol_mwh,
                             n_events=len(plan.events), added_mwh=sum(e.size for e in plan.events if e.kind == "augment"),
                             warranty_break_year=wc["break_year"], **pv))
            if label == "base":
                best_plans[strat] = plan
                paths.append(table(plan).assign(strategy=strat, **key).merge(cf, on="year"))
    out["strategies"] = pd.DataFrame(rows)
    out["paths"] = pd.concat(paths, ignore_index=True)
    out["plans"] = {(task.duration, task.profile, task.temp_c): best_plans}
    grid = [(p.events[0].year, p.events[1].year, npv(p)) for p in aug if len(p.events) == 2]
    out["schedule_grid"] = pd.DataFrame(grid, columns=["year_1", "year_2", "npv"]).groupby(
        ["year_1", "year_2"], as_index=False)["npv"].max().assign(**key)
    return out


def _scenarios(cfg: dict, duration: float, sensitivity: bool) -> dict:
    p, L = cfg["battery"]["power_mw"], cfg["capacity_market"]["contract_years"]
    out = {"base": C.economics_for(cfg, p, duration, L)}
    if sensitivity:
        for d in cfg["costs"]["decline"]:
            if d != cfg["costs"]["decline_base"]:
                out[f"decline_{d}"] = C.economics_for(cfg, p, duration, L, decline=d)
        for r in cfg["economics"]["discount_rates"]:
            if r != cfg["economics"]["discount_rate"]:
                out[f"discount_{r:.0%}"] = C.economics_for(cfg, p, duration, L, discount_rate=r)
        for v in cfg["capacity_market"]["vintages"]:
            if v != cfg["capacity_market"]["base_vintage"]:
                out[f"vintage_{v}"] = C.economics_for(cfg, p, duration, L, vintage_name=v)
    return out


def analyse(cfg: dict, profiles: dict, model: AgingModel, jobs: int = 1) -> dict:
    """Compliance years for every profile, duration and temperature; strategies where configured."""
    st = stresses(profiles)
    a = cfg["analysis"]
    tasks = []
    for (name, d), s in st.items():
        if d not in cfg["battery"]["durations_h"]:
            continue                                     # other durations only give the revenue curves
        curve, scale = curve_for(cfg, profiles, name, d)
        for t in a["temperatures_c"]:
            full = name in a["strategy_profiles"] and (t == a["temp_c"] or name == a["base_profile"])
            sens = name == a["base_profile"] and t == a["temp_c"]
            tasks.append(Task(d, name, t, model, s, curve, scale, profiles[(name, d)].discharged_per_year, cfg,
                              _scenarios(cfg, d, sens), full))
    log.info("%d tasks (%d with strategy search)", len(tasks), sum(t.full for t in tasks))
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as ex:
            results = list(ex.map(run_task, tasks))
    else:
        results = [run_task(t) for t in tasks]
    out = {}
    for k in ("soh_path", "compliance", "strategies", "paths", "schedule_grid"):
        parts = [r[k] for r in results if k in r]
        out[k] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    out["plans"] = {}
    for r in results:
        out["plans"].update(r.get("plans", {}))
    out["stress"] = st
    out["strategies"] = add_restrict(cfg, out["strategies"])
    return out


def add_restrict(cfg: dict, strat: pd.DataFrame) -> pd.DataFrame:
    """Strategy (c): the best plan of each restricted profile, compared with the base profile's strategies."""
    a = cfg["analysis"]
    extra = []
    for name in a["restricted_profiles"]:
        r = strat[(strat["profile"] == name) & (strat["strategy"].isin(["overbuild", "augment_opt", "replace_opt"]))]
        if r.empty:
            continue
        best = r.loc[r.groupby(["duration_h", "temp_c", "scenario"])["npv"].idxmax()].copy()
        best["restricted_profile"] = name
        best["plan"] = best["strategy"] + " under " + name + ": " + best["plan"]
        best["strategy"] = "restrict_" + name
        best["profile"] = a["base_profile"]
        extra.append(best)
    out = pd.concat([strat] + extra, ignore_index=True)
    keys = ["duration_h", "profile", "temp_c", "scenario"]
    out["npv_vs_best"] = out["npv"] - out.groupby(keys)["npv"].transform("max")
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Risk, warranty
# ---------------------------------------------------------------------------------------------------------------------

def _mc_values(plan: Plan, sizes: np.ndarray, bol, res: dict, econ: Economics, curve, scale) -> np.ndarray:
    """NPV and PV of battery capex (initial build + actions) of every Monte Carlo sample, shape (B, 2)."""
    n = res["avail_end"].shape[1]
    bol = np.broadcast_to(np.asarray(bol, float), (n,))
    out = np.empty((n, 2))
    for j in range(n):
        ev = tuple(Event(e.year, e.kind, float(sizes[k, j])) for k, e in enumerate(plan.events))
        pv = cashflows(Plan("mc", float(bol[j]), ev), sample_table(res, j), econ, curve, scale)[1]
        out[j] = pv["npv"], pv["capex0"] + pv["pv_augment"] + pv["pv_replace"]
    return out


def _band(res: dict, **key) -> pd.DataFrame:
    e = res["avail_end"]
    return pd.DataFrame(dict(key, year=np.arange(1, e.shape[0] + 1), p05=np.quantile(e, 0.05, 1),
                             p50=np.quantile(e, 0.5, 1), p95=np.quantile(e, 0.95, 1)))


def risk(cfg: dict, fit: dict, stress: dict, plans: dict, curves: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monte Carlo of the ageing parameters at the base temperature: risk of the central designs (fixed sizes), and
    designs for a given probability of compliance (overbuild sized at a quantile of the required energy; for
    augmentation the first segment at that quantile and later actions sized adaptively)."""
    r, a = cfg["risk"], cfg["analysis"]
    rows, bands = [], []
    for name in r["profiles"]:
        for d in cfg["profiles"][name]["durations"]:
            key = (d, name, a["temp_c"])
            if key not in plans:
                continue
            case = make_case(cfg, fit["model"], stress[(name, d)], d, a["temp_c"])
            mc = sample_case(case, fit, r["samples"], r["temp_sd_c"], r["seed"])
            econ = C.economics_for(cfg, cfg["battery"]["power_mw"], d, case.years)
            curve, scale = curves[(name, d)]
            for strat in r["strategies"]:
                plan = plans[key].get(strat)
                if plan is None:
                    continue
                tag = dict(duration_h=d, profile=name, strategy=strat)
                res = run_fixed(plan, mc)
                sizes = np.array([[e.size] * r["samples"] for e in plan.events], float).reshape(len(plan.events),
                                                                                              r["samples"])
                v = _mc_values(plan, sizes, plan.bol_mwh, res, econ, curve, scale)
                rows.append(dict(tag, design="central (P50), fixed sizes", bol_mwh=plan.bol_mwh, **summarize(res),
                                 mean_npv=float(v[:, 0].mean()), mean_pv_battery_capex=float(v[:, 1].mean())))
                bands.append(_band(res, design="central", **tag))
                first = plan.events[0].year if plan.events else case.years + 1
                need = required_bol(mc, first, case.e_op, 4 * case.e_req)
                for q in r["quantiles"]:
                    bol_q = float(np.quantile(need, q))
                    if plan.events:
                        res_q, sizes_q = run_adaptive(plan, mc, bol=bol_q)
                        design = f"P{q * 100:.0f} first segment, adaptive sizes"
                    else:
                        res_q, sizes_q = run_fixed(Plan(plan.name, bol_q), mc), np.empty((0, r["samples"]))
                        design = f"P{q * 100:.0f} overbuild"
                    v = _mc_values(plan, sizes_q, bol_q, res_q, econ, curve, scale)
                    rows.append(dict(tag, design=design, bol_mwh=bol_q, **summarize(res_q),
                                     mean_npv=float(v[:, 0].mean()), mean_pv_battery_capex=float(v[:, 1].mean())))
                    bands.append(_band(res_q, design=f"P{q * 100:.0f}", **tag))
            log.info("risk %s %g h done", name, d)
    return pd.DataFrame(rows), pd.concat(bands, ignore_index=True)


def warranty_table(cfg: dict, profiles: dict, stress: dict, paths: pd.DataFrame) -> pd.DataFrame:
    w = C.warranty_from(cfg)
    a = cfg["analysis"]
    rows = []
    for (name, d), r in profiles.items():
        p = paths[(paths["profile"] == name) & (paths["duration_h"] == d) & (paths["temp_c"] == a["temp_c"])]
        if p.empty:
            continue
        c = check(w, r.discharged_per_year, cfg["battery"]["power_mw"] * d, cfg["battery"]["power_mw"], a["temp_c"],
                  p["soh_initial_modules"].to_numpy(), stress[(name, d)].mean_soc)
        rows.append(dict(profile=name, duration_h=d, **c))
    return pd.DataFrame(rows)


def call_exposure(cfg: dict, soh_path: pd.DataFrame) -> pd.DataFrame:
    """For every auction vintage: the year the battery without headroom can no longer deliver its obligation
    through the design call, and the penalty it would pay in a year with the configured calls."""
    cm = cfg["capacity_market"]
    pw = cfg["battery"]["power_mw"]
    rows = []
    for (name, d, t), p in soh_path.groupby(["profile", "duration_h", "temp_c"]):
        e = p["avail_end_mwh"].to_numpy()
        for v, spec in cm["vintages"].items():
            obl = C.obligation_mw(cfg, pw, d, v)
            need = C.requirement_mwh(cfg, pw, d, v, mode="call")
            short = np.flatnonzero(e < need - 1e-9)
            pen = C.call_penalty(cfg, e, obl, v)
            revenue = obl * spec["price_pln_per_kw_year"] * 1000
            rows.append({"profile": name, "duration_h": d, "temp_c": t, "vintage": v, "kwd": spec["kwd"],
                         "obligation_mw": obl, "call_requirement_mwh": need,
                         "call_requirement_share": need / (pw * d),
                         "first_call_short_year": int(short[0] + 1) if len(short) else 0,
                         "four_hour_requirement_mwh": C.requirement_mwh(cfg, pw, d, v, mode="four_hour_rule"),
                         "capacity_revenue_pln": revenue, "penalty_last_year_pln": float(pen[-1]),
                         "penalty_share_of_revenue_last_year": float(pen[-1] / revenue) if revenue else np.nan})
    return pd.DataFrame(rows)

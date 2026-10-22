"""Risk of missing the capacity obligation when the ageing model is uncertain.

Ageing parameters are drawn from the joint normal distribution of the cell fit (parameter covariance, so the strong
correlation between a rate constant at 25 degC and its activation energy is kept), and the cell temperature from a
normal distribution around the design temperature (HVAC control, spread between containers). Every plan designed on
the central parameters is then run for all samples at once:

* fixed plan: the energy built and the augmentation sizes are those of the central design;
* adaptive plan: the augmentation years are kept, but every augmentation is sized for the sample's own ageing, as an
  operator who measures capacity every year would size it (an optimistic bound: the future ageing rate is known);
* required energy: the initial energy each sample needs to stay compliant to the end of the contract (overbuild) or
  to the first augmentation, whose quantiles give the design for a 90 % or 95 % chance of compliance.

Usage:
    from bess_augment.risk import sample_case, run_fixed, run_adaptive
    case_mc = sample_case(case, fit, n=500, temp_sd=2.0, seed=1)
    res = run_fixed(plan, case_mc)
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from bess_augment.fleet import Case, Fleet, Plan, advance, apply_event, bridge


def sample_case(case: Case, fit: dict, n: int, temp_sd: float = 2.0, seed: int = 1) -> Case:
    """``case`` with n samples of the ageing parameters and the cell temperature."""
    rng = np.random.default_rng(seed)
    cal, cyc = fit["calendar"], fit["cycle"]
    mc = rng.multivariate_normal([cal.params[k] for k in ("k_cal", "ea_cal", "gamma")], cal.cov, n)
    my = rng.multivariate_normal([cyc.params[k] for k in ("k_cyc", "ea_cyc")], cyc.cov, n)
    m = case.model
    model = replace(m, k_cal=np.maximum(mc[:, 0], 0.1 * m.k_cal), ea_cal=mc[:, 1], gamma=mc[:, 2],
                    k_cyc=np.maximum(my[:, 0], 0.1 * m.k_cyc), ea_cyc=my[:, 1],
                    z_cal=np.full(n, m.z_cal), z_cyc=np.full(n, m.z_cyc), beta=np.full(n, m.beta))
    temp = np.asarray(case.temp_c, float) + rng.standard_normal(n) * temp_sd
    return replace(case, model=model, temp_c=temp)


def _batch(case: Case) -> int:
    return int(np.size(case.temp_c))


def _run(case: Case, bol, events: list[tuple[int, str, np.ndarray]]) -> dict:
    """Simulate a batch; ``events`` are (year, kind, sizes per sample). Returns arrays of shape (years, B)."""
    n = _batch(case)
    fleet = Fleet.build(np.broadcast_to(np.asarray(bol, float), (n,)))
    by_year = {y: (k, s) for y, k, s in events}
    start, end, installed, added, replaced = (np.zeros((case.years, n)) for _ in range(5))
    for y in range(1, case.years + 1):
        if y in by_year:
            kind, size = by_year[y]
            if kind == "augment":
                added[y - 1] = size
            else:
                replaced[y - 1] = size * fleet.energy[:, 0]
            fleet = apply_event(fleet, kind, size)
        installed[y - 1] = fleet.installed()
        start[y - 1] = fleet.available(case.model)
        end[y - 1] = advance(fleet, case, 1)[0]
    return {"avail_start": start, "avail_end": end, "installed": installed, "added": added, "replaced": replaced,
            "short": np.clip(1 - end / case.e_req, 0, None)}


def sample_table(res: dict, j: int) -> pd.DataFrame:
    """Year table of sample ``j`` in the format of :func:`bess_augment.fleet.simulate`."""
    years = res["avail_end"].shape[0]
    return pd.DataFrame({"year": np.arange(1, years + 1), "installed_mwh": res["installed"][:, j],
                         "added_mwh": res["added"][:, j], "replaced_mwh": res["replaced"][:, j],
                         "avail_start_mwh": res["avail_start"][:, j], "avail_end_mwh": res["avail_end"][:, j],
                         "shortfall_frac": res["short"][:, j]})


def summarize(res: dict, tol: float = 1e-6) -> dict:
    short = res["short"]
    anyshort = (short > tol).any(0)
    return {"p_noncompliance": float(anyshort.mean()),
            "exp_shortfall_years": float((short > tol).sum(0).mean()),
            "exp_shortfall_frac_years": float(short.sum(0).mean()),
            "p95_max_shortfall": float(np.quantile(short.max(0), 0.95))}


def run_fixed(plan: Plan, case_mc: Case) -> dict:
    n = _batch(case_mc)
    events = [(e.year, e.kind, np.full(n, e.size)) for e in plan.events]
    return _run(case_mc, plan.bol_mwh, events)


def required_bol(case_mc: Case, stop: int, lo: float, hi: float) -> np.ndarray:
    """Initial energy each sample needs to stay compliant until the start of year ``stop``."""
    size, _ = bridge(None, case_mc, "augment", 1, stop, lo, hi, batch=_batch(case_mc))
    return size


def run_adaptive(plan: Plan, case_mc: Case, bol=None, hi_factor: float = 3.0) -> tuple[dict, np.ndarray]:
    """Keep the plan's action years, size every action for each sample's own ageing.

    Returns the simulation arrays and the action sizes, shape (events, B). ``bol`` overrides the initial energy
    (e.g. a quantile of :func:`required_bol`).
    """
    n = _batch(case_mc)
    bol = np.broadcast_to(np.asarray(plan.bol_mwh if bol is None else bol, float), (n,)).copy()
    if not plan.events:
        return _run(case_mc, bol, []), np.empty((0, n))
    fleet = Fleet.build(bol)
    years = [e.year for e in plan.events]
    stops = years[1:] + [case_mc.years + 1]
    advance(fleet, case_mc, years[0] - 1)
    sizes = []
    for ev, stop in zip(plan.events, stops):
        hi = 1.0 if ev.kind == "replace" else hi_factor * case_mc.e_req
        s, fleet = bridge(fleet, case_mc, ev.kind, ev.year, stop, 0.0, hi)
        sizes.append(np.where(np.isfinite(s), s, hi))
    events = [(e.year, e.kind, s) for e, s in zip(plan.events, sizes)]
    return _run(case_mc, bol, events), np.array(sizes).reshape(len(sizes), n)

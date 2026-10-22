"""Operating profiles of a 1 MW battery: perfect-foresight RCE arbitrage (MILP, HiGHS via SciPy) and a synthetic
reserve-provision profile with shallow cycles.

Arbitrage. Each delivery day with known 15-minute prices p_t (PLN/MWh, dt = 0.25 h) is solved as

    max  sum_t p_t dt (d_t - c_t)  -  w sum_t dt d_t
    s.t. e_t = e_{t-1} + eta dt c_t - dt d_t / eta                  (one-way efficiency eta = sqrt(RTE))
         lo E <= e_t <= hi E,   e_0 = e_end = (lo + hi) / 2 E        (operating SoC window, no free energy)
         0 <= c_t <= P u_t,  0 <= d_t <= P (1 - u_t),  u_t binary    (no simultaneous charge and discharge)
         sum_t dt d_t <= n_max (hi - lo) E                           (cycle cap, optional)

``w`` is a small wear cost that breaks ties between trades of no value. Prices are known the day before delivery,
so this is the upper bound of a pure day-ahead arbitrage strategy.

Reserves. Balancing activation is not public at the resolution needed, so the reserve profile is synthetic: the
activated power (fraction of P, positive = discharge) is the sum of two Ornstein-Uhlenbeck processes, a fast one
(frequency containment, minutes) and a slower one (frequency restoration, tens of minutes), clipped to [-1, 1], plus
a proportional SoC management that steers the battery back to 50 % (as an operator does with intraday trades). It
produces many shallow cycles around mid-SoC; its parameters are assumptions in ``config/assumptions.toml``.

Usage:
    from bess_augment.dispatch import ArbitrageParams, run_arbitrage, reserve_profile
    res = run_arbitrage(prices, ArbitrageParams(energy_mwh=2.0, max_cycles=1.0), jobs=8)
    soc = reserve_profile(energy_mwh=2.0, days=365, seed=1)
"""
from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.signal import lfilter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbitrageParams:
    power_mw: float = 1.0
    energy_mwh: float = 2.0         # operating (usable) energy at the connection point
    rte: float = 0.87               # round-trip efficiency at the connection point
    soc_lo: float = 0.0             # operating window, fraction of energy_mwh
    soc_hi: float = 1.0
    max_cycles: float = np.inf      # per day, in cycles of the operating window
    wear_cost: float = 1.0          # PLN per MWh discharged (objective only)

    @property
    def eta(self) -> float:
        return float(np.sqrt(self.rte))


def optimize_day(prices: np.ndarray, p: ArbitrageParams, dt: float = 0.25) -> tuple[float, float, np.ndarray]:
    """Solve one day. Returns (revenue PLN, discharged MWh, SoC fraction at the end of every interval)."""
    price = np.asarray(prices, float)
    n = len(price)
    eta, P = p.eta, p.power_mw
    e_lo, e_hi = p.energy_mwh * p.soc_lo, p.energy_mwh * p.soc_hi
    e0 = (e_lo + e_hi) / 2
    ic, idd, ie, iu = 0, n, 2 * n, 3 * n
    nv = 4 * n
    cost = np.zeros(nv)
    cost[ic:ic + n] = price * dt
    cost[idd:idd + n] = -(price - p.wear_cost) * dt
    eye = sparse.identity(n, format="csr")
    shift = sparse.eye(n, k=-1, format="csr")
    zeros = sparse.csr_matrix((n, n))
    balance = sparse.hstack([-eta * dt * eye, (dt / eta) * eye, eye - shift, zeros])
    rhs = np.zeros(n)
    rhs[0] = e0
    end_row = sparse.csr_matrix(([1.0], ([0], [ie + n - 1])), shape=(1, nv))
    ch_mode = sparse.hstack([eye, zeros, zeros, -P * eye])
    dis_mode = sparse.hstack([zeros, eye, zeros, P * eye])
    constraints = [LinearConstraint(balance, rhs, rhs), LinearConstraint(end_row, e0, e0),
                   LinearConstraint(ch_mode, -np.inf, 0.0), LinearConstraint(dis_mode, -np.inf, P)]
    if np.isfinite(p.max_cycles):
        thr = sparse.csr_matrix((np.full(n, dt), (np.zeros(n, int), np.arange(idd, idd + n))), shape=(1, nv))
        constraints.append(LinearConstraint(thr, 0.0, p.max_cycles * (e_hi - e_lo)))
    lb = np.concatenate([np.zeros(2 * n), np.full(n, e_lo), np.zeros(n)])
    ub = np.concatenate([np.full(2 * n, P), np.full(n, e_hi), np.ones(n)])
    integrality = np.concatenate([np.zeros(3 * n), np.ones(n)])
    res = milp(cost, constraints=constraints, bounds=Bounds(lb, ub), integrality=integrality,
               options={"disp": False, "mip_rel_gap": 1e-6})
    if not res.success:
        raise RuntimeError(f"MILP failed: {res.message}")
    c, d, e = res.x[ic:ic + n], res.x[idd:idd + n], res.x[ie:ie + n]
    c = np.where(c < 1e-9, 0.0, c)
    d = np.where(d < 1e-9, 0.0, d)
    return float(np.sum(price * dt * (d - c))), float(d.sum() * dt), np.clip(e / p.energy_mwh, 0.0, 1.0)


def _solve_days(args) -> list[tuple[float, float, np.ndarray]]:
    days, p = args
    return [optimize_day(x, p) for x in days]


@dataclass
class ProfileResult:
    soc: np.ndarray                 # SoC fraction of the operating energy, one value per interval (start included)
    dt_hours: float
    daily: pd.DataFrame             # per day: revenue_pln, discharged_mwh
    revenue_per_year: float         # PLN per year (365 days)
    discharged_per_year: float      # MWh per year


def run_arbitrage(prices: pd.DataFrame, p: ArbitrageParams, jobs: int = 1) -> ProfileResult:
    """Optimise every complete business day of ``prices`` (columns price, business_date) and chain the days."""
    groups = [(d, g["price"].to_numpy(float)) for d, g in prices.groupby("business_date", sort=True)]
    groups = [(d, x) for d, x in groups if len(x) >= 92 and not np.isnan(x).any()]
    days = [x for _, x in groups]
    chunks = [days[i::max(jobs, 1)] for i in range(max(jobs, 1))]
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as ex:
            parts = list(ex.map(_solve_days, [(c, p) for c in chunks]))
    else:
        parts = [_solve_days((chunks[0], p))]
    out: list = [None] * len(days)
    for j, part in enumerate(parts):          # undo the round-robin split
        for k, r in enumerate(part):
            out[j + k * len(chunks)] = r
    start = (p.soc_lo + p.soc_hi) / 2
    soc = np.concatenate([[start]] + [r[2] for r in out])
    daily = pd.DataFrame({"revenue_pln": [r[0] for r in out], "discharged_mwh": [r[1] for r in out]},
                         index=pd.Index([d for d, _ in groups], name="business_date"))
    scale = 365.0 / len(daily)
    return ProfileResult(soc=soc, dt_hours=0.25, daily=daily, revenue_per_year=float(daily["revenue_pln"].sum() * scale),
                         discharged_per_year=float(daily["discharged_mwh"].sum() * scale))


@dataclass(frozen=True)
class ReserveParams:
    fast_std: float = 0.10          # std of the fast activation component (fraction of P)
    fast_tau_min: float = 3.0       # its correlation time, minutes
    slow_std: float = 0.20
    slow_tau_min: float = 30.0
    soc_tau_h: float = 2.0          # time constant of the SoC management towards 50 %
    rte: float = 0.87


def reserve_profile(energy_mwh: float, power_mw: float = 1.0, days: int = 365, seed: int = 1,
                    p: ReserveParams = ReserveParams()) -> ProfileResult:
    """Synthetic one-minute SoC profile of reserve provision (see the module docstring)."""
    rng = np.random.default_rng(seed)
    n = days * 1440
    dt_min = 1.0

    def ou(std, tau):                            # AR(1) recursion x_i = a x_{i-1} + noise_i
        a = np.exp(-dt_min / tau)
        return lfilter([1.0], [1.0, -a], rng.standard_normal(n) * std * np.sqrt(1 - a * a))

    act = ou(p.fast_std, p.fast_tau_min) + ou(p.slow_std, p.slow_tau_min)
    eta = np.sqrt(p.rte)
    soc = np.empty(n + 1)
    soc[0] = 0.5
    dt_h = dt_min / 60
    k = 1.0 / p.soc_tau_h
    discharged = 0.0
    for i in range(n):
        u = act[i] + k * (soc[i] - 0.5) * energy_mwh / power_mw       # management: discharge when above 50 %
        u = min(max(u, -1.0), 1.0)
        mw = u * power_mw
        if mw >= 0:
            de = -mw * dt_h / eta
            discharged += mw * dt_h
        else:
            de = -mw * dt_h * eta
        soc[i + 1] = min(max(soc[i] + de / energy_mwh, 0.0), 1.0)
    daily = pd.DataFrame({"revenue_pln": np.nan, "discharged_mwh": discharged / days}, index=pd.RangeIndex(days))
    return ProfileResult(soc=soc, dt_hours=dt_h, daily=daily, revenue_per_year=float("nan"),
                         discharged_per_year=discharged * 365.0 / days)

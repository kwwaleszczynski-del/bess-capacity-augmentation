"""Semi-empirical capacity-fade model of an LFP battery (calendar + rainflow cycle ageing) and its fit to cell data.

Capacity loss (fraction of the initial capacity) is the sum of two power laws:

    Q_cal(t) = k_cal * A(T, Ea_cal) * exp(gamma * (SoC - 0.5)) * t ** z_cal          t in days
    Q_cyc(N) = k_cyc * A(T, Ea_cyc) * N_eq ** z_cyc,   N_eq = sum_i n_i * DoD_i ** beta
    A(T, Ea) = exp(-Ea / R * (1 / T - 1 / T_ref)),    T_ref = 25 degC

N_eq counts rainflow cycles (n_i = 1 or 0.5, depth DoD_i) as equivalent 100 %-deep cycles. beta = 1 makes cycle
ageing depend on energy throughput only, beta > 1 makes deep cycles more harmful per MWh than shallow ones. This is
the structure of Schmalstieg et al. (J. Power Sources 257, 2014) and Naumann et al. (J. Energy Storage 17, 2018;
J. Power Sources 451, 2020) with the rainflow cycle stress of Xu et al. (IEEE Trans. Smart Grid 9, 2018).

When conditions change, each term is accumulated in "virtual time", which is exact for a power law: Q ** (1 / z)
grows at the rate k ** (1 / z), so Q(t) = (sum_i k_i ** (1 / z) dt_i) ** z.

The parameters are fitted to the checkup capacities of large-format LFP cells for stationary storage (Yagci et al.,
J. Energy Storage 124, 2025; data on Zenodo, CC BY 4.0), shipped in ``resources/yagci2025_checkups_50A.csv``: eight
cells stored at 75 % and 100 % SoC at 35 and 50 degC (calendar term) and four cells cycled at full depth in 35 and
50 degC chambers (cycle term, after subtracting the fitted calendar ageing).

Usage:
    from bess_augment.aging import AgingModel, profile_stress, fit_cells
    fit = fit_cells()                          # parameters, standard errors, residuals
    stress = profile_stress(soc_15min, dt_hours=0.25)
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib import resources

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from bess_augment.rainflow import count_cycles

R_GAS = 8.314
T_REF = 298.15
BETA_GRID = np.linspace(0.5, 2.5, 41)


def arrhenius(temp_c, ea) -> np.ndarray:
    return np.exp(-np.asarray(ea, float) / R_GAS * (1.0 / (np.asarray(temp_c, float) + 273.15) - 1.0 / T_REF))


@dataclass(frozen=True)
class AgingModel:
    """Model parameters; every field may also be a 1-d array (one value per Monte Carlo sample)."""
    k_cal: float = 1.64e-3         # fraction per day**z_cal at 25 degC and SoC 0.5
    ea_cal: float = 30_500.0       # J/mol
    gamma: float = 0.0             # SoC sensitivity of calendar ageing (per unit SoC, exponential)
    z_cal: float = 0.5
    k_cyc: float = 2.1e-5          # fraction per equivalent full cycle**z_cyc at 25 degC
    ea_cyc: float = 31_800.0       # J/mol
    z_cyc: float = 1.0
    beta: float = 1.0              # depth-of-cycle exponent (not identifiable from full-depth cell tests)

    def k_cal_at(self, soc, temp_c) -> np.ndarray:
        return self.k_cal * arrhenius(temp_c, self.ea_cal) * np.exp(self.gamma * (np.asarray(soc, float) - 0.5))

    def k_cyc_at(self, temp_c) -> np.ndarray:
        return self.k_cyc * arrhenius(temp_c, self.ea_cyc)

    def calendar_loss(self, days, soc, temp_c) -> np.ndarray:
        """Calendar loss after ``days`` at constant SoC and temperature."""
        return self.k_cal_at(soc, temp_c) * np.asarray(days, float) ** self.z_cal

    def cycle_loss(self, n_eq, temp_c) -> np.ndarray:
        return self.k_cyc_at(temp_c) * np.asarray(n_eq, float) ** self.z_cyc

    def with_(self, **kw) -> "AgingModel":
        return replace(self, **kw)


@dataclass(frozen=True)
class Stress:
    """Ageing-relevant summary of one year of an operating profile (SoC as a fraction of the operating energy)."""
    levels: np.ndarray             # mean SoC of each occupied SoC bin
    weights: np.ndarray            # share of time in each bin (sums to 1)
    fec_per_year: float            # equivalent full cycles per year (sum of depth * count)
    mean_soc: float
    n_eq_grid: np.ndarray = field(repr=False)   # sum(count * depth**beta) per year on BETA_GRID
    depth_hist: pd.DataFrame = field(repr=False)

    def n_eq_per_year(self, beta) -> np.ndarray:
        """Equivalent cycles per year for a depth exponent ``beta`` (scalar or array)."""
        return np.exp(np.interp(beta, BETA_GRID, np.log(np.maximum(self.n_eq_grid, 1e-300))))

    def scaled(self, r: float) -> "Stress":
        """The same profile on a battery with 1/r times more energy: SoC compressed around 0.5 by the factor r."""
        levels = (1 - r) / 2 + r * self.levels
        return replace(self, levels=levels, fec_per_year=self.fec_per_year * r,
                       mean_soc=(1 - r) / 2 + r * self.mean_soc, n_eq_grid=self.n_eq_grid * r ** BETA_GRID)


def profile_stress(soc, dt_hours: float, bins: int = 20, min_range: float = 0.0) -> Stress:
    """SoC histogram and rainflow cycles of a SoC profile (fraction, evenly spaced every ``dt_hours``)."""
    s = np.clip(np.asarray(soc, float), 0.0, 1.0)
    s = s[~np.isnan(s)]
    b = np.minimum((s * bins).astype(int), bins - 1)
    count = np.bincount(b, minlength=bins)
    total = np.bincount(b, weights=s, minlength=bins)
    used = count > 0
    cycles = count_cycles(s, min_range)
    years = len(s) * dt_hours / 8760
    depth, n = cycles["depth"].to_numpy(), cycles["count"].to_numpy()
    grid = np.array([(n * depth ** bt).sum() for bt in BETA_GRID]) / years
    edges = np.array([0, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0])
    cut = pd.cut(depth, edges, include_lowest=True)
    hist = (pd.DataFrame({"cycles": n, "efc": n * depth}).groupby(cut, observed=False).sum() / years)
    hist.index = [f"{a:.0%}-{b:.0%}" for a, b in zip(edges[:-1], edges[1:])]
    return Stress(levels=total[used] / count[used], weights=count[used] / count.sum(),
                  fec_per_year=float((depth * n).sum() / years), mean_soc=float(s.mean()), n_eq_grid=grid,
                  depth_hist=hist)


# ---------------------------------------------------------------------------------------------------------------------
# Fit to the cell data
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class FitResult:
    params: dict
    stderr: dict
    c0: pd.Series                  # fitted initial capacity per cell (Ah)
    rmse_pct: float                # residual RMSE, % of initial capacity
    residuals: pd.DataFrame
    cov: np.ndarray                # covariance of the named parameters (in their own units)


def load_cells() -> pd.DataFrame:
    """Checkup capacities (50 A discharge, Ah) of the twelve cells; see the module docstring for the source."""
    with resources.files("bess_augment.resources").joinpath("yagci2025_checkups_50A.csv").open() as f:
        return pd.read_csv(f)


def _fit(data: pd.DataFrame, loss_fn, names: list[str], x0, lower, upper, scale) -> FitResult:
    cells = list(data["cell"].unique())
    ci = data["cell"].map({c: i for i, c in enumerate(cells)}).to_numpy()
    c0_init = data.groupby("cell")["q_dis_ah"].max().reindex(cells).to_numpy()
    q = data["q_dis_ah"].to_numpy(float)
    scale = np.asarray(scale, float)

    def resid(z):
        p = dict(zip(names, z[:len(names)] * scale))
        c0 = z[len(names):]
        return (c0[ci] * (1 - loss_fn(p, data)) - q) / c0[ci]

    z0 = np.r_[np.asarray(x0) / scale, c0_init]
    lb = np.r_[np.asarray(lower) / scale, c0_init * 0.9]
    ub = np.r_[np.asarray(upper) / scale, c0_init * 1.1]
    res = least_squares(resid, z0, bounds=(lb, ub))
    r = res.fun
    dof = max(len(r) - len(res.x), 1)
    k = len(names)
    try:
        cov = np.linalg.inv(res.jac.T @ res.jac) * (r @ r) / dof
    except np.linalg.LinAlgError:
        cov = np.full((len(res.x), len(res.x)), np.nan)
    se = np.sqrt(np.diag(cov))
    out = data[["cell", "checkup", "days", "q_dis_ah"]].copy()
    out["resid_pct"] = r * 100
    return FitResult(params=dict(zip(names, res.x[:len(names)] * scale)),
                     stderr=dict(zip(names, se[:len(names)] * scale)),
                     c0=pd.Series(res.x[len(names):], index=cells),
                     rmse_pct=float(np.sqrt(np.mean(r ** 2)) * 100), residuals=out,
                     cov=cov[:k, :k] * np.outer(scale, scale))


def fit_calendar(cells: pd.DataFrame) -> FitResult:
    """k_cal, Ea_cal and gamma on the storage cells (z_cal = 0.5), with a free initial capacity per cell.

    Checkup 0 (formation, on another cycler) is excluded: several cells gained 1-3 % capacity between it and the
    first regular checkup, a measurement offset rather than ageing.
    """
    d = cells[(cells["mode"] == "calendar") & (cells["checkup"] > 0)].copy()
    d["soc"] = d["storage_soc"] / 100.0

    def loss(p, x):
        return p["k_cal"] * arrhenius(x["chamber_temp_c"], p["ea_cal"]) * np.exp(p["gamma"] * (x["soc"] - 0.5)) \
            * x["days"] ** 0.5

    res = _fit(d, loss, ["k_cal", "ea_cal", "gamma"], [2e-3, 40_000, 1.0], [1e-5, 0, -10], [1e-1, 150_000, 10],
               [1e-3, 1e4, 1.0])
    res.params["z_cal"] = 0.5
    return res


def fit_cycle(cells: pd.DataFrame, calendar: AgingModel) -> FitResult:
    """k_cyc and Ea_cyc (z_cyc = 1) on the cycling cells at their measured temperature, after subtracting the
    calendar ageing the fitted calendar model predicts for their test time (averaged over SoC 0..1)."""
    d = cells[(cells["mode"] == "cycling") & (cells["checkup"] > 0)].copy()
    socs = np.linspace(0, 1, 21)
    temps = d["cell_temp_c"].to_numpy(float)
    k_cal = np.array([np.mean(calendar.k_cal_at(socs, t) ** 2) ** 0.5 for t in temps])
    cal = k_cal * d["days"].to_numpy(float) ** 0.5
    n_eq = d["checkup"].to_numpy(float)

    def loss(p, x):
        return cal + p["k_cyc"] * arrhenius(temps, p["ea_cyc"]) * n_eq

    res = _fit(d, loss, ["k_cyc", "ea_cyc"], [3e-5, 30_000], [1e-8, 0], [1e-2, 150_000], [1e-5, 1e4])
    res.params["z_cyc"] = 1.0
    return res


def fit_cells(cells: pd.DataFrame | None = None) -> dict:
    """Fit both terms; returns {'model': AgingModel, 'calendar': FitResult, 'cycle': FitResult}."""
    cells = load_cells() if cells is None else cells
    cal = fit_calendar(cells)
    cal_model = AgingModel(k_cal=cal.params["k_cal"], ea_cal=cal.params["ea_cal"], gamma=cal.params["gamma"])
    cyc = fit_cycle(cells, cal_model)
    model = cal_model.with_(k_cyc=cyc.params["k_cyc"], ea_cyc=cyc.params["ea_cyc"])
    return {"model": model, "calendar": cal, "cycle": cyc}

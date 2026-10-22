"""Available energy of a battery made of cohorts of modules (initial build, augmentations, replacements).

Every cohort keeps its own ageing state: calendar time since installation and the cycles it has seen. The battery
is operated as a fixed product (constant-energy operation): the energy management system dispatches an operating
energy ``e_op`` (MWh at the connection point), and any installed energy above it is headroom. All cohorts share the
relative state of charge, so with available energy E the cells see the operating profile compressed by
r = min(1, e_op / E) around 50 % SoC: cycle depths times r and SoC levels (1 - r) / 2 + r * SoC. Headroom therefore
both keeps the energy above the requirement and slows the ageing (shallower cycles, mid-range SoC).

Each ageing term is accumulated per cohort in virtual time (exact for the power laws of :mod:`bess_augment.aging`).
All state arrays carry a leading batch dimension B, so one call simulates many candidate sizes or Monte Carlo
samples at once; model parameters may be scalars or arrays of shape (B,).

Usage:
    from bess_augment.fleet import Case, Fleet, simulate, Plan, Event
    case = Case(model, stress, temp_c=25, e_op=2.0, e_req=2.0, years=17)
    table = simulate(Plan("overbuild", bol_mwh=2.8), case)          # one row per contract year
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

import numpy as np
import pandas as pd

from bess_augment.aging import AgingModel, Stress, arrhenius


def _col(x):
    x = np.asarray(x, float)
    return x if x.ndim == 0 else x[:, None]


@dataclass(frozen=True)
class Case:
    """Everything that drives ageing and compliance for one battery and operating profile."""
    model: AgingModel
    stress: Stress                 # operating profile (SoC as a fraction of e_op)
    temp_c: float | np.ndarray     # cell temperature, degC (scalar or per batch element)
    e_op: float                    # operating energy, MWh at the connection point
    e_req: float                   # energy the capacity obligation requires, MWh
    years: int                     # contract length (delivery years 1..years)
    steps_per_year: int = 2

    def tiled(self, g: int) -> "Case":
        """Repeat every per-sample parameter g times (sample-major), for a batch of g candidates per sample."""
        def rep(x):
            x = np.asarray(x, float)
            return x if x.ndim == 0 else np.repeat(x, g)
        model = replace(self.model, **{f.name: rep(getattr(self.model, f.name)) for f in fields(self.model)})
        return replace(self, model=model, temp_c=rep(self.temp_c))


@dataclass
class Fleet:
    energy: np.ndarray             # (B, C) energy of each cohort when new, MWh
    a_cal: np.ndarray              # (B, C) accumulated calendar virtual quantity, Q_cal ** (1 / z_cal)
    a_cyc: np.ndarray              # (B, C) same for cycle ageing
    year: int = 1                  # next contract year to be simulated

    @classmethod
    def build(cls, energy) -> "Fleet":
        e = np.atleast_1d(np.asarray(energy, float))[:, None]
        return cls(e, np.zeros_like(e), np.zeros_like(e))

    @property
    def batch(self) -> int:
        return self.energy.shape[0]

    def copy(self) -> "Fleet":
        return Fleet(self.energy.copy(), self.a_cal.copy(), self.a_cyc.copy(), self.year)

    def tiled(self, g: int) -> "Fleet":
        return Fleet(np.repeat(self.energy, g, 0), np.repeat(self.a_cal, g, 0), np.repeat(self.a_cyc, g, 0), self.year)

    def soh(self, model: AgingModel) -> np.ndarray:
        loss = self.a_cal ** _col(model.z_cal) + self.a_cyc ** _col(model.z_cyc)
        return np.clip(1.0 - loss, 0.0, 1.0)

    def available(self, model: AgingModel) -> np.ndarray:
        return (self.energy * self.soh(model)).sum(1)

    def installed(self) -> np.ndarray:
        return self.energy.sum(1)

    def augment(self, mwh) -> "Fleet":
        """Add a new cohort of ``mwh`` (per batch element)."""
        add = np.broadcast_to(np.asarray(mwh, float), (self.batch,))[:, None]
        z = np.zeros_like(add)
        return Fleet(np.hstack([self.energy, add]), np.hstack([self.a_cal, z]), np.hstack([self.a_cyc, z]), self.year)

    def replace_modules(self, frac, cohort: int = 0) -> "Fleet":
        """Replace a fraction of one cohort's modules by new ones (same energy when new)."""
        f = np.broadcast_to(np.asarray(frac, float), (self.batch,))
        new = self.energy[:, cohort] * f
        energy = self.energy.copy()
        energy[:, cohort] -= new
        out = Fleet(energy, self.a_cal, self.a_cyc, self.year)
        return out.augment(new)


def advance(fleet: Fleet, case: Case, n_years: int = 1) -> np.ndarray:
    """Operate ``n_years`` in place; returns the available energy at the end of every year, shape (n_years, B)."""
    m, s = case.model, case.stress
    dt = 1.0 / case.steps_per_year
    z_cal, z_cyc = _col(m.z_cal), _col(m.z_cyc)
    k_cyc = np.asarray(m.k_cyc, float) * arrhenius(case.temp_c, m.ea_cyc)
    neq_full = s.n_eq_per_year(np.asarray(m.beta, float))                       # at r = 1
    k_cal = _col(np.asarray(m.k_cal, float) * arrhenius(case.temp_c, m.ea_cal))
    gamma = _col(m.gamma)
    out = np.empty((n_years, fleet.batch))
    for y in range(n_years):
        for _ in range(case.steps_per_year):
            avail = fleet.available(m)
            r = np.clip(case.e_op / np.maximum(avail, 1e-12), 0.0, 1.0)
            levels = (1 - r)[:, None] / 2 + r[:, None] * s.levels[None, :]
            k = k_cal * np.exp(gamma * (levels - 0.5))
            rate_cal = (s.weights[None, :] * k ** (1 / z_cal)).sum(1)               # per day
            neq = neq_full * r ** np.asarray(m.beta, float) * dt
            fleet.a_cal += (rate_cal * 365.25 * dt)[:, None]
            fleet.a_cyc += (k_cyc ** (1 / np.asarray(m.z_cyc, float)) * neq)[:, None]
        out[y] = fleet.available(m)
        fleet.year += 1
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    year: int                      # at the start of this contract year
    kind: str                      # "augment" (size in MWh) or "replace" (fraction of the initial modules)
    size: float


@dataclass(frozen=True)
class Plan:
    name: str
    bol_mwh: float                 # energy installed at the start
    events: tuple[Event, ...] = field(default_factory=tuple)

    def label(self) -> str:
        parts = [f"BOL {self.bol_mwh:.2f} MWh"]
        for e in self.events:
            parts.append(f"y{e.year}: +{e.size:.2f} MWh" if e.kind == "augment" else f"y{e.year}: replace {e.size:.0%}")
        return ", ".join(parts)


def apply_event(fleet: Fleet, kind: str, size) -> Fleet:
    if kind == "augment":
        return fleet.augment(size)
    if kind == "replace":
        return fleet.replace_modules(size)
    raise ValueError(kind)


def simulate(plan: Plan, case: Case) -> pd.DataFrame:
    """Year-by-year energy of a plan (single case, batch 1)."""
    fleet = Fleet.build(plan.bol_mwh)
    by_year = {e.year: e for e in plan.events}
    rows = []
    for y in range(1, case.years + 1):
        added = replaced = 0.0
        if y in by_year:
            e = by_year[y]
            if e.kind == "augment":
                added = e.size
            else:
                replaced = e.size * fleet.energy[0, 0]
            fleet = apply_event(fleet, e.kind, e.size)
        start = float(fleet.available(case.model)[0])
        end = float(advance(fleet, case, 1)[0, 0])
        rows.append({"year": y, "installed_mwh": float(fleet.installed()[0]), "added_mwh": added,
                     "replaced_mwh": replaced, "avail_start_mwh": start, "avail_end_mwh": end,
                     "soh_initial_modules": float(fleet.soh(case.model)[0, 0]),
                     "shortfall_frac": max(0.0, 1.0 - end / case.e_req)})
    return pd.DataFrame(rows)


def bridge(fleet: Fleet | None, case: Case, kind: str, start: int, stop: int, lo, hi, grid: int = 17,
           rounds: int = 3, batch: int = 1) -> tuple[np.ndarray, Fleet]:
    """Smallest action size in [lo, hi] (per batch element) such that the end-of-year available energy stays at or
    above ``case.e_req`` in years start..stop-1 when the action is applied at the start of year ``start``.

    ``fleet`` is the state at the start of year ``start`` (or None to build the initial battery, for ``batch``
    samples of ``case``). Returns the sizes
    (inf where even ``hi`` is not enough) and the fleet at the start of year ``stop`` after acting with them.
    Feasibility is monotone in the size, so a grid search refined ``rounds`` times brackets the threshold.
    """
    b = batch if fleet is None else fleet.batch
    lo = np.broadcast_to(np.asarray(lo, float), (b,)).copy()
    hi = np.broadcast_to(np.asarray(hi, float), (b,)).copy()
    case_g = case.tiled(grid)

    def run(sizes: np.ndarray) -> np.ndarray:            # sizes (b, g) -> feasible (b, g)
        flat = sizes.reshape(-1)
        if fleet is None:
            f = Fleet.build(flat)
        else:
            f = apply_event(fleet.tiled(grid), kind, flat)
        ends = advance(f, case_g, stop - start)
        return (ends >= case.e_req * (1 - 1e-9)).all(0).reshape(b, grid)

    a, z = lo.copy(), hi.copy()
    ok_hi = None
    for _ in range(rounds):
        cand = a[:, None] + (z - a)[:, None] * np.linspace(0, 1, grid)[None, :]
        feas = run(cand)
        if ok_hi is None:                                # the first grid ends at hi
            ok_hi = feas[:, -1]
        first = np.where(feas.any(1), feas.argmax(1), grid - 1)
        z = cand[np.arange(b), first]
        a = cand[np.arange(b), np.maximum(first - 1, 0)]
        a = np.where(first == 0, z, a)
    size = np.where(ok_hi, z, np.inf)
    use = np.where(np.isfinite(size), size, hi)
    f = Fleet.build(use) if fleet is None else apply_event(fleet, kind, use)
    advance(f, case, stop - start)
    return size, f

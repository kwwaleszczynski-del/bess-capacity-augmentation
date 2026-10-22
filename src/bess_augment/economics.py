"""Cash flows and NPV of a plan: capacity payments, arbitrage revenue, battery capex, augmentation, module
replacement, O&M and the cost of not meeting the capacity obligation. All values are real (constant prices).

Timing: the battery is built at t = 0 and delivers in contract years 1..L; actions planned for year y happen at its
start (t = y - 1), operating cash flows at its end (t = y).

Usage:
    from bess_augment.economics import Economics, cashflows
    cf, summary = cashflows(plan, table, econ, revenue_curve)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from bess_augment.fleet import Plan


@dataclass(frozen=True)
class Economics:
    power_mw: float
    e_op: float                         # operating energy, MWh
    obligation_mw: float                # capacity obligation (power x KWD)
    capacity_price: float               # PLN per MW-year of obligation
    shortfall_penalty: float            # PLN per MW-year of shortfall on top of the lost payment (expected penalty)
    energy_cost: np.ndarray             # PLN per MWh of energy-related capex, by contract year 0..L
    power_cost: float                   # PLN per MW, power-related capex (PCS, transformer, grid), year 0
    augment_premium: float              # extra share on the energy cost for augmentation
    augment_fixed: float                # PLN per augmentation event (per MW of plant)
    module_share: float                 # share of the energy cost that is modules (replacement)
    replace_extra: float                # PLN per MWh replaced: removal, disposal, labour
    replace_fixed: float                # PLN per replacement event
    om_per_mw: float                    # PLN per MW-year
    om_per_mwh: float                   # PLN per installed MWh-year
    discount_rate: float
    arbitrage_capture: float = 1.0      # share of the perfect-foresight revenue earned


def interp_revenue(curve: pd.Series | None, duration_h) -> np.ndarray:
    """Arbitrage revenue (PLN per MW-year) at a usable duration, interpolated in ``curve`` (index: hours)."""
    if curve is None or curve.isna().all():
        return np.zeros_like(np.asarray(duration_h, float))
    x = curve.index.to_numpy(float)
    return np.interp(duration_h, x, curve.to_numpy(float), left=0.0)


def cashflows(plan: Plan, table: pd.DataFrame, econ: Economics, revenue_curve: pd.Series | None = None,
              revenue_scale: float = 1.0) -> tuple[pd.DataFrame, dict]:
    """Yearly cash flows of a simulated plan (``table`` from :func:`bess_augment.fleet.simulate`).

    ``revenue_curve`` gives the arbitrage revenue of the operating profile by usable duration; ``revenue_scale``
    rescales it (a restricted operating profile earns a fixed share of the unrestricted one at equal energy).
    """
    years = table["year"].to_numpy(int)
    disc_end = (1 + econ.discount_rate) ** -years
    disc_start = (1 + econ.discount_rate) ** -(years - 1.0)
    e = econ.energy_cost
    usable = np.minimum(econ.e_op, (table["avail_start_mwh"] + table["avail_end_mwh"]).to_numpy() / 2)
    arbitrage = interp_revenue(revenue_curve, usable / econ.power_mw) * econ.power_mw * revenue_scale \
        * econ.arbitrage_capture
    short = table["shortfall_frac"].to_numpy() * econ.obligation_mw
    capacity = econ.obligation_mw * econ.capacity_price - short * econ.capacity_price
    penalty = short * econ.shortfall_penalty
    om = econ.om_per_mw * econ.power_mw + econ.om_per_mwh * table["installed_mwh"].to_numpy()
    aug = np.zeros(len(years))
    rep = np.zeros(len(years))
    for ev in plan.events:
        i = ev.year - 1
        if ev.kind == "augment":
            aug[i] += ev.size * e[ev.year - 1] * (1 + econ.augment_premium) + econ.augment_fixed * econ.power_mw
        else:
            mwh = table.loc[i, "replaced_mwh"]
            rep[i] += mwh * (e[ev.year - 1] * econ.module_share + econ.replace_extra) \
                + econ.replace_fixed * econ.power_mw
    capex0 = plan.bol_mwh * e[0] + econ.power_cost * econ.power_mw
    cf = pd.DataFrame({"year": years, "capacity_pln": capacity, "arbitrage_pln": arbitrage, "penalty_pln": -penalty,
                       "om_pln": -om, "augment_pln": -aug, "replace_pln": -rep})
    operating = capacity + arbitrage - penalty - om
    pv = {
        "capex0": capex0,
        "pv_capacity": float((capacity * disc_end).sum()),
        "pv_arbitrage": float((arbitrage * disc_end).sum()),
        "pv_penalty": float((penalty * disc_end).sum()),
        "pv_om": float((om * disc_end).sum()),
        "pv_augment": float((aug * disc_start).sum()),
        "pv_replace": float((rep * disc_start).sum()),
    }
    pv["npv"] = float((operating * disc_end).sum() - (aug * disc_start).sum() - (rep * disc_start).sum() - capex0)
    pv["pv_cost"] = capex0 + pv["pv_om"] + pv["pv_augment"] + pv["pv_replace"] + pv["pv_penalty"]
    pv["shortfall_mw_years"] = float(short.sum())
    pv["years_short"] = int((table["shortfall_frac"] > 1e-6).sum())
    first = table.loc[table["shortfall_frac"] > 1e-6, "year"]
    pv["first_short_year"] = int(first.iloc[0]) if len(first) else 0
    return cf, pv

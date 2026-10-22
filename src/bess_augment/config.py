"""Assumptions file (TOML) and the objects built from it.

Every number that is not computed from data lives in ``config/assumptions.toml``, with its source next to it.

Capacity market (Poland). A battery's capacity obligation is at most its net achievable power times the availability
derating factor (KWD) of the auction it won; the net achievable power is the power it can sustain for at least four
consecutive hours, so for a battery P_net = min(P, E / 4 h). Two requirements follow:

    four_hour_rule   E >= 4 h * P_net          the energy behind the certified net achievable power
    call             E >= P_obl * H_call       the energy to deliver the obligation through a long call period
                                               (H_call: sum of the hourly obligation shares of the call, MWh per MW)

Usage:
    from bess_augment.config import load, economics_for, requirement_mwh
    cfg = load(Path("config/assumptions.toml"))
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from bess_augment.economics import Economics
from bess_augment.warranty import Warranty

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT = Path(__file__).resolve().parents[2] / "config" / "assumptions.toml"


def load(path: Path | None = None) -> dict:
    with open(path or DEFAULT, "rb") as f:
        return tomllib.load(f)


def vintage(cfg: dict, name: str | None = None) -> dict:
    cm = cfg["capacity_market"]
    return cm["vintages"][name or cm["base_vintage"]]


def kwd(cfg: dict, name: str | None = None) -> float:
    """Availability derating factor (KWD) of batteries in the given auction (one value for every duration)."""
    return float(vintage(cfg, name)["kwd"])


def net_power(cfg: dict, power_mw: float, duration_h: float) -> float:
    """Net achievable power: the power the battery sustains for the minimum duration (4 h)."""
    return min(power_mw, power_mw * duration_h / cfg["capacity_market"]["min_duration_h"])


def obligation_mw(cfg: dict, power_mw: float, duration_h: float, name: str | None = None) -> float:
    return net_power(cfg, power_mw, duration_h) * kwd(cfg, name)


def requirement_mwh(cfg: dict, power_mw: float, duration_h: float, name: str | None = None,
                    mode: str | None = None) -> float:
    """Energy (MWh at the connection point) the unit needs to keep meeting its obligation."""
    cm = cfg["capacity_market"]
    mode = mode or cm["requirement"]
    if mode == "four_hour_rule":
        return net_power(cfg, power_mw, duration_h) * cm["min_duration_h"]
    if mode == "call":
        return obligation_mw(cfg, power_mw, duration_h, name) * cm["design_call_mwh_per_mw"]
    raise ValueError(mode)


def cost_path(cfg: dict, years: int, scenario: str | None = None, first_delivery_year: int | None = None) -> np.ndarray:
    """Energy-related battery capex (PLN/MWh) by contract year 0..years: index 0 is the build (the calendar year
    before delivery starts); an action at the start of contract year y is bought at index y - 1."""
    c = cfg["costs"]
    pts = c["decline"][scenario or c["decline_base"]]
    xs = np.array([p[0] for p in pts], float)
    fs = np.array([p[1] for p in pts], float)
    first = first_delivery_year or vintage(cfg)["first_delivery_year"]
    cal = first - 1 + np.arange(years + 1)
    factor = np.interp(cal, xs, fs) / np.interp(c["base_year"], xs, fs)
    return c["energy_usd_per_kwh"] * c["fx_usd_pln"] * 1000 * factor


def economics_for(cfg: dict, power_mw: float, duration_h: float, years: int, decline: str | None = None,
                  discount_rate: float | None = None, vintage_name: str | None = None) -> Economics:
    cm, c, e = cfg["capacity_market"], cfg["costs"], cfg["economics"]
    v = vintage(cfg, vintage_name)
    fx = c["fx_usd_pln"]
    return Economics(
        power_mw=power_mw,
        e_op=power_mw * duration_h,
        obligation_mw=obligation_mw(cfg, power_mw, duration_h, vintage_name),
        capacity_price=v["price_pln_per_kw_year"] * 1000,
        shortfall_penalty=(cm["secondary_cover"] - 1) * v["price_pln_per_kw_year"] * 1000,
        energy_cost=cost_path(cfg, years, decline, v["first_delivery_year"]),
        power_cost=c["power_usd_per_kw"] * fx * 1000,
        augment_premium=c["augment_premium"],
        augment_fixed=c["augment_fixed_pln_per_mw"],
        module_share=c["module_share"],
        replace_extra=c["replace_extra_usd_per_kwh"] * fx * 1000,
        replace_fixed=c["replace_fixed_pln_per_mw"],
        om_per_mw=c["om_usd_per_kw_year"] * fx * 1000,
        om_per_mwh=c["om_usd_per_kwh_year"] * fx * 1000,
        discount_rate=e["discount_rate"] if discount_rate is None else discount_rate,
        arbitrage_capture=e["arbitrage_capture"],
    )


def call_penalty(cfg: dict, energy_mwh, obligation: float, name: str | None = None) -> np.ndarray:
    """Penalty (PLN) in a year with the configured call periods if the unit keeps its obligation with only
    ``energy_mwh`` available (fully charged before every call), capped at the annual limit."""
    cm = cfg["capacity_market"]
    e = np.asarray(energy_mwh, float)
    short = sum(np.maximum(0.0, obligation * h - e) for h in cm["calls_mwh_per_mw"])
    cap = cm["penalty_cap_multiple"] * obligation * vintage(cfg, name)["price_pln_per_kw_year"] * 1000
    return np.minimum(short * cm["penalty_pln_per_mwh"], cap)


def warranty_from(cfg: dict) -> Warranty:
    w = cfg["warranty"]
    return Warranty(years=w["years"], fec_per_year=w["fec_per_year"], fec_total=w["fec_total"],
                    retention=w["retention"], max_temp_c=w["max_temp_c"], max_c_rate=w["max_c_rate"],
                    max_mean_soc=w["max_mean_soc"])

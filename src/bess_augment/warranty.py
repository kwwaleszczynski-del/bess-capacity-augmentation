"""Generalised manufacturer warranty of a utility-scale LFP system and when an operating strategy breaks it.

Typical warranties (see ``config/assumptions.toml`` for the generalised terms and their public sources) guarantee a
minimum share of the initial energy (state of health) at the end of the warranty (year-by-year schedules are rarely
public, so only the end of the term is checked here), provided the battery stays within
a throughput allowance (equivalent full cycles per year and in total, counted on the energy at the start), a C-rate
and an average cell temperature. Here a strategy "breaks" the warranty in the first year in which it leaves one of
these limits: the annual cycle limit, the cumulative throughput allowance, the temperature, the C-rate or the average
state of charge.

Usage:
    from bess_augment.warranty import Warranty, check
    w = Warranty(years=15, fec_per_year=365, fec_total=5475, retention=0.7, max_temp_c=25, max_c_rate=0.5,
                 max_mean_soc=0.5)
    check(w, discharged_mwh_per_year=1300, bol_mwh=2.0, power_mw=1.0, temp_c=25,
          soh_initial=table["soh_initial_modules"], mean_soc=0.49)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Warranty:
    years: int                     # warranty period
    fec_per_year: float            # annual throughput limit, equivalent full cycles of the initial energy
    fec_total: float               # cumulative throughput allowance over the warranty
    retention: float               # guaranteed share of the initial energy at the end of the warranty
    max_temp_c: float              # maximum average cell temperature
    max_c_rate: float              # maximum continuous C-rate (power / energy)
    max_mean_soc: float = 1.0      # maximum average state of charge over a year


def check(w: Warranty, discharged_mwh_per_year: float, bol_mwh: float, power_mw: float, temp_c: float,
          soh_initial, mean_soc: float = 0.5) -> dict:
    """Which limits a strategy keeps and the first year it breaks the warranty (0 = kept for the whole term)."""
    fec = discharged_mwh_per_year / bol_mwh
    years = np.arange(1, w.years + 1)
    cum = fec * years
    breaks = []
    if fec > w.fec_per_year * (1 + 1e-9):
        breaks.append(1)
    over = years[cum > w.fec_total * (1 + 1e-9)]
    year_total = int(over[0]) if len(over) else 0
    if year_total:
        breaks.append(year_total)
    temp_ok = temp_c <= w.max_temp_c
    c_ok = power_mw / bol_mwh <= w.max_c_rate * (1 + 1e-9)
    soc_ok = mean_soc <= w.max_mean_soc
    if not (temp_ok and c_ok and soc_ok):
        breaks.append(1)
    soh = np.asarray(soh_initial, float)
    n = min(len(soh), w.years)
    end = float(soh[n - 1]) if n else float("nan")
    return {"fec_per_year": fec, "annual_limit_ok": fec <= w.fec_per_year * (1 + 1e-9),
            "throughput_used_up_year": year_total, "temp_ok": bool(temp_ok), "c_rate_ok": bool(c_ok),
            "mean_soc": float(mean_soc), "mean_soc_ok": bool(soc_ok),
            "break_year": min(breaks) if breaks else 0,
            "soh_end_of_warranty": end, "below_retention_at_end": bool(end < w.retention)}

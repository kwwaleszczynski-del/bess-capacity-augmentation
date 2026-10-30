"""RCE prices of PSE, read with the pse-opendata client (https://pypi.org/project/pse-opendata/).

RCE ("rynkowa cena energii", PLN/MWh) is published per 15-minute interval; until 30 September 2025 the four quarters
of an hour carry the hourly price, from the delivery day 1 October 2025 they are true 15-minute prices.

pse-opendata downloads the public PSE reports API (no key) and caches whole months as Parquet files in ``cache_dir``
(``api/rce-pln/<YYYY-MM>.parquet``); a month is downloaded again until three days after its end (the revision window
of prices). Frames returned here are indexed by interval START (UTC).

Usage:
    from bess_augment.pse_api import fetch_range
    prices = fetch_range(date(2025, 10, 1), date(2026, 9, 30), cache_dir=Path("data/rce"))
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import pse_opendata as pse

ENDPOINT, FIELD = "rce-pln", "rce_pln"
log = logging.getLogger(__name__)


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["price", "business_date"]).rename_axis("ts_utc")


def fetch_range(start: date, end: date, cache_dir: Path, refresh: bool = False,
                client: pse.Client | None = None) -> pd.DataFrame:
    """RCE for business dates start..end (inclusive): columns price and business_date, index ts_utc.

    Whole months are requested, so that pse-opendata caches them; ``refresh`` downloads again months the cache holds
    as final. Intervals without a price are kept; a repeated interval keeps its last row.
    """
    client = client or pse.Client(cache_dir=cache_dir, refresh=refresh)
    loaded = len(client.loads)
    raw = client.get(ENDPOINT, date(start.year, start.month, 1), (pd.Timestamp(end) + pd.offsets.MonthEnd(0)).date(),
                     fields=[FIELD])
    for load in client.loads[loaded:]:
        log.info("rce %s: %d intervals (%s)", f"{load.month:%Y-%m}", load.rows, load.status)
    raw = raw[(raw["business_date"] >= start) & (raw["business_date"] <= end)]
    if raw.empty:
        return empty_frame()
    return pd.DataFrame({"price": raw[FIELD].to_numpy(), "business_date": raw["business_date"].to_numpy()},
                        index=pd.DatetimeIndex(raw["start_utc"], name="ts_utc"))

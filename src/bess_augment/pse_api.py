"""Client for the public PSE reports API (https://api.raporty.pse.pl): RCE prices, cached as monthly Parquet files.

RCE ("rynkowa cena energii", PLN/MWh) is published per 15-minute interval; until 30 September 2025 the four quarters
of an hour carry the hourly price, from the delivery day 1 October 2025 they are true 15-minute prices.

Usage:
    from bess_augment.pse_api import fetch_range
    prices = fetch_range(date(2025, 10, 1), date(2026, 9, 30), cache_dir=Path("data/rce"))
"""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://api.raporty.pse.pl/api"
ENDPOINT, FIELD = "rce-pln", "rce_pln"
INTERVAL = pd.Timedelta("15min")
log = logging.getLogger(__name__)


def parse_records(records: list[dict]) -> pd.DataFrame:
    """Turn API records into a frame indexed by interval START in UTC.

    The API's ``dtime_utc`` is the END of the 15-minute interval ("00:00 - 00:15" -> dtime 00:15).
    """
    if not records:
        return pd.DataFrame(columns=["price", "business_date"]).rename_axis("ts_utc")
    df = pd.DataFrame.from_records(records)
    out = pd.DataFrame({
        "ts_utc": pd.to_datetime(df["dtime_utc"]).dt.tz_localize("UTC") - INTERVAL,
        "price": pd.to_numeric(df[FIELD], errors="coerce"),
        "business_date": pd.to_datetime(df["business_date"]).dt.date,
    })
    return out.drop_duplicates("ts_utc").set_index("ts_utc").sort_index()


def _get(session: requests.Session, url: str, params: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            log.warning("PSE API request failed (%s), retrying", e)
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def fetch_period(start: date, end: date, session: requests.Session | None = None) -> pd.DataFrame:
    """Fetch RCE for business dates start..end (inclusive), following pagination links."""
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "bess-capacity-augmentation (open-source analysis)")
    params = {
        "$filter": f"business_date ge '{start:%Y-%m-%d}' and business_date le '{end:%Y-%m-%d}'",
        "$select": f"dtime_utc,{FIELD},business_date",
        "$first": 20000,
    }
    payload = _get(session, f"{BASE_URL}/{ENDPOINT}", params)
    records = list(payload.get("value", []))
    while payload.get("nextLink"):
        payload = _get(session, payload["nextLink"])
        records += payload.get("value", [])
    return parse_records(records)


def fetch_range(start: date, end: date, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    """Fetch start..end month by month; complete past months are cached as Parquet."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    frames = []
    for month_start in pd.date_range(date(start.year, start.month, 1), end, freq="MS"):
        m_start = max(month_start.date(), start)
        m_end = min((month_start + pd.offsets.MonthEnd(0)).date(), end)
        cache = cache_dir / f"rce_{month_start:%Y-%m}.parquet"
        complete = (month_start + pd.offsets.MonthEnd(0)).date() < date.today()
        if cache.exists() and not refresh and complete:
            df = pd.read_parquet(cache)
        else:
            df = fetch_period(m_start, m_end, session)
            if complete and m_start == month_start.date():
                df.to_parquet(cache)
            log.info("rce %s: %d intervals", f"{month_start:%Y-%m}", len(df))
        frames.append(df[(df["business_date"] >= m_start) & (df["business_date"] <= m_end)])
    return pd.concat(frames).sort_index() if frames else parse_records([])

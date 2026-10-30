from datetime import date, datetime, timezone

import pandas as pd
import pse_opendata as pse

from bess_augment.pse_api import fetch_range


class FakeResponse:
    def __init__(self, payload):
        self.status_code, self.headers, self.reason, self.payload = 200, {}, "OK", payload

    def json(self):
        return self.payload


class FakeSession:
    """Answers the business_date filters of pse-opendata from fixed RCE records; records every call."""

    def __init__(self, records):
        self.records, self.calls = records, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        lo, hi = [part.split("'")[1] for part in params["$filter"].split(" and ")]
        return FakeResponse({"value": [r for r in self.records if lo <= r["business_date"] <= hi]})


def client(session, cache_dir):
    return pse.Client(session=session, cache_dir=cache_dir, now=lambda: datetime(2025, 12, 1, tzinfo=timezone.utc),
                      sleep=lambda s: None, min_interval=0, retries=1)


def test_interval_end_becomes_start(tmp_path):
    records = [
        {"dtime_utc": "2025-09-30 22:15:00", "rce_pln": "412.5", "business_date": "2025-10-01"},
        {"dtime_utc": "2025-09-30 22:30:00", "rce_pln": "-10", "business_date": "2025-10-01"},
        {"dtime_utc": "2025-09-30 22:30:00", "rce_pln": "-10", "business_date": "2025-10-01"},
    ]
    df = fetch_range(date(2025, 10, 1), date(2025, 10, 1), tmp_path, client=client(FakeSession(records), tmp_path))
    assert len(df) == 2 and df.index.name == "ts_utc" and list(df.columns) == ["price", "business_date"]
    assert df.index[0] == pd.Timestamp("2025-09-30 22:00", tz="UTC")
    assert df.index[1] - df.index[0] == pd.Timedelta("15min")
    assert df["price"].tolist() == [412.5, -10.0]
    assert str(df["business_date"].iloc[0]) == "2025-10-01"


def test_whole_months_are_requested_and_cut(tmp_path):
    records = [{"dtime_utc": f"2025-10-{d:02d} 10:00:00", "rce_pln": d, "business_date": f"2025-10-{d:02d}"}
               for d in range(1, 32)]
    session = FakeSession(records)
    df = fetch_range(date(2025, 10, 10), date(2025, 10, 11), tmp_path, client=client(session, tmp_path))
    assert session.calls[0][1]["$filter"] == "business_date ge '2025-10-01' and business_date le '2025-10-31'"
    assert df["price"].tolist() == [10.0, 11.0]
    assert (tmp_path / "api" / "rce-pln" / "2025-10.parquet").exists()


def test_empty(tmp_path):
    df = fetch_range(date(2025, 10, 1), date(2025, 10, 31), tmp_path, client=client(FakeSession([]), tmp_path))
    assert df.empty and list(df.columns) == ["price", "business_date"]

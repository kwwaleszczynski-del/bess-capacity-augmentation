import pandas as pd

from bess_augment.pse_api import parse_records


def test_interval_end_becomes_start():
    records = [
        {"dtime_utc": "2025-09-30 22:15:00", "rce_pln": "412.5", "business_date": "2025-10-01"},
        {"dtime_utc": "2025-09-30 22:30:00", "rce_pln": "-10", "business_date": "2025-10-01"},
        {"dtime_utc": "2025-09-30 22:30:00", "rce_pln": "-10", "business_date": "2025-10-01"},
    ]
    df = parse_records(records)
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp("2025-09-30 22:00", tz="UTC")
    assert df.index[1] - df.index[0] == pd.Timedelta("15min")
    assert df["price"].tolist() == [412.5, -10.0]
    assert str(df["business_date"].iloc[0]) == "2025-10-01"


def test_empty():
    df = parse_records([])
    assert df.empty and list(df.columns) == ["price", "business_date"]

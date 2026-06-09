"""データローダの日付ロジック (src/data_loader.py) のユニットテスト。

API に触れない純粋な営業日計算部分を検証する。
"""
import datetime as dt

from src.data_loader import fetch_window, latest_business_date


def d(y, m, day):
    return dt.date(y, m, day)


# ---------- latest_business_date ----------
def test_latest_business_date_rolls_back_weekend():
    # 2026-06-08(月) → 前日 06-07(日) → 直近平日 06-05(金)
    assert latest_business_date(d(2026, 6, 8)) == d(2026, 6, 5)


def test_latest_business_date_weekday():
    # 2026-06-10(水) → 前日 06-09(火) は平日なのでそのまま
    assert latest_business_date(d(2026, 6, 10)) == d(2026, 6, 9)


# ---------- fetch_window ----------
def test_fetch_window_returns_business_days_ascending():
    # 末尾 2026-06-10(水) から営業日3本 → [月,火,水] 昇順
    win = fetch_window(client=None, end_date=d(2026, 6, 10), days_back=3)
    assert win == ["2026-06-08", "2026-06-09", "2026-06-10"]


def test_fetch_window_skips_weekends():
    # 末尾 2026-06-08(月) から3本 → 週末を飛ばし [水,木,金,月] のうち3本
    win = fetch_window(client=None, end_date=d(2026, 6, 8), days_back=3)
    assert win == ["2026-06-04", "2026-06-05", "2026-06-08"]
    # すべて平日であること
    for s in win:
        assert dt.date.fromisoformat(s).weekday() < 5


def test_fetch_window_length():
    win = fetch_window(client=None, end_date=d(2026, 6, 10), days_back=10)
    assert len(win) == 10

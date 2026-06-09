"""テクニカル指標 (src/indicators.py) のユニットテスト。

numpy ベースの純粋関数なので、既知の入力に対する戻り値を決定的に検証する。
"""
import numpy as np
import pytest

from src import indicators as ind


def arr(*vals):
    return np.array(vals, dtype=float)


# ---------- SMA ----------
def test_sma_basic():
    assert ind.sma(arr(1, 2, 3, 4, 5), 3) == pytest.approx(4.0)  # mean(3,4,5)


def test_sma_returns_none_when_too_short():
    assert ind.sma(arr(1, 2), 3) is None
    assert ind.sma(None, 3) is None


def test_sma_at_offset():
    # offset=0 が最新。offset=1 で1日前を末尾とする SMA。
    series = arr(1, 2, 3, 4, 5)
    assert ind.sma_at(series, 3, 0) == pytest.approx(4.0)  # mean(3,4,5)
    assert ind.sma_at(series, 3, 1) == pytest.approx(3.0)  # mean(2,3,4)


def test_sma_at_out_of_range():
    assert ind.sma_at(arr(1, 2, 3), 3, 1) is None  # start < 0


# ---------- RSI ----------
def test_rsi_all_gains_is_100():
    # 単調増加 → avg_loss=0 → RSI=100
    assert ind.rsi(arr(1, 2, 3, 4, 5, 6), 3) == pytest.approx(100.0)


def test_rsi_balanced_is_50():
    # 利得と損失が等量 → RSI=50
    close = arr(10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10)
    assert ind.rsi(close, 14) == pytest.approx(50.0, abs=1.0)


def test_rsi_returns_none_when_too_short():
    assert ind.rsi(arr(1, 2), 14) is None


# ---------- returns / volatility ----------
def test_pct_return():
    assert ind.pct_return(arr(100, 110), 1) == pytest.approx(0.10)
    assert ind.pct_return(arr(100, 90, 120), 2) == pytest.approx(0.20)


def test_pct_return_guards():
    assert ind.pct_return(arr(100), 5) is None
    assert ind.pct_return(arr(0, 100), 1) is None  # 分母0


def test_volatility_zero_for_flat_series():
    assert ind.volatility(arr(5, 5, 5, 5, 5), 3) == pytest.approx(0.0)


# ---------- range / extremes / zscore ----------
def test_range_ratio():
    # (max-min)/mean = (5-1)/3
    assert ind.range_ratio(arr(1, 2, 3, 4, 5), 5) == pytest.approx(4.0 / 3.0)


def test_high_low_n():
    series = arr(3, 1, 4, 1, 5, 9, 2)
    assert ind.high_n(series, 7) == pytest.approx(9.0)
    assert ind.low_n(series, 7) == pytest.approx(1.0)


def test_zscore_none_when_flat():
    assert ind.zscore(arr(2, 2, 2, 2), 4) is None  # 標準偏差0


def test_zscore_value():
    # window=[0,0,0,4], mean=1, sd=sqrt(3), z=(4-1)/sqrt(3)
    assert ind.zscore(arr(0, 0, 0, 4), 4) == pytest.approx(3 / np.sqrt(3))


# ---------- volume / contraction ----------
def test_volume_ratio_none_when_short():
    assert ind.volume_ratio(arr(1, 2, 3), recent_n=2, base_n=60) is None


def test_volume_ratio_value():
    vol = np.concatenate([np.full(55, 100.0), np.full(5, 300.0)])
    # 直近5平均=300, 全60平均=(55*100+5*300)/60
    base = (55 * 100 + 5 * 300) / 60
    assert ind.volume_ratio(vol, recent_n=5, base_n=60) == pytest.approx(300 / base)


def test_vol_contraction_none_when_short():
    assert ind.vol_contraction(arr(1, 2, 3)) is None

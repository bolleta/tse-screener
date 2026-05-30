"""テクニカル指標。numpy ベース・依存最小。"""
from __future__ import annotations

import numpy as np


def sma(series: np.ndarray, n: int):
    if series is None or len(series) < n:
        return None
    return float(np.mean(series[-n:]))


def sma_at(series: np.ndarray, n: int, offset: int):
    """offset=0 が最新。offset=1 で1日前のSMA。"""
    end = len(series) - offset
    start = end - n
    if start < 0:
        return None
    return float(np.mean(series[start:end]))


def rsi(close: np.ndarray, n: int = 14):
    if close is None or len(close) < n + 1:
        return None
    diff = np.diff(close[-(n + 1):])
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_gain = float(np.mean(gain))
    avg_loss = float(np.mean(loss))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def pct_return(close: np.ndarray, days: int):
    if close is None or len(close) < days + 1 or close[-days - 1] == 0:
        return None
    return float(close[-1] / close[-days - 1] - 1.0)


def volatility(close: np.ndarray, n: int):
    if close is None or len(close) < n + 1:
        return None
    rets = np.diff(close[-(n + 1):]) / close[-(n + 1):-1]
    return float(np.std(rets))


def range_ratio(close: np.ndarray, n: int):
    if close is None or len(close) < n:
        return None
    window = close[-n:]
    mean = float(np.mean(window))
    if mean == 0:
        return None
    return float((np.max(window) - np.min(window)) / mean)


def high_n(close: np.ndarray, n: int):
    if close is None or len(close) < n:
        return None
    return float(np.max(close[-n:]))


def low_n(close: np.ndarray, n: int):
    if close is None or len(close) < n:
        return None
    return float(np.min(close[-n:]))


def zscore(close: np.ndarray, n: int):
    if close is None or len(close) < n:
        return None
    window = close[-n:]
    sd = float(np.std(window))
    if sd == 0:
        return None
    return float((close[-1] - np.mean(window)) / sd)


def volume_ratio(volume: np.ndarray, recent_n: int = 5, base_n: int = 60):
    if volume is None or len(volume) < base_n:
        return None
    recent = float(np.mean(volume[-recent_n:]))
    base = float(np.mean(volume[-base_n:]))
    if base == 0:
        return None
    return recent / base


def vol_contraction(close: np.ndarray, short_n: int = 10, long_n: int = 60):
    short = volatility(close, short_n)
    long = volatility(close, long_n)
    if short is None or long is None or long == 0:
        return None
    return short / long

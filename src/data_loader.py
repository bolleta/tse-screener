"""J-Quants 生データ → 銘柄別 features への変換層。

データ流入:
  - listed/info        : 銘柄一覧 (Code, CompanyName, Sector33Code, MarketCode, ...)
  - prices/daily_quotes: 日付ごとの全銘柄日足 (Open/High/Low/Close/Volume)
  - fins/statements    : 日付ごとの全社の決算開示 (DisclosedDate ベース)

戦略:
  - Light は株価12週遅延 → 「最新営業日」を today - 84日 として算出
  - 価格は直近 LOOKBACK_PRICE_DAYS 営業日, 財務は直近 LOOKBACK_STMT_DAYS 暦日 を取得
  - キャッシュ済み日付はスキップ
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# 重い依存(numpy, indicators)は load_all() 呼び出し時にレイジーロード。
# これにより --source mock/csv モードでは numpy ロード不要で即起動する。
_np = None
_ind = None


def _ensure_heavy_imports():
    global _np, _ind
    if _np is None:
        import numpy as _np_mod
        from . import indicators as _ind_mod
        _np = _np_mod
        _ind = _ind_mod
    return _np, _ind


LOOKBACK_PRICE_DAYS = 260   # 1年強
LOOKBACK_STMT_DAYS = 120    # 4ヶ月(直近四半期決算を捕捉)
LIGHT_DELAY_DAYS = 1        # Light(V2): ほぼリアルタイム、当日締め後の反映を見越して1日バッファ


@dataclass
class StockFeatures:
    code: str
    name: str = ""
    sector33_code: str = ""
    market_code: str = ""
    # price-derived
    close: Optional[float] = None
    sma50: Optional[float] = None
    sma100: Optional[float] = None
    sma200: Optional[float] = None
    sma50_prev: Optional[float] = None
    sma200_prev: Optional[float] = None
    rsi14: Optional[float] = None
    high60: Optional[float] = None
    return_5d: Optional[float] = None
    return_1m: Optional[float] = None
    return_3m: Optional[float] = None
    return_6m: Optional[float] = None
    return_1y: Optional[float] = None
    volatility_60: Optional[float] = None
    range60_ratio: Optional[float] = None
    vol_ratio: Optional[float] = None
    vol_contraction: Optional[float] = None
    zscore20: Optional[float] = None
    pct_52w_high: Optional[float] = None  # 価格 / 52週高値 (0-1)
    # fundamentals
    per: Optional[float] = None
    pbr: Optional[float] = None
    peg: Optional[float] = None
    roe: Optional[float] = None
    op_margin: Optional[float] = None  # 営業利益率 (fraction)
    equity_ratio: Optional[float] = None
    revenue_growth: Optional[float] = None
    eps_growth: Optional[float] = None
    dividend_yield: Optional[float] = None
    dividend_growth_3y: Optional[float] = None
    bps: Optional[float] = None
    net_income: Optional[float] = None
    prev_net_income: Optional[float] = None
    market_cap: Optional[float] = None
    # event
    days_since_statement: Optional[int] = None
    days_since_listing: Optional[int] = None
    days_to_exdiv: Optional[int] = None
    post_earn_drift: Optional[float] = None
    # cross-sectional factors (filled after all stocks computed)
    factor_value: Optional[float] = None
    factor_momentum: Optional[float] = None
    factor_quality: Optional[float] = None
    sector_momentum_3m: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


def latest_business_date(today: dt.date) -> dt.date:
    """Light の最新利用可能日 = today - 12週 を平日に丸める。"""
    target = today - dt.timedelta(days=LIGHT_DELAY_DAYS)
    while target.weekday() >= 5:
        target -= dt.timedelta(days=1)
    return target


def fetch_window(client, end_date: dt.date, days_back: int) -> list[str]:
    """end_date から営業日 days_back 日分の日付文字列リストを返す。"""
    out: list[str] = []
    d = end_date
    while len(out) < days_back:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def load_all(client, today: Optional[dt.date] = None) -> tuple[dict, dt.date]:
    """全銘柄のfeaturesを生成して返す。client は JQuantsClient インスタンス。"""
    today = today or dt.date.today()
    end_d = latest_business_date(today)

    # 1) 銘柄マスタ (V2: /equities/master)
    #    フィールド: Code, CoName, S33 (sector33), Mkt (market code)
    info = client.listed_info()
    features: dict[str, StockFeatures] = {}
    for row in info:
        code = str(row.get("Code", "")).strip()
        if not code:
            continue
        features[code] = StockFeatures(
            code=code,
            name=row.get("CoName") or row.get("CompanyName", ""),
            sector33_code=str(row.get("S33") or row.get("Sector33Code", "")),
            market_code=str(row.get("Mkt") or row.get("MarketCode", "")),
        )

    # 2) 価格データ
    price_dates = fetch_window(client, end_d, LOOKBACK_PRICE_DAYS)
    closes: dict[str, list[tuple[str, float]]] = defaultdict(list)
    volumes: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for date_str in price_dates:
        rows = client.daily_quotes_by_date(date_str)
        for r in rows:
            code = str(r.get("Code", "")).strip()
            # V2: AdjC/AdjVo (株式分割調整済) を優先、なければ C/Vo
            c = r.get("AdjC") or r.get("C") or r.get("Close")
            v = r.get("AdjVo") or r.get("Vo") or r.get("Volume")
            if code and c is not None:
                closes[code].append((date_str, float(c)))
            if code and v is not None:
                volumes[code].append((date_str, float(v)))

    # 3) 財務データ
    stmt_dates: list[str] = []
    d = end_d
    while len(stmt_dates) < LOOKBACK_STMT_DAYS:
        if d.weekday() < 5:
            stmt_dates.append(d.isoformat())
        d -= dt.timedelta(days=1)
    statements_per_stock: dict[str, list[dict]] = defaultdict(list)
    for date_str in stmt_dates:
        rows = client.statements_by_date(date_str)
        for r in rows:
            # V2 では Code (5桁、master と同じ形式)
            code = str(r.get("Code") or r.get("LocalCode", "")).strip()
            if not code:
                continue
            statements_per_stock[code].append(r)

    # 4) feature 計算
    for code, feat in features.items():
        _compute_price_features(feat, closes.get(code, []), volumes.get(code, []), end_d)
        _compute_fundamental_features(feat, statements_per_stock.get(code, []), end_d)

    # 5) cross-sectional factors
    _attach_factors(features)
    _attach_sector_momentum(features)

    return features, end_d


def _compute_price_features(
    feat: StockFeatures,
    closes_seq: list[tuple[str, float]],
    volumes_seq: list[tuple[str, float]],
    end_d: dt.date,
) -> None:
    if not closes_seq:
        return
    np, ind = _ensure_heavy_imports()
    closes_seq.sort(key=lambda x: x[0])
    closes = np.array([c for _, c in closes_seq], dtype=float)
    volumes_seq.sort(key=lambda x: x[0])
    volumes = np.array([v for _, v in volumes_seq], dtype=float)

    feat.close = float(closes[-1])
    feat.sma50 = ind.sma(closes, 50)
    feat.sma100 = ind.sma(closes, 100)
    feat.sma200 = ind.sma(closes, 200)
    feat.sma50_prev = ind.sma_at(closes, 50, 1)
    feat.sma200_prev = ind.sma_at(closes, 200, 1)
    feat.rsi14 = ind.rsi(closes, 14)
    feat.high60 = ind.high_n(closes, 60)
    feat.return_5d = ind.pct_return(closes, 5)
    feat.return_1m = ind.pct_return(closes, 21)
    feat.return_3m = ind.pct_return(closes, 63)
    feat.return_6m = ind.pct_return(closes, 126)
    feat.return_1y = ind.pct_return(closes, 252)
    feat.volatility_60 = ind.volatility(closes, 60)
    feat.range60_ratio = ind.range_ratio(closes, 60)
    feat.zscore20 = ind.zscore(closes, 20)
    feat.vol_ratio = ind.volume_ratio(volumes)
    feat.vol_contraction = ind.vol_contraction(closes)
    # 52週高値比 (1.0 = 52週高値タッチ中)
    window_252 = closes[-252:] if len(closes) >= 252 else closes
    high_52w = float(np.max(window_252))
    if high_52w > 0:
        feat.pct_52w_high = float(closes[-1]) / high_52w


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_date(s: str) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _compute_fundamental_features(
    feat: StockFeatures,
    statements: list[dict],
    end_d: dt.date,
) -> None:
    if not statements:
        return
    # V2 と V1 両対応の getter
    def g(d, *keys):
        for k in keys:
            v = d.get(k)
            if v is not None and v != "":
                return v
        return None

    statements_sorted = sorted(
        statements,
        key=lambda r: g(r, "DiscDate", "DisclosedDate") or "",
        reverse=True,
    )
    latest = statements_sorted[0]
    disclosed = _parse_date(g(latest, "DiscDate", "DisclosedDate") or "")
    if disclosed:
        feat.days_since_statement = (end_d - disclosed).days

    # V2 フィールド: NP, EPS, BPS, Sales, OP, Eq, TA, DivTotalAnn, FDivTotalAnn, FEPS
    ni = _to_float(g(latest, "NP", "Profit"))
    eps = _to_float(g(latest, "EPS", "EarningsPerShare"))
    bps = _to_float(g(latest, "BPS", "BookValuePerShare"))
    revenue = _to_float(g(latest, "Sales", "NetSales"))
    op_profit = _to_float(g(latest, "OP", "OperatingProfit"))
    equity = _to_float(g(latest, "Eq", "Equity"))
    assets = _to_float(g(latest, "TA", "TotalAssets"))
    # 重要: V2 では DivAnn = 1株あたり年間配当, DivTotalAnn = 企業全体の配当総額。
    # 利回り計算には DivAnn (per share) を使う。TotalAnn を間違えて使うと
    # 利回り = 配当総額(円) / 株価(円) = 天文学的数値になる。
    forecast_div_total = _to_float(g(latest, "FDivAnn",
                                       "ForecastDividendPerShareAnnual"))
    result_div_total = _to_float(g(latest, "DivAnn",
                                    "ResultDividendPerShareAnnual"))

    feat.net_income = ni
    feat.bps = bps
    if assets and equity:
        feat.equity_ratio = equity / assets
    if equity and ni and equity > 0:
        feat.roe = ni / equity
    if revenue and op_profit is not None and revenue > 0:
        feat.op_margin = op_profit / revenue
    if feat.close and eps and eps > 0:
        feat.per = feat.close / eps
    if feat.close and bps and bps > 0:
        feat.pbr = feat.close / bps
    div = forecast_div_total or result_div_total
    if feat.close and div is not None and feat.close > 0:
        feat.dividend_yield = div / feat.close

    if len(statements_sorted) >= 5:
        prev_yr = statements_sorted[4]
        prev_ni = _to_float(g(prev_yr, "NP", "Profit"))
        prev_eps = _to_float(g(prev_yr, "EPS", "EarningsPerShare"))
        prev_rev = _to_float(g(prev_yr, "Sales", "NetSales"))
        feat.prev_net_income = prev_ni
        if prev_eps and eps is not None and prev_eps > 0:
            feat.eps_growth = (eps - prev_eps) / prev_eps
        if prev_rev and revenue is not None and prev_rev > 0:
            feat.revenue_growth = (revenue - prev_rev) / prev_rev

    # PEG = PER / (EPS成長率%)
    forecast_eps = _to_float(g(latest, "FEPS", "ForecastEarningsPerShare"))
    growth_for_peg: Optional[float] = None
    if forecast_eps and eps is not None and eps > 0:
        growth_for_peg = (forecast_eps - eps) / eps
    elif feat.eps_growth is not None:
        growth_for_peg = feat.eps_growth
    if feat.per is not None and growth_for_peg and growth_for_peg > 0:
        feat.peg = feat.per / (growth_for_peg * 100)

    # 配当成長(3年)
    if len(statements_sorted) >= 13:
        old_div = _to_float(g(statements_sorted[12], "DivAnn",
                              "ResultDividendPerShareAnnual"))
        cur_div = result_div_total
        if old_div and cur_div is not None and old_div > 0:
            cagr = (cur_div / old_div) ** (1 / 3) - 1
            feat.dividend_growth_3y = cagr

    # 時価総額 (V2: ShOutFY = 期末発行済株式数)
    issued = _to_float(g(latest, "ShOutFY",
                         "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYear"))
    if issued and feat.close:
        feat.market_cap = issued * feat.close

    # 決算後ドリフト (簡易近似)
    if feat.days_since_statement is not None and feat.return_1m is not None:
        if feat.days_since_statement <= 30:
            feat.post_earn_drift = feat.return_1m

    # 配当落ち日 (近似): 期末日が60日以内なら ex-div 接近とみなす
    end_period_str = g(latest, "CurPerEn", "CurrentPeriodEndDate") or ""
    end_period = _parse_date(end_period_str)
    if end_period:
        delta = (end_period - end_d).days
        if 0 < delta <= 60:
            feat.days_to_exdiv = delta


def _rank_percentile(values: list[tuple[str, float]]) -> dict[str, float]:
    """[(code, value)] → {code: percentile(0-1, 高いほど良い)}"""
    cleaned = [(c, v) for c, v in values if v is not None and not math.isnan(v)]
    cleaned.sort(key=lambda x: x[1])
    n = len(cleaned)
    if n == 0:
        return {}
    return {c: (i + 1) / n for i, (c, _) in enumerate(cleaned)}


def _attach_factors(features: dict) -> None:
    value_src = []
    momentum_src = []
    quality_src = []
    for c, f in features.items():
        if f.per is not None and f.per > 0:
            value_src.append((c, 1.0 / f.per))
        if f.return_6m is not None:
            momentum_src.append((c, f.return_6m))
        if f.roe is not None:
            quality_src.append((c, f.roe))
    v_rank = _rank_percentile(value_src)
    m_rank = _rank_percentile(momentum_src)
    q_rank = _rank_percentile(quality_src)
    for c, f in features.items():
        f.factor_value = v_rank.get(c)
        f.factor_momentum = m_rank.get(c)
        f.factor_quality = q_rank.get(c)


def _attach_sector_momentum(features: dict) -> None:
    by_sector: dict[str, list[float]] = defaultdict(list)
    for f in features.values():
        if f.sector33_code and f.return_3m is not None:
            by_sector[f.sector33_code].append(f.return_3m)
    sector_avg = {k: sum(v) / len(v) for k, v in by_sector.items() if v}
    for f in features.values():
        if f.sector33_code in sector_avg:
            f.sector_momentum_3m = sector_avg[f.sector33_code]

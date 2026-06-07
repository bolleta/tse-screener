"""これから短期で大化けしそうな銘柄の『先行サイン』スクリーナー群。

過去に1ヶ月で2倍化した銘柄に共通する初動の形を、J-Quants 日足(調整後OHLCV)から
決定的に計算して抽出する。4戦略:

  momentum : モメンタム加速(継続ブレイク)  — すでに動意づいた銘柄の2段目を狙う順張り
  volume   : 出来高急増ブレイク             — 売買代金の急増を伴う初動を捕まえる
  vcp      : ボラティリティ収縮(VCP)         — 値幅が収縮し切った保ち合いの放れ際を拾う
  newhigh  : 新高値ブレイク                  — 上値抵抗のない新高値更新を起点とする

データ取得は movers と同じ「指定日の全銘柄日足(1コール=1キャッシュ)」を、直近K営業日
ぶん束ねて全銘柄の時系列を作る方式。まず最新日スナップショットで小型株フィルタを通し、
通過した銘柄だけ時系列を構築してスコアリングするためメモリ/速度とも軽い。

注意: 本モジュールは投資助言ではない。2倍化は確率事象であり、ここでの抽出は
「過去の大化け銘柄に多かった初動の形」を機械的に拾うだけで、上昇を保証しない。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
# 戦略スクリーニング結果の保存先(moversのsaved_searches直下とは分離してサブディレクトリに)
SAVED_DIR = ROOT / "saved_searches" / "strategies"

# 各戦略の表示名(保存メタ/UI用)
STRATEGY_LABELS = {
    "momentum": "① モメンタム加速",
    "volume": "② 出来高急増ブレイク",
    "vcp": "③ ボラ収縮VCP",
    "newhigh": "④ 新高値ブレイク",
    "overlap": "⑤ 複合(重複)",
}

# 戦略ごとの既定ルックバック(参照する営業日数)。新高値だけ長期が要る。
DEFAULT_LOOKBACK = {"momentum": 70, "volume": 70, "vcp": 70, "newhigh": 240}

# 小型株フィルタの既定(「標準」設定)。2倍化は小型株に偏るため。
FILTER_DEFAULTS = {
    "market": "small",              # small=グロース/スタンダード, all=全市場
    "min_turnover": 30_000_000.0,   # 0.3億円(出来上がりの薄商いを除外)
    "max_turnover": 5_000_000_000.0,  # 50億円(大型株を除外。0で無制限)
    "min_price": 100.0,
    "max_price": 10_000.0,          # 0で無制限
}

# 各戦略が結果テーブルに出す固有の指標列(フロントが汎用描画に使う)
COLUMNS = {
    "momentum": [
        {"key": "r5", "label": "5日%"}, {"key": "r20", "label": "20日%"},
        {"key": "r60", "label": "60日%"}, {"key": "accel", "label": "加速度"},
        {"key": "vs_sma25", "label": "25MA乖離%"},
    ],
    "volume": [
        {"key": "surge", "label": "出来高倍率"}, {"key": "vs_high", "label": "高値超%"},
        {"key": "r20", "label": "20日%"}, {"key": "turn_avg", "label": "平均代金"},
    ],
    "vcp": [
        {"key": "contraction", "label": "収縮比"}, {"key": "tight", "label": "値幅%"},
        {"key": "vs_basehigh", "label": "基準高値比%"},
    ],
    "newhigh": [
        {"key": "vs_priorhigh", "label": "新高値超%"}, {"key": "base_days", "label": "土台日数"},
        {"key": "vsurge", "label": "出来高倍率"},
    ],
}


# ---------- データ取得ヘルパ ----------
def _f(x) -> Optional[float]:
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ohlcv_map(rows: list[dict]) -> dict[str, dict]:
    """日足行リスト → {code: {h,l,c,t}}。high/low 欠損は close で埋める。"""
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        c = _f(r.get("AdjC"))
        if c is None:
            c = _f(r.get("C"))
        if c is None:
            c = _f(r.get("Close"))
        if c is None or c <= 0:
            continue
        h = _f(r.get("AdjH"))
        l = _f(r.get("AdjL"))
        out[code] = {
            "h": h if h is not None else c,
            "l": l if l is not None else c,
            "c": c,
            "t": _f(r.get("Va")),  # 売買代金(円)
        }
    return out


def load_window(client, lookback: int, asof: Optional[dt.date] = None):
    """直近 lookback 営業日のスナップショットを集める。

    戻り値: (date_now_iso, dates_sorted, {date: ohlcv_map})。最新日は uncached の
    raw 取得(最新足の鮮度確保)、過去日はディスクキャッシュ経由。
    """
    from . import movers
    today = asof or dt.date.today()
    date_now, rows_now = movers.find_latest_available(client, today)
    now_key = date_now.isoformat()
    dates = [now_key]
    maps = {now_key: _ohlcv_map(rows_now)}

    d = date_now - dt.timedelta(days=1)
    safety = 0
    limit = lookback * 3 + 60  # 祝日/連休があっても必要日数を集め切るための上限
    while len(dates) < lookback and safety < limit:
        safety += 1
        if d.weekday() < 5:  # 平日のみ試行
            rows = client.daily_quotes_by_date(d.isoformat())
            if rows:
                key = d.isoformat()
                dates.append(key)
                maps[key] = _ohlcv_map(rows)
        d -= dt.timedelta(days=1)
    dates.sort()  # 古→新
    return now_key, dates, maps


def _passes_filter(market: str, price: Optional[float], turnover: Optional[float], flt: dict) -> bool:
    if flt["market"] == "small":
        if not (("グロース" in market) or ("スタンダード" in market)):
            return False
    if price is None or price < flt["min_price"]:
        return False
    if flt["max_price"] and price > flt["max_price"]:
        return False
    tv = turnover or 0.0
    if tv < flt["min_turnover"]:
        return False
    if flt["max_turnover"] and tv > flt["max_turnover"]:
        return False
    return True


def _filter_overrides(params: dict) -> dict:
    out: dict = {}
    if params.get("market") in ("small", "all"):
        out["market"] = params["market"]
    for k in ("min_turnover", "max_turnover", "min_price", "max_price"):
        v = params.get(k)
        if v not in (None, "", "null"):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


# ---------- 戦略本体(各 (series, params) → {score, metrics} or None) ----------
def _st_momentum(s: dict, p: dict, np) -> Optional[dict]:
    """短期>中期で加速中・移動平均上・上昇継続中の銘柄(2段目狙い)。"""
    c = s["c"]
    n = len(c)
    if n < 61:
        return None

    def ret(k):
        base = c[-1 - k]
        return (c[-1] / base - 1) if base > 0 else None

    r5, r20, r60 = ret(5), ret(20), ret(60)
    if None in (r5, r20, r60):
        return None
    sma25 = sum(c[-25:]) / 25
    if c[-1] <= sma25 or r20 <= 0 or r5 <= 0:
        return None
    accel = (r5 / 5) - (r20 / 20)  # 1日あたり速度が短期>中期 = 加速
    if accel <= 0:
        return None
    min_r60 = float(p.get("min_r60", 10.0))  # 既に一定以上トレンドが出ているもの(%)
    if r60 * 100 < min_r60:
        return None
    vs_sma = c[-1] / sma25 - 1
    score = r5 * 60 + r20 * 40 + accel * 2000 + vs_sma * 20
    return {"score": score, "metrics": {
        "r5": round(r5 * 100, 1), "r20": round(r20 * 100, 1), "r60": round(r60 * 100, 1),
        "accel": round(accel * 1000, 2), "vs_sma25": round(vs_sma * 100, 1),
    }}


def _st_volume(s: dict, p: dict, np) -> Optional[dict]:
    """売買代金が平均比で急増し、直近高値をブレイクした初動。"""
    c, h, t = s["c"], s["h"], s["t"]
    n = len(c)
    base_days = int(float(p.get("vol_base", 20)))
    if n < base_days + 2:
        return None
    base = [x for x in t[-(base_days + 1):-1] if x]
    if not base:
        return None
    avg = sum(base) / len(base)
    if avg <= 0:
        return None
    surge = (t[-1] or 0.0) / avg
    mult = float(p.get("vol_mult", 3.0))
    hi = max(h[-(base_days + 1):-1])
    if surge < mult or c[-1] < hi or c[-1] <= c[-2]:
        return None
    r20 = (c[-1] / c[-21] - 1) if (n >= 21 and c[-21] > 0) else 0.0
    score = surge * 10 + (c[-1] / hi - 1) * 200 + r20 * 30
    return {"score": score, "metrics": {
        "surge": round(surge, 2), "vs_high": round((c[-1] / hi - 1) * 100, 2),
        "r20": round(r20 * 100, 1), "turn_avg": round(avg),
    }}


def _st_vcp(s: dict, p: dict, np) -> Optional[dict]:
    """変動率が収縮し切り、高値圏で値幅が締まった保ち合い(放れ前夜)。"""
    c = np.array(s["c"], dtype="float64")
    h = np.array(s["h"], dtype="float64")
    l = np.array(s["l"], dtype="float64")
    n = len(c)
    if n < 50:
        return None
    rets = np.diff(c) / c[:-1]
    recent = rets[-10:]
    prior = rets[-40:-10]
    rv = float(np.std(recent))
    pv = float(np.std(prior))
    if rv <= 0:
        return None
    base_hi = float(np.max(h[-30:]))
    if base_hi <= 0:
        return None
    tight = (float(np.max(h[-15:])) - float(np.min(l[-15:]))) / float(c[-1])
    # 値動きが凍結した銘柄(値幅ほぼ0)はコイルではなく非流動。除外。
    if tight < 0.015:
        return None
    # rv を下限でクリップし、収縮比の発散(凍結銘柄が巨大値)を防いで上限も設ける
    contraction = pv / max(rv, 0.004)
    contraction = min(contraction, 10.0)
    near_top = c[-1] >= 0.90 * base_hi
    cmin = float(p.get("vcp_contraction", 1.3))
    tmax = float(p.get("vcp_tight", 0.20))
    if contraction < cmin or tight > tmax or not near_top:
        return None
    score = contraction * 20 + (1.0 / max(tight, 0.01)) * 5 + (float(c[-1]) / base_hi) * 30
    return {"score": score, "metrics": {
        "contraction": round(contraction, 2), "tight": round(tight * 100, 1),
        "vs_basehigh": round((float(c[-1]) / base_hi - 1) * 100, 1),
    }}


def _st_newhigh(s: dict, p: dict, np) -> Optional[dict]:
    """期間内高値を更新した新高値ブレイク(土台が長いほど高評価)。"""
    c, h, t = s["c"], s["h"], s["t"]
    n = len(c)
    min_bars = int(float(p.get("min_bars", 120)))
    if n < min_bars:
        return None
    prior_high = max(h[:-1])
    if prior_high <= 0:
        return None
    ratio = c[-1] / prior_high
    nh_hi = float(p.get("nh_hi", 1.20))  # 伸び切ったものは除外(ブレイク初動を狙う)
    if ratio < 1.0 or ratio > nh_hi:
        return None
    # 前回高値の位置(=土台の長さ)。長い保ち合いからの新高値ほど強い。
    idx = max(range(n - 1), key=lambda i: h[i])
    base_days = (n - 1) - idx
    recent = [x for x in t[-21:-1] if x]
    avg_t = (sum(recent) / len(recent)) if recent else 0.0
    vsurge = ((t[-1] or 0.0) / avg_t) if avg_t > 0 else 0.0
    score = base_days * 0.3 + vsurge * 8 + (2.0 - abs(ratio - 1.0) * 10)
    return {"score": score, "metrics": {
        "vs_priorhigh": round((ratio - 1) * 100, 2), "base_days": base_days,
        "vsurge": round(vsurge, 2),
    }}


STRATEGIES = {
    "momentum": _st_momentum,
    "volume": _st_volume,
    "vcp": _st_vcp,
    "newhigh": _st_newhigh,
}


# ---------- 結果の保存 / 一覧 / 参照 ----------
def _csv_fields(record: dict) -> list[str]:
    """保存レコードからCSVの列順を決める(単一戦略 or 複合)。"""
    base_head = ["rank", "code", "name"]
    base_tail = ["price_now", "turnover_now", "sector33_name", "market"]
    if record.get("columns") is not None:  # 単一戦略
        return base_head + ["score"] + [c["key"] for c in record["columns"]] + base_tail
    order = record.get("order", [])  # 複合
    return base_head + ["count", "score"] + ["rk_" + s for s in order] + base_tail


def _save_screen(record: dict) -> None:
    SAVED_DIR.mkdir(parents=True, exist_ok=True)
    sid = record["meta"]["search_id"]
    (SAVED_DIR / f"{sid}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = _csv_fields(record)
    with open(SAVED_DIR / f"{sid}.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(fields)
        for r in record["results"]:
            w.writerow([r.get(k, "") for k in fields])


def list_saved() -> list[dict]:
    if not SAVED_DIR.exists():
        return []
    metas = []
    for p in sorted(SAVED_DIR.glob("*.json"), reverse=True):
        try:
            metas.append(json.loads(p.read_text(encoding="utf-8"))["meta"])
        except (json.JSONDecodeError, KeyError):
            continue
    return metas


def load_saved(search_id: str) -> dict:
    if not re.fullmatch(r"[\w\-]+", search_id):
        raise ValueError(f"不正な検索ID: {search_id!r}")
    p = SAVED_DIR / f"{search_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"保存済み結果が見つかりません: {search_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def _no_save(params: dict) -> bool:
    return str(params.get("no_save", "")).lower() in ("1", "true", "yes")


# ---------- エントリポイント ----------
def screen(strategy: str, params: dict) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy!r}")
    import numpy as np
    from . import movers
    from .jquants_client import JQuantsClient, load_config_from_env

    client = JQuantsClient(load_config_from_env(CACHE_DIR))
    flt = {**FILTER_DEFAULTS, **_filter_overrides(params)}
    lookback = int(float(params.get("lookback") or DEFAULT_LOOKBACK[strategy]))
    lookback = max(30, min(lookback, 300))

    date_now, dates, maps = load_window(client, lookback)
    if not dates:
        raise RuntimeError("日足データを取得できませんでした")
    last = dates[-1]
    last_map = maps[last]
    master = movers._master_map(client)

    # 1) 最新日スナップショットで小型株フィルタ → 通過銘柄(survivors)を決める
    survivors = []
    for code, x in last_map.items():
        m = master.get(code, {})
        if _passes_filter(m.get("market", ""), x["c"], x["t"], flt):
            survivors.append(code)
    survivors_set = set(survivors)

    # 2) survivors だけ時系列を構築(メモリ節約)
    series: dict[str, dict] = {
        code: {"d": [], "h": [], "l": [], "c": [], "t": []} for code in survivors
    }
    for d in dates:
        m = maps[d]
        for code in survivors:
            x = m.get(code)
            if x is None:
                continue
            s = series[code]
            s["d"].append(d)
            s["h"].append(x["h"])
            s["l"].append(x["l"])
            s["c"].append(x["c"])
            s["t"].append(x["t"])

    # 3) スコアリング
    fn = STRATEGIES[strategy]
    hits = []
    for code in survivors:
        s = series[code]
        if not s["d"] or s["d"][-1] != last:
            continue
        res = fn(s, params, np)
        if res is None:
            continue
        m = master.get(code, {})
        row = {
            "code": code,
            "name": m.get("name", ""),
            "sector33_name": m.get("sector33_name", ""),
            "market": m.get("market", ""),
            "price_now": round(s["c"][-1], 1),
            "turnover_now": s["t"][-1],
            "score": round(res["score"], 2),
        }
        row.update(res["metrics"])
        hits.append(row)

    hits.sort(key=lambda h: h["score"], reverse=True)
    top = int(float(params.get("top") or 100))
    hits = hits[:top]
    for i, h in enumerate(hits, 1):
        h["rank"] = i

    run_ts = dt.datetime.now()
    record = {
        "meta": {
            "kind": "strategy",
            "strategy": strategy,
            "label": STRATEGY_LABELS.get(strategy, strategy),
            "search_id": f"strat_{strategy}_{run_ts:%Y%m%d_%H%M%S}",
            "created_at": run_ts.isoformat(timespec="seconds"),
            "date_now": date_now,
            "lookback": lookback,
            "universe": len(survivors),
            "hit_count": len(hits),
            "filter": flt,
        },
        "columns": COLUMNS[strategy],
        "results": hits,
    }
    if not _no_save(params):
        _save_screen(record)
    return record


# 複合スクリーン用の戦略順(列順もこれに従う)
OVERLAP_ORDER = ["momentum", "volume", "vcp", "newhigh"]


def screen_overlap(params: dict) -> dict:
    """①〜④の全戦略を実行し、複数戦略に同時出現した銘柄を重複数順に並べる。

    共通の小型株フィルタは全戦略に同一適用。各戦略は自前の既定パラメータ(min_r60等)で
    上位 per_top 件まで出し、それらを銘柄コードで突き合わせる。重複数(出現戦略数)の
    多い順、同数なら Σ(1/各戦略内順位) の大きい順(各リストの上位に居るほど高評価)。
    """
    per_top = int(float(params.get("per_top") or 100))
    per_top = max(5, min(per_top, 500))
    sub = dict(params)
    sub["top"] = per_top      # 各戦略はこの件数まで抽出して突き合わせる
    sub["no_save"] = "1"      # サブ戦略は保存しない(複合結果のみ保存する)

    agg: dict[str, dict] = {}
    per_counts: dict[str, int] = {}
    date_now = None
    flt = None
    for strat in OVERLAP_ORDER:
        r = screen(strat, sub)
        date_now = r["meta"]["date_now"]
        flt = r["meta"]["filter"]
        per_counts[strat] = r["meta"]["hit_count"]
        for row in r["results"]:
            code = row["code"]
            a = agg.get(code)
            if a is None:
                a = agg[code] = {
                    "code": code, "name": row["name"],
                    "sector33_name": row["sector33_name"], "market": row["market"],
                    "price_now": row["price_now"], "turnover_now": row["turnover_now"],
                    "ranks": {},
                }
            a["ranks"][strat] = row["rank"]

    rows = []
    for code, a in agg.items():
        ranks = a["ranks"]
        count = len(ranks)
        a["count"] = count
        a["score"] = round(sum(1.0 / rk for rk in ranks.values()), 4)
        a["strategies"] = [s for s in OVERLAP_ORDER if s in ranks]
        for s in OVERLAP_ORDER:
            a["rk_" + s] = ranks.get(s)  # 各戦略内の順位(未ヒットは None)
        a.pop("ranks", None)
        rows.append(a)

    # 重複数の多い順 → 同数なら Σ(1/順位) の大きい順
    rows.sort(key=lambda x: (-x["count"], -x["score"]))
    top = int(float(params.get("top") or 100))
    rows = rows[:top]
    for i, x in enumerate(rows, 1):
        x["rank"] = i

    run_ts = dt.datetime.now()
    record = {
        "meta": {
            "kind": "overlap",
            "strategy": "overlap",
            "label": STRATEGY_LABELS["overlap"],
            "search_id": f"overlap_{run_ts:%Y%m%d_%H%M%S}",
            "created_at": run_ts.isoformat(timespec="seconds"),
            "date_now": date_now,
            "per_top": per_top,
            "per_strategy_counts": per_counts,
            "hit_count": len(rows),
            "multi_count": sum(1 for x in rows if x["count"] >= 2),
            "filter": flt,
        },
        "order": OVERLAP_ORDER,
        "results": rows,
    }
    if not _no_save(params):
        _save_screen(record)
    return record

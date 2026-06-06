"""「1ヶ月で N倍」になった銘柄スクリーナー + 結果保存 / 参照ツール。

J-Quants V2 日足 (AdjC = 株式分割調整済終値) を使い、
最新営業日 T と その約 --days 日前の営業日 T0 を比較して
    AdjC(T) / AdjC(T0) >= --multiple
を満たす銘柄を抽出する。分割調整済を使うため、株式分割による
見かけの株価変動を「2倍」と誤検知しない。

サブコマンド:
  search   スクリーニングを実行し、結果を保存して表示
  list     保存済み検索の一覧
  show     保存済み検索の中身を表示

使い方:
  python -m src.movers search                 # 直近1ヶ月で2倍 (既定)
  python -m src.movers search --multiple 3 --days 30 --top 50
  python -m src.movers search --min-price 100 --min-turnover 50000000
  python -m src.movers list
  python -m src.movers show movers_20260606_101500

データ出典: J-Quants V2 (要 .env の JQUANTS_API_KEY / JQUANTS_REFRESH_TOKEN)。
保存先: saved_searches/<id>.json (+ 閲覧用 <id>.csv)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
SAVED_DIR = ROOT / "saved_searches"

# 結果CSV/表示の列順 (= JSON results 内のキー)
RESULT_FIELDS = [
    "rank", "code", "name", "sector33", "sector33_name", "market",
    "price_then", "price_now", "ratio", "pct_change",
    "turnover_now", "volume_now",
]


# ---------- データ取得ヘルパ ----------
def _adjclose_map(rows: list[dict]) -> dict[str, dict]:
    """日足行リスト → {code: {adjclose, turnover, volume}}。"""
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        c = r.get("AdjC")
        if c is None:
            c = r.get("C") or r.get("Close")
        if c is None:
            continue
        try:
            adjc = float(c)
        except (TypeError, ValueError):
            continue
        turnover = r.get("Va")
        volume = r.get("AdjVo") or r.get("Vo") or r.get("Volume")
        out[code] = {
            "adjclose": adjc,
            "turnover": float(turnover) if turnover not in (None, "") else None,
            "volume": float(volume) if volume not in (None, "") else None,
        }
    return out


def find_latest_available(client, today: dt.date, max_back: int = 12) -> tuple[dt.date, list[dict]]:
    """データが存在する最新営業日を探索して (日付, 日足行) を返す。"""
    d = today
    tried = 0
    while tried < max_back:
        if d.weekday() < 5:
            rows = client.daily_quotes_raw(d.isoformat())
            if rows:
                return d, rows
            tried += 1
        d -= dt.timedelta(days=1)
    raise RuntimeError(
        f"最新 {max_back} 営業日に日足データが見つかりませんでした "
        f"(API/プラン/権限を確認してください)"
    )


def fetch_day_with_data(client, target: dt.date, max_back: int = 7) -> tuple[dt.date, dict]:
    """target 以前で日足データのある営業日を探し (日付, adjclose_map) を返す。

    祝日などでデータが無い場合は1営業日ずつ遡る。確定した日は
    ディスクキャッシュ経由 (daily_quotes_by_date) で取得する。
    """
    d = target
    tried = 0
    while tried < max_back:
        if d.weekday() < 5:  # 平日のみ試行カウント。土日/祝日は1日ずつ素通り
            rows = client.daily_quotes_by_date(d.isoformat())
            if rows:
                return d, _adjclose_map(rows)
            tried += 1
        d -= dt.timedelta(days=1)
    raise RuntimeError(f"{target.isoformat()} 付近で日足データが見つかりませんでした")


def _master_map(client) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in client.listed_info():
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        out[code] = {
            "name": r.get("CoName") or r.get("CompanyName", ""),
            "sector33": str(r.get("S33") or r.get("Sector33Code", "")),
            "sector33_name": r.get("S33Nm") or r.get("Sector33CodeName", ""),
            "market": r.get("MktNm") or r.get("MarketCodeName", ""),
        }
    return out


# ---------- スクリーニング本体 ----------
def run_search(args: argparse.Namespace) -> dict:
    from .jquants_client import JQuantsClient, load_config_from_env

    today = dt.date.fromisoformat(args.asof) if args.asof else dt.date.today()
    cfg = load_config_from_env(CACHE_DIR)
    client = JQuantsClient(cfg)

    print("[info] 最新営業日を探索中 ...")
    date_now, rows_now = find_latest_available(client, today)
    map_now = _adjclose_map(rows_now)
    print(f"[info] 最新営業日 (T)  = {date_now.isoformat()}  ({len(map_now)} 銘柄)")

    target_then = date_now - dt.timedelta(days=args.days)
    date_then, map_then = fetch_day_with_data(client, target_then)
    print(f"[info] 比較営業日 (T0) = {date_then.isoformat()}  "
          f"({len(map_then)} 銘柄)  [約 {(date_now - date_then).days} 日前]")

    master = _master_map(client)

    hits = []
    for code, now in map_now.items():
        then = map_then.get(code)
        if not then:
            continue  # T0時点で未上場/データ無し → 1ヶ月リターン算出不可
        p0 = then["adjclose"]
        p1 = now["adjclose"]
        if p0 is None or p1 is None or p0 <= 0 or p1 <= 0:
            continue
        ratio = p1 / p0
        if ratio < args.multiple:
            continue
        if args.min_price is not None and p1 < args.min_price:
            continue
        if args.min_turnover is not None and (now["turnover"] or 0) < args.min_turnover:
            continue
        m = master.get(code, {})
        hits.append({
            "code": code,
            "name": m.get("name", ""),
            "sector33": m.get("sector33", ""),
            "sector33_name": m.get("sector33_name", ""),
            "market": m.get("market", ""),
            "price_then": round(p0, 2),
            "price_now": round(p1, 2),
            "ratio": round(ratio, 4),
            "pct_change": round((ratio - 1) * 100, 2),
            "turnover_now": now["turnover"],
            "volume_now": now["volume"],
        })

    hits.sort(key=lambda h: h["ratio"], reverse=True)
    for i, h in enumerate(hits, 1):
        h["rank"] = i

    run_ts = dt.datetime.now()
    meta = {
        "search_id": f"movers_{run_ts:%Y%m%d_%H%M%S}",
        "created_at": run_ts.isoformat(timespec="seconds"),
        "multiple": args.multiple,
        "window_days": args.days,
        "date_now": date_now.isoformat(),
        "date_then": date_then.isoformat(),
        "calendar_days": (date_now - date_then).days,
        "min_price": args.min_price,
        "min_turnover": args.min_turnover,
        # 母数 = T と T0 の両方に存在する銘柄数 (1ヶ月リターンを算出できた銘柄)
        "universe_size": sum(1 for c in map_now if c in map_then),
        "hit_count": len(hits),
    }
    record = {"meta": meta, "results": hits}

    if not args.no_save:
        _save(record)
        print(f"[saved] saved_searches/{meta['search_id']}.json (+ .csv)")

    _print_results(record, top=args.top)
    return record


# ---------- 保存 / 参照 ----------
def _save(record: dict) -> None:
    SAVED_DIR.mkdir(parents=True, exist_ok=True)
    sid = record["meta"]["search_id"]
    (SAVED_DIR / f"{sid}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with open(SAVED_DIR / f"{sid}.csv", "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(RESULT_FIELDS)
        for h in record["results"]:
            w.writerow([h.get(k, "") for k in RESULT_FIELDS])


def _load(search_id: str) -> dict:
    # パストラバーサル/グロブ injection 防止 (英数_- のみ許可)
    if not re.fullmatch(r"[\w\-]+", search_id):
        raise ValueError(f"不正な検索ID: {search_id!r}")
    # 完全IDでも、タイムスタンプ部分だけ(部分一致)でも引けるようにする
    p = SAVED_DIR / f"{search_id}.json"
    if not p.exists():
        cand = sorted(SAVED_DIR.glob(f"*{search_id}*.json"))
        if not cand:
            raise FileNotFoundError(f"保存済み検索が見つかりません: {search_id}")
        p = cand[-1]  # 部分一致が複数なら辞書順=時刻順で最新を採用
    return json.loads(p.read_text(encoding="utf-8"))


def _list_saved() -> list[dict]:
    if not SAVED_DIR.exists():
        return []
    metas = []
    for p in sorted(SAVED_DIR.glob("*.json"), reverse=True):
        try:
            metas.append(json.loads(p.read_text(encoding="utf-8"))["meta"])
        except (json.JSONDecodeError, KeyError):
            continue
    return metas


def _print_results(record: dict, top: Optional[int] = None) -> None:
    meta = record["meta"]
    results = record["results"]
    shown = results[: top] if top else results
    print(f"\n{'='*78}")
    print(f"  {meta['date_then']} → {meta['date_now']} "
          f"({meta['calendar_days']}日) で {meta['multiple']}倍以上: "
          f"{meta['hit_count']} 銘柄  (母数 {meta['universe_size']})")
    if meta.get("min_price") or meta.get("min_turnover"):
        print(f"  フィルタ: 最低株価={meta.get('min_price')} / "
              f"最低売買代金={meta.get('min_turnover')}")
    print(f"{'='*78}")
    if not results:
        print("  該当なし")
        return
    print(f"{'順':>3} {'コード':<7} {'銘柄名':<22} {'倍率':>6} "
          f"{'騰落':>8} {'前':>9} {'現':>9} {'市場':<8}")
    print(f"{'-'*78}")
    for h in shown:
        name = (h['name'] or '')[:20]
        print(f"{h['rank']:>3} {h['code']:<7} {name:<22} "
              f"{h['ratio']:>5.2f}x {h['pct_change']:>+7.1f}% "
              f"{h['price_then']:>9.1f} {h['price_now']:>9.1f} {(h['market'] or '')[:8]:<8}")
    if top and len(results) > top:
        print(f"  ... 他 {len(results) - top} 銘柄 (全件は CSV/JSON 参照)")


def cmd_list(_args: argparse.Namespace) -> None:
    metas = _list_saved()
    if not metas:
        print("保存済みの検索はありません。`python -m src.movers search` を実行してください。")
        return
    print(f"{'ID':<24} {'実行日時':<20} {'条件':<18} {'区間':<25} {'件数':>5}")
    print("-" * 95)
    for m in metas:
        cond = f"{m['multiple']}倍/{m['window_days']}日"
        span = f"{m['date_then']}→{m['date_now']}"
        print(f"{m['search_id']:<24} {m.get('created_at',''):<20} "
              f"{cond:<18} {span:<25} {m['hit_count']:>5}")


def cmd_show(args: argparse.Namespace) -> None:
    record = _load(args.id)
    _print_results(record, top=args.top)
    print(f"\nファイル: saved_searches/{record['meta']['search_id']}.json / .csv")


def main() -> None:
    p = argparse.ArgumentParser(
        description="1ヶ月で N倍 になった銘柄スクリーナー (J-Quants)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("search", help="スクリーニング実行 + 保存")
    sp.add_argument("--multiple", type=float, default=2.0, help="倍率しきい値 (既定2.0)")
    sp.add_argument("--days", type=int, default=30, help="比較する遡及日数 (既定30=約1ヶ月)")
    sp.add_argument("--top", type=int, default=50, help="標準出力の表示件数")
    sp.add_argument("--min-price", type=float, default=None, help="最低株価フィルタ(現在値)")
    sp.add_argument("--min-turnover", type=float, default=None,
                    help="最低売買代金フィルタ(現在日のVa,円)")
    sp.add_argument("--asof", default=None, help="基準日 (YYYY-MM-DD, 既定=今日)")
    sp.add_argument("--no-save", action="store_true", help="保存せず表示のみ")
    sp.set_defaults(func=run_search)

    lp = sub.add_parser("list", help="保存済み検索の一覧")
    lp.set_defaults(func=cmd_list)

    shp = sub.add_parser("show", help="保存済み検索の表示")
    shp.add_argument("id", help="検索ID (例: movers_20260606_101500)")
    shp.add_argument("--top", type=int, default=None, help="表示件数 (既定=全件)")
    shp.set_defaults(func=cmd_show)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        sys.exit(0)
    try:
        args.func(args)
    except Exception as e:
        print(f"[error] {e.__class__.__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""エンドツーエンドパイプライン (CLI)。

データソース:
  api  : J-Quants Light REST APIから全銘柄取得 (要 .env)
  mock : 内蔵モックデータで3銘柄を評価 (動作確認)
  csv  : 指標が計算済みのCSVを読み込んで評価 (オフライン検証)

使い方:
  python -m src.pipeline --source mock
  python -m src.pipeline --source csv --csv-input sample_data.csv
  python -m src.pipeline --source api

filters:
  --strategy "#3"          特定の手法だけで絞る
  --group A                A〜H 群で絞る
  --min-matches 2          指定本数以上ヒットした銘柄のみ
  --top 30                 標準出力プレビュー件数
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Optional

from .data_loader import StockFeatures
from .exclusions import load_exclusions, is_excluded, normalize_code
from .rule_engine import load_strategies, score_stock
# jquants_client (requests) と data_loader.load_all (numpy) は
# --source api 時のみ遅延 import する。

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "catalog" / "strategies.yaml"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / "cache"

NUMERIC_FIELDS = (
    "close", "sma50", "sma100", "sma200", "sma50_prev", "sma200_prev",
    "rsi14", "high60",
    "return_5d", "return_1m", "return_3m", "return_6m", "return_1y",
    "volatility_60", "range60_ratio", "vol_ratio", "vol_contraction",
    "zscore20", "pct_52w_high",
    "per", "pbr", "peg", "roe", "op_margin", "equity_ratio",
    "revenue_growth", "eps_growth", "dividend_yield", "dividend_growth_3y",
    "bps", "net_income", "prev_net_income", "market_cap",
    "post_earn_drift",
    "factor_value", "factor_momentum", "factor_quality", "sector_momentum_3m",
)
INT_FIELDS = ("days_since_statement", "days_since_listing", "days_to_exdiv")


def run(args: argparse.Namespace) -> Path:
    strategies = load_strategies(CATALOG_PATH)
    impl_count = sum(1 for s in strategies if s.implemented)
    print(f"[info] catalog loaded: {len(strategies)} strategies (implemented: {impl_count})")

    if args.source == "mock":
        features, end_d = _mock_features()
    elif args.source == "csv":
        features, end_d = _load_features_csv(Path(args.csv_input))
    elif args.source == "api":
        from .jquants_client import JQuantsClient, load_config_from_env
        from .data_loader import load_all
        cfg = load_config_from_env(CACHE_DIR)
        client = JQuantsClient(cfg)
        client.authenticate()
        print("[info] authenticated. fetching all stocks ...")
        features, end_d = load_all(client)
    else:
        raise ValueError(f"unknown source: {args.source}")

    print(f"[info] features built for {len(features)} stocks (asof {end_d.isoformat()})")

    scores = [score_stock(f, strategies) for f in features.values()]

    # MBO/TOB/上場廃止予定など、Lightでは判定できない銘柄を手動リストで除外
    if not args.no_exclude:
        exclusions = load_exclusions()
        if exclusions:
            before = len(scores)
            removed = [sc for sc in scores if is_excluded(sc.code, exclusions)]
            scores = [sc for sc in scores if not is_excluded(sc.code, exclusions)]
            if removed:
                for sc in removed:
                    meta = exclusions[normalize_code(sc.code)]
                    print(f"[exclude] {sc.code} {sc.name} — {meta.get('reason') or '除外リスト'}")
            print(f"[info] excluded {before - len(scores)} stock(s) via catalog/exclusions.yaml")

    if args.strategy:
        sid = int(args.strategy.lstrip("#").strip())
        filtered = []
        for sc in scores:
            hit = next((m for m in sc.matches if m.strategy_id == sid and m.matched), None)
            if hit:
                sc._focus_strength = hit.strength
                filtered.append(sc)
        scores = filtered
    if args.group:
        for sc in scores:
            sc.matches = [m for m in sc.matches if m.category == args.group]
    if args.min_matches > 0:
        scores = [sc for sc in scores if sc.match_count >= args.min_matches]

    if args.strategy:
        scores.sort(key=lambda s: getattr(s, "_focus_strength", 0.0), reverse=True)
    else:
        scores.sort(key=lambda s: (s.composite_score, s.match_count), reverse=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"jquants_match_{end_d.strftime('%Y%m%d')}.csv"
    _write_csv(scores, strategies, out_path)
    _print_preview(scores, args.top, args.strategy, args.group)
    print(f"[done] wrote {out_path} ({len(scores)} rows)")
    return out_path


def _write_csv(scores, strategies, out_path: Path) -> None:
    impl_ids = [s.id for s in strategies if s.implemented]
    header = [
        "code", "name", "sector33",
        "composite_score", "match_count",
        "matched_strategies", "top_reasons",
    ] + [f"s{i}" for i in impl_ids]

    with open(out_path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(header)
        for s in scores:
            strength_by_id = {m.strategy_id: m.strength for m in s.matches if m.matched}
            row = [
                s.code, s.name, s.sector,
                f"{s.composite_score:.1f}", s.match_count,
                s.matched_strategies_str(),
                s.top_reasons_str(),
            ] + [
                f"{strength_by_id[sid]:.1f}" if sid in strength_by_id else ""
                for sid in impl_ids
            ]
            w.writerow(row)


def _print_preview(scores, top: int, strategy, group) -> None:
    label = ""
    if strategy:
        label += f" / 手法 {strategy}"
    if group:
        label += f" / {group}群"
    print(f"\n{'='*70}")
    print(f"  該当 {len(scores)} 銘柄{label}")
    print(f"{'='*70}")
    for sc in scores[:top]:
        tags = " ".join(f"#{m.strategy_id}{m.strategy_name}" for m in sc.matched_sorted()[:6])
        print(f"\n[{sc.code}] {sc.name}  ({sc.sector})")
        print(f"  合成スコア={sc.composite_score}  マッチ数={sc.match_count}")
        print(f"  手法: {tags}")
        if sc.matched_sorted():
            top_m = sc.matched_sorted()[0]
            print(f"  主因: #{top_m.strategy_id}{top_m.strategy_name} (強度{top_m.strength}) — {top_m.reason}")


def _load_features_csv(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"CSV input not found: {path}")
    features = {}
    with open(path, encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            code = (row.get("code") or "").strip()
            if not code:
                continue
            feat = StockFeatures(
                code=code,
                name=row.get("name", ""),
                sector33_code=row.get("sector33") or row.get("sector", "") or "",
            )
            for k in NUMERIC_FIELDS:
                v = row.get(k)
                if v not in (None, "", "-"):
                    try:
                        setattr(feat, k, float(v))
                    except (TypeError, ValueError):
                        pass
            for k in INT_FIELDS:
                v = row.get(k)
                if v not in (None, "", "-"):
                    try:
                        setattr(feat, k, int(float(v)))
                    except (TypeError, ValueError):
                        pass
            features[code] = feat
    return features, dt.date.today()


def _mock_features():
    end_d = dt.date.today() - dt.timedelta(days=84)
    features = {}

    a = StockFeatures(code="0001", name="モックバリュー", sector33_code="9050")
    a.close = 1000.0; a.per = 8.0; a.pbr = 0.7
    a.dividend_yield = 0.045; a.roe = 0.18; a.op_margin = 0.18; a.equity_ratio = 0.65
    a.bps = 1500.0; a.market_cap = 1e10
    a.return_6m = 0.05; a.return_3m = 0.02; a.return_1m = 0.01
    a.factor_value = 0.92; a.factor_momentum = 0.45; a.factor_quality = 0.75
    a.sma50 = 1010; a.sma100 = 990; a.sma200 = 950
    a.sma50_prev = 940; a.sma200_prev = 905
    a.rsi14 = 55; a.high60 = 1010; a.volatility_60 = 0.012
    a.range60_ratio = 0.08; a.zscore20 = 0.2; a.vol_ratio = 0.9; a.vol_contraction = 0.4
    a.pct_52w_high = 0.99
    a.days_since_statement = 3
    features["0001"] = a

    b = StockFeatures(code="0002", name="モックグロース", sector33_code="3700")
    b.close = 5000.0; b.per = 25.0; b.pbr = 6.0
    b.revenue_growth = 0.35; b.eps_growth = 0.45; b.peg = 0.56
    b.roe = 0.22; b.op_margin = 0.20; b.equity_ratio = 0.55
    b.return_6m = 0.55; b.return_3m = 0.20; b.return_1m = 0.08; b.return_5d = 0.02
    b.return_1y = 0.80
    b.factor_value = 0.10; b.factor_momentum = 0.95; b.factor_quality = 0.85
    b.sma50 = 4800; b.sma100 = 4500; b.sma200 = 4000
    b.rsi14 = 68; b.high60 = 5100; b.volatility_60 = 0.025
    b.vol_ratio = 1.3; b.pct_52w_high = 0.96
    b.days_since_listing = 45
    features["0002"] = b

    c = StockFeatures(code="0003", name="モック逆張り", sector33_code="5050")
    c.close = 800.0; c.per = 18.0; c.pbr = 0.9
    c.roe = 0.06; c.op_margin = 0.05; c.equity_ratio = 0.35
    c.return_1m = -0.25; c.return_5d = 0.04; c.return_1y = -0.40
    c.return_6m = -0.30; c.return_3m = -0.20
    c.factor_value = 0.55; c.factor_momentum = 0.05; c.factor_quality = 0.30
    c.sma50 = 900; c.sma100 = 950; c.sma200 = 1000
    c.rsi14 = 22; c.high60 = 1100; c.volatility_60 = 0.035
    c.zscore20 = -2.3; c.pct_52w_high = 0.68
    features["0003"] = c

    return features, end_d


def main():
    p = argparse.ArgumentParser(description="J-Quants 82手法スクリーニングパイプライン")
    p.add_argument("--source", choices=["api", "mock", "csv"], default="mock")
    p.add_argument("--csv-input", default="sample_data.csv")
    p.add_argument("--strategy", default=None, help='特定手法だけで絞る (例: --strategy "#3")')
    p.add_argument("--group", default=None, choices=[None, "A", "B", "C", "D", "E", "F", "G", "H"])
    p.add_argument("--min-matches", type=int, default=0)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--no-exclude", action="store_true",
                   help="catalog/exclusions.yaml の除外リストを無視する")
    args = p.parse_args()
    try:
        run(args)
    except Exception as e:
        print(f"[error] {e.__class__.__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

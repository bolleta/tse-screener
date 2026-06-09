# TSE Screener

> A rule-driven screening engine for **Tokyo Stock Exchange** equities, built on the
> [J-Quants](https://jpx-jquants.com/) V2 REST API. It turns a catalog of 82 investment
> strategies into a deterministic scoring pipeline and ships three front-ends: a batch
> CSV pipeline, an "N-bagger" momentum screener, and a dependency-free HTML UI with
> interactive candlestick charts.

[![CI](https://github.com/bolleta/tse-screener/actions/workflows/ci.yml/badge.svg)](https://github.com/bolleta/tse-screener/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

English | [日本語](#日本語)

---

## Highlights

- **Strategy catalog as data, not code** — 82 strategies live in
  [`catalog/strategies.yaml`](catalog/strategies.yaml) as small Python expressions.
  Adding or tuning a rule means editing YAML, not Python. 31 are currently implemented
  (the rest require data beyond the J-Quants Light plan).
- **Strength-based scoring** — every rule returns `(matched, strength 0–100)` via a
  linear `_ramp(value, lo, hi)` mapping, so results are *ranked by conviction*, not just
  a binary hit/miss. A stock's composite score = Σ(strength × weight).
- **Sandboxed rule evaluation** — YAML expressions are evaluated with a restricted
  builtins whitelist; a malformed or malicious rule is caught and degraded to "no match"
  rather than crashing the run or escaping the sandbox.
- **Resilient API client** — disk-cached responses (atomic writes), 60 req/min throttle,
  and bounded 429 retry/backoff for the J-Quants Light rate limit.
- **Split-adjusted everywhere** — the N-bagger screener compares *adjusted* close
  (`AdjC`), so a stock split is never mistaken for a 2× move.
- **Tested & CI-gated** — 51 unit tests cover indicators, the rule engine, the
  sandbox, exclusion matching, and date logic; GitHub Actions runs them on 3.9 / 3.11 / 3.12.

## Architecture

```
                ┌──────────────────────┐
   J-Quants V2  │  jquants_client.py   │  REST + disk cache + throttle/retry
   REST API ───▶│  (requests)          │
                └──────────┬───────────┘
                           │ raw rows
                ┌──────────▼───────────┐
                │   data_loader.py     │  per-stock feature extraction
                │   + indicators.py    │  (SMA / RSI / momentum / vol / factors)
                └──────────┬───────────┘
                           │ StockFeatures
          ┌────────────────┼─────────────────────┐
          │                │                      │
  ┌───────▼──────┐  ┌──────▼────────┐     ┌───────▼────────┐
  │ rule_engine  │  │   movers.py   │     │ strategies.py  │
  │ + strategies │  │  N-bagger     │     │ precursor-     │
  │   .yaml      │  │  screener     │     │ signal screens │
  └───────┬──────┘  └──────┬────────┘     └───────┬────────┘
          │                │                      │
  ┌───────▼──────┐  ┌──────▼──────────────────────▼────────┐
  │ pipeline.py  │  │           web/server.py              │
  │ (batch CSV)  │  │  stdlib HTTP UI + chart.html (charts) │
  └──────────────┘  └──────────────────────────────────────┘
```

## Tech stack

| Layer        | Choice                                                              |
| ------------ | ------------------------------------------------------------------ |
| Language     | Python 3.9+ (`from __future__ import annotations` throughout)       |
| Numerics     | NumPy (lazy-imported — `mock`/`csv` modes start without it)         |
| Config       | PyYAML (strategy & exclusion catalogs)                             |
| HTTP         | `requests` (API client) / stdlib `http.server` (web UI, no Flask)   |
| Charts       | TradingView Lightweight Charts, Chart.js (CDN, no build step)       |
| Tests / CI   | pytest, GitHub Actions                                              |

## Project layout

```
tse-screener/
├── catalog/
│   ├── strategies.yaml     # 82-strategy catalog (expr / strength / weight)
│   └── exclusions.yaml     # manual exclusions (TOB/MBO/delisting, etc.)
├── src/
│   ├── jquants_client.py   # J-Quants V2 REST client (cache, throttle, retry)
│   ├── data_loader.py      # raw rows → per-stock StockFeatures
│   ├── indicators.py       # SMA / RSI / momentum / volatility / z-score
│   ├── rule_engine.py      # YAML expr eval + _ramp strength + scoring
│   ├── exclusions.py       # 4/5-digit code normalization & matching
│   ├── movers.py           # "N× in a month" screener + save/load
│   ├── strategies.py       # precursor-signal screeners (web backend)
│   └── pipeline.py         # end-to-end batch CLI
├── web/
│   ├── server.py           # stdlib HTTP backend for the HTML UI
│   ├── index.html          # screener UI
│   ├── chart.html          # candlestick + volume chart
│   └── run_html.sh         # launcher
├── tests/                  # 51 unit tests (offline, deterministic)
├── sample_data.csv         # offline fixture for `--source csv`
└── requirements*.txt
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # runtime
# pip install -r requirements-dev.txt   # + pytest

cp .env.example .env                    # then add your J-Quants V2 API key
```

`.env`:

```
JQUANTS_API_KEY=your-api-key-here
```

> A J-Quants **Light** plan key works for everything here. The key is read from `.env`,
> never committed, and the web UI binds to `127.0.0.1` only.

## Usage

### 1) Batch screening pipeline (`src/pipeline.py`)

```bash
python -m src.pipeline --source mock                          # 3 synthetic stocks, no API
python -m src.pipeline --source csv --csv-input sample_data.csv
python -m src.pipeline --source api                           # full TSE universe (needs .env)

# filters
python -m src.pipeline --source csv --csv-input sample_data.csv --strategy "#1"
python -m src.pipeline --source csv --csv-input sample_data.csv --group A --min-matches 2
```

Writes `output/jquants_match_YYYYMMDD.csv`, ranked by composite score. Each row carries
the matched strategies, human-readable reasons, and per-strategy strength columns.

### 2) "N× in a month" screener (`src/movers.py`)

Compares adjusted close on the latest trading day `T` against ~`--days` ago `T0` and
keeps `AdjC(T) / AdjC(T0) >= --multiple`.

```bash
export $(grep -v '^#' .env | xargs)

python -m src.movers search                                   # ≥2× over ~30d (defaults)
python -m src.movers search --multiple 3 --days 30 --min-turnover 50000000 --top 50
python -m src.movers list                                     # past searches
python -m src.movers show movers_20260606_101500              # re-display a saved run
```

Results auto-save to `saved_searches/<id>.json` (+ `.csv` for Excel).

### 3) HTML UI (`web/server.py`)

A dependency-free site (Python stdlib only) over the same logic:

```bash
./web/run_html.sh            # http://localhost:8765
PORT=9000 ./web/run_html.sh
```

- condition form → runs the screener → sortable results table
- click a row → daily candlestick + volume chart (`chart.html`, 1M/3M/6M/1Y/All)
- "ranking trend" tab → weekly bump chart of each stock's rank over time
- CSV download; auto-loads `.env`, binds to `127.0.0.1` only

## How scoring works

Each rule in the catalog is `(expr, strength, reason)`:

```yaml
- id: 1
  name: バリュー投資            # Value
  category: A
  expr: "f.per is not None and f.pbr is not None and 0 < f.per <= 12 and 0 < f.pbr <= 1.0"
  strength: "(_ramp(f.per, 12, 5) + _ramp(f.pbr, 1.0, 0.5)) / 2"
  reason: "PER={f.per:.1f}/PBR={f.pbr:.2f}"
```

- `expr` → did it match? (`f` is the stock's `StockFeatures`)
- `strength` → 0–100 conviction. `_ramp(v, lo, hi)` maps `v∈[lo,hi]` to `0→100` with
  clipping; `lo > hi` reverses it (smaller = stronger, e.g. PER).
- composite score = Σ(strength × weight), used for ranking.

Strategies flagged `implemented: false` (data not available on Light) simply produce
empty columns — enabling one later is a pure YAML edit.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

All tests are offline and deterministic (no API calls). Coverage includes the indicator
math, `_ramp`, rule matching/strength, the eval **sandbox**, exclusion-code normalization,
business-day arithmetic, and the adjusted-close parser.

## Engineering notes

- **Lazy heavy imports** — NumPy and the indicator module load only when `--source api`
  actually needs them, so `mock`/`csv` modes (and the test suite) start instantly.
- **Atomic cache writes** — API responses are written to `*.tmp` then `replace()`d, so an
  interrupted run never leaves a half-written JSON that poisons the cache.
- **Input hardening** — saved-search IDs are validated against `[\w\-]+` before any glob
  to prevent path traversal; the rule sandbox whitelists builtins.

## Notes & disclaimer

- Not investment advice. Screening output is a starting point; do your own research.
- J-Quants data is fine for personal use, but redistribution / reselling (content feeds,
  SaaS, etc.) is outside personal-use scope — keep this for your own analysis.
- High-turnover strategies should be evaluated net of tax and financing costs.

---

## 日本語

[J-Quants](https://jpx-jquants.com/) V2 REST API を使った**東証銘柄スクリーナー**。
82手法カタログ([`catalog/strategies.yaml`](catalog/strategies.yaml))を決定的なスコアリング
パイプラインに落とし込み、3つのフロントエンド（バッチCSV / N倍スクリーナー / 依存ゼロの
HTML UI）を提供します。

### 特徴

- **手法をコードではなくデータで管理** — 82手法を YAML の小さな Python 式として記述。
  ルールの追加・調整は YAML 編集のみ（実装済み31 / Light非対応は未実装フラグ）。
- **強度ベースのスコアリング** — 各ルールは `(matched, strength 0–100)` を返す。
  `_ramp(value, lo, hi)` の線形マッピングで「効き具合」を連続値化し、合成スコア
  = Σ(strength × weight) でランキング。
- **サンドボックス評価** — YAML式は builtins をホワイトリスト制限して評価。不正な式は
  例外を握りつぶして「非マッチ」に縮退し、実行全体を止めない／サンドボックスを破らない。
- **堅牢なAPIクライアント** — ディスクキャッシュ（アトミック書き込み）、60 req/min スロット
  リング、429 のリトライ/バックオフ。
- **常に分割調整済** — N倍スクリーナーは調整後終値 `AdjC` で比較し、株式分割を「2倍」と
  誤検知しない。
- **テスト + CI** — 指標計算・ルールエンジン・サンドボックス・除外照合・日付計算を
  51 ユニットテストで検証し、GitHub Actions が 3.9 / 3.11 / 3.12 で実行。

### セットアップ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # J-Quants V2 の APIキーを記入
```

### 使い方（概要）

```bash
# バッチスクリーニング
python -m src.pipeline --source mock                  # API不要の動作確認
python -m src.pipeline --source api                   # 東証全銘柄（要 .env）

# N倍スクリーナー
export $(grep -v '^#' .env | xargs)
python -m src.movers search --multiple 2 --days 30

# HTML UI
./web/run_html.sh                                     # http://localhost:8765

# テスト
pip install -r requirements-dev.txt && pytest
```

詳細な CLI フラグ・出力スキーマ・スコアの考え方は上記の英語セクションを参照してください。

### 注意

- 本ツールは投資助言ではありません。スクリーニング結果は出発点であり、最終判断はご自身で。
- J-Quants データの個人利用は規約上問題ありませんが、第三者への継続提供・配布（SaaS等）は
  個人利用範囲外です。

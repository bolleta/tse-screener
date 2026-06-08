"""movers スクリーナーを操作する HTML UI 用の軽量バックエンド。

Python 標準ライブラリのみ (Flask等の追加依存なし)。
src/movers.py の検索/保存/参照ロジックをそのまま HTTP 越しに呼び出す。

起動:
  python3 web/server.py                 # http://localhost:8765
  PORT=9000 python3 web/server.py       # ポート変更

エンドポイント:
  GET  /                  index.html (UI)
  GET  /api/list          保存済み検索の一覧 (meta)
  GET  /api/show?id=...   保存済み検索の中身 (results)
  GET  /api/csv?id=...    保存済みCSVをダウンロード
  POST /api/search        スクリーニング実行 (JSON body) → 保存して結果を返す

127.0.0.1 のみにバインド (APIキーを使うためローカル限定)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WEB_DIR = ROOT / "web"
CACHE_DIR = ROOT / "cache"
SAVED_DIR = ROOT / "saved_searches"
ID_RE = re.compile(r"[\w\-]+")
CODE_RE = re.compile(r"[0-9A-Za-z]+")

# プロセス内キャッシュ (銘柄マスタは初回のみロード)
_MASTER: dict[str, dict] = {}


def _load_env() -> None:
    """.env を os.environ に流し込む (既存の環境変数は上書きしない)。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _run_search(params: dict) -> dict:
    from src import movers

    def _num(key, cast):
        v = params.get(key)
        if v in (None, "", "null"):
            return None
        return cast(v)

    args = argparse.Namespace(
        multiple=float(params.get("multiple") or 2.0),
        days=int(params.get("days") or 30),
        top=int(params.get("top") or 50),
        min_price=_num("min_price", float),
        min_turnover=_num("min_turnover", float),
        asof=(params.get("asof") or None),
        no_save=False,
    )
    return movers.run_search(args)


def _client():
    from src.jquants_client import JQuantsClient, load_config_from_env
    return JQuantsClient(load_config_from_env(CACHE_DIR))


def _name_for(client, code: str) -> str:
    global _MASTER
    if not _MASTER:
        from src import movers
        _MASTER = movers._master_map(client)
    return _MASTER.get(code, {}).get("name", "")


def _bars(code: str, years: float) -> dict:
    """単一銘柄の日足(調整後OHLCV)を取得してチャート用JSONに整形。"""
    client = _client()
    today = dt.date.today()
    frm = (today - dt.timedelta(days=int(365 * years))).isoformat()
    rows = client.daily_quotes_by_code(code, frm, today.isoformat())
    bars = []
    for r in rows:
        date = r.get("Date")
        o, h, l, c = r.get("AdjO"), r.get("AdjH"), r.get("AdjL"), r.get("AdjC")
        v = r.get("AdjVo")
        if not date or None in (o, h, l, c):
            continue
        bars.append({
            "date": str(date)[:10],
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": float(v) if v not in (None, "") else 0.0,
        })
    bars.sort(key=lambda b: b["date"])
    return {"code": code, "name": _name_for(client, code), "bars": bars}


def _resolve_day(client, target, memo: dict):
    """target 以前で日足のある営業日を (date, adjclose_map) で返す (メモ化)。"""
    from src import movers
    key = target.isoformat()
    if key in memo:
        return memo[key]
    sd, smap = movers.fetch_day_with_data(client, target, max_back=7)
    memo[key] = (sd, smap)
    memo[sd.isoformat()] = (sd, smap)  # 解決後の日付でも引けるように
    return sd, smap


def _ranking_trend(weeks: int, top: int, days: int) -> dict:
    """週次スナップショットごとに「1ヶ月上昇率ランキング」を計算し、

    各週Top `top` に入った銘柄の和集合について、週ごとの順位推移を返す
    (バンプチャート用)。
    """
    from src import movers
    client = _client()
    today = dt.date.today()
    date_now, rows_now = movers.find_latest_available(client, today)
    memo = {date_now.isoformat(): (date_now, movers._adjclose_map(rows_now))}

    snapshots: dict[str, dict] = {}   # 解決日 -> {code: (rank, pct)}
    then_of: dict[str, str] = {}      # 解決日 -> 比較日(T0)
    for i in range(weeks):
        target = date_now - dt.timedelta(days=7 * i)
        sd, smap = _resolve_day(client, target, memo)
        skey = sd.isoformat()
        if skey in snapshots:
            continue  # 祝日等で同じ営業日に解決したらスキップ
        td, tmap = _resolve_day(client, sd - dt.timedelta(days=days), memo)
        rets = []
        for code, now in smap.items():
            t = tmap.get(code)
            if not t:
                continue
            p0, p1 = t["adjclose"], now["adjclose"]
            if p0 and p1 and p0 > 0 and p1 > 0:
                rets.append((code, p1 / p0))
        rets.sort(key=lambda x: x[1], reverse=True)
        rank_map = {code: (idx, round((ratio - 1) * 100, 1))
                    for idx, (code, ratio) in enumerate(rets[:top], 1)}
        snapshots[skey] = rank_map
        then_of[skey] = td.isoformat()

    dates = sorted(snapshots.keys())  # 古→新
    union = set()
    for rm in snapshots.values():
        union.update(rm.keys())
    master = movers._master_map(client)

    series = []
    for code in union:
        ranks, pcts = [], []
        for d in dates:
            e = snapshots[d].get(code)
            ranks.append(e[0] if e else None)
            pcts.append(e[1] if e else None)
        best = min(r for r in ranks if r is not None)
        series.append({
            "code": code,
            "name": master.get(code, {}).get("name", ""),
            "ranks": ranks, "pcts": pcts, "best": best,
        })
    series.sort(key=lambda s: s["best"])
    return {
        "snapshots": dates,
        "then_dates": [then_of[d] for d in dates],
        "series": series,
        "meta": {"weeks": len(dates), "top": top, "days": days,
                 "date_now": date_now.isoformat(), "universe": len(union)},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "moversweb/1.0"

    # ---------- レスポンスヘルパ ----------
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET ----------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._bytes(
                (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8"
            )
        if u.path.endswith(".html"):
            # ディレクトリ部を捨ててWEB_DIR内のみ参照 (トラバーサル防止)
            fp = WEB_DIR / Path(u.path).name
            if fp.exists():
                return self._bytes(fp.read_bytes(), "text/html; charset=utf-8")
            return self._json({"error": "not found"}, 404)
        if u.path == "/api/bars":
            qs = parse_qs(u.query)
            code = (qs.get("code") or [""])[0]
            if not CODE_RE.fullmatch(code):
                return self._json({"error": "不正な銘柄コード"}, 400)
            try:
                years = float((qs.get("years") or ["2"])[0])
            except ValueError:
                years = 2.0
            years = min(max(years, 0.1), 5.0)
            try:
                return self._json(_bars(code, years))
            except Exception as e:
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)
        if u.path == "/api/ranking_trend":
            qs = parse_qs(u.query)

            def _qint(name, default, lo, hi):
                try:
                    return min(max(int((qs.get(name) or [default])[0]), lo), hi)
                except ValueError:
                    return default
            weeks = _qint("weeks", 8, 2, 26)
            top = _qint("top", 100, 1, 500)
            days = _qint("days", 30, 1, 120)
            try:
                return self._json(_ranking_trend(weeks, top, days))
            except Exception as e:
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)
        if u.path == "/api/strategy_screen":
            from src import strategies
            qs = parse_qs(u.query)
            strat = (qs.get("strategy") or [""])[0]
            if strat not in strategies.STRATEGIES:
                return self._json({"error": f"未知の戦略: {strat}"}, 400)
            sp = {k: v[0] for k, v in qs.items()}  # 各パラメータは先頭値を採用
            try:
                return self._json(strategies.screen(strat, sp))
            except Exception as e:
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)
        if u.path == "/api/overlap_screen":
            from src import strategies
            sp = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                return self._json(strategies.screen_overlap(sp))
            except Exception as e:
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)
        if u.path == "/api/strategy_list":
            from src import strategies
            return self._json({"searches": strategies.list_saved()})
        if u.path == "/api/strategy_show":
            from src import strategies
            sid = (parse_qs(u.query).get("id") or [""])[0]
            try:
                return self._json(strategies.load_saved(sid))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/strategy_csv":
            from src import strategies
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not ID_RE.fullmatch(sid):
                return self._json({"error": "不正なID"}, 400)
            p = strategies.SAVED_DIR / f"{sid}.csv"
            if not p.exists():
                return self._json({"error": "CSVが見つかりません"}, 404)
            return self._bytes(
                p.read_bytes(), "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{sid}.csv"'},
            )
        if u.path == "/api/list":
            from src import movers
            return self._json({"searches": movers._list_saved()})
        if u.path == "/api/show":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            try:
                from src import movers
                return self._json(movers._load(sid))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/csv":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            if not ID_RE.fullmatch(sid):
                return self._json({"error": "不正なID"}, 400)
            p = SAVED_DIR / f"{sid}.csv"
            if not p.exists():
                return self._json({"error": "CSVが見つかりません"}, 404)
            return self._bytes(
                p.read_bytes(), "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{sid}.csv"'},
            )
        return self._json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path == "/api/search":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                params = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "JSONが不正です"}, 400)
            try:
                return self._json(_run_search(params))
            except Exception as e:
                return self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)
        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


def main() -> None:
    _load_env()
    port = int(os.environ.get("PORT", "8765"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[movers-web] http://localhost:{port}  (Ctrl-C で停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[movers-web] stopped")
        httpd.server_close()


if __name__ == "__main__":
    main()

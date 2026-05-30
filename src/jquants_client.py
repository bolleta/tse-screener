"""J-Quants REST API V2 client (Light plan)。

V2 (2025-12-22 移行) 仕様:
  - Base URL: https://api.jquants.com/v2
  - 認証: x-api-key ヘッダに APIキー (ダッシュボード発行) を入れるだけ
  - レスポンス: {"data": [...], "pagination_key": "..."} 形式
  - レートリミット: Light = 60 req/min (= ~1.0 sec/req)

実装済みエンドポイント (Light で取れるもののみ):
  GET /v2/equities/master        : 銘柄一覧
  GET /v2/equities/bars/daily    : 日次OHLCV (date= で全銘柄一括 / code= で個別)
  GET /v2/fins/summary           : 財務開示 (date= で当日全社 / code= で個別履歴)
  GET /v2/markets/calendar       : 営業日カレンダー

すべて取得結果はローカルキャッシュ(JSON)に保存。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

API_BASE = "https://api.jquants.com/v2"
DEFAULT_THROTTLE_SEC = 1.05  # Light = 60/min なので 1秒超で安全側


@dataclass
class JQuantsConfig:
    cache_dir: Path
    api_key: str
    throttle: float = DEFAULT_THROTTLE_SEC


class JQuantsClient:
    def __init__(self, cfg: JQuantsConfig):
        self.cfg = cfg
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({"x-api-key": cfg.api_key})

    def authenticate(self) -> None:
        """V2 は APIキーヘッダのみなので、軽い疎通確認だけ実施。"""
        self._throttle()
        r = self._session.get(f"{API_BASE}/equities/master", timeout=15)
        r.raise_for_status()

    def _throttle(self) -> None:
        wait = self.cfg.throttle - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _get(self, path: str, params: Optional[dict] = None) -> list[dict]:
        params = dict(params or {})
        rows: list[dict] = []
        pagination_key: Optional[str] = None
        retries = 0
        max_retries = 5
        while True:
            q = dict(params)
            if pagination_key:
                q["pagination_key"] = pagination_key
            self._throttle()
            url = f"{API_BASE}{path}"
            if q:
                url = f"{url}?{urlencode(q)}"
            r = self._session.get(url, timeout=30)
            if r.status_code == 429:
                # レート制限ヒット → 60秒待ってリトライ (上限あり=無限ループ防止)
                retries += 1
                if retries > max_retries:
                    r.raise_for_status()
                time.sleep(60)
                continue
            retries = 0  # ページ取得成功でリセット
            r.raise_for_status()
            body = r.json()
            page = body.get("data") or []
            rows.extend(page)
            pagination_key = body.get("pagination_key")
            if not pagination_key:
                break
        return rows

    def _cached(self, key: str, fetch_fn):
        f = self.cfg.cache_dir / f"{key}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 破損キャッシュ(部分書き込み等)は捨てて再取得
                f.unlink(missing_ok=True)
        data = fetch_fn()
        # アトミック書き込み: 途中終了で壊れたJSONを残さない
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        return data

    # ---------- 公開メソッド ----------
    def listed_info(self) -> list[dict]:
        """全上場銘柄情報。日次更新だが本パイプラインでは1回キャッシュ。"""
        return self._cached("listed_info", lambda: self._get("/equities/master"))

    def daily_quotes_by_date(self, date: str) -> list[dict]:
        """指定日(YYYY-MM-DD)の全銘柄日足。V2 Lightはほぼ翌日反映。"""
        return self._cached(
            f"prices_{date}",
            lambda: self._get("/equities/bars/daily", {"date": date}),
        )

    def daily_quotes_raw(self, date: str) -> list[dict]:
        """ディスクキャッシュを使わず日足を取得 (最新営業日の探索用)。

        未公開日(データ未反映)を空配列としてキャッシュに焼き付けないよう、
        最新日探索ではこちらを使う。
        """
        return self._get("/equities/bars/daily", {"date": date})

    def daily_quotes_by_code(
        self, code: str, from_: Optional[str] = None, to_: Optional[str] = None
    ) -> list[dict]:
        """単一銘柄の日足履歴 (chart用)。最新足を表示するためキャッシュしない。

        昇順(古→新)で返り、AdjO/AdjH/AdjL/AdjC/AdjVo に分割調整済OHLCVを含む。
        """
        params = {"code": code}
        if from_:
            params["from"] = from_
        if to_:
            params["to"] = to_
        return self._get("/equities/bars/daily", params)

    def statements_by_date(self, date: str) -> list[dict]:
        """指定日に開示された全社の財務サマリ。"""
        return self._cached(
            f"statements_{date}",
            lambda: self._get("/fins/summary", {"date": date}),
        )

    def trading_calendar(self, from_: str, to_: str) -> list[dict]:
        return self._cached(
            f"calendar_{from_}_{to_}",
            lambda: self._get("/markets/calendar", {"from": from_, "to": to_}),
        )


def load_config_from_env(cache_dir: Path) -> JQuantsConfig:
    # V2 は APIキー方式 (refreshToken / mail+password は V1 のみ)。
    # 互換維持のため JQUANTS_REFRESH_TOKEN もキー名として受ける。
    api_key = (
        os.environ.get("JQUANTS_API_KEY")
        or os.environ.get("JQUANTS_REFRESH_TOKEN")
    )
    if not api_key:
        raise RuntimeError(
            "環境変数 JQUANTS_API_KEY を設定してください "
            "(V2 APIキー — ダッシュボード発行)"
        )
    return JQuantsConfig(cache_dir=cache_dir, api_key=api_key)

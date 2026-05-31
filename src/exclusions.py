"""銘柄除外リスト (catalog/exclusions.yaml) のローダ。

J-Quants Light では取得できない MBO/TOB/上場廃止予定などの銘柄を
手動登録し、パイプライン・教科書生成・Web UI の全段で除外するための共有モジュール。

依存は PyYAML と標準ライブラリのみ。src 配下・scripts 配下・web 配下の
どこからでも import できるよう、他の src モジュールには依存しない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "catalog" / "exclusions.yaml"


def normalize_code(code: str) -> str:
    """銘柄コードを照合用に正規化する。

    J-Quants の銘柄コードは 5桁 (4桁ティッカー + 末尾0)。
    ユーザーが 4桁 (例: 7490) で登録しても 5桁 (74900) と一致させたいので、
    4桁なら末尾に0を足して 5桁に揃える。
    """
    c = (code or "").strip()
    if len(c) == 4 and c.isdigit():
        return c + "0"
    return c


def load_exclusions(path: Optional[Path] = None) -> dict[str, dict]:
    """除外リストを {正規化コード: {code,name,reason,source,date}} で返す。

    ファイルが無い / 空 の場合は空 dict。
    """
    p = path or DEFAULT_PATH
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    entries = data.get("exclusions") or []
    out: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        raw = str(e.get("code", "")).strip()
        if not raw:
            continue
        out[normalize_code(raw)] = {
            "code": raw,
            "name": e.get("name", ""),
            "reason": e.get("reason", ""),
            "source": e.get("source", ""),
            "date": e.get("date", ""),
        }
    return out


def is_excluded(code: str, exclusions: dict[str, dict]) -> bool:
    """指定コードが除外対象か。4桁/5桁どちらの表記でも照合する。"""
    return normalize_code(code) in exclusions

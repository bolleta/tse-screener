"""82手法ルールエンジン (強度スコア対応版)。

各手法は catalog/strategies.yaml に Python expression として記述。
評価コンテキストは StockFeatures インスタンス(`f`) と数値ヘルパー(`_ramp` 等)。

評価フロー:
  1) expr (bool)            -> マッチしたかを判定
  2) strength (0-100 float) -> マッチ時の強度。条件の効き具合を連続値で表現
                                strength 省略時はマッチで一律 100、不一致で 0
  3) reason                  -> 人間向け説明テキスト (f-string format)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .data_loader import StockFeatures


def _ramp(value, lo: float, hi: float) -> float:
    """value が lo→hi に動くにつれ 0→100 に線形変化(クリップ付き)。

    lo > hi のとき逆向き(値が小さいほど強い)になる。
    """
    if value is None:
        return 0.0
    if hi == lo:
        return 100.0 if value >= hi else 0.0
    t = (value - lo) / (hi - lo)
    return max(0.0, min(100.0, t * 100.0))


SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "len": len, "round": round,
    "any": any, "all": all, "sum": sum,
}


@dataclass
class Strategy:
    id: int
    name: str
    category: str
    light_compatible: bool
    implemented: bool
    expr: Optional[str]
    strength: Optional[str]
    reason: Optional[str]
    weight: float = 1.0


@dataclass
class StrategyMatch:
    strategy_id: int
    strategy_name: str
    category: str
    matched: bool
    strength: float
    reason: str
    weight: float


def load_strategies(yaml_path: Path) -> list[Strategy]:
    with open(yaml_path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp)
    out: list[Strategy] = []
    for entry in data["strategies"]:
        out.append(
            Strategy(
                id=entry["id"],
                name=entry["name"],
                category=entry["category"],
                light_compatible=bool(entry.get("light_compatible", False)),
                implemented=bool(entry.get("implemented", False)),
                expr=entry.get("expr"),
                strength=entry.get("strength"),
                reason=entry.get("reason"),
                weight=float(entry.get("weight", 1.0)),
            )
        )
    return out


def _eval_ctx(feat: StockFeatures) -> dict:
    return {
        "f": feat,
        "_ramp": _ramp,
        "__builtins__": SAFE_BUILTINS,
    }


def evaluate(strategy: Strategy, feat: StockFeatures) -> StrategyMatch:
    if not strategy.implemented or not strategy.expr:
        return StrategyMatch(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            category=strategy.category,
            matched=False, strength=0.0,
            reason="not_implemented",
            weight=strategy.weight,
        )
    ctx = _eval_ctx(feat)
    try:
        matched = bool(eval(strategy.expr, ctx))
    except Exception as e:
        return StrategyMatch(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            category=strategy.category,
            matched=False, strength=0.0,
            reason=f"eval_error:{e.__class__.__name__}",
            weight=strategy.weight,
        )
    if not matched:
        return StrategyMatch(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            category=strategy.category,
            matched=False, strength=0.0,
            reason="",
            weight=strategy.weight,
        )

    strength_val = 100.0
    if strategy.strength:
        try:
            strength_val = float(eval(strategy.strength, ctx))
            strength_val = max(0.0, min(100.0, strength_val))
        except Exception:
            strength_val = 100.0

    reason_text = ""
    if strategy.reason:
        try:
            reason_text = strategy.reason.format(f=feat)
        except Exception:
            reason_text = strategy.reason

    return StrategyMatch(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        category=strategy.category,
        matched=True,
        strength=round(strength_val, 1),
        reason=reason_text,
        weight=strategy.weight,
    )


@dataclass
class StockScore:
    code: str
    name: str
    sector: str
    matches: list

    @property
    def composite_score(self) -> float:
        return round(sum(m.strength * m.weight for m in self.matches if m.matched), 1)

    @property
    def match_count(self) -> int:
        return sum(1 for m in self.matches if m.matched)

    def matched_sorted(self) -> list:
        return sorted(
            [m for m in self.matches if m.matched],
            key=lambda m: m.strength,
            reverse=True,
        )

    def matched_strategies_str(self) -> str:
        return ";".join(
            f"#{m.strategy_id}{m.strategy_name}({m.strength:.0f})"
            for m in self.matched_sorted()
        )

    def top_reasons_str(self, k: int = 5) -> str:
        top = self.matched_sorted()[:k]
        return " | ".join(
            f"#{m.strategy_id} {m.reason}" for m in top if m.reason
        )


def score_stock(feat: StockFeatures, strategies: list) -> StockScore:
    matches = [evaluate(s, feat) for s in strategies]
    return StockScore(
        code=feat.code,
        name=feat.name,
        sector=feat.sector33_code,
        matches=matches,
    )

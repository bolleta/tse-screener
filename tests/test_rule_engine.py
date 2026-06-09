"""ルールエンジン (src/rule_engine.py) のユニットテスト。

_ramp の線形マッピング、式評価のマッチ判定/強度/エラー耐性、
合成スコアの集計、そして式評価のサンドボックス性 (危険な builtin が
使えないこと) を検証する。
"""
import pytest

from src.data_loader import StockFeatures
from src.rule_engine import (
    Strategy,
    StockScore,
    _ramp,
    evaluate,
    score_stock,
)


# ---------- _ramp ----------
def test_ramp_none_is_zero():
    assert _ramp(None, 0, 100) == 0.0


def test_ramp_forward():
    assert _ramp(0, 0, 100) == pytest.approx(0.0)
    assert _ramp(50, 0, 100) == pytest.approx(50.0)
    assert _ramp(100, 0, 100) == pytest.approx(100.0)


def test_ramp_reverse_direction():
    # lo>hi: 値が小さいほど強い (例: PER は小さいほど割安)
    assert _ramp(12, 12, 5) == pytest.approx(0.0)
    assert _ramp(5, 12, 5) == pytest.approx(100.0)
    assert _ramp(8.5, 12, 5) == pytest.approx(50.0)


def test_ramp_clips_to_0_100():
    assert _ramp(-50, 0, 100) == 0.0     # 下限クリップ
    assert _ramp(150, 0, 100) == 100.0   # 上限クリップ
    assert _ramp(3, 12, 5) == 100.0      # 逆向きで境界超え


def test_ramp_equal_bounds():
    assert _ramp(5, 5, 5) == 100.0   # value >= hi
    assert _ramp(4, 5, 5) == 0.0


# ---------- evaluate ----------
def _strategy(**kw):
    base = dict(
        id=1, name="テスト", category="A",
        light_compatible=True, implemented=True,
        expr="f.per is not None and f.per < 15",
        strength=None, reason=None, weight=1.0,
    )
    base.update(kw)
    return Strategy(**base)


def test_evaluate_match_default_strength_100():
    feat = StockFeatures(code="0001", per=10.0)
    m = evaluate(_strategy(), feat)
    assert m.matched is True
    assert m.strength == 100.0


def test_evaluate_no_match():
    feat = StockFeatures(code="0001", per=30.0)
    m = evaluate(_strategy(), feat)
    assert m.matched is False
    assert m.strength == 0.0


def test_evaluate_not_implemented():
    feat = StockFeatures(code="0001", per=10.0)
    m = evaluate(_strategy(implemented=False), feat)
    assert m.matched is False
    assert m.reason == "not_implemented"


def test_evaluate_strength_expression_clipped():
    feat = StockFeatures(code="0001", per=5.0)
    m = evaluate(_strategy(strength="_ramp(f.per, 15, 5)"), feat)
    assert m.matched is True
    assert m.strength == 100.0  # PER=5 は割安側いっぱい


def test_evaluate_bad_expr_is_caught():
    feat = StockFeatures(code="0001", per=10.0)
    m = evaluate(_strategy(expr="f.nonexistent_attr.foo"), feat)
    assert m.matched is False
    assert m.reason.startswith("eval_error")


def test_evaluate_reason_template():
    feat = StockFeatures(code="0001", per=8.3)
    m = evaluate(_strategy(reason="PER={f.per:.1f}"), feat)
    assert m.reason == "PER=8.3"


def test_eval_is_sandboxed():
    """式評価コンテキストに危険な builtin が露出していないこと。

    __import__ 等は SAFE_BUILTINS に無いため NameError となり、
    evaluate がそれを握りつぶして matched=False を返す (例外を漏らさない)。
    """
    feat = StockFeatures(code="0001", per=10.0)
    m = evaluate(_strategy(expr="__import__('os').system('echo hacked')"), feat)
    assert m.matched is False
    assert m.reason.startswith("eval_error")


# ---------- StockScore 集計 ----------
def test_stock_score_aggregation():
    feat = StockFeatures(code="0001", name="テスト株", sector33_code="9050", per=8.0)
    strategies = [
        _strategy(id=1, name="バリュー", expr="f.per < 15", strength="80", weight=1.0),
        _strategy(id=2, name="非該当", expr="f.per > 100", weight=1.0),
        _strategy(id=3, name="高重み", expr="f.per < 15", strength="50", weight=2.0),
    ]
    score = score_stock(feat, strategies)
    assert score.match_count == 2
    # composite = 80*1.0 + 50*2.0 = 180
    assert score.composite_score == pytest.approx(180.0)
    # 強度降順で #1(80) が先
    assert score.matched_sorted()[0].strategy_id == 1
    assert "#1バリュー(80)" in score.matched_strategies_str()

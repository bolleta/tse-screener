"""パイプライン (src/pipeline.py) のスモークテスト。

モックデータ + 実カタログ (catalog/strategies.yaml) でスコアリングが
端から端まで動くことを、API に触れずに確認する。
"""
from src.pipeline import CATALOG_PATH, _mock_features
from src.rule_engine import load_strategies, score_stock


def test_catalog_loads():
    strategies = load_strategies(CATALOG_PATH)
    assert len(strategies) > 0
    # 実装済み戦略が1つ以上ある
    assert any(s.implemented for s in strategies)


def test_mock_value_stock_scores():
    strategies = load_strategies(CATALOG_PATH)
    features, _ = _mock_features()
    score = score_stock(features["0001"], strategies)  # モックバリュー株
    # バリュー寄りのモック株は複数手法にヒットし、正のスコアを持つ
    assert score.match_count > 0
    assert score.composite_score > 0
    # 出力フォーマットが壊れていない
    assert score.matched_strategies_str() != ""


def test_all_mock_stocks_score_without_error():
    strategies = load_strategies(CATALOG_PATH)
    features, _ = _mock_features()
    for feat in features.values():
        score = score_stock(feat, strategies)
        assert score.composite_score >= 0

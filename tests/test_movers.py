"""N倍スクリーナー (src/movers.py) のヘルパのユニットテスト。

日足行のパースと、保存済み検索ロードの入力検証 (パストラバーサル防止) を検証する。
"""
import pytest

from src import movers


# ---------- _adjclose_map ----------
def test_adjclose_prefers_adjusted_close():
    rows = [{"Code": "13010", "AdjC": "150", "C": "90",
             "Va": "1000000", "AdjVo": "5000"}]
    m = movers._adjclose_map(rows)
    assert m["13010"]["adjclose"] == pytest.approx(150.0)  # AdjC 優先
    assert m["13010"]["turnover"] == pytest.approx(1000000.0)
    assert m["13010"]["volume"] == pytest.approx(5000.0)


def test_adjclose_falls_back_to_close():
    rows = [{"Code": "13010", "C": "90"}]
    m = movers._adjclose_map(rows)
    assert m["13010"]["adjclose"] == pytest.approx(90.0)
    assert m["13010"]["turnover"] is None


def test_adjclose_skips_rows_without_price():
    rows = [{"Code": "13010"}, {"Code": "", "AdjC": "100"}]
    assert movers._adjclose_map(rows) == {}


def test_adjclose_skips_unparseable_price():
    rows = [{"Code": "13010", "AdjC": "N/A"}]
    assert movers._adjclose_map(rows) == {}


# ---------- _load 入力検証 ----------
def test_load_rejects_path_traversal():
    # 英数・_- 以外を含む ID は弾く (グロブ/パストラバーサル injection 防止)
    with pytest.raises(ValueError):
        movers._load("../../etc/passwd")
    with pytest.raises(ValueError):
        movers._load("foo/bar")


def test_load_missing_id_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(movers, "SAVED_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        movers._load("movers_99999999_000000")

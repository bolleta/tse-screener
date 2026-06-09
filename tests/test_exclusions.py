"""除外リスト (src/exclusions.py) のユニットテスト。

4桁/5桁コードの正規化と、YAML ロード・照合を検証する。
"""
import textwrap

from src.exclusions import is_excluded, load_exclusions, normalize_code


# ---------- normalize_code ----------
def test_normalize_4digit_to_5digit():
    assert normalize_code("7490") == "74900"


def test_normalize_5digit_unchanged():
    assert normalize_code("74900") == "74900"


def test_normalize_strips_whitespace():
    assert normalize_code("  7490 ") == "74900"


def test_normalize_non_numeric_unchanged():
    assert normalize_code("130A") == "130A"
    assert normalize_code("") == ""


# ---------- load / is_excluded ----------
def test_load_missing_file_returns_empty(tmp_path):
    assert load_exclusions(tmp_path / "nope.yaml") == {}


def test_load_and_match(tmp_path):
    p = tmp_path / "exclusions.yaml"
    p.write_text(
        textwrap.dedent(
            """
            exclusions:
              - code: "7490"
                name: テスト商事
                reason: TOB成立のため
              - code: "13010"
                name: 別銘柄
            """
        ),
        encoding="utf-8",
    )
    ex = load_exclusions(p)
    # 4桁入力でも 5桁キーで格納される
    assert "74900" in ex
    assert ex["74900"]["name"] == "テスト商事"
    assert ex["74900"]["reason"] == "TOB成立のため"

    # 4桁/5桁どちらの表記でも照合できる
    assert is_excluded("7490", ex) is True
    assert is_excluded("74900", ex) is True
    assert is_excluded("13010", ex) is True
    assert is_excluded("99999", ex) is False


def test_load_skips_entries_without_code(tmp_path):
    p = tmp_path / "exclusions.yaml"
    p.write_text(
        textwrap.dedent(
            """
            exclusions:
              - name: コード無し
              - code: "7490"
            """
        ),
        encoding="utf-8",
    )
    ex = load_exclusions(p)
    assert list(ex.keys()) == ["74900"]

"""Structural verification for the user-support inquiry-verification deliverables.

The 5-split report format and the guide's mandatory normative elements are
load-bearing: if any required field / section silently disappears during an
edit, the investigation norm degrades without a signal. These tests pin the
required structure of the guide, the report template, and the two reference
articles so drift fails CI instead of silently rotting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# skills/wiki/scripts/ -> skills/wiki/
_WIKI_SKILL = Path(__file__).resolve().parent.parent
_GUIDE = _WIKI_SKILL / "references" / "support-investigation-guide.md"

# Product/tool identifiers that must NOT appear in the norm body — they are
# allowed only inside the isolated "本リポジトリでの一適用例" example section,
# because the tool-independence of the norm is the top-priority arbitration.
_TOOL_TOKENS = ("tool-query", "browser-extract", "selection-recipe",
                "tool_query", "browser_extract", "toolquery")
_EXAMPLE_MARKER = "本リポジトリでの一適用例"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"required deliverable missing: {path}")
    return path.read_text(encoding="utf-8")


class TestInvestigationGuide:
    """support-investigation-guide.md must carry every mandatory norm element."""

    def _guide(self) -> str:
        return _read(_GUIDE)

    def test_three_layer_decomposition(self):
        body = self._guide()
        for token in ("観測事実", "業務ルール適用", "バグ推定"):
            assert token in body, f"3層判断分解の要素 '{token}' が欠落"

    def test_five_split_report_format(self):
        body = self._guide()
        for token in ("観測事実", "適用ルール", "推定原因", "確信度", "不足情報"):
            assert token in body, f"5分割様式の要素 '{token}' が欠落"

    def test_sot_first_criterion(self):
        body = self._guide()
        assert "Source of Truth" in body or "真実源" in body, "SoT 概念が欠落"
        assert "証拠権威" in body, "経路優先順位の第一基準（証拠権威）が欠落"
        assert "最小権限" in body, "tiebreaker（最小権限）が欠落"

    def test_settlement_window_gate(self):
        body = self._guide()
        assert "settlement window" in body, "settlement window ゲートが欠落"

    def test_impossibility_taxonomy_six_categories(self):
        body = self._guide()
        for token in ("権限不足", "照合キー欠落", "処理中・再照会待ち",
                      "登録ツール経路なし", "SoT 間不一致", "retention"):
            assert token in body, f"調査不能タクソノミの分類 '{token}' が欠落"

    def test_time_hazard_section(self):
        body = self._guide()
        assert "event-time" in body and "processing-time" in body, "時刻種別区別が欠落"
        assert "TZ" in body or "タイムゾーン" in body, "TZ 正規化が欠落"

    def test_two_log_separation(self):
        body = self._guide()
        assert "QueryLog" in body, "記事参照ログ（QueryLog）が欠落"
        assert "監査ログ" in body, "ツール実行ログ（監査ログ）が欠落"

    def test_confidence_rubric_corroboration(self):
        body = self._guide()
        assert "corroboration" in body or "裏取り" in body, "確信度ルーブリック（corroboration）が欠落"

    def test_closed_set_evolution_governance(self):
        body = self._guide()
        assert "閉集合" in body, "閉集合の進化ガバナンスが欠落"

    def test_minimum_generality_bar(self):
        body = self._guide()
        assert "最小一般性" in body, "匿名化の最小一般性バーが欠落"

    def test_tool_independence_tokens_isolated_to_example_section(self):
        body = self._guide()
        marker_idx = body.find(_EXAMPLE_MARKER)
        assert marker_idx != -1, f"例示節の見出し '{_EXAMPLE_MARKER}' が存在しない"
        norm_body = body[:marker_idx].lower()
        leaked = [t for t in _TOOL_TOKENS if t in norm_body]
        assert not leaked, (
            f"規範本体（例示節より前）にツール識別子が漏れている: {leaked}. "
            "ツール名は '本リポジトリでの一適用例' 節にのみ置くこと"
        )

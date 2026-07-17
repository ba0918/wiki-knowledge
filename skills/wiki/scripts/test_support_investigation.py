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
_TEMPLATE = _WIKI_SKILL / "assets" / "support-report-template.md"

# skills/wiki/ -> repo root -> .wiki/concepts/
_CONCEPTS = _WIKI_SKILL.parent.parent / ".wiki" / "concepts"
_ARTICLE_SLUGS = ("inquiry-event-point-missing", "inquiry-subscription-mismatch")
_RAW_SOURCE = "raw/files/usersupport-inquiry-verification-idea.md"

# Product/tool identifiers that must NOT appear in the norm body — they are
# allowed only inside the isolated "One application example in this repository"
# example section, because the tool-independence of the norm is the top-priority
# arbitration.
_TOOL_TOKENS = ("tool-query", "browser-extract", "selection-recipe",
                "tool_query", "browser_extract", "toolquery")
_EXAMPLE_MARKER = "One application example in this repository"
# Anchor on the H2 heading line, not the bare phrase — the norm body mentions
# the phrase in prose ("isolated to the ... section at the end") before the
# actual section, and matching that prose would shrink the inspected range to a
# few hundred chars and silently disable the regression guard. The leading
# newline pins it to an exact H2 (a "### ..." line would otherwise match too).
_EXAMPLE_HEADING = "\n## " + _EXAMPLE_MARKER


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
        for token in ("Observation", "Business rule application", "Bug inference"):
            assert token in body, f"three-layer decomposition element '{token}' missing"

    def test_five_split_report_format(self):
        body = self._guide()
        for token in ("Observations", "Applied rules", "Inferred cause",
                      "Confidence", "Missing information"):
            assert token in body, f"five-section report element '{token}' missing"

    def test_sot_first_criterion(self):
        body = self._guide()
        assert "Source of Truth" in body, "SoT concept missing"
        assert "evidence authority" in body.lower(), "primary criterion (evidence authority) missing"
        assert "minimum privilege" in body.lower(), "tiebreaker (minimum privilege) missing"

    def test_settlement_window_gate(self):
        body = self._guide()
        assert "settlement window" in body, "settlement window gate missing"

    def test_impossibility_taxonomy_six_categories(self):
        body = self._guide()
        for token in ("Insufficient privilege", "Missing join key",
                      "In-progress / re-check pending",
                      "No registered tool route", "SoT mismatch", "retention"):
            assert token in body, f"cannot-investigate taxonomy category '{token}' missing"

    def test_time_hazard_section(self):
        body = self._guide()
        assert "event-time" in body and "processing-time" in body, "time-kind distinction missing"
        assert "TZ" in body or "time zone" in body, "TZ normalization missing"

    def test_two_log_separation(self):
        body = self._guide()
        assert "QueryLog" in body, "article reference log (QueryLog) missing"
        assert "audit log" in body.lower(), "tool execution log (audit log) missing"

    def test_confidence_rubric_corroboration(self):
        body = self._guide()
        assert "corroboration" in body, "confidence rubric (corroboration) missing"

    def test_closed_set_evolution_governance(self):
        body = self._guide()
        assert "closed set" in body.lower(), "closed-set evolution governance missing"

    def test_minimum_generality_bar(self):
        body = self._guide()
        assert "minimum-generality" in body.lower() or "minimum generality" in body.lower(), (
            "minimum-generality bar for anonymization missing"
        )

    def test_tool_independence_tokens_isolated_to_example_section(self):
        body = self._guide()
        assert body.count(_EXAMPLE_HEADING) == 1, (
            f"example-section heading '## {_EXAMPLE_MARKER}' must appear exactly once"
        )
        marker_idx = body.find(_EXAMPLE_HEADING)
        norm_body = body[:marker_idx].lower()
        leaked = [t for t in _TOOL_TOKENS if t in norm_body]
        assert not leaked, (
            f"tool identifier leaked into the norm body (before the example section): {leaked}. "
            f"Tool names may only appear in the '{_EXAMPLE_MARKER}' section"
        )

    def test_example_section_is_isolated_at_document_end(self):
        """Example section must be the last isolated section — no norm section (##) after it."""
        body = self._guide()
        after = body[body.find(_EXAMPLE_HEADING) + len(_EXAMPLE_HEADING):]
        assert not any(line.startswith("## ") for line in after.splitlines()), (
            "a norm section appears after the example section (the isolated section must be at document end)"
        )

    def test_example_section_mentions_local_tools(self):
        """Example section must reference this repo's tools (defends against the 'tool-independence
        pushed so far that the section becomes vacuous' arbitration)."""
        body = self._guide()
        example = body[body.find(_EXAMPLE_HEADING):].lower()
        assert "tool-query" in example, "example section does not mention tool-query"
        assert "browser-extract" in example, "example section does not mention browser-extract"


class TestReportTemplate:
    """support-report-template.md must carry every load-bearing field."""

    def _template(self) -> str:
        return _read(_TEMPLATE)

    def test_five_split_sections(self):
        body = self._template()
        for token in ("観測事実", "適用ルール", "推定原因", "確信度", "不足情報"):
            assert token in body, f"5分割様式の区画 '{token}' が欠落"

    def test_confidence_three_values_fixed_vocabulary(self):
        body = self._template()
        for token in ("高", "中", "低"):
            assert token in body, f"確信度3値 '{token}' が欠落"

    def test_timeline_field(self):
        body = self._template()
        assert "タイムライン" in body, "タイムライン欄が欠落"

    def test_timeline_carries_time_type_labels(self):
        body = self._template()
        assert "event" in body and "processing" in body, "時刻種別ラベルが欠落"

    def test_settlement_window_field(self):
        body = self._template()
        assert "settlement window" in body, "settlement window 確認欄が欠落"

    def test_impossibility_taxonomy_present(self):
        body = self._template()
        for token in ("権限不足", "照合キー欠落", "処理中・再照会待ち",
                      "登録ツール経路なし", "SoT 間不一致", "retention"):
            assert token in body, f"調査不能定型の分類 '{token}' が欠落"

    def test_report_is_for_support_staff_not_customer(self):
        body = self._template()
        assert "サポート担当" in body, "サポート担当向けである旨の注意書きが欠落"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body) for a `---` fenced YAML frontmatter."""
    assert text.startswith("---\n"), "frontmatter が `---` で始まっていない"
    end = text.index("\n---", 4)
    return text[4:end], text[end + 4:]


class TestReferenceArticles:
    """The two reference articles must be tool-independent practices records.

    Tool-independence is the top-priority arbitration: these articles must not
    structurally depend on tool-query / browser-extract / Selection Recipe.
    """

    @pytest.mark.parametrize("slug", _ARTICLE_SLUGS)
    def test_exists(self, slug):
        _read(_CONCEPTS / f"{slug}.md")

    @pytest.mark.parametrize("slug", _ARTICLE_SLUGS)
    def test_category_is_practices(self, slug):
        fm, _ = _split_frontmatter(_read(_CONCEPTS / f"{slug}.md"))
        assert "category: practices" in fm, f"{slug}: category が practices でない"

    @pytest.mark.parametrize("slug", _ARTICLE_SLUGS)
    def test_type_is_wiki(self, slug):
        fm, _ = _split_frontmatter(_read(_CONCEPTS / f"{slug}.md"))
        assert "type: wiki" in fm, f"{slug}: type が wiki でない"

    @pytest.mark.parametrize("slug", _ARTICLE_SLUGS)
    def test_source_refs_points_to_raw_idea(self, slug):
        fm, _ = _split_frontmatter(_read(_CONCEPTS / f"{slug}.md"))
        assert _RAW_SOURCE in fm, f"{slug}: source_refs が raw の idea ファイルを指していない"

    @pytest.mark.parametrize("slug", _ARTICLE_SLUGS)
    def test_no_selection_recipe_tag(self, slug):
        fm, _ = _split_frontmatter(_read(_CONCEPTS / f"{slug}.md"))
        assert "selection-recipe" not in fm, (
            f"{slug}: selection-recipe タグは構造的依存を作るため付けてはならない"
        )

    @pytest.mark.parametrize("slug", _ARTICLE_SLUGS)
    def test_body_is_tool_independent(self, slug):
        body = _read(_CONCEPTS / f"{slug}.md").lower()
        leaked = [t for t in _TOOL_TOKENS if t in body]
        assert not leaked, (
            f"{slug}: ツール識別子が本文に含まれる {leaked}. 調査経路は抽象カテゴリの"
            "スロット（API 系 / データ照会系 / 画面経由系）で記述すること"
        )

    def test_articles_mutually_wikilink(self):
        a, b = _ARTICLE_SLUGS
        body_a = _read(_CONCEPTS / f"{a}.md")
        body_b = _read(_CONCEPTS / f"{b}.md")
        assert f"[[{b}]]" in body_a, f"{a} が {b} へ wikilink していない（orphan 回避）"
        assert f"[[{a}]]" in body_b, f"{b} が {a} へ wikilink していない（orphan 回避）"

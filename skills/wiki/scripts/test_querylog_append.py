"""Tests for querylog_append.py — QueryLog エントリの組み立てと追記."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from querylog_append import (
    REQUIRED_FIELDS,
    append_jsonl,
    build_entry,
    entry_id_from_iso,
    extract_cited,
    main,
    validate_entry,
)


NOW = "2026-07-07T12:34:56Z"


def _entry(**overrides):
    base = build_entry(
        question="Trust Score はどう計算される？",
        consulted=["concepts/trust-score.md"],
        answer_text="[[trust-score]] で計算される。",
        gap_topics=[],
        promoted=False,
        promoted_to=None,
        now=NOW,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# extract_cited
# ---------------------------------------------------------------------------

class TestExtractCited:
    def test_extracts_and_converts_to_paths(self):
        text = "答えは [[trust-score]] と [[querylog]] を参照。"
        assert extract_cited(text) == [
            "concepts/trust-score.md",
            "concepts/querylog.md",
        ]

    def test_dedup_preserves_first_occurrence_order(self):
        text = "[[b-article]] then [[a-article]] then [[b-article]]"
        assert extract_cited(text) == ["concepts/b-article.md", "concepts/a-article.md"]

    def test_ignores_non_slug_wikilinks(self):
        assert extract_cited("[[Not A Slug]] [[UPPER]]") == []

    def test_rendered_github_form_still_matches(self):
        text = "[[trust-score]] ([↗](trust-score.md))"
        assert extract_cited(text) == ["concepts/trust-score.md"]

    def test_empty_text(self):
        assert extract_cited("") == []


# ---------------------------------------------------------------------------
# entry_id_from_iso
# ---------------------------------------------------------------------------

class TestEntryId:
    def test_basic(self):
        assert entry_id_from_iso("2026-07-07T12:34:56Z") == "q_20260707T123456"

    def test_subsecond_precision_is_dropped(self):
        assert entry_id_from_iso("2026-07-07T12:34:56.789Z") == "q_20260707T123456"

    def test_invalid_iso_raises(self):
        with pytest.raises(ValueError):
            entry_id_from_iso("not-a-timestamp")


# ---------------------------------------------------------------------------
# build_entry
# ---------------------------------------------------------------------------

class TestBuildEntry:
    def test_happy_path(self):
        e = _entry()
        assert e["id"] == "q_20260707T123456"
        assert e["timestamp"] == NOW
        assert e["question"] == "Trust Score はどう計算される？"
        assert e["sources_consulted"] == ["concepts/trust-score.md"]
        assert e["sources_cited"] == ["concepts/trust-score.md"]
        assert e["gap_noted"] is False
        assert e["gap_topics"] == []
        assert e["promoted"] is False
        assert e["promoted_to"] is None

    def test_consulted_filters_non_concepts(self):
        e = build_entry(
            question="q",
            consulted=["concepts/a.md", "index.md", "outputs/queries/x.md", "concepts/b.md"],
            answer_text="",
            gap_topics=[],
            promoted=False,
            promoted_to=None,
            now=NOW,
        )
        assert e["sources_consulted"] == ["concepts/a.md", "concepts/b.md"]

    def test_gap_noted_derived_from_topics(self):
        e = _entry()
        assert e["gap_noted"] is False
        e2 = build_entry(
            question="q",
            consulted=[],
            answer_text="",
            gap_topics=["RAG architecture"],
            promoted=False,
            promoted_to=None,
            now=NOW,
        )
        assert e2["gap_noted"] is True
        assert e2["gap_topics"] == ["RAG architecture"]


# ---------------------------------------------------------------------------
# validate_entry
# ---------------------------------------------------------------------------

class TestValidateEntry:
    def test_valid_entry_has_no_errors(self):
        assert validate_entry(_entry()) == []

    def test_empty_question_is_error(self):
        assert validate_entry(_entry(question="")) != []

    def test_bad_id_pattern_is_error(self):
        assert validate_entry(_entry(id="q_bad")) != []

    def test_promoted_true_requires_promoted_to(self):
        assert validate_entry(_entry(promoted=True, promoted_to=None)) != []

    def test_promoted_false_requires_null_promoted_to(self):
        assert validate_entry(_entry(promoted=False, promoted_to="concepts/x.md")) != []

    def test_missing_field_is_error(self):
        e = _entry()
        del e["gap_topics"]
        assert validate_entry(e) != []

    def test_required_fields_match_schema_of_record(self):
        """REQUIRED_FIELDS と .wiki/schema/querylog-schema.json の required の同期を機械検証する。"""
        schema_path = (
            Path(__file__).resolve().parents[3]
            / ".wiki"
            / "schema"
            / "querylog-schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert set(REQUIRED_FIELDS) == set(schema["required"])
        # id パターンも schema-of-record と一致していること
        entry = _entry()
        assert re.fullmatch(schema["properties"]["id"]["pattern"].strip("^$"), entry["id"])


# ---------------------------------------------------------------------------
# append_jsonl
# ---------------------------------------------------------------------------

class TestAppendJsonl:
    def test_appends_one_json_line(self, tmp_path):
        path = tmp_path / "outputs" / "querylog.jsonl"
        append_jsonl(path, _entry())
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["id"] == "q_20260707T123456"

    def test_appends_after_existing_lines(self, tmp_path):
        path = tmp_path / "querylog.jsonl"
        append_jsonl(path, _entry())
        append_jsonl(path, _entry(id="q_20260707T123457"))
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_question_is_not_ascii_escaped(self, tmp_path):
        path = tmp_path / "querylog.jsonl"
        append_jsonl(path, _entry())
        assert "Trust Score はどう計算される？" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------

class TestMain:
    def _run(self, tmp_path, capsys, extra=None):
        answer = tmp_path / "answer.md"
        answer.write_text("[[trust-score]] を参照。", encoding="utf-8")
        argv = [
            "--wiki-root", str(tmp_path),
            "--question", "Trust Score はどう計算される？",
            "--consulted", "concepts/trust-score.md",
            "--answer-file", str(answer),
            "--now", NOW,
        ] + (extra or [])
        return main(argv)

    def test_happy_path_appends_and_exits_0(self, tmp_path, capsys):
        assert self._run(tmp_path, capsys) == 0
        logfile = tmp_path / "outputs" / "querylog.jsonl"
        entry = json.loads(logfile.read_text(encoding="utf-8").splitlines()[0])
        assert entry["id"] == "q_20260707T123456"
        assert entry["sources_cited"] == ["concepts/trust-score.md"]
        out = capsys.readouterr().out
        assert "q_20260707T123456" in out

    def test_gap_topics_and_promote(self, tmp_path, capsys):
        rc = self._run(
            tmp_path,
            capsys,
            extra=[
                "--gap-topics", "RAG architecture", "vector search",
                "--promoted", "--promoted-to", "concepts/new-article.md",
            ],
        )
        assert rc == 0
        entry = json.loads(
            (tmp_path / "outputs" / "querylog.jsonl").read_text(encoding="utf-8")
        )
        assert entry["gap_noted"] is True
        assert entry["gap_topics"] == ["RAG architecture", "vector search"]
        assert entry["promoted"] is True
        assert entry["promoted_to"] == "concepts/new-article.md"

    def test_validation_failure_exits_1_without_append(self, tmp_path, capsys):
        answer = tmp_path / "answer.md"
        answer.write_text("x", encoding="utf-8")
        rc = main([
            "--wiki-root", str(tmp_path),
            "--question", "q",
            "--answer-file", str(answer),
            "--promoted",  # --promoted-to 欠落 → 整合性エラー
            "--now", NOW,
        ])
        assert rc == 1
        assert not (tmp_path / "outputs" / "querylog.jsonl").exists()

    def test_json_format(self, tmp_path, capsys):
        assert self._run(tmp_path, capsys, extra=["--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["id"] == "q_20260707T123456"

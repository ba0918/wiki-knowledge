"""Tests for log_append.py — log.md 操作ログの定型追記."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from log_append import append_line, format_entry, main


DATE = "2026-07-07"


def _wiki(tmp_path: Path) -> Path:
    log = tmp_path / "log.md"
    log.write_text(
        "# Wiki Operation Log\n\n## [2026-04-05] init | Wiki initialized\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# format_entry — SKILL.md の各テンプレート
# ---------------------------------------------------------------------------

class TestFormatEntry:
    def test_ingest(self):
        line = format_entry("ingest", DATE, {"slug": "trust-score-v2", "source_kind": "article"})
        assert line == "## [2026-07-07] ingest | trust-score-v2 (article)"

    def test_ingest_repo_kind(self):
        line = format_entry("ingest", DATE, {"slug": "myrepo", "source_kind": "repo @ abc1234"})
        assert line == "## [2026-07-07] ingest | myrepo (repo @ abc1234)"

    def test_compile_singular_source(self):
        line = format_entry(
            "compile", DATE, {"title": "Trust Score", "word_count": 320, "sources": 1}
        )
        assert line == "## [2026-07-07] compile | Trust Score (320 words, 1 source)"

    def test_compile_plural_sources(self):
        line = format_entry(
            "compile", DATE, {"title": "Trust Score", "word_count": 500, "sources": 2}
        )
        assert line == "## [2026-07-07] compile | Trust Score (500 words, 2 sources)"

    def test_promote(self):
        line = format_entry("promote", DATE, {"title": "RAG アーキテクチャ"})
        assert line == "## [2026-07-07] promote | RAG アーキテクチャ (from query)"

    def test_query(self):
        line = format_entry("query", DATE, {"summary": "Trust Score の計算方法"})
        assert line == "## [2026-07-07] query | Trust Score の計算方法"

    def test_lint(self):
        line = format_entry("lint", DATE, {"errors": 0, "warnings": 3, "info": 1})
        assert line == "## [2026-07-07] lint | 0 errors, 3 warnings, 1 info"

    def test_note_suffix(self):
        line = format_entry(
            "compile",
            DATE,
            {"title": "Trust Score", "word_count": 500, "sources": 2},
            note="updated: v2 絶対スケール",
        )
        assert line == (
            "## [2026-07-07] compile | Trust Score (500 words, 2 sources)"
            " — updated: v2 絶対スケール"
        )

    def test_unknown_op_raises(self):
        with pytest.raises(ValueError):
            format_entry("unknown", DATE, {})


# ---------------------------------------------------------------------------
# append_line
# ---------------------------------------------------------------------------

class TestAppendLine:
    def test_appends_preserving_existing_content(self, tmp_path):
        wiki = _wiki(tmp_path)
        append_line(wiki / "log.md", "## [2026-07-07] query | test")
        text = (wiki / "log.md").read_text(encoding="utf-8")
        assert text.startswith("# Wiki Operation Log")
        assert text.endswith("## [2026-07-07] query | test\n")

    def test_adds_newline_when_file_lacks_trailing_newline(self, tmp_path):
        log = tmp_path / "log.md"
        log.write_text("# Log\n## [2026-01-01] init | x", encoding="utf-8")
        append_line(log, "## [2026-07-07] query | test")
        lines = log.read_text(encoding="utf-8").splitlines()
        assert lines[-2] == "## [2026-01-01] init | x"
        assert lines[-1] == "## [2026-07-07] query | test"

    def test_missing_log_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            append_line(tmp_path / "log.md", "## x")


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------

class TestMain:
    def test_ingest_subcommand(self, tmp_path, capsys):
        wiki = _wiki(tmp_path)
        rc = main([
            "ingest", "--wiki-root", str(wiki), "--date", DATE,
            "--slug", "foo-bar", "--source-kind", "url",
        ])
        assert rc == 0
        assert "## [2026-07-07] ingest | foo-bar (url)" in (wiki / "log.md").read_text(
            encoding="utf-8"
        )
        assert "ingest | foo-bar (url)" in capsys.readouterr().out

    def test_lint_subcommand(self, tmp_path, capsys):
        wiki = _wiki(tmp_path)
        rc = main([
            "lint", "--wiki-root", str(wiki), "--date", DATE,
            "--errors", "2", "--warnings", "0", "--info", "5",
        ])
        assert rc == 0
        assert "lint | 2 errors, 0 warnings, 5 info" in (wiki / "log.md").read_text(
            encoding="utf-8"
        )

    def test_default_date_is_today(self, tmp_path, capsys):
        wiki = _wiki(tmp_path)
        rc = main(["query", "--wiki-root", str(wiki), "--summary", "test"])
        assert rc == 0
        today = date.today().isoformat()
        assert f"## [{today}] query | test" in (wiki / "log.md").read_text(encoding="utf-8")

    def test_invalid_date_exits_2(self, tmp_path, capsys):
        wiki = _wiki(tmp_path)
        rc = main([
            "query", "--wiki-root", str(wiki), "--date", "07/07/2026", "--summary", "x",
        ])
        assert rc == 2

    def test_missing_log_md_exits_1(self, tmp_path, capsys):
        rc = main(["query", "--wiki-root", str(tmp_path), "--summary", "x"])
        assert rc == 1

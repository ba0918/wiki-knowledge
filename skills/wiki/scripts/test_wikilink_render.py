"""Tests for wikilink_render: pure transform + CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from wikilink_render import render_wikilinks, RENDERED_PATTERN  # noqa: E402


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

class TestRenderWikilinks:
    def test_basic_slug(self):
        assert render_wikilinks("see [[foo]] now") == "see [[foo]] ([↗](foo.md)) now"

    def test_idempotent_already_rendered(self):
        text = "see [[foo]] ([↗](foo.md)) now"
        assert render_wikilinks(text) == text

    def test_double_run_stable(self):
        text = "ref [[bar]] here"
        once = render_wikilinks(text)
        twice = render_wikilinks(once)
        assert once == twice

    def test_inline_code_excluded(self):
        text = "use `[[foo]]` literal"
        assert render_wikilinks(text) == text

    def test_fenced_code_excluded(self):
        text = "```\n[[foo]]\n```\n[[bar]] outside"
        out = render_wikilinks(text)
        assert "```\n[[foo]]\n```" in out
        assert "[[bar]] ([↗](bar.md))" in out

    def test_alias_pipe(self):
        text = "see [[foo|表示]] here"
        assert render_wikilinks(text) == "see [[foo|表示]] ([↗](foo.md)) here"

    def test_multiple_same_line(self):
        text = "[[a]] and [[b]] and [[c]]"
        out = render_wikilinks(text)
        assert out == "[[a]] ([↗](a.md)) and [[b]] ([↗](b.md)) and [[c]] ([↗](c.md))"

    def test_dead_link_still_rendered(self):
        # responsibility separation: renderer doesn't check existence
        text = "[[nonexistent-slug]]"
        assert render_wikilinks(text) == "[[nonexistent-slug]] ([↗](nonexistent-slug.md))"

    def test_related_section(self):
        text = "## 関連\n\n- [[foo]]\n- [[bar]]\n"
        out = render_wikilinks(text)
        assert "[[foo]] ([↗](foo.md))" in out
        assert "[[bar]] ([↗](bar.md))" in out

    def test_alias_already_rendered(self):
        text = "[[foo|disp]] ([↗](foo.md))"
        assert render_wikilinks(text) == text

    def test_empty(self):
        assert render_wikilinks("") == ""

    def test_no_wikilinks(self):
        assert render_wikilinks("plain markdown text\n") == "plain markdown text\n"

    @pytest.mark.xfail(reason="Tilde fences are a known limitation inherited from lib/inventory.py")
    def test_tilde_fence_excluded(self):
        text = "~~~\n[[foo]]\n~~~"
        # Ideally unchanged, but current impl will transform it.
        assert render_wikilinks(text) == text

    def test_rendered_pattern_matches(self):
        assert RENDERED_PATTERN.search("[[foo]] ([↗](foo.md))") is not None


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

SCRIPT = _HERE / "wikilink_render.py"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class TestCLI:
    def test_check_clean(self, tmp_path: Path):
        wiki = tmp_path / ".wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        f = concepts / "a.md"
        f.write_text("[[foo]] ([↗](foo.md))\n", encoding="utf-8")
        r = _run("--check", str(f), cwd=tmp_path)
        assert r.returncode == 0, r.stderr

    def test_check_dirty(self, tmp_path: Path):
        wiki = tmp_path / ".wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        f = concepts / "a.md"
        f.write_text("[[foo]]\n", encoding="utf-8")
        r = _run("--check", str(f), cwd=tmp_path)
        assert r.returncode == 1

    def test_write_updates_file(self, tmp_path: Path):
        wiki = tmp_path / ".wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        f = concepts / "a.md"
        f.write_text("[[foo]]\n", encoding="utf-8")
        r = _run("--write", str(f), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "([↗](foo.md))" in f.read_text(encoding="utf-8")

    def test_directory_recursive(self, tmp_path: Path):
        wiki = tmp_path / ".wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "a.md").write_text("[[a]]\n", encoding="utf-8")
        (concepts / "b.md").write_text("[[b]]\n", encoding="utf-8")
        r = _run("--write", str(concepts), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "([↗](a.md))" in (concepts / "a.md").read_text(encoding="utf-8")
        assert "([↗](b.md))" in (concepts / "b.md").read_text(encoding="utf-8")

    def test_path_outside_wiki_rejected(self, tmp_path: Path):
        outside = tmp_path / "other"
        outside.mkdir()
        f = outside / "a.md"
        f.write_text("[[foo]]\n", encoding="utf-8")
        r = _run("--check", str(f), cwd=tmp_path)
        assert r.returncode != 0
        assert ".wiki" in (r.stderr + r.stdout)

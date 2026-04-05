#!/usr/bin/env python3
"""Tests for querylog-stats.py."""

import json
import sys
from pathlib import Path

import pytest

# テスト対象モジュールをインポートするためにパスを追加
sys.path.insert(0, str(Path(__file__).parent))

from querylog_stats import (
    compute_stats,
    load_querylog,
    resolve_concepts,
)


def _make_entry(
    *,
    sources_consulted: list[str] | None = None,
    sources_cited: list[str] | None = None,
    gap_noted: bool = False,
    gap_topics: list[str] | None = None,
    promoted: bool = False,
    promoted_to: str | None = None,
) -> dict:
    """テスト用の querylog エントリを生成するヘルパー。"""
    return {
        "id": "q_20260405T120000",
        "timestamp": "2026-04-05T12:00:00+09:00",
        "question": "テスト質問",
        "sources_consulted": sources_consulted or [],
        "sources_cited": sources_cited or [],
        "gap_noted": gap_noted,
        "gap_topics": gap_topics or [],
        "promoted": promoted,
        "promoted_to": promoted_to,
    }


# --- load_querylog ---


class TestLoadQuerylog:
    """querylog.jsonl の読み込みテスト。"""

    def test_normal_entries(self, tmp_path: Path) -> None:
        """正常系: 複数エントリを正しく読み込む。"""
        entries = [
            _make_entry(sources_consulted=["concepts/a.md"]),
            _make_entry(sources_consulted=["concepts/b.md"]),
        ]
        logfile = tmp_path / "querylog.jsonl"
        logfile.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )
        result = load_querylog(logfile)
        assert len(result) == 2

    def test_file_not_found(self, tmp_path: Path) -> None:
        """ファイル不在: 空リストを返す。"""
        logfile = tmp_path / "querylog.jsonl"
        result = load_querylog(logfile)
        assert result == []

    def test_empty_file(self, tmp_path: Path) -> None:
        """空ファイル: 空リストを返す。"""
        logfile = tmp_path / "querylog.jsonl"
        logfile.write_text("", encoding="utf-8")
        result = load_querylog(logfile)
        assert result == []

    def test_invalid_json_skipped(self, tmp_path: Path, capsys) -> None:
        """不正 JSON 行: スキップして stderr に警告。"""
        valid = _make_entry(sources_consulted=["concepts/a.md"])
        logfile = tmp_path / "querylog.jsonl"
        logfile.write_text(
            json.dumps(valid, ensure_ascii=False) + "\n"
            + "NOT VALID JSON\n"
            + json.dumps(valid, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result = load_querylog(logfile)
        assert len(result) == 2
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "warning" in captured.err.lower()


# --- resolve_concepts ---


class TestResolveConcepts:
    """concepts/ のファイル一覧取得テスト。"""

    def test_concepts_found(self, tmp_path: Path) -> None:
        """正常系: concepts/ 内の .md ファイルを返す。"""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "alpha.md").write_text("# Alpha", encoding="utf-8")
        (concepts_dir / "beta.md").write_text("# Beta", encoding="utf-8")
        result = resolve_concepts(concepts_dir)
        assert sorted(result) == ["alpha.md", "beta.md"]

    def test_concepts_dir_missing(self, tmp_path: Path) -> None:
        """concepts/ が存在しない場合: 空リストを返す。"""
        concepts_dir = tmp_path / "concepts"
        result = resolve_concepts(concepts_dir)
        assert result == []

    def test_concepts_dir_empty(self, tmp_path: Path) -> None:
        """concepts/ が空の場合: 空リストを返す。"""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        result = resolve_concepts(concepts_dir)
        assert result == []


# --- compute_stats ---


class TestComputeStats:
    """統計計算の純粋関数テスト。"""

    def test_normal_stats(self) -> None:
        """正常系: 複数エントリの統計が正しいこと。"""
        entries = [
            _make_entry(
                sources_consulted=["concepts/a.md", "concepts/b.md"],
                gap_noted=True,
                gap_topics=["RAG architecture"],
                promoted=True,
                promoted_to="concepts/new.md",
            ),
            _make_entry(
                sources_consulted=["concepts/a.md", "concepts/c.md"],
                gap_noted=True,
                gap_topics=["RAG architecture", "embedding models"],
            ),
            _make_entry(
                sources_consulted=["concepts/a.md"],
                gap_noted=False,
            ),
        ]
        concept_files = ["a.md", "b.md", "c.md", "d.md"]
        stats = compute_stats(entries, concept_files)

        assert stats["total_queries"] == 3
        assert stats["sources"]["total_concepts"] == 4
        assert stats["sources"]["consulted_unique"] == 3  # a, b, c
        assert stats["sources"]["never_consulted"] == ["d.md"]
        assert stats["sources"]["consultation_rate"] == 0.75
        assert stats["gaps"]["queries_with_gaps"] == 2
        assert stats["gaps"]["gap_rate"] == 0.667  # round(2/3, 3)
        assert stats["promotions"]["promoted_count"] == 1
        assert stats["promotions"]["promotion_rate"] == 0.333  # round(1/3, 3)

    def test_empty_entries(self) -> None:
        """空エントリ: total_queries = 0、ゼロ除算なし。"""
        stats = compute_stats([], ["a.md", "b.md"])
        assert stats["total_queries"] == 0
        assert stats["sources"]["total_concepts"] == 2
        assert stats["sources"]["consulted_unique"] == 0
        assert sorted(stats["sources"]["never_consulted"]) == ["a.md", "b.md"]
        assert stats["sources"]["consultation_rate"] == 0.0
        assert stats["gaps"]["queries_with_gaps"] == 0
        assert stats["gaps"]["gap_rate"] == 0.0
        assert stats["gaps"]["top_topics"] == []
        assert stats["promotions"]["promoted_count"] == 0
        assert stats["promotions"]["promotion_rate"] == 0.0

    def test_no_concepts(self) -> None:
        """concepts が空: total_concepts = 0, consultation_rate = 0.0。"""
        entries = [_make_entry(sources_consulted=["concepts/a.md"])]
        stats = compute_stats(entries, [])
        assert stats["sources"]["total_concepts"] == 0
        assert stats["sources"]["consultation_rate"] == 0.0
        assert stats["sources"]["never_consulted"] == []

    def test_never_consulted_detection(self) -> None:
        """一度も参照されていない記事を正しく検出する。"""
        entries = [
            _make_entry(sources_consulted=["concepts/a.md"]),
            _make_entry(sources_consulted=["concepts/a.md", "concepts/b.md"]),
        ]
        concept_files = ["a.md", "b.md", "c.md", "d.md"]
        stats = compute_stats(entries, concept_files)
        assert sorted(stats["sources"]["never_consulted"]) == ["c.md", "d.md"]

    def test_gap_topics_frequency(self) -> None:
        """同一トピックが複数回出現した場合のカウント。"""
        entries = [
            _make_entry(gap_noted=True, gap_topics=["RAG architecture"]),
            _make_entry(gap_noted=True, gap_topics=["RAG architecture", "embedding models"]),
            _make_entry(gap_noted=True, gap_topics=["embedding models"]),
            _make_entry(gap_noted=True, gap_topics=["RAG architecture"]),
        ]
        stats = compute_stats(entries, [])
        topics = stats["gaps"]["top_topics"]
        assert topics[0] == {"topic": "RAG architecture", "count": 3}
        assert topics[1] == {"topic": "embedding models", "count": 2}

    def test_top_topics_sorted_descending(self) -> None:
        """top_topics が count 降順でソートされている。"""
        entries = [
            _make_entry(gap_noted=True, gap_topics=["topic-c"]),
            _make_entry(gap_noted=True, gap_topics=["topic-a", "topic-b"]),
            _make_entry(gap_noted=True, gap_topics=["topic-a", "topic-b"]),
            _make_entry(gap_noted=True, gap_topics=["topic-a"]),
        ]
        stats = compute_stats(entries, [])
        topics = stats["gaps"]["top_topics"]
        counts = [t["count"] for t in topics]
        assert counts == sorted(counts, reverse=True)
        assert topics[0]["topic"] == "topic-a"
        assert topics[0]["count"] == 3
        assert topics[1]["topic"] == "topic-b"
        assert topics[1]["count"] == 2
        assert topics[2]["topic"] == "topic-c"
        assert topics[2]["count"] == 1


# --- 結合テスト: main 関数 ---


class TestMainIntegration:
    """CLI 結合テスト。"""

    def test_main_outputs_json(self, tmp_path: Path) -> None:
        """main() が正しい JSON を stdout に出力する。"""
        from querylog_stats import main as qs_main

        # wiki root 構造を作成
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        concepts_dir = wiki_root / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "alpha.md").write_text("# Alpha", encoding="utf-8")
        outputs_dir = wiki_root / "outputs"
        outputs_dir.mkdir()
        entry = _make_entry(sources_consulted=["concepts/alpha.md"])
        (outputs_dir / "querylog.jsonl").write_text(
            json.dumps(entry, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        import io
        from unittest.mock import patch

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("sys.argv", ["querylog-stats.py", "--wiki-root", str(wiki_root)]):
                qs_main()

        output = json.loads(mock_stdout.getvalue())
        assert output["total_queries"] == 1
        assert output["sources"]["total_concepts"] == 1

    def test_main_file_not_found_exit_0(self, tmp_path: Path) -> None:
        """querylog.jsonl 不在でも exit 0。"""
        from querylog_stats import main as qs_main

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        (wiki_root / "concepts").mkdir()

        import io
        from unittest.mock import patch

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with patch("sys.argv", ["querylog-stats.py", "--wiki-root", str(wiki_root)]):
                qs_main()

        output = json.loads(mock_stdout.getvalue())
        assert output["total_queries"] == 0

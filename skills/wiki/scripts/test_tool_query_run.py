"""CLI 統合テスト — tool_query_run.py.

Step 1 では ``catalog-validate`` subcommand の exit code 契約（0/1/2）と
stdout/stderr 分離を検証する。prepare/approve/execute は後続ステップで追加。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tool_query_run
from lib.service.test_tool_catalog import make_catalog_data, make_entry, write_catalog


# ---------------------------------------------------------------------------
# catalog-validate
# ---------------------------------------------------------------------------


class TestCatalogValidate:
    def test_valid_catalog_exits_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_catalog(tmp_path, make_catalog_data())
        code = tool_query_run.main(["catalog-validate", "--wiki-root", str(tmp_path)])
        assert code == 0
        out = capsys.readouterr().out
        assert "events-db" in out

    def test_schema_violation_exits_1_with_errors_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_catalog(tmp_path, make_catalog_data([make_entry(type="postgres")]))
        code = tool_query_run.main(["catalog-validate", "--wiki-root", str(tmp_path)])
        assert code == 1
        captured = capsys.readouterr()
        assert captured.err != ""

    def test_missing_catalog_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = tool_query_run.main(["catalog-validate", "--wiki-root", str(tmp_path)])
        assert code == 1
        assert capsys.readouterr().err != ""

    def test_format_json_emits_machine_readable_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_catalog(tmp_path, make_catalog_data())
        code = tool_query_run.main(
            ["catalog-validate", "--wiki-root", str(tmp_path), "--format", "json"]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["tools"] == ["events-db"]

    def test_format_json_on_violation_keeps_stdout_parseable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_catalog(tmp_path, make_catalog_data([make_entry(type="postgres")]))
        code = tool_query_run.main(
            ["catalog-validate", "--wiki-root", str(tmp_path), "--format", "json"]
        )
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["errors"] != []


# ---------------------------------------------------------------------------
# 共通 CLI 契約
# ---------------------------------------------------------------------------


class TestCliContract:
    def test_no_subcommand_exits_2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            tool_query_run.main([])
        assert exc.value.code == 2

    def test_unknown_subcommand_exits_2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            tool_query_run.main(["frobnicate"])
        assert exc.value.code == 2

    def test_missing_wiki_root_exits_2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            tool_query_run.main(["catalog-validate"])
        assert exc.value.code == 2

    def test_python_older_than_311_exits_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """setlimit() が使えない環境では防御層を黙って欠落させず起動を拒否する。"""
        monkeypatch.setattr(
            tool_query_run.sys, "version_info", (3, 10, 12, "final", 0)
        )
        write_catalog(tmp_path, make_catalog_data())
        code = tool_query_run.main(["catalog-validate", "--wiki-root", str(tmp_path)])
        assert code == 2
        assert "3.11" in capsys.readouterr().err

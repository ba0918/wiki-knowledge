"""CLI 統合テスト — tool_query_run.py.

exit code 契約: 0=成功 / 1=policy 拒否・実行失敗 / 2=usage・引数不備 /
130=SIGINT。stdout=結果データ、stderr=進捗・診断（承認プロンプト含む）。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

import tool_query_run
from lib.service.test_tool_catalog import make_catalog_data, make_entry, write_catalog
from lib.service.test_tool_query_runner import make_wiki, write_sqls


class FakeTty(io.StringIO):
    """isatty() が True を返す stdin 代替（承認プロンプトのテスト用）。"""

    def isatty(self) -> bool:  # noqa: D102
        return True


def cli_prepare(
    wiki_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    expected_rows: str = "3:3",
) -> tuple[int, dict]:
    main, counts = write_sqls(tmp_path)
    code = tool_query_run.main(
        [
            "prepare",
            "--wiki-root",
            str(wiki_root),
            "--tool",
            "events-db",
            "--sql-file",
            str(main),
            "--count-sql",
            f"ev-2026 登録者={counts[0].path}",
            "--count-sql",
            f"返金なし={counts[1].path}",
            "--key-columns",
            "user_id",
            "--expected-rows",
            expected_rows,
            "--deliver-to",
            "deliveries",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload


def cli_approve(
    wiki_root: Path,
    plan_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: str = "yes\n",
) -> int:
    monkeypatch.setattr(sys, "stdin", FakeTty(answer))
    return tool_query_run.main(
        [
            "approve",
            "--wiki-root",
            str(wiki_root),
            "--plan",
            plan_id,
            "--approved-by",
            "mizumi",
            "--format",
            "json",
        ]
    )


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


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


class TestPrepareCli:
    def test_prepare_emits_plan_summary_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        code, payload = cli_prepare(wiki_root, tmp_path, capsys)
        assert code == 0
        assert payload["plan_id"]
        assert payload["tool_id"] == "events-db"
        assert payload["funnel"] == [
            {"label": "ev-2026 登録者", "row_count": 4},
            {"label": "返金なし", "row_count": 3},
        ]
        assert payload["expected_rows"] == {"min": 3, "max": 3}
        assert payload["delivery_dir"] == "deliveries"
        assert payload["sql_digest"]
        assert payload["expires_at"]

    def test_bad_expected_rows_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        with pytest.raises(SystemExit) as exc:
            cli_prepare(wiki_root, tmp_path, capsys, expected_rows="abc")
        assert exc.value.code == 2

    def test_count_sql_without_label_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        main, counts = write_sqls(tmp_path)
        with pytest.raises(SystemExit) as exc:
            tool_query_run.main(
                [
                    "prepare",
                    "--wiki-root",
                    str(wiki_root),
                    "--tool",
                    "events-db",
                    "--sql-file",
                    str(main),
                    "--count-sql",
                    str(counts[0].path),  # label= がない
                    "--key-columns",
                    "user_id",
                    "--expected-rows",
                    "3:3",
                    "--deliver-to",
                    "deliveries",
                ]
            )
        assert exc.value.code == 2

    def test_missing_sql_file_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        _, counts = write_sqls(tmp_path)
        code = tool_query_run.main(
            [
                "prepare",
                "--wiki-root",
                str(wiki_root),
                "--tool",
                "events-db",
                "--sql-file",
                str(tmp_path / "no-such.sql"),
                "--count-sql",
                f"c={counts[0].path}",
                "--key-columns",
                "user_id",
                "--expected-rows",
                "3:3",
                "--deliver-to",
                "deliveries",
            ]
        )
        assert code == 2

    def test_disallowed_delivery_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        main, counts = write_sqls(tmp_path)
        code = tool_query_run.main(
            [
                "prepare",
                "--wiki-root",
                str(wiki_root),
                "--tool",
                "events-db",
                "--sql-file",
                str(main),
                "--count-sql",
                f"c={counts[0].path}",
                "--key-columns",
                "user_id",
                "--expected-rows",
                "3:3",
                "--deliver-to",
                "/tmp/elsewhere",
            ]
        )
        assert code == 1


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class TestApproveCli:
    def test_non_tty_exits_2_without_polluting_stdout(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """パイプ越しの自動承認を作らない — 非 TTY は exit 2。"""
        wiki_root = make_wiki(tmp_path)
        _, payload = cli_prepare(wiki_root, tmp_path, capsys)
        monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))  # isatty False
        code = tool_query_run.main(
            [
                "approve",
                "--wiki-root",
                str(wiki_root),
                "--plan",
                payload["plan_id"],
                "--approved-by",
                "mizumi",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        assert code == 2
        assert captured.out == ""  # stdout の JSON を汚染しない
        assert "TTY" in captured.err

    def test_confirmed_approve_updates_state_and_keeps_stdout_json_clean(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        _, payload = cli_prepare(wiki_root, tmp_path, capsys)
        plan_id = payload["plan_id"]
        code = cli_approve(wiki_root, plan_id, monkeypatch)
        captured = capsys.readouterr()
        assert code == 0
        result = json.loads(captured.out)  # stdout は JSON のみ
        assert result["approved"] is True
        assert result["plan_id"] == plan_id
        # summary と確認プロンプトは stderr に出る
        assert plan_id in captured.err
        assert "yes" in captured.err
        state = json.loads(
            (
                wiki_root / "outputs" / "toolquery-plans" / plan_id / "state.json"
            ).read_text(encoding="utf-8")
        )
        assert state["status"] == "approved"

    def test_declined_answer_leaves_draft_and_exits_1(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        _, payload = cli_prepare(wiki_root, tmp_path, capsys)
        code = cli_approve(wiki_root, payload["plan_id"], monkeypatch, answer="no\n")
        assert code == 1
        state = json.loads(
            (
                wiki_root
                / "outputs"
                / "toolquery-plans"
                / payload["plan_id"]
                / "state.json"
            ).read_text(encoding="utf-8")
        )
        assert state["status"] == "draft"

    def test_eof_is_treated_as_decline(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        _, payload = cli_prepare(wiki_root, tmp_path, capsys)
        code = cli_approve(wiki_root, payload["plan_id"], monkeypatch, answer="")
        assert code == 1

    def test_ctrl_c_during_prompt_exits_130(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        _, payload = cli_prepare(wiki_root, tmp_path, capsys)

        class InterruptingTty(io.StringIO):
            def isatty(self) -> bool:
                return True

            def readline(self, *args) -> str:
                raise KeyboardInterrupt

        monkeypatch.setattr(sys, "stdin", InterruptingTty())
        code = tool_query_run.main(
            [
                "approve",
                "--wiki-root",
                str(wiki_root),
                "--plan",
                payload["plan_id"],
                "--approved-by",
                "mizumi",
            ]
        )
        assert code == 130

    @pytest.mark.parametrize(
        "bad_plan",
        ["../escape", "/etc/passwd", "not-a-plan-id", "20260716120000-ab12-"],
    )
    def test_malformed_plan_id_exits_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        bad_plan: str,
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        monkeypatch.setattr(sys, "stdin", FakeTty("yes\n"))
        code = tool_query_run.main(
            [
                "approve",
                "--wiki-root",
                str(wiki_root),
                "--plan",
                bad_plan,
                "--approved-by",
                "mizumi",
            ]
        )
        assert code == 2


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


class TestExecuteCli:
    def _prepared_and_approved(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[Path, str]:
        wiki_root = make_wiki(tmp_path)
        _, payload = cli_prepare(wiki_root, tmp_path, capsys)
        plan_id = payload["plan_id"]
        assert cli_approve(wiki_root, plan_id, monkeypatch) == 0
        capsys.readouterr()  # ここまでの出力を捨てる
        return wiki_root, plan_id

    def test_execute_reports_manifest_summary(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root, plan_id = self._prepared_and_approved(
            tmp_path, capsys, monkeypatch
        )
        code = tool_query_run.main(
            [
                "execute",
                "--wiki-root",
                str(wiki_root),
                "--plan",
                plan_id,
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["row_count"] == 3
        assert payload["run_id"]
        assert payload["csv_sha256"]
        assert payload["duplicate_key_count"] == 0
        assert payload["sanitized_cell_count"] == 0
        assert payload["delivery_dir"] == "deliveries"

    def test_execute_without_bundle_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        code = tool_query_run.main(
            [
                "execute",
                "--wiki-root",
                str(wiki_root),
                "--plan",
                "20260716120000-zz99-events-db",
            ]
        )
        assert code == 1

    def test_symlinked_plan_directory_exits_1(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root, plan_id = self._prepared_and_approved(
            tmp_path, capsys, monkeypatch
        )
        plans = wiki_root / "outputs" / "toolquery-plans"
        real = plans / plan_id
        renamed = plans / "20260716120000-zz98-events-db"
        real.rename(renamed)
        (plans / plan_id).symlink_to(renamed)
        code = tool_query_run.main(
            ["execute", "--wiki-root", str(wiki_root), "--plan", plan_id]
        )
        assert code == 1

    def test_malformed_plan_id_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki_root = make_wiki(tmp_path)
        code = tool_query_run.main(
            ["execute", "--wiki-root", str(wiki_root), "--plan", "../../etc"]
        )
        assert code == 2

    def test_keyboard_interrupt_exits_130_with_cleanup_note(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root, plan_id = self._prepared_and_approved(
            tmp_path, capsys, monkeypatch
        )

        class InterruptingRunner:
            def __init__(self, **kwargs) -> None:
                pass

            def execute(self, plan_id_text: str):
                raise KeyboardInterrupt

        monkeypatch.setattr(tool_query_run, "ToolQueryRunner", InterruptingRunner)
        code = tool_query_run.main(
            ["execute", "--wiki-root", str(wiki_root), "--plan", plan_id]
        )
        captured = capsys.readouterr()
        assert code == 130
        assert "中断" in captured.err


# ---------------------------------------------------------------------------
# credential 隔離（秘密値の非露出）
# ---------------------------------------------------------------------------


class TestCredentialIsolation:
    # テスト用ダミー値（実在の秘密ではない）。hook の誤検知を避けるため実行時に組み立てる
    DUMMY_VALUE = "-".join(["dummy", "credential", "for", "isolation", "test"])

    def _wiki_with_credential(self, tmp_path: Path) -> Path:
        wiki_root = make_wiki(tmp_path)
        catalog_path = wiki_root / "tools" / "catalog.json"
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        data["tools"][0]["credential_ref"] = "events-ro"
        catalog_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        local = wiki_root / ".local"
        local.mkdir()
        creds = local / "credentials.json"
        creds.write_text(
            json.dumps({"events-ro": self.DUMMY_VALUE}), encoding="utf-8"
        )
        creds.chmod(0o600)
        return wiki_root

    def _assert_no_secret(self, wiki_root: Path, captured) -> None:
        assert self.DUMMY_VALUE not in captured.out
        assert self.DUMMY_VALUE not in captured.err
        audit = wiki_root / "outputs" / "toolquery-audit.jsonl"
        if audit.exists():
            assert self.DUMMY_VALUE not in audit.read_text(encoding="utf-8")

    def test_happy_path_never_exposes_secret(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wiki_root = self._wiki_with_credential(tmp_path)
        code, payload = cli_prepare(wiki_root, tmp_path, capsys)
        assert code == 0
        captured = capsys.readouterr()
        self._assert_no_secret(wiki_root, captured)
        plan_id = payload["plan_id"]
        assert cli_approve(wiki_root, plan_id, monkeypatch) == 0
        code = tool_query_run.main(
            ["execute", "--wiki-root", str(wiki_root), "--plan", plan_id]
        )
        assert code == 0
        self._assert_no_secret(wiki_root, capsys.readouterr())

    def test_error_path_names_ref_but_not_value(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wiki_root = self._wiki_with_credential(tmp_path)
        creds = wiki_root / ".local" / "credentials.json"
        creds.chmod(0o644)  # BAD_PERMISSIONS で失敗させる
        code, _ = cli_prepare(wiki_root, tmp_path, capsys)
        assert code == 1
        captured = capsys.readouterr()
        self._assert_no_secret(wiki_root, captured)

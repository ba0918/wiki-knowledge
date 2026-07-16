"""Tests for tool_delivery.py — staging → no-clobber atomic publish と CSV 無害化.

POSIX の rename は既存**空 directory** を黙って置換し得るため、publish は
「親 dir lock 取得 → 最終名の不存在確認 → rename」を同一 lock 区間で行う。
CSV 無害化は OWASP CSV injection 準拠（先頭 ``=+-@`` + 先頭 tab/CR/空白の
あとに式文字が続くケース）。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.file_lock import FakeFileLock, RealFileLock
from lib.service.tool_delivery import (
    DeliveryError,
    cell_size_bytes,
    cleanup_staging,
    create_staging_dir,
    encode_csv_row,
    publish_run_dir,
    sanitize_cell,
)


RUN_ID = "20260716140000-cd34-events-db"


# ---------------------------------------------------------------------------
# CSV 無害化（純粋）
# ---------------------------------------------------------------------------


class TestSanitizeCell:
    @pytest.mark.parametrize(
        "dangerous",
        [
            "=cmd()",
            "+1+2",
            "-1-2",
            "@import",
            "\t=1",
            "\r+1",
            " =1",
            "   @x",
            " \t-2",
        ],
    )
    def test_formula_cells_are_escaped(self, dangerous: str) -> None:
        sanitized, changed = sanitize_cell(dangerous)
        assert changed is True
        assert sanitized.startswith("'")
        assert sanitized == "'" + dangerous

    @pytest.mark.parametrize(
        "safe",
        ["hello", "123", "", "a=b", "  hello", "\tplain", "名前"],
    )
    def test_plain_cells_are_untouched(self, safe: str) -> None:
        sanitized, changed = sanitize_cell(safe)
        assert changed is False
        assert sanitized == safe

    def test_non_str_values_are_stringified_without_escape(self) -> None:
        assert sanitize_cell(123) == ("123", False)
        assert sanitize_cell(1.5) == ("1.5", False)
        assert sanitize_cell(None) == ("", False)

    def test_negative_number_as_int_is_not_escaped(self) -> None:
        """数値型の -1 は式インジェクションにならない（文字列の "-1" は別）。"""
        assert sanitize_cell(-1) == ("-1", False)

    def test_bytes_are_hex_encoded(self) -> None:
        sanitized, changed = sanitize_cell(b"\x00\xff")
        assert sanitized == "00ff"
        assert changed is False


class TestEncodeCsvRow:
    def test_encodes_utf8_with_crlf_terminator(self) -> None:
        data, sanitized = encode_csv_row(["a", "b"])
        assert data.decode("utf-8") == "a,b\r\n"
        assert sanitized == 0

    def test_quotes_cells_with_commas_and_newlines(self) -> None:
        data, _ = encode_csv_row(["a,b", 'say "hi"', "line1\nline2"])
        parsed = next(csv.reader(io.StringIO(data.decode("utf-8"))))
        assert parsed == ["a,b", 'say "hi"', "line1\nline2"]

    def test_counts_sanitized_cells(self) -> None:
        data, sanitized = encode_csv_row(["=cmd()", "safe", "@x"])
        assert sanitized == 2
        parsed = next(csv.reader(io.StringIO(data.decode("utf-8"))))
        assert parsed == ["'=cmd()", "safe", "'@x"]

    def test_unicode_cells_roundtrip(self) -> None:
        data, _ = encode_csv_row(["補填対象者", None, 42])
        parsed = next(csv.reader(io.StringIO(data.decode("utf-8"))))
        assert parsed == ["補填対象者", "", "42"]


class TestCellSizeBytes:
    def test_text_is_utf8_encoded_bytes(self) -> None:
        assert cell_size_bytes("abc") == 3
        assert cell_size_bytes("あ") == 3

    def test_blob_is_raw_bytes(self) -> None:
        assert cell_size_bytes(b"\x00\x01\x02") == 3

    def test_numbers_are_stringified_bytes(self) -> None:
        assert cell_size_bytes(12345) == 5
        assert cell_size_bytes(1.5) == 3

    def test_null_is_zero(self) -> None:
        assert cell_size_bytes(None) == 0


# ---------------------------------------------------------------------------
# staging / publish
# ---------------------------------------------------------------------------


class TestStaging:
    def test_staging_dir_is_created_exclusive_and_private(
        self, tmp_path: Path
    ) -> None:
        result = create_staging_dir(delivery_dir=tmp_path, run_id=RUN_ID)
        assert is_ok(result)
        staging = result.value
        assert staging.parent == tmp_path
        assert staging.name == f".staging-{RUN_ID}"
        assert staging.is_dir()
        assert (staging.stat().st_mode & 0o777) == 0o700

    def test_existing_staging_dir_is_conflict(self, tmp_path: Path) -> None:
        assert is_ok(create_staging_dir(delivery_dir=tmp_path, run_id=RUN_ID))
        result = create_staging_dir(delivery_dir=tmp_path, run_id=RUN_ID)
        assert is_err(result)
        assert result.error == DeliveryError.STAGING_FAILED

    def test_missing_delivery_dir_is_staging_failed(self, tmp_path: Path) -> None:
        result = create_staging_dir(
            delivery_dir=tmp_path / "no-such-dir", run_id=RUN_ID
        )
        assert is_err(result)
        assert result.error == DeliveryError.STAGING_FAILED

    def test_cleanup_removes_staging_recursively(self, tmp_path: Path) -> None:
        staging = create_staging_dir(delivery_dir=tmp_path, run_id=RUN_ID).value
        (staging / "result.csv").write_text("a,b\n", encoding="utf-8")
        cleanup_staging(staging)
        assert not staging.exists()

    def test_cleanup_of_missing_dir_is_noop(self, tmp_path: Path) -> None:
        cleanup_staging(tmp_path / "gone")  # 例外にならない


class TestPublish:
    def _staged(self, tmp_path: Path) -> Path:
        staging = create_staging_dir(delivery_dir=tmp_path, run_id=RUN_ID).value
        (staging / "result.csv").write_text("user_id\n1\n", encoding="utf-8")
        (staging / "manifest.json").write_text("{}", encoding="utf-8")
        return staging

    def test_publish_renames_staging_to_run_id(self, tmp_path: Path) -> None:
        staging = self._staged(tmp_path)
        result = publish_run_dir(
            staging_dir=staging,
            delivery_dir=tmp_path,
            run_id=RUN_ID,
            lock=FakeFileLock(),
            lock_timeout=5.0,
        )
        assert is_ok(result)
        final = result.value
        assert final == tmp_path / RUN_ID
        assert (final / "result.csv").read_text(encoding="utf-8") == "user_id\n1\n"
        assert not staging.exists()

    def test_existing_run_dir_with_content_is_conflict(self, tmp_path: Path) -> None:
        staging = self._staged(tmp_path)
        conflict = tmp_path / RUN_ID
        conflict.mkdir()
        (conflict / "old.csv").write_text("x", encoding="utf-8")
        result = publish_run_dir(
            staging_dir=staging,
            delivery_dir=tmp_path,
            run_id=RUN_ID,
            lock=FakeFileLock(),
            lock_timeout=5.0,
        )
        assert is_err(result)
        assert result.error == DeliveryError.CONFLICT
        assert (conflict / "old.csv").exists()  # 既存物は無傷

    def test_existing_empty_run_dir_is_also_conflict(self, tmp_path: Path) -> None:
        """POSIX rename は空 directory を黙って置換するため明示的に検出する。"""
        staging = self._staged(tmp_path)
        (tmp_path / RUN_ID).mkdir()
        result = publish_run_dir(
            staging_dir=staging,
            delivery_dir=tmp_path,
            run_id=RUN_ID,
            lock=FakeFileLock(),
            lock_timeout=5.0,
        )
        assert is_err(result)
        assert result.error == DeliveryError.CONFLICT
        assert staging.exists()  # 呼び出し側が cleanup する

    def test_publish_holds_parent_dir_lock(self, tmp_path: Path) -> None:
        staging = self._staged(tmp_path)
        lock = FakeFileLock()
        publish_run_dir(
            staging_dir=staging,
            delivery_dir=tmp_path,
            run_id=RUN_ID,
            lock=lock,
            lock_timeout=5.0,
        )
        assert len(lock.history) == 1
        assert lock.history[0][1] == 5.0

    def test_lock_timeout_leaves_staging_intact(self, tmp_path: Path) -> None:
        staging = self._staged(tmp_path)
        result = publish_run_dir(
            staging_dir=staging,
            delivery_dir=tmp_path,
            run_id=RUN_ID,
            lock=FakeFileLock(always_times_out=True),
            lock_timeout=5.0,
        )
        assert is_err(result)
        assert result.error == DeliveryError.PUBLISH_FAILED
        assert staging.exists()
        assert not (tmp_path / RUN_ID).exists()

    def test_concurrent_publish_only_one_wins(self, tmp_path: Path) -> None:
        """同一最終名への**同時** publish は「存在確認 + rename が同一 lock 区間」
        により一方だけ成功する（barrier で同時開始させる）。"""
        import threading

        staging_a = create_staging_dir(delivery_dir=tmp_path, run_id="a" + RUN_ID[1:]).value
        (staging_a / "result.csv").write_text("A", encoding="utf-8")
        staging_b = create_staging_dir(delivery_dir=tmp_path, run_id="b" + RUN_ID[1:]).value
        (staging_b / "result.csv").write_text("B", encoding="utf-8")
        lock = RealFileLock()
        barrier = threading.Barrier(2)
        results: list = [None, None]

        def worker(i: int, staging: Path) -> None:
            barrier.wait()
            results[i] = publish_run_dir(
                staging_dir=staging,
                delivery_dir=tmp_path,
                run_id=RUN_ID,
                lock=lock,
                lock_timeout=5.0,
            )

        threads = [
            threading.Thread(target=worker, args=(0, staging_a)),
            threading.Thread(target=worker, args=(1, staging_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if is_ok(r)]
        errs = [r for r in results if is_err(r)]
        assert len(oks) == 1
        assert len(errs) == 1
        assert errs[0].error == DeliveryError.CONFLICT
        content = (tmp_path / RUN_ID / "result.csv").read_text(encoding="utf-8")
        assert content in ("A", "B")  # 勝者の内容が無傷で残る

    def test_dangling_symlink_at_final_name_is_conflict(self, tmp_path: Path) -> None:
        """最終名が dangling symlink でも黙って置換せず CONFLICT にする。"""
        staging = self._staged(tmp_path)
        (tmp_path / RUN_ID).symlink_to(tmp_path / "no-such-target")
        result = publish_run_dir(
            staging_dir=staging,
            delivery_dir=tmp_path,
            run_id=RUN_ID,
            lock=FakeFileLock(),
            lock_timeout=5.0,
        )
        assert is_err(result)
        assert result.error == DeliveryError.CONFLICT
        assert staging.exists()

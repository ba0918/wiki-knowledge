"""Tests for tool_paths.py — tool query 系パスの全 segment symlink 拒否 wrapper.

``resolve_safe_path()`` は base 内 symlink を許可する既存仕様のため、
tool query 系（DB path / delivery 先 / credential）はこの wrapper を通す。
"""

from __future__ import annotations

from pathlib import Path

from lib.domain.types import is_err, is_ok
from lib.service.tool_paths import ToolPathError, resolve_no_symlink_path


class TestResolveOk:
    def test_nested_relative_path_resolves_under_base(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        result = resolve_no_symlink_path(base=tmp_path, relative="data/events.sqlite3")
        assert is_ok(result)
        assert result.value == tmp_path.resolve() / "data" / "events.sqlite3"

    def test_nonexistent_target_is_allowed(self, tmp_path: Path) -> None:
        """存在しないパスも返す（作成前の delivery 先などに使うため）。"""
        result = resolve_no_symlink_path(base=tmp_path, relative="not/yet/created.csv")
        assert is_ok(result)

    def test_single_segment(self, tmp_path: Path) -> None:
        result = resolve_no_symlink_path(base=tmp_path, relative="db.sqlite3")
        assert is_ok(result)
        assert result.value == tmp_path.resolve() / "db.sqlite3"


class TestRejectTraversal:
    def test_parent_traversal_outside_base_is_rejected(self, tmp_path: Path) -> None:
        result = resolve_no_symlink_path(base=tmp_path, relative="../outside.db")
        assert is_err(result)
        assert result.error == ToolPathError.OUTSIDE_BASE

    def test_internal_parent_segment_is_rejected_even_if_contained(
        self, tmp_path: Path
    ) -> None:
        """`a/../b` は最終解決先が base 内でも拒否する（.. を一切許可しない）。"""
        (tmp_path / "a").mkdir()
        result = resolve_no_symlink_path(base=tmp_path, relative="a/../b.db")
        assert is_err(result)
        assert result.error == ToolPathError.PARENT_SEGMENT

    def test_absolute_path_is_rejected(self, tmp_path: Path) -> None:
        result = resolve_no_symlink_path(base=tmp_path, relative="/etc/passwd")
        assert is_err(result)
        assert result.error == ToolPathError.ABSOLUTE

    def test_empty_is_rejected(self, tmp_path: Path) -> None:
        result = resolve_no_symlink_path(base=tmp_path, relative="")
        assert is_err(result)
        assert result.error == ToolPathError.EMPTY

    def test_nul_byte_is_rejected(self, tmp_path: Path) -> None:
        result = resolve_no_symlink_path(base=tmp_path, relative="a\x00b")
        assert is_err(result)
        assert result.error == ToolPathError.NUL_BYTE


class TestRejectSymlink:
    def test_terminal_symlink_inside_base_is_rejected(self, tmp_path: Path) -> None:
        """base 内 → base 内の終端 symlink も拒否（resolve_safe_path との差分）。"""
        real = tmp_path / "real.db"
        real.write_bytes(b"")
        link = tmp_path / "link.db"
        link.symlink_to(real)
        result = resolve_no_symlink_path(base=tmp_path, relative="link.db")
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

    def test_intermediate_symlink_directory_is_rejected(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        (real_dir / "db.sqlite3").write_bytes(b"")
        link_dir = tmp_path / "link_dir"
        link_dir.symlink_to(real_dir)
        result = resolve_no_symlink_path(base=tmp_path, relative="link_dir/db.sqlite3")
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

    def test_symlink_escaping_base_is_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        base = tmp_path / "base"
        base.mkdir()
        link = base / "escape"
        link.symlink_to(outside)
        result = resolve_no_symlink_path(base=base, relative="escape/x.db")
        assert is_err(result)
        # escape は containment 検査（resolve_safe_path）か symlink 検査の
        # どちらかで必ず落ちる。どちらの経路でも Err であることが契約。
        assert result.error in (
            ToolPathError.SYMLINK_COMPONENT,
            ToolPathError.SYMLINK_ESCAPE,
            ToolPathError.OUTSIDE_BASE,
        )

    def test_base_itself_being_symlink_is_rejected(self, tmp_path: Path) -> None:
        """catalog が symlink の絶対 base_dir を宣言しても「symlink 全拒否」を保つ。"""
        real = tmp_path / "real_base"
        real.mkdir()
        link = tmp_path / "base_link"
        link.symlink_to(real)
        result = resolve_no_symlink_path(base=link, relative="x.db")
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

    def test_symlink_ancestor_of_base_is_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real_root"
        (real / "sub").mkdir(parents=True)
        link = tmp_path / "root_link"
        link.symlink_to(real)
        result = resolve_no_symlink_path(base=link / "sub", relative="x.db")
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

    def test_dangling_symlink_is_rejected(self, tmp_path: Path) -> None:
        link = tmp_path / "dangling.db"
        link.symlink_to(tmp_path / "no-such-target")
        result = resolve_no_symlink_path(base=tmp_path, relative="dangling.db")
        assert is_err(result)
        assert result.error == ToolPathError.SYMLINK_COMPONENT

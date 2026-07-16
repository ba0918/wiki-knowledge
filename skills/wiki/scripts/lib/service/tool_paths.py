"""tool query 系パスの封じ込め wrapper（全 segment symlink 拒否）.

:func:`~lib.service.path_validator.resolve_safe_path` は base 内で完結する
symlink を明示的に許可する既存仕様（wiki 記事の運用ではそれが正しい）。
一方 tool query 系のパス（DB path / delivery 先 / credential ファイル）は
「git レビュー済み catalog が宣言した場所」以外を一切指せないことが安全
境界なので、base 配下の全 segment を ``lstat()`` 検査して symlink を拒否
する本 wrapper を必ず介す。``..`` segment も（最終解決先が base 内でも）
拒否する — catalog / CLI が正規の相対パスを書けばよいだけで、traversal
記法を許すメリットがない。
"""

from __future__ import annotations

import os
import unicodedata
from enum import Enum
from pathlib import Path

from lib.domain.types import Err, Ok, is_err
from lib.service.path_validator import resolve_safe_path


class ToolPathError(str, Enum):
    """Discriminator for tool-path validation failures.

    値は :class:`~lib.service.path_validator.PathValidationError` と互換
    （同名 discriminator は同じ文字列値）で、本 wrapper 固有の失敗として
    ``PARENT_SEGMENT`` / ``SYMLINK_COMPONENT`` を追加する。
    """

    EMPTY = "empty"
    ABSOLUTE = "absolute"
    OUTSIDE_BASE = "outside_base"
    NUL_BYTE = "nul_byte"
    TOO_LONG = "too_long"
    SYMLINK_ESCAPE = "symlink_escape"
    INVALID_TYPE = "invalid_type"
    PARENT_SEGMENT = "parent_segment"
    SYMLINK_COMPONENT = "symlink_component"


def _first_symlink_segment(absolute: Path) -> Path | None:
    """ルートから ``absolute`` まで lexical に降りながら lstat 検査し、
    最初に見つかった symlink segment を返す（なければ None）。"""

    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def resolve_no_symlink_path(
    *, base: Path, relative: str
) -> Ok[Path] | Err[ToolPathError]:
    """Resolve ``relative`` against ``base``, rejecting every symlink segment.

    base 自身の lexical path（ルートから base まで）も検査対象 — catalog が
    絶対 base_dir に symlink を宣言しても「symlink 全拒否」が破れないため。
    成功時は base 配下の絶対パスを返す（存在しなくてもよい — delivery 先の
    作成前検証にも使うため）。失敗は :class:`ToolPathError` の Err。
    """

    # abspath は symlink を解決せず lexical に絶対化する（resolve() だと
    # symlink が消えて検査できない）
    base_abs = Path(os.path.abspath(base))
    base_symlink = _first_symlink_segment(base_abs)
    if base_symlink is not None:
        return Err(
            error=ToolPathError.SYMLINK_COMPONENT,
            detail=f"symlink segment in base: {base_symlink.name!r}",
        )

    inner = resolve_safe_path(base=base_abs, relative=relative)
    if is_err(inner):
        return Err(error=ToolPathError(inner.error.value), detail=inner.detail)

    normalized = unicodedata.normalize("NFC", relative)
    parts = Path(normalized).parts
    if ".." in parts:
        return Err(
            error=ToolPathError.PARENT_SEGMENT,
            detail="'..' segment is not allowed in tool paths",
        )

    # base までは上で、containment は resolve_safe_path が保証済み。ここでは
    # relative の segment だけを base から終端まで lstat で検査する。
    current = base_abs
    for part in parts:
        current = current / part
        if current.is_symlink():
            return Err(
                error=ToolPathError.SYMLINK_COMPONENT,
                detail=f"symlink segment: {part!r}",
            )

    return Ok(value=current)

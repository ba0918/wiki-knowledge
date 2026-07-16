"""delivery — staging dir → no-clobber atomic publish と CSV 無害化.

publish の手順（一意に固定）:

1. staging dir（``.staging-{run_id}``、mode 0700）を排他的に作成
2. 呼び出し側が CSV + manifest を書き切る
3. :func:`publish_run_dir` — 各ファイル fsync → staging dir fsync →
   **delivery 親 dir の FileLock 下で最終名の不存在を確認してから rename**
   （POSIX の rename は既存**空 directory** を黙って置換し得るため、存在確認と
   rename を同一 lock 区間で行う。全 writer が本スクリプト経由である前提）→
   親 dir fsync
4. 失敗・timeout・SIGINT 時は :func:`cleanup_staging` で staging ごと削除

CSV 無害化は OWASP CSV injection 準拠: 先頭 ``=+-@`` に加え、先頭が
tab・CR・空白でその後に式文字が続くセルもエスケープ対象。
"""

from __future__ import annotations

import csv
import io
import os
import shutil
from enum import Enum
from pathlib import Path
from typing import Sequence

from lib.domain.types import Err, Ok
from lib.service.file_lock import FileLock, FileLockTimeout


class DeliveryError(str, Enum):
    STAGING_FAILED = "staging_failed"
    CONFLICT = "delivery_conflict"
    PUBLISH_FAILED = "publish_failed"


# ---------------------------------------------------------------------------
# CSV 無害化（純粋ヘルパー）
# ---------------------------------------------------------------------------

_FORMULA_CHARS = ("=", "+", "-", "@")
_LEADING_SKIP = (" ", "\t", "\r")


def sanitize_cell(value: object) -> tuple[str, bool]:
    """セル値を CSV 出力用の文字列にし、式インジェクションをエスケープする。

    エスケープは文字列セルのみ対象（数値型の -1 は式にならない）。bytes は
    hex 文字列にする（バイナリを CSV に生で埋めない）。返り値は
    ``(出力文字列, エスケープしたか)``。
    """

    if value is None:
        return "", False
    if isinstance(value, bytes):
        return value.hex(), False
    if not isinstance(value, str):
        return str(value), False

    stripped = value.lstrip("".join(_LEADING_SKIP))
    if stripped[:1] in _FORMULA_CHARS:
        return "'" + value, True
    return value, False


def encode_csv_row(cells: Sequence[object]) -> tuple[bytes, int]:
    """1 行分を無害化して CSV encode し、``(UTF-8 bytes, 無害化セル数)`` を返す。

    max_result_bytes の計数対象は**この無害化後 bytes**（Runner が累積する）。
    """

    sanitized_count = 0
    out_cells: list[str] = []
    for cell in cells:
        text, changed = sanitize_cell(cell)
        if changed:
            sanitized_count += 1
        out_cells.append(text)
    buffer = io.StringIO()
    csv.writer(buffer).writerow(out_cells)
    return buffer.getvalue().encode("utf-8"), sanitized_count


def cell_size_bytes(value: object) -> int:
    """max_cell_bytes の計測規則: TEXT = UTF-8 encoded bytes / BLOB = raw bytes /
    数値 = 文字列化後 bytes / NULL = 0。"""

    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(str(value))


# ---------------------------------------------------------------------------
# staging / publish（I/O）
# ---------------------------------------------------------------------------


def create_staging_dir(
    *, delivery_dir: Path, run_id: str
) -> Ok[Path] | Err[DeliveryError]:
    """``.staging-{run_id}`` を mode 0700 で排他的に作成する。

    ``.staging-`` prefix により、クラッシュで残った残骸は dead と判別できる。
    """

    staging = delivery_dir / f".staging-{run_id}"
    try:
        os.mkdir(staging, mode=0o700)
    except OSError as exc:
        return Err(error=DeliveryError.STAGING_FAILED, detail=str(exc))
    return Ok(value=staging)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_run_dir(
    *,
    staging_dir: Path,
    delivery_dir: Path,
    run_id: str,
    lock: FileLock,
    lock_timeout: float,
) -> Ok[Path] | Err[DeliveryError]:
    """staging を最終名 ``{run_id}/`` へ no-clobber rename する。

    衝突（既存名、空 directory 含む）は :attr:`DeliveryError.CONFLICT`。
    失敗時に staging は削除しない（cleanup は呼び出し側の finally が担う —
    ここで消すと監査より先に成果物が消える順序を作りかねない）。
    """

    final = delivery_dir / run_id
    try:
        for child in staging_dir.iterdir():
            if child.is_file():
                _fsync_path(child)
        _fsync_path(staging_dir)

        with lock.acquire(
            str(delivery_dir / ".publish.lock"), timeout=lock_timeout
        ):
            # lexists: dangling symlink が置かれていても衝突として扱う
            if os.path.lexists(final):
                return Err(
                    error=DeliveryError.CONFLICT,
                    detail=f"delivery 先に {run_id!r} が既に存在します",
                )
            os.rename(staging_dir, final)
            _fsync_path(delivery_dir)
    except FileLockTimeout as exc:
        return Err(error=DeliveryError.PUBLISH_FAILED, detail=str(exc))
    except OSError as exc:
        return Err(error=DeliveryError.PUBLISH_FAILED, detail=str(exc))
    return Ok(value=final)


def cleanup_staging(staging_dir: Path) -> None:
    """staging dir を削除する（失敗・SIGINT 時の finally 用。存在しなくてもよい）。"""

    shutil.rmtree(staging_dir, ignore_errors=True)

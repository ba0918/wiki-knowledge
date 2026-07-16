"""browser session state の封じ込め store（credential と同格の規律）.

session state（Playwright storage_state）は認証済み cookie/localStorage を含む
機密。credential と同格の封じ込めで扱う:

* 保存先は ``{wiki_root}/.local/browser-sessions/{tool_id}.json``（git 管理外）
* 書込みは 0600 atomic（``O_CREAT|O_EXCL|O_NOFOLLOW`` の temp → fsync → rename）
* 読取は全 segment symlink 拒否 + ``O_NOFOLLOW`` の同一 fd で fstat 検証
  （lookup を 1 回にして検査と読み取りの間の差し替えを防ぐ）
* regular file / permission 0600 以下 / 構造検証 / TTL / **tool・origin・account
  束縛の照合**（汎用 profile の持込み・tool 間共有を拒否）

tool_id は保存パスの一部（tool 間共有の構造的禁止）、origin / account は record 内の
束縛メタデータと照合する。秘密値（storage_state）は接続にのみ使い、ログ・stdout・
例外メッセージに載せない（本 module の detail も束縛メタと reason のみ）。
"""

from __future__ import annotations

import errno
import json
import os
import stat as _stat
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from lib.domain.types import Err, Ok, is_err
from lib.service.tool_paths import ToolPathError, resolve_no_symlink_path


SESSIONS_RELATIVE_DIR = ".local/browser-sessions"


class SessionError(str, Enum):
    NOT_FOUND = "session_not_found"
    NOT_REGULAR_FILE = "session_not_regular_file"
    BAD_PERMISSIONS = "session_bad_permissions"
    MALFORMED = "session_malformed"
    EXPIRED = "session_expired"
    BINDING_MISMATCH = "session_binding_mismatch"
    SYMLINK = "session_symlink"
    WRITE_FAILED = "session_write_failed"


@dataclass(frozen=True)
class SessionBinding:
    """session を束縛する tool / origin / account の 3 つ組。"""

    tool_id: str
    origin: str
    account: str


def _relative_path(tool_id: str) -> str:
    return f"{SESSIONS_RELATIVE_DIR}/{tool_id}.json"


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def save_session(
    *,
    wiki_root: Path,
    binding: SessionBinding,
    storage_state: dict,
    captured_at: str,
    expires_at: str,
) -> Ok[Path] | Err[SessionError]:
    """storage_state + 束縛メタを 0600 atomic で永続化する。

    Playwright デフォルト（0644 平文 JSON）で書かせない — temp を
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` の 0600 で作り、fsync 後に最終名へ rename する。
    """

    resolved = resolve_no_symlink_path(
        base=wiki_root, relative=_relative_path(binding.tool_id)
    )
    if is_err(resolved):
        return Err(error=SessionError.SYMLINK, detail="session パスに symlink")
    target = resolved.value
    record = {
        "binding": {
            "tool_id": binding.tool_id,
            "origin": binding.origin,
            "account": binding.account,
        },
        "captured_at": captured_at,
        "expires_at": expires_at,
        "storage_state": storage_state,
    }
    blob = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 既存 temp は落とす（O_EXCL 衝突回避）。symlink 追従は O_NOFOLLOW が拒否
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(
            tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, blob)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        return Err(error=SessionError.WRITE_FAILED, detail=str(exc.errno))
    return Ok(value=target)


def load_session(
    *, wiki_root: Path, binding: SessionBinding, now: str
) -> Ok[dict] | Err[SessionError]:
    """束縛と TTL を照合して storage_state を返す（fail-closed）。"""

    resolved = resolve_no_symlink_path(
        base=wiki_root, relative=_relative_path(binding.tool_id)
    )
    if is_err(resolved):
        if resolved.error in (
            ToolPathError.SYMLINK_COMPONENT,
            ToolPathError.SYMLINK_ESCAPE,
        ):
            return Err(error=SessionError.NOT_REGULAR_FILE, detail="経路に symlink")
        return Err(error=SessionError.NOT_FOUND, detail="session が解決できない")

    try:
        fd = os.open(resolved.value, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return Err(error=SessionError.NOT_FOUND, detail="session ファイルなし")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return Err(error=SessionError.NOT_REGULAR_FILE, detail="symlink 不可")
        return Err(error=SessionError.NOT_FOUND, detail=str(exc.errno))

    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return Err(
                error=SessionError.NOT_REGULAR_FILE, detail="regular file が必要"
            )
        if st.st_mode & 0o077:
            return Err(error=SessionError.BAD_PERMISSIONS, detail="0600 が必要")
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                fd = -1
                data = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return Err(error=SessionError.MALFORMED, detail="JSON として読めない")
    finally:
        if fd >= 0:
            os.close(fd)

    if not isinstance(data, dict):
        return Err(error=SessionError.MALFORMED, detail="record がオブジェクトではない")
    bind = data.get("binding")
    storage_state = data.get("storage_state")
    expires_at = data.get("expires_at")
    if (
        not isinstance(bind, dict)
        or not isinstance(storage_state, dict)
        or not isinstance(expires_at, str)
    ):
        return Err(error=SessionError.MALFORMED, detail="record の構造が不正")

    # TTL を束縛より先に見る（期限切れは束縛の正否によらず拒否）
    try:
        if _parse_iso(now) >= _parse_iso(expires_at):
            return Err(error=SessionError.EXPIRED, detail=expires_at)
    except ValueError:
        return Err(error=SessionError.MALFORMED, detail="expires_at が日時ではない")

    if (
        bind.get("tool_id") != binding.tool_id
        or bind.get("origin") != binding.origin
        or bind.get("account") != binding.account
    ):
        return Err(
            error=SessionError.BINDING_MISMATCH,
            detail="tool/origin/account 束縛が catalog 宣言と一致しない",
        )

    return Ok(value=storage_state)

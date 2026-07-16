"""Tests for browser_session_store — session state の封じ込め（常時実行・非ブラウザ）.

session state は credential と同格の封じ込め: 0600 以下・全 segment symlink 拒否・
O_NOFOLLOW・TTL・tool/origin/account 束縛。汎用ブラウザ profile の持込みや profile の
tool 間共有を束縛不一致として拒否する。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.browser_session_store import (
    SessionBinding,
    SessionError,
    load_session,
    save_session,
)

NOW = "2026-07-16T12:00:00Z"
LATER = "2026-07-18T00:00:00Z"
BINDING = SessionBinding(
    tool_id="events-web", origin="https://app.example.com", account="svc-ro"
)
STORAGE_STATE = {"cookies": [{"name": "sid", "value": "x"}], "origins": []}


def make_wiki(tmp_path: Path) -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / ".local").mkdir(parents=True)
    return wiki_root


def do_save(wiki_root: Path, **overrides):
    args = dict(
        wiki_root=wiki_root,
        binding=BINDING,
        storage_state=STORAGE_STATE,
        captured_at=NOW,
        expires_at=LATER,
    )
    args.update(overrides)
    return save_session(**args)


class TestSaveLoad:
    def test_roundtrip_returns_storage_state(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        result = load_session(
            wiki_root=wiki_root, binding=BINDING, now=NOW
        )
        assert is_ok(result), result
        assert result.value == STORAGE_STATE

    def test_saved_file_is_0600(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        path = wiki_root / ".local" / "browser-sessions" / "events-web.json"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0  # group/other に権限なし


class TestContainment:
    def test_group_readable_session_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        path = wiki_root / ".local" / "browser-sessions" / "events-web.json"
        path.chmod(0o640)
        result = load_session(wiki_root=wiki_root, binding=BINDING, now=NOW)
        assert is_err(result)
        assert result.error == SessionError.BAD_PERMISSIONS

    def test_symlinked_session_path_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        sessions = wiki_root / ".local" / "browser-sessions"
        real = sessions / "events-web.json"
        target = tmp_path / "evil.json"
        target.write_text(json.dumps({"storage_state": {}}), encoding="utf-8")
        target.chmod(0o600)
        real.unlink()
        real.symlink_to(target)
        result = load_session(wiki_root=wiki_root, binding=BINDING, now=NOW)
        assert is_err(result)
        assert result.error in (
            SessionError.NOT_REGULAR_FILE,
            SessionError.SYMLINK,
        )

    def test_missing_session_is_not_found(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        result = load_session(wiki_root=wiki_root, binding=BINDING, now=NOW)
        assert is_err(result)
        assert result.error == SessionError.NOT_FOUND


class TestTtlAndBinding:
    def test_expired_session_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root, expires_at=LATER))
        result = load_session(
            wiki_root=wiki_root, binding=BINDING, now="2026-07-19T00:00:00Z"
        )
        assert is_err(result)
        assert result.error == SessionError.EXPIRED

    def test_wrong_origin_binding_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        other = SessionBinding(
            tool_id="events-web", origin="https://evil.example.com", account="svc-ro"
        )
        result = load_session(wiki_root=wiki_root, binding=other, now=NOW)
        assert is_err(result)
        assert result.error == SessionError.BINDING_MISMATCH

    def test_wrong_account_binding_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        other = SessionBinding(
            tool_id="events-web", origin="https://app.example.com", account="admin"
        )
        result = load_session(wiki_root=wiki_root, binding=other, now=NOW)
        assert is_err(result)
        assert result.error == SessionError.BINDING_MISMATCH

    def test_tool_id_is_part_of_path_binding(self, tmp_path: Path) -> None:
        """別 tool_id の session を読もうとしても NOT_FOUND（tool 間共有禁止）。"""
        wiki_root = make_wiki(tmp_path)
        assert is_ok(do_save(wiki_root))
        other = SessionBinding(
            tool_id="other-web", origin="https://app.example.com", account="svc-ro"
        )
        result = load_session(wiki_root=wiki_root, binding=other, now=NOW)
        assert is_err(result)
        assert result.error == SessionError.NOT_FOUND

    def test_malformed_session_json_is_rejected(self, tmp_path: Path) -> None:
        wiki_root = make_wiki(tmp_path)
        sessions = wiki_root / ".local" / "browser-sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / "events-web.json"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(fd, b"not json")
        os.close(fd)
        result = load_session(wiki_root=wiki_root, binding=BINDING, now=NOW)
        assert is_err(result)
        assert result.error == SessionError.MALFORMED

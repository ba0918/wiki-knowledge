"""form / form+totp の自動フォームログインと human-assisted 捕捉の共有コア.

session 捕捉は封じ込め context 内で行い（interception + ephemeral profile）、成果は
Playwright storage_state（cookie / localStorage）。抽出・delivery の経路は持たない。

* **form / form+totp**: headless で catalog 宣言のログインフォームを自動入力する
  （`form_login`）。TOTP は RFC 6238（stdlib hmac）
* **human-assisted**: 人間が headed でログインし、捕捉直後に有効性を検証する
  （`finalize_capture` を共有）

秘密（password / TOTP secret / storage_state）はログ・stdout・例外に載せない。login
境界を越える例外は閉じた reason に sanitize する（生の URL / セレクタ / DOM を通さない）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from pathlib import Path

from lib.domain.types import Err, Ok, is_err
from lib.service.browser_flow_runner import (
    BrowserReason,
    OriginRuleLite,
    contained_context,
    rules_from_entry,
    sanitize_exception,
)
from lib.service.browser_session_store import (
    SessionBinding,
    SessionError,
    load_session,
    save_session,
)


# ---------------------------------------------------------------------------
# TOTP（RFC 6238、stdlib のみ）— 生成の唯一の真実源
# ---------------------------------------------------------------------------


def totp_code(secret_b32: str, *, at_unix: float, step: int = 30, digits: int = 6) -> str:
    """base32 secret から指定時刻の TOTP を計算する（RFC 6238 / HMAC-SHA1）。"""

    key = base64.b32decode(secret_b32)
    counter = int(at_unix // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


# ---------------------------------------------------------------------------
# login 用 allowlist（抽出 rules + login_origins をログイン中のみ併用）
# ---------------------------------------------------------------------------


def login_rules(entry) -> tuple[OriginRuleLite, ...]:
    """抽出 origin_allowlist に加え、ログイン中のみ有効な login_origins を併用する。"""

    rules = list(rules_from_entry(entry))
    for origin in entry.auth.login_origins:
        origin = origin.rstrip("/")
        for method in ("GET", "POST"):
            for rtype in ("document", "other", "xhr", "fetch"):
                rules.append(
                    OriginRuleLite(
                        origin=origin,
                        method=method,
                        path_prefix="/",
                        resource_type=rtype,
                    )
                )
    return tuple(rules)


def _sanitize_login(exc: BaseException) -> BrowserReason:
    """login 境界の例外を閉じた reason に写像する（timeout = 未完了 → session_expired）。"""

    reason = sanitize_exception(exc)
    if reason == BrowserReason.FLOW_TIMEOUT:
        return BrowserReason.SESSION_EXPIRED
    return reason


def form_login(
    pw,
    *,
    entry,
    username: str,
    password: str,
    totp_secret: str | None = None,
    at_unix: float | None = None,
    default_timeout_ms: int = 15_000,
):
    """headless で catalog 宣言のログインフォームを自動入力し storage_state を返す。

    完了検知は post-login URL（``login.success_url_contains``）。捕捉した storage_state
    の永続化・束縛は呼び出し側（session 解決）が行う。
    """

    login = entry.auth.login
    if login is None:
        return Err(error=BrowserReason.SESSION_EXPIRED, detail="login 設定なし")
    origin = entry.account.origin.rstrip("/")
    now_unix = time.time() if at_unix is None else at_unix
    rules = login_rules(entry)
    try:
        with contained_context(
            pw, rules=rules, default_timeout_ms=default_timeout_ms
        ) as context:
            page = context.new_page()
            page.goto(origin + "/" + login.route.strip("/"))
            page.get_by_label(login.username_label).fill(username)
            page.get_by_label(login.password_label).fill(password)
            if totp_secret and login.totp_label:
                page.get_by_label(login.totp_label).fill(
                    totp_code(totp_secret, at_unix=now_unix)
                )
            page.get_by_role(login.submit_role, name=login.submit_name).click()
            # 完了検知: post-login URL 遷移（wrong creds は遷移せず timeout → session_expired）
            page.wait_for_url("**" + login.success_url_contains + "**")
            storage_state = context.storage_state()
            return Ok(value=storage_state)
    except BaseException as exc:  # noqa: BLE001 - 境界で全例外を sanitize
        return Err(error=_sanitize_login(exc), detail="")


# ---------------------------------------------------------------------------
# 捕捉直後の有効性検証 + 束縛 + 永続化（form / human-assisted 共有）
# ---------------------------------------------------------------------------


def _binding_of(entry) -> SessionBinding:
    return SessionBinding(
        tool_id=entry.tool_id,
        origin=entry.account.origin.rstrip("/"),
        account=entry.account.id,
    )


def finalize_capture(
    *,
    wiki_root: Path,
    entry,
    storage_state: dict,
    now_iso: str,
    expires_at: str,
):
    """捕捉した storage_state を束縛して 0600 で永続化する（guide §6/§10）。

    束縛メタ（tool / origin / account）を付けて保存し、直後に load で読み戻して
    有効性（束縛一致・TTL 内・0600）を検証する。表示情報（束縛 + TTL）を返す。
    秘密（storage_state）は返り値に含めない。
    """

    binding = _binding_of(entry)
    saved = save_session(
        wiki_root=wiki_root,
        binding=binding,
        storage_state=storage_state,
        captured_at=now_iso,
        expires_at=expires_at,
    )
    if is_err(saved):
        return Err(error=BrowserReason.SESSION_EXPIRED, detail="session 保存に失敗")
    # 捕捉直後に読み戻して有効性を検証（未認証のまま捕捉して後日顕在化させない）
    reloaded = load_session(wiki_root=wiki_root, binding=binding, now=now_iso)
    if is_err(reloaded):
        return Err(
            error=BrowserReason.SESSION_BINDING_MISMATCH
            if reloaded.error == SessionError.BINDING_MISMATCH
            else BrowserReason.SESSION_EXPIRED,
            detail="捕捉直後の有効性検証に失敗",
        )
    return Ok(
        value={
            "tool_id": binding.tool_id,
            "origin": binding.origin,
            "account": binding.account,
            "expires_at": expires_at,
        }
    )

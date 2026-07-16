#!/usr/bin/env python3
"""wiki-browser-extract CLI — seal-at-prepare の prepare / approve / execute /
doctor / login.

承認モデルは SQL 系の approve-then-execute ではなく **seal-at-prepare**:

* prepare = フロー実行 + 抽出 + 検証契約 enforce まで完了し、成果物 + manifest を
  隔離 bundle（spool = ``outputs/browser-plans/{plan_id}/``）に**封印**（SHA-256 確定）。
  封印ハッシュを ``prepared`` 監査イベント（spool 外アンカー）に記録する
* approve = 人間 TTY。**封印 artifact + manifest から表示時にハッシュを再導出**し、
  ``prepared`` 監査アンカーと **fail-closed 照合**（不一致 = 拒否）。spool 内の保存
  プレビューを信用しない。manifest 改変も「拒否」のみ合格
* execute = **封印済み成果物の delivery 解放のみ**（ブラウザ再実行なし・single-use）

承認がゲートするのは delivery（マシン外搬出）のみ。機密性境界は「そのマシン」に後退する
（honest scoping、guide §0）。監査 JSONL 自体の可書性は既知の限界。

exit: 0=成功 / 1=拒否・失敗 / 2=usage / 130=SIGINT。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lib.domain.browser_contract import (  # noqa: E402
    CheckEvidence,
    enforce_checks,
    parse_browser_catalog,
    resolve_browser_entry,
    validate_params_schema,
)
from lib.domain.tool_query import (  # noqa: E402
    TransitionTable,
    apply_transition,
    build_plan_id,
    compute_expires_at,
    is_expired,
    parse_plan_id,
    sha256_hex,
)
from lib.domain.types import Err, Ok, is_err  # noqa: E402
from lib.service.browser_flow_runner import BrowserReason  # noqa: E402
from lib.service.clock import Clock, SystemClock  # noqa: E402
from lib.service.file_lock import FileLock, RealFileLock  # noqa: E402
from lib.service.tool_approval import (  # noqa: E402
    ApprovalService,
    write_json_durable,
)
from lib.service.tool_audit import AuditEvent, AuditLog, AuditRegistry  # noqa: E402
from lib.service.tool_delivery import (  # noqa: E402
    cleanup_staging,
    create_staging_dir,
    publish_run_dir,
)
from lib.service.tool_paths import resolve_declared_dir  # noqa: E402


BROWSER_CATALOG_RELATIVE = "tools/browser-catalog.json"
PLANS_RELATIVE = "outputs/browser-plans"
PREVIEW_ROWS = 10

# seal-at-prepare の状態機械（guide §9）
BROWSER_TABLE = TransitionTable(
    initial="prepared",
    edges={
        "prepared": ("approved",),
        "approved": ("delivering",),
        "delivering": ("delivered", "failed"),
    },
    terminal=frozenset({"delivered", "failed", "expired"}),
)

# 監査 registry（別ファイル・同形式、値なしメタデータのみ）
BROWSER_AUDIT_REGISTRY = AuditRegistry(
    events={
        "prepared": True,
        "approved": True,
        "delivering": True,
        "delivered": True,
        "execute_attempted": True,
        "rejected": True,
        "failed": True,
        "expired": True,
        "login": False,
        "doctor": False,
    },
    subcommands=frozenset(
        {"prepare", "approve", "execute", "doctor", "login", "catalog-validate"}
    ),
    allowed_reasons=frozenset(
        {r.value for r in BrowserReason}
        | {
            "bundle_missing",
            "audit_write_failed",
            "lock_timeout",
            "plan_conflict",
            "not_approved",
            "already_consumed",
            "ttl_expired",
            "params_invalid",
            "unknown_tool",
            "delivery_not_allowed",
        }
    ),
    allowed_digest_keys=frozenset({"artifact_digest", "manifest_digest"}),
    relative_path="outputs/browser-audit.jsonl",
)


# ---------------------------------------------------------------------------
# reason hint（what / why / next）— guide §10 の hint 表と配線
# ---------------------------------------------------------------------------

_REASON_HINTS: dict[str, dict[str, str]] = {
    "selector_not_found": {
        "what": "期待する locator が見つからない",
        "why": "UI 変更 or フロー誤り",
        "next": "doctor を実行 → フロー修正 PR",
    },
    "ui_drift": {
        "what": "画面構造が doctor 基準から乖離",
        "why": "UI 変更",
        "next": "doctor を実行 → フロー修正 PR",
    },
    "session_expired": {
        "what": "session state が失効",
        "why": "TTL 超過 or サーバー側失効",
        "next": "再認証（login or form）",
    },
    "session_binding_mismatch": {
        "what": "session が tool/origin/account と不一致",
        "why": "別 profile の持込み",
        "next": "正しい session で再取得",
    },
    "origin_blocked": {
        "what": "宣言外 origin/method/path へのリクエスト",
        "why": "フロー誤り or 攻撃",
        "next": "フロー修正 PR / allowlist 見直し PR",
    },
    "readback_mismatch": {
        "what": "filter_readback が params と不一致",
        "why": "フィルタ未反映",
        "next": "フロー修正 PR / パラメータ確認",
    },
    "seal_mismatch": {
        "what": "再導出ハッシュが監査アンカーと不一致",
        "why": "prepare 後の bundle 改変",
        "next": "承認せず再 prepare",
    },
    "flow_timeout": {
        "what": "hard wall-clock timeout 超過",
        "why": "遅延 or 無限待ち",
        "next": "フロー修正 PR / timeout 見直し",
    },
    "bundle_cap_exceeded": {
        "what": "未承認 bundle 数が上限超過",
        "why": "承認滞留",
        "next": "未承認 bundle を approve or 失効させる",
    },
    "flow_pin_mismatch": {
        "what": "flow の SHA-256 が catalog 宣言と不一致",
        "why": "未追跡コード",
        "next": "catalog 更新 PR / フロー復元",
    },
    "flow_ast_violation": {
        "what": "AST ゲート違反（import/exec/dunder 等）",
        "why": "禁止構文",
        "next": "フロー修正 PR",
    },
    "internal_error": {
        "what": "分類不能のエラー",
        "why": "予期しない例外",
        "next": "ログ確認・issue 化",
    },
}


def reason_hint(reason: str) -> dict[str, str]:
    """reason code に対応する what/why/next の hint を返す（未知は internal_error）。"""

    return _REASON_HINTS.get(reason, _REASON_HINTS["internal_error"])


# ---------------------------------------------------------------------------
# プレビュー描画（未信頼バイトとして扱う）
# ---------------------------------------------------------------------------


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def render_preview_cell(value: object, *, width: int = 40) -> str:
    """抽出セルを未信頼バイトとして端末安全に描画する。

    非印字文字・ESC はエスケープ表示（端末エスケープ注入で承認プロンプトを偽装
    させない）、East Asian width を考慮した幅認識クリップ + truncation マーカー。
    """

    text = "" if value is None else str(value)
    safe_chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            safe_chars.append(f"\\x{code:02x}")
        else:
            safe_chars.append(ch)
    safe = "".join(safe_chars)

    if _display_width(safe) <= width:
        return safe
    clipped: list[str] = []
    acc = 0
    for ch in safe:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if acc + w > width - 1:
            break
        clipped.append(ch)
        acc += w
    return "".join(clipped) + "…"


# ---------------------------------------------------------------------------
# params 値検証（params_schema に対する値の照合）
# ---------------------------------------------------------------------------


def _validate_params_values(schema: dict, params: dict) -> list[str]:
    import re

    errors: list[str] = []
    props = schema.get("properties", {})
    required = schema.get("required", [])
    for key in params:
        if key not in props:
            errors.append(f"未知パラメータ: {key}")
    for name in required:
        if name not in params:
            errors.append(f"必須パラメータ欠損: {name}")
    for name, spec in props.items():
        if name not in params:
            continue
        val = params[name]
        if "enum" in spec and val not in spec["enum"]:
            errors.append(f"{name} が enum 外")
        if "pattern" in spec:
            if not isinstance(val, str) or re.fullmatch(spec["pattern"], val) is None:
                errors.append(f"{name} が pattern 不一致")
        if "maxLength" in spec and isinstance(val, str) and len(val) > spec["maxLength"]:
            errors.append(f"{name} が maxLength 超過")
    return errors


# ---------------------------------------------------------------------------
# 型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepareOutcome:
    plan_id: str
    tool_id: str
    row_count: int
    artifact_digest: str
    manifest_digest: str
    expires_at: str
    bundle_dir: Path


@dataclass(frozen=True)
class ExecuteOutcome:
    run_id: str
    row_count: int
    published_path: Path


def _err(reason: object, detail: str = "") -> Err:
    value = reason.value if isinstance(reason, BrowserReason) else str(reason)
    return Err(error=value, detail=detail)


# ---------------------------------------------------------------------------
# BrowserRunner
# ---------------------------------------------------------------------------


class BrowserRunner:
    """seal-at-prepare のユースケース組み立て（extractor は注入可能）。"""

    def __init__(
        self,
        *,
        wiki_root: Path,
        clock: Clock,
        lock: FileLock,
        extractor,
        nonce: Callable[[], str],
        lock_timeout: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        login_fn: Callable | None = None,
    ) -> None:
        self._wiki_root = Path(wiki_root)
        self._clock = clock
        self._lock = lock
        self._extractor = extractor
        self._nonce = nonce
        self._monotonic = monotonic
        # form / form+totp の自動ログイン（実体は headless form login、テストは fake 注入）
        self._login_fn = login_fn
        self._audit = AuditLog(
            wiki_root=self._wiki_root,
            lock=lock,
            clock=clock,
            lock_timeout=lock_timeout,
            registry=BROWSER_AUDIT_REGISTRY,
        )
        self._approval = ApprovalService(
            plans_root=self._wiki_root / PLANS_RELATIVE,
            lock=lock,
            lock_timeout=lock_timeout,
        )

    # -- catalog -----------------------------------------------------------

    def _load_entry(self, tool_id: str):
        path = self._wiki_root / BROWSER_CATALOG_RELATIVE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _err("unknown_tool", str(exc))
        parsed = parse_browser_catalog(data)
        if is_err(parsed):
            return _err(parsed.error.value, parsed.detail)
        return resolve_browser_entry(parsed.value, tool_id)

    # -- prepare -----------------------------------------------------------

    def prepare(self, *, tool_id: str, params: dict, deliver_to: str):
        loaded = self._load_entry(tool_id)
        if is_err(loaded):
            return _err(
                "unknown_tool"
                if getattr(loaded.error, "value", loaded.error) == "browser_unknown_tool"
                else loaded.error,
                getattr(loaded, "detail", ""),
            )
        entry = loaded.value

        schema_errors = validate_params_schema(entry.params_schema)
        if schema_errors:
            return _err("params_invalid", "; ".join(schema_errors))
        value_errors = _validate_params_values(entry.params_schema, params)
        if value_errors:
            return _err("params_invalid", "; ".join(value_errors))

        if deliver_to not in entry.delivery_allowed_dirs:
            return _err("delivery_not_allowed", deliver_to)

        cap = self._count_unapproved(tool_id)
        if cap >= entry.limits.max_unapproved_bundles:
            return _err(BrowserReason.BUNDLE_CAP_EXCEEDED, f"未承認 {cap} 本")

        resolved = self._resolve_session_state(entry)
        if is_err(resolved):
            return resolved
        session_state = resolved.value
        deadline = self._monotonic() + entry.limits.max_flow_seconds
        extracted = self._extractor.extract(
            entry=entry,
            params=params,
            session_state=session_state,
            deadline_monotonic=deadline,
        )
        if is_err(extracted):
            return _err(extracted.error, "")
        result = extracted.value

        evidence = CheckEvidence(
            rows=tuple(result.rows),
            columns=tuple(result.columns),
            params=dict(params),
            readbacks=dict(result.readbacks),
            ui_total=result.ui_total,
            file_row_count=len(result.rows),
            account_id=result.account_id,
            screen_fingerprint=result.screen_fingerprint,
        )
        outcomes, all_ok = enforce_checks(entry.checks, evidence)
        if not all_ok:
            failed = next(o for o in outcomes if not o.passed)
            return _err(failed.reason or "internal_error", f"check={failed.check}")

        row_count = len(result.rows)
        artifact_bytes = result.artifact_bytes
        artifact_digest = sha256_hex(artifact_bytes)
        created_at = self._clock.now()
        expires_at = compute_expires_at(
            created_at=created_at, ttl_hours=entry.retention.ttl_hours
        )
        preview = [
            [render_preview_cell(c) for c in row] for row in result.rows[:PREVIEW_ROWS]
        ]

        digests_holder: dict[str, tuple[str, str]] = {}

        def build_files(plan_id: str) -> dict[str, bytes]:
            manifest = {
                "plan_id": plan_id,
                "tool_id": tool_id,
                "tier": entry.tier,
                "row_count": row_count,
                "columns": list(result.columns),
                "preview_rows": preview,
                "column_source_map": {c: "extracted" for c in result.columns},
                "anchor_results": [
                    {"check": o.check, "passed": o.passed, "reason": o.reason}
                    for o in outcomes
                ],
                "guarantees": {
                    "integrity": entry.guarantees.integrity,
                    "identity": entry.guarantees.identity,
                    "filter_correctness": entry.guarantees.filter_correctness,
                    "completeness": entry.guarantees.completeness,
                    "human_verification": entry.guarantees.human_verification,
                },
                "extracted_at": result.extracted_at,
                "expires_at": expires_at,
            }
            manifest_bytes = (
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            manifest_digest = sha256_hex(manifest_bytes)
            state = {
                "status": "prepared",
                "sealed_artifact_digest": artifact_digest,
                "sealed_manifest_digest": manifest_digest,
                "approved_by": None,
                "approved_at": None,
                "run_id": None,
                "expires_at": expires_at,
                "extracted_at": result.extracted_at,
            }
            state_bytes = (
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            digests_holder[plan_id] = (artifact_digest, manifest_digest)
            return {
                "artifact.bin": artifact_bytes,
                "manifest.json": manifest_bytes,
                "state.json": state_bytes,
            }

        plan_id = build_plan_id(
            now_iso=created_at, nonce=self._nonce(), tool_id=tool_id
        )

        def rebuild() -> str:
            return build_plan_id(
                now_iso=created_at, nonce=self._nonce(), tool_id=tool_id
            )

        published = self._approval.publish_bundle(
            plan_id=plan_id, rebuild_plan_id=rebuild, build_files=build_files
        )
        if is_err(published):
            return _err(published.error, published.detail)
        final_id = published.value.plan_id
        art_dig, man_dig = digests_holder[final_id]

        audit = self._audit.append(
            AuditEvent(
                event="prepared",
                plan_id=final_id,
                tool_id=tool_id,
                subcommand="prepare",
                row_count=row_count,
                digests={"artifact_digest": art_dig, "manifest_digest": man_dig},
            )
        )
        if is_err(audit):
            return _err("audit_write_failed", audit.detail)

        return Ok(
            value=PrepareOutcome(
                plan_id=final_id,
                tool_id=tool_id,
                row_count=row_count,
                artifact_digest=art_dig,
                manifest_digest=man_dig,
                expires_at=expires_at,
                bundle_dir=published.value.bundle_dir,
            )
        )

    # -- session 解決（form / form+totp）-----------------------------------

    def _resolve_session_state(self, entry):
        """profile に応じて session_state を解決する（none=None / form 系=store or 自動login）。

        form / form+totp: session store に有効な束縛済み session があれば再利用、無ければ
        ``login_fn`` で捕捉して 0600 保存し再利用する。human-assisted: store 必須
        （無ければ session_expired → login サブコマンドを hint）。
        """

        from lib.service.browser_session_store import (
            SessionBinding,
            load_session,
            save_session,
        )
        from lib.domain.types import is_ok

        profile = entry.auth.profile
        if profile == "none":
            return Ok(value=None)

        binding = SessionBinding(
            tool_id=entry.tool_id,
            origin=entry.account.origin.rstrip("/"),
            account=entry.account.id,
        )
        loaded = load_session(
            wiki_root=self._wiki_root, binding=binding, now=self._clock.now()
        )
        if is_ok(loaded):
            return Ok(value=loaded.value)

        if profile in ("form", "form+totp"):
            if self._login_fn is None:
                return _err(BrowserReason.SESSION_EXPIRED, "自動ログインが未設定")
            captured = self._login_fn(entry)
            if is_err(captured):
                return captured
            ttl_hours = entry.auth.session_ttl_hours or entry.retention.ttl_hours
            expires_at = compute_expires_at(
                created_at=self._clock.now(), ttl_hours=ttl_hours
            )
            saved = save_session(
                wiki_root=self._wiki_root,
                binding=binding,
                storage_state=captured.value,
                captured_at=self._clock.now(),
                expires_at=expires_at,
            )
            if is_err(saved):
                return _err(BrowserReason.SESSION_EXPIRED, "session 保存に失敗")
            return Ok(value=captured.value)

        # human-assisted は login サブコマンドでの捕捉が前提
        return _err(BrowserReason.SESSION_EXPIRED, "login サブコマンドで捕捉が必要")

    # -- doctor（接続の事前診断・データ非接触）-----------------------------

    def doctor(self, tool_id: str):
        """flow 完全性 + catalog 整合の事前診断（抽出・成果物生成をしない）。

        honest scoping（guide §16）: doctor はデータ非接触を主張するが、実 chromium
        を要する疎通・selector 実在確認はログイン副作用を持つため smoke ゲート下でのみ
        走る。ここでは browser 非依存の検査（catalog resolve / flow pin / AST ゲート /
        params_schema）を実施し、chromium 検査は SKIP 明示する。
        """

        from lib.service.browser_flow_runner import check_flow_ast, verify_flow_pin

        checks: list[tuple[str, str, str]] = []
        loaded = self._load_entry(tool_id)
        if is_err(loaded):
            checks.append(("catalog_resolve", "NG", str(loaded.error)))
            return Ok(value=tuple(checks))
        entry = loaded.value
        checks.append(("catalog_resolve", "OK", tool_id))

        flow_path = self._wiki_root / "tools" / "flows" / entry.flow.ref
        try:
            source_bytes = flow_path.read_bytes()
        except OSError:
            checks.append(("flow_pin", "NG", "flow ファイルを読めない"))
            checks.append(("flow_ast", "SKIP", "flow 未読"))
        else:
            pin = verify_flow_pin(source_bytes, entry.flow.sha256)
            checks.append(
                ("flow_pin", "OK" if not is_err(pin) else "NG", entry.flow.ref)
            )
            gate = check_flow_ast(source_bytes.decode("utf-8", errors="replace"))
            checks.append(
                ("flow_ast", "OK" if not is_err(gate) else "NG", entry.flow.ref)
            )

        schema_ok = not validate_params_schema(entry.params_schema)
        checks.append(("params_schema", "OK" if schema_ok else "NG", ""))

        # 実 chromium を要する疎通・selector 実在確認（ログイン副作用あり）は smoke のみ
        if not os.environ.get("BROWSER_EXTRACT_SMOKE"):
            checks.append(("login_reachability", "SKIP", "BROWSER_EXTRACT_SMOKE 未設定"))
            checks.append(("selector_exists", "SKIP", "BROWSER_EXTRACT_SMOKE 未設定"))
        else:
            checks.extend(self._doctor_chromium(entry))

        self._audit.append(
            AuditEvent(
                event="doctor", plan_id=None, tool_id=tool_id, subcommand="doctor"
            )
        )
        return Ok(value=tuple(checks))

    def _doctor_chromium(self, entry):  # pragma: no cover - smoke（実 chromium）
        """login 疎通 + selector 実在を実 chromium で診断する（データ非接触の範囲で）。

        honest scoping（guide §16）: doctor はデータ非接触を主張しない。ログインページへの
        遷移と selector 実在確認はログイン副作用を持つが、**抽出・成果物生成はしない**。
        trace / screenshot は記録しない。login_reachability = 遷移到達、selector_exists =
        login フォームの username/password/submit の実在。
        """

        from lib.service.browser_flow_runner import contained_context, sanitize_exception
        from lib.service.browser_login import login_rules

        results: list[tuple[str, str, str]] = []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            results.append(("login_reachability", "SKIP", "playwright 未導入"))
            results.append(("selector_exists", "SKIP", "playwright 未導入"))
            return results

        login = entry.auth.login
        if login is None:
            results.append(("login_reachability", "SKIP", "login 設定なし profile"))
            results.append(("selector_exists", "SKIP", "login 設定なし profile"))
            return results

        origin = entry.account.origin.rstrip("/")
        try:
            with sync_playwright() as pw:
                with contained_context(
                    pw, rules=login_rules(entry), default_timeout_ms=8000
                ) as context:
                    page = context.new_page()
                    resp = page.goto(origin + "/" + login.route.strip("/"))
                    if resp is not None and resp.status < 400:
                        results.append(("login_reachability", "OK", login.route))
                    else:
                        status = resp.status if resp is not None else "none"
                        results.append(
                            ("login_reachability", "NG", f"status={status}")
                        )
                    missing: list[str] = []
                    if page.get_by_label(login.username_label).count() == 0:
                        missing.append("username")
                    if page.get_by_label(login.password_label).count() == 0:
                        missing.append("password")
                    if (
                        page.get_by_role(
                            login.submit_role, name=login.submit_name
                        ).count()
                        == 0
                    ):
                        missing.append("submit")
                    if missing:
                        results.append(
                            ("selector_exists", "NG", ",".join(missing) + " 欠落")
                        )
                    else:
                        results.append(("selector_exists", "OK", "login form"))
        except BaseException as exc:  # noqa: BLE001 - 生例外を通さない
            reason = sanitize_exception(exc)
            results.append(("login_reachability", "NG", reason.value))
            results.append(("selector_exists", "SKIP", "login 未到達"))
        return results

    # -- login（human-assisted の session 捕捉・束縛のみ、抽出・delivery なし）---

    def login(self, tool_id: str):
        """human-assisted profile 用: 人間ログイン → session state を捕捉・束縛する。

        headed で起動するが**抽出・delivery の経路を持たない**（session state の捕捉と
        束縛メタデータ付与のみ）。捕捉直後に有効性を検証し、束縛メタ（tool/origin/
        account）と TTL を人間に表示する。headed 部は実 chromium + 人間操作を要するため
        自動テスト対象外（Non-Goal）。捕捉後の finalize（束縛・0600 保存・有効性検証）は
        browser_login.finalize_capture が担い常時実行テストで検証する。
        """

        loaded = self._load_entry(tool_id)
        if is_err(loaded):
            return loaded
        entry = loaded.value
        if entry.auth.profile != "human-assisted":
            return _err("params_invalid", "login は human-assisted profile 専用")

        captured = self._capture_human_login(entry)
        if is_err(captured):
            return captured
        storage_state = captured.value

        from lib.service.browser_login import finalize_capture

        ttl_hours = entry.auth.session_ttl_hours or entry.retention.ttl_hours
        expires_at = compute_expires_at(
            created_at=self._clock.now(), ttl_hours=ttl_hours
        )
        final = finalize_capture(
            wiki_root=self._wiki_root,
            entry=entry,
            storage_state=storage_state,
            now_iso=self._clock.now(),
            expires_at=expires_at,
        )
        if is_err(final):
            return _err(final.error, getattr(final, "detail", ""))

        self._audit.append(
            AuditEvent(
                event="login", plan_id=None, tool_id=tool_id, subcommand="login"
            )
        )
        return Ok(value={"status": "login_captured", **final.value})

    def _capture_human_login(self, entry):  # pragma: no cover - headed（人間操作）
        """headed ブラウザで人間ログインを待ち、storage_state を捕捉する。

        完了検知: post-login URL / セレクタ検知 + TTY Enter 待ちフォールバック +
        タイムアウト（guide §10）。抽出・delivery の経路は持たない。
        """

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _err(BrowserReason.INTERNAL_ERROR, "playwright 未インストール")

        from lib.service.browser_flow_runner import contained_context
        from lib.service.browser_login import login_rules

        origin = entry.account.origin.rstrip("/")
        start = origin + (
            "/" + entry.auth.login.route.strip("/") if entry.auth.login else "/"
        )
        success = (
            entry.auth.login.success_url_contains if entry.auth.login else None
        )
        try:
            with sync_playwright() as pw:
                with contained_context(
                    pw,
                    rules=login_rules(entry),
                    default_timeout_ms=300_000,
                    headless=False,
                ) as context:
                    page = context.new_page()
                    page.goto(start)
                    print(
                        "ブラウザでログインしてください。完了後にこのターミナルで "
                        "Enter を押してください…",
                        file=sys.stderr,
                    )
                    if success:
                        try:
                            page.wait_for_url("**" + success + "**", timeout=300_000)
                        except BaseException:  # noqa: BLE001 - Enter フォールバック
                            pass
                    try:
                        input()
                    except EOFError:
                        pass
                    return Ok(value=context.storage_state())
        except BaseException as exc:  # noqa: BLE001 - 生例外を通さない
            from lib.service.browser_flow_runner import sanitize_exception

            return _err(sanitize_exception(exc), "")

    def _count_unapproved(self, tool_id: str) -> int:
        plans_root = self._wiki_root / PLANS_RELATIVE
        if not plans_root.is_dir():
            return 0
        count = 0
        for child in plans_root.iterdir():
            if not child.is_dir() or child.name.startswith(".staging-"):
                continue
            if not child.name.endswith(f"-{tool_id}"):
                continue
            state_path = child / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if state.get("status") == "prepared":
                count += 1
        return count

    # -- 監査アンカー照合 ---------------------------------------------------

    def _prepared_anchor(self, plan_id: str) -> tuple[str, str] | None:
        """browser-audit.jsonl から plan の prepared 封印ハッシュ（spool 外アンカー）を引く。"""

        path = self._wiki_root / "outputs" / "browser-audit.jsonl"
        if not path.exists():
            return None
        anchor: tuple[str, str] | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "prepared" and event.get("plan_id") == plan_id:
                art = event.get("artifact_digest")
                man = event.get("manifest_digest")
                if isinstance(art, str) and isinstance(man, str):
                    anchor = (art, man)
        return anchor

    def _rederive_and_verify(self, bundle: Path, plan_id: str) -> Ok[dict] | Err:
        """封印 artifact + manifest からハッシュを再導出し、監査アンカーと fail-closed 照合。

        不一致は表示への反映ではなく**拒否**（seal_mismatch）。manifest の中身は
        artifact から再導出できないため、その完全性はこの監査アンカー照合が担う。
        """

        try:
            artifact_bytes = (bundle / "artifact.bin").read_bytes()
            manifest_bytes = (bundle / "manifest.json").read_bytes()
        except OSError as exc:
            return _err("bundle_missing", str(exc))
        art_now = sha256_hex(artifact_bytes)
        man_now = sha256_hex(manifest_bytes)
        anchor = self._prepared_anchor(plan_id)
        if anchor is None or anchor != (art_now, man_now):
            return _err(BrowserReason.SEAL_MISMATCH, "封印ハッシュが監査アンカーと不一致")
        return Ok(value={"artifact_digest": art_now, "manifest_digest": man_now})

    # -- approve -----------------------------------------------------------

    def approve(self, plan_id_text: str, *, approved_by: str):
        parsed = parse_plan_id(plan_id_text)
        if is_err(parsed):
            return _err(parsed.error, parsed.detail)
        bundle_result = self._approval.resolve_bundle(parsed.value)
        if is_err(bundle_result):
            return bundle_result
        bundle = bundle_result.value

        def validate(b: Path):
            verified = self._rederive_and_verify(b, parsed.value)
            if is_err(verified):
                return verified
            try:
                state = json.loads((b / "state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return _err("bundle_missing", str(exc))
            t = apply_transition(
                BROWSER_TABLE, current=state.get("status", ""), target="approved"
            )
            if is_err(t):
                return _err("not_approved", state.get("status", ""))
            if is_expired(now=self._clock.now(), expires_at=state.get("expires_at", "")):
                return _err("ttl_expired", state.get("expires_at", ""))
            return Ok(value=(state, verified.value))

        def do_approve(ctx):
            state, digests = ctx
            new = dict(state)
            new["status"] = "approved"
            new["approved_by"] = approved_by
            new["approved_at"] = self._clock.now()
            return Ok(value=new)

        def audit_approved():
            return self._audit.append(
                AuditEvent(
                    event="approved",
                    plan_id=parsed.value,
                    tool_id=_tool_id_of(parsed.value),
                    subcommand="approve",
                    digests={
                        "artifact_digest": sha256_hex(
                            (bundle / "artifact.bin").read_bytes()
                        ),
                        "manifest_digest": sha256_hex(
                            (bundle / "manifest.json").read_bytes()
                        ),
                    },
                )
            )

        def write_state(b: Path, new: dict) -> None:
            write_json_durable(b / "state.json", new)

        return self._approval.approve_cas(
            bundle=bundle,
            validate=validate,
            do_approve=do_approve,
            audit_approved=audit_approved,
            write_state=write_state,
        )

    # -- execute（delivery 解放のみ）---------------------------------------

    def execute(self, plan_id_text: str):
        parsed = parse_plan_id(plan_id_text)
        if is_err(parsed):
            return _err(parsed.error, parsed.detail)
        bundle_result = self._approval.resolve_bundle(parsed.value)
        if is_err(bundle_result):
            return bundle_result
        bundle = bundle_result.value
        tool_id = _tool_id_of(parsed.value)

        def _audit(event: str, *, reason: str | None = None, **kw):
            return self._audit.append(
                AuditEvent(
                    event=event,
                    plan_id=parsed.value,
                    tool_id=tool_id,
                    subcommand="execute",
                    reason=reason,
                    **kw,
                )
            )

        def validate(b: Path):
            try:
                state = json.loads((b / "state.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return _err("bundle_missing", str(exc))
            t = apply_transition(
                BROWSER_TABLE, current=state.get("status", ""), target="delivering"
            )
            if is_err(t):
                reason = (
                    "already_consumed"
                    if state.get("status") in ("delivering", "delivered", "failed")
                    else "not_approved"
                )
                _audit("rejected", reason=reason)
                return _err(reason, state.get("status", ""))
            # defense in depth: delivery 解放前にも封印ハッシュを再照合
            verified = self._rederive_and_verify(b, parsed.value)
            if is_err(verified):
                _audit("rejected", reason=verified.error)
                return verified
            if is_expired(now=self._clock.now(), expires_at=state.get("expires_at", "")):
                _audit("rejected", reason="ttl_expired")
                return _err("ttl_expired", "")
            return Ok(value=state)

        def audit_attempted():
            return _audit("execute_attempted")

        def make_run_id() -> str:
            return build_plan_id(
                now_iso=self._clock.now(), nonce=self._nonce(), tool_id=tool_id
            )

        def do_consume(state: dict, run_id: str):
            new = dict(state)
            new["status"] = "delivering"
            new["run_id"] = run_id
            return Ok(value=new)

        def write_state(b: Path, new: dict) -> None:
            write_json_durable(b / "state.json", new)

        outcome = self._approval.consume_cas(
            bundle=bundle,
            validate=validate,
            audit_attempted=audit_attempted,
            make_run_id=make_run_id,
            do_consume=do_consume,
            write_state=write_state,
        )
        if is_err(outcome):
            return outcome
        state = outcome.value.context
        run_id = outcome.value.run_id

        # 封印済み成果物を delivery へ解放（ブラウザ再実行なし）
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        tool_id = manifest["tool_id"]
        loaded = self._load_entry(tool_id)
        if is_err(loaded):
            return loaded
        entry = loaded.value
        deliver_to = entry.delivery_allowed_dirs[0]
        base_result = resolve_declared_dir(
            wiki_root=self._wiki_root, declared=deliver_to
        )
        if is_err(base_result):
            return _err("delivery_not_allowed", base_result.detail)
        delivery_base = base_result.value

        staging_result = create_staging_dir(delivery_dir=delivery_base, run_id=run_id)
        if is_err(staging_result):
            return _err("plan_conflict", staging_result.detail)
        staging = staging_result.value
        published = False
        try:
            (staging / "result.csv").write_bytes(
                (bundle / "artifact.bin").read_bytes()
            )
            (staging / "manifest.json").write_bytes(
                (bundle / "manifest.json").read_bytes()
            )
            pub = publish_run_dir(
                staging_dir=staging,
                delivery_dir=delivery_base,
                run_id=run_id,
                lock=self._lock,
                lock_timeout=10.0,
            )
            if is_err(pub):
                return _err(pub.error, pub.detail)
            published = True
            new_state = dict(state)
            new_state["status"] = "delivered"
            new_state["run_id"] = run_id
            write_json_durable(bundle / "state.json", new_state)
            _audit("delivered", row_count=manifest["row_count"], delivery_dir=deliver_to)
            return Ok(
                value=ExecuteOutcome(
                    run_id=run_id,
                    row_count=manifest["row_count"],
                    published_path=pub.value,
                )
            )
        finally:
            if not published:
                cleanup_staging(staging)


def _tool_id_of(plan_id: str) -> str:
    # plan_id = {YYYYMMDDHHMMSS}-{nonce}-{tool_id}
    return plan_id.split("-", 2)[2]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_error(reason: str) -> None:
    hint = reason_hint(reason)
    print(f"error: {reason}", file=sys.stderr)
    print(
        f"  what: {hint['what']}\n  why : {hint['why']}\n  next: {hint['next']}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="browser_extract_run")
    parser.add_argument("--wiki-root", required=True)
    parser.add_argument("--format", choices=["table", "json"], default="table")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--tool", required=True)
    p_prepare.add_argument("--param", action="append", default=[])
    p_prepare.add_argument("--deliver-to", required=True)

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--plan-id", required=True)
    p_approve.add_argument("--approved-by", required=True)

    p_execute = sub.add_parser("execute")
    p_execute.add_argument("--plan-id", required=True)

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--tool", required=True)

    p_login = sub.add_parser("login")
    p_login.add_argument("--tool", required=True)

    sub.add_parser("catalog-validate")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    wiki_root = Path(args.wiki_root)

    if args.command == "catalog-validate":
        try:
            data = json.loads(
                (wiki_root / BROWSER_CATALOG_RELATIVE).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        result = parse_browser_catalog(data)
        if is_err(result):
            print(f"invalid: {result.detail}", file=sys.stderr)
            return 1
        print("ok")
        return 0

    runner = BrowserRunner(
        wiki_root=wiki_root,
        clock=SystemClock(),
        lock=RealFileLock(),
        extractor=_real_extractor(wiki_root),
        nonce=lambda: os.urandom(2).hex(),
        login_fn=_real_login_fn(wiki_root),
    )

    try:
        if args.command == "prepare":
            params = dict(kv.split("=", 1) for kv in args.param)
            result = runner.prepare(
                tool_id=args.tool, params=params, deliver_to=args.deliver_to
            )
        elif args.command == "approve":
            result = runner.approve(args.plan_id, approved_by=args.approved_by)
        elif args.command == "execute":
            result = runner.execute(args.plan_id)
        elif args.command == "doctor":
            result = runner.doctor(args.tool)
        elif args.command == "login":  # pragma: no cover - smoke（headed）
            result = runner.login(args.tool)
        else:  # pragma: no cover
            return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130

    if is_err(result):
        _print_error(str(result.error))
        return 1
    if args.command == "doctor":
        _print_doctor(result.value, fmt=args.format)
        return 0 if all(s != "NG" for _, s, _ in result.value) else 1
    if args.format == "json":
        print(json.dumps(_outcome_json(result.value), ensure_ascii=False))
    else:
        print(_outcome_table(result.value))
    return 0


def _print_doctor(checks, *, fmt: str) -> None:
    if fmt == "json":
        print(
            json.dumps(
                [{"check": c, "status": s, "detail": d} for c, s, d in checks],
                ensure_ascii=False,
            )
        )
        return
    for name, status, detail in checks:
        print(f"{status:5}  {name:22}  {detail}")


def _real_extractor(wiki_root: Path):  # pragma: no cover - smoke
    from lib.service.browser_flow_runner import BrowserFlowRunner

    return BrowserFlowRunner(wiki_root=wiki_root, monotonic=time.monotonic)


def _real_login_fn(wiki_root: Path):  # pragma: no cover - smoke（実 chromium）
    """form / form+totp の自動ログイン: credential 解決 + headless form login。"""

    def login_fn(entry):
        from lib.service.browser_login import form_login
        from lib.service.tool_catalog import load_credential

        password = load_credential(
            wiki_root=wiki_root, ref=entry.auth.credential_ref
        )
        if is_err(password):
            return _err(BrowserReason.SESSION_EXPIRED, "credential 解決に失敗")
        totp_secret = None
        if entry.auth.profile == "form+totp":
            secret = load_credential(
                wiki_root=wiki_root, ref=entry.auth.totp_credential_ref
            )
            if is_err(secret):
                return _err(BrowserReason.SESSION_EXPIRED, "TOTP secret 解決に失敗")
            totp_secret = secret.value
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return form_login(
                pw,
                entry=entry,
                username=entry.auth.username,
                password=password.value,
                totp_secret=totp_secret,
            )

    return login_fn


def _outcome_json(outcome) -> dict:
    if isinstance(outcome, PrepareOutcome):
        return {
            "plan_id": outcome.plan_id,
            "row_count": outcome.row_count,
            "artifact_digest": outcome.artifact_digest,
            "manifest_digest": outcome.manifest_digest,
            "expires_at": outcome.expires_at,
        }
    if isinstance(outcome, ExecuteOutcome):
        return {
            "run_id": outcome.run_id,
            "row_count": outcome.row_count,
            "published_path": str(outcome.published_path),
        }
    return {"status": "approved"}


def _outcome_table(outcome) -> str:  # pragma: no cover - 表示整形
    return json.dumps(_outcome_json(outcome), ensure_ascii=False, indent=2)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

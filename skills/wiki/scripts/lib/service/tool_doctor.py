"""doctor — 実 DB / API への接続疎通・read-only 状態・delivery 書込可否の診断.

doctor は「実データに触れない診断」であり **COUNT すら実行しない**。read-only の
検証は単一の write 試行ではなく独立 check に分解する（同一接続では session
read-only と role 拒否を区別できないため、計画「doctor サブコマンド」節）:

* ``session_readonly`` — **実クエリと同じ transaction 内**の read-only 状態を
  introspection で確認（pg: ``SELECT current_setting('transaction_read_only')``
  — ``SHOW`` は named cursor で実行できないため current_setting を使う /
  mysql: ``SELECT @@session.transaction_read_only``）
* ``role_grants`` — role / grant の introspection（pg: allowlist relation 全件で
  INSERT/UPDATE/DELETE/TRUNCATE が false / mysql: ``SHOW GRANTS`` を parse し
  SELECT 以外の権限が無いこと）
* ``role_write_denial`` — 通常実行では機械検証しない（既定 SKIP）
* ``role_uninspected_privileges`` — CREATE / TEMPORARY / EXECUTE 等は機械検証
  対象外として SKIP を明示（OK に含めない）

出力契約: 固定列 ``tool / check / status(OK|NG|SKIP) / reason_code / hint``。
NG には復旧ヒント 1 行、SKIP は理由付き。summary に SKIP 件数を必ず含める。
監査は **plan 非依存の診断イベント**（``doctor``、plan_id なし）として記録する。

**write probe の限定**（``--probe-write``）: 本 connector は read-only session
専用のため、canary への INSERT は session read-only + role の**重畳**で拒否
される。probe は「書込が拒否されること」を確認するが、role 単独の書込拒否を
session から分離しては検証しない（その旨を hint に明記する。role_grants の
introspection が role 側の第一情報源）。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from lib.domain.types import Err, Ok, is_err
from lib.service.clock import Clock
from lib.service.file_lock import FileLock
from lib.service.tool_audit import AuditEvent, AuditLog
from lib.service.tool_catalog import (
    ToolEntry,
    load_catalog,
    load_credential,
    resolve_entry,
)
from lib.service.tool_connector import ConnectorStreamError, ToolConnectorError
from lib.service.tool_connector_registry import ConnectorRegistry, default_registry
from lib.service.tool_paths import resolve_declared_dir
from lib.service.tool_sql_gate import canonical_allowlist


class CheckStatus(str, Enum):
    OK = "ok"
    NG = "ng"
    SKIP = "skip"


@dataclass(frozen=True)
class DoctorCheck:
    """check の宣言的定義（名前・適用 type・必須/任意）。

    **実行は grouped**（session_readonly と role_grants は同一接続・同一
    transaction で走らせる必要があるため、check ごとに接続を開く純粋 registry
    にはしない）。この registry は「どの check がどの type に適用され、
    必須か」の真実源であり、出力の網羅性検証（test で全 emit が登録済みか、
    必須 check が揃うかを機械検証）に使う。
    """

    name: str
    applies: tuple[str, ...]  # connector type
    required: bool


# check メタデータの真実源。tool_doctor が emit する check は必ずここに登録し、
# test_tool_doctor.py::TestCheckRegistry が emit との同期を機械検証する。
_SQL_TYPES = ("postgres", "mysql")
CHECK_REGISTRY: tuple[DoctorCheck, ...] = (
    DoctorCheck("credential_resolves", ("postgres", "mysql", "http"), required=True),
    DoctorCheck("connectivity", ("sqlite", "postgres", "mysql"), required=True),
    DoctorCheck("tls", _SQL_TYPES, required=True),
    DoctorCheck("session_readonly", _SQL_TYPES, required=True),
    DoctorCheck("role_grants", _SQL_TYPES, required=True),
    DoctorCheck("role_write_denial", _SQL_TYPES, required=False),
    DoctorCheck("role_uninspected_privileges", _SQL_TYPES, required=False),
    DoctorCheck("http_allowlist", ("http",), required=True),
    DoctorCheck("delivery_writable", ("sqlite", "postgres", "mysql", "http"), required=True),
    DoctorCheck("write_probe", _SQL_TYPES, required=False),  # --probe-write 時のみ
    DoctorCheck("audit", ("sqlite", "postgres", "mysql", "http"), required=True),
)
CHECK_NAMES = frozenset(c.name for c in CHECK_REGISTRY)


@dataclass(frozen=True)
class CheckOutcome:
    check: str
    status: CheckStatus
    reason_code: str = ""
    hint: str = ""


@dataclass(frozen=True)
class ToolDiagnosis:
    tool_id: str
    type: str
    outcomes: tuple[CheckOutcome, ...]


@dataclass(frozen=True)
class DoctorReport:
    diagnoses: tuple[ToolDiagnosis, ...]

    def has_ng(self) -> bool:
        return any(
            o.status == CheckStatus.NG
            for d in self.diagnoses
            for o in d.outcomes
        )

    def skip_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.diagnoses:
            for o in d.outcomes:
                if o.status == CheckStatus.SKIP:
                    counts[o.reason_code] = counts.get(o.reason_code, 0) + 1
        return counts


class DoctorError(str, Enum):
    UNKNOWN_TOOL = "unknown_tool"
    CATALOG_INVALID = "catalog_invalid"
    INVALID_PROBE = "invalid_probe_target"


def _val(reason: object) -> str:
    return reason.value if isinstance(reason, Enum) else str(reason)


def _ok(check: str) -> CheckOutcome:
    return CheckOutcome(check=check, status=CheckStatus.OK)


def _ng(check: str, reason_code: str, hint: str) -> CheckOutcome:
    return CheckOutcome(check=check, status=CheckStatus.NG, reason_code=reason_code, hint=hint)


def _skip(check: str, reason_code: str, hint: str = "") -> CheckOutcome:
    return CheckOutcome(check=check, status=CheckStatus.SKIP, reason_code=reason_code, hint=hint)


class Doctor:
    def __init__(
        self,
        *,
        wiki_root: Path,
        clock: Clock,
        lock: FileLock,
        registry: ConnectorRegistry | None = None,
        monotonic: Callable[[], float] | None = None,
        lock_timeout: float = 10.0,
    ) -> None:
        self._wiki_root = Path(wiki_root)
        self._clock = clock
        self._lock = lock
        self._registry = registry or default_registry()
        import time

        self._monotonic = monotonic or time.monotonic
        self._audit = AuditLog(
            wiki_root=self._wiki_root, lock=lock, clock=clock, lock_timeout=lock_timeout
        )

    # -- 公開 API -----------------------------------------------------------

    def run_checked(
        self,
        *,
        tool: str | None = None,
        probe_write: str | None = None,
        announce: Callable[[str], None] = lambda _m: None,
    ) -> Ok[DoctorReport] | Err[DoctorError]:
        catalog_result = load_catalog(wiki_root=self._wiki_root)
        if is_err(catalog_result):
            return Err(
                error=DoctorError.CATALOG_INVALID, detail=catalog_result.detail
            )
        entries = catalog_result.value.entries
        if tool is not None:
            resolved = resolve_entry(catalog_result.value, tool)
            if is_err(resolved):
                return Err(error=DoctorError.UNKNOWN_TOOL, detail=tool)
            entries = (resolved.value,)

        # --probe-write の対象検証（usage エラーは実行前に弾く）
        if probe_write is not None:
            target = resolve_entry(catalog_result.value, probe_write)
            if is_err(target):
                return Err(
                    error=DoctorError.UNKNOWN_TOOL,
                    detail=f"--probe-write の対象が未知の tool: {probe_write}",
                )
            if target.value.type not in ("postgres", "mysql"):
                return Err(
                    error=DoctorError.INVALID_PROBE,
                    detail=f"--probe-write は postgres/mysql のみ対象: {probe_write}",
                )
            if tool is not None and tool != probe_write:
                return Err(
                    error=DoctorError.INVALID_PROBE,
                    detail="--probe-write の対象が --tool と一致しません",
                )

        diagnoses: list[ToolDiagnosis] = []
        for entry in entries:
            outcomes = self._diagnose(entry, probe_write=probe_write, announce=announce)
            # plan 非依存の診断イベント。監査が書けなければ NG として計上する
            # （必須 check の一部 — 監査不能を無言で流さない）
            audit_result = self._audit.append(
                AuditEvent(
                    event="doctor",
                    plan_id=None,
                    tool_id=entry.tool_id,
                    subcommand="doctor",
                )
            )
            if is_err(audit_result):
                outcomes.append(
                    _ng(
                        "audit",
                        "audit_write_failed",
                        "監査ログを書けません（ディスク・権限・lock を確認）",
                    )
                )
            else:
                outcomes.append(_ok("audit"))
            diagnoses.append(
                ToolDiagnosis(
                    tool_id=entry.tool_id, type=entry.type, outcomes=tuple(outcomes)
                )
            )
        return Ok(value=DoctorReport(diagnoses=tuple(diagnoses)))

    def run(
        self,
        *,
        tool: str | None = None,
        probe_write: str | None = None,
        announce: Callable[[str], None] = lambda _m: None,
    ) -> DoctorReport:
        result = self.run_checked(tool=tool, probe_write=probe_write, announce=announce)
        if is_err(result):
            raise ValueError(f"doctor: {result.error.value}: {result.detail}")
        return result.value

    # -- 診断本体 -----------------------------------------------------------

    def _diagnose(
        self, entry: ToolEntry, *, probe_write: str | None, announce
    ) -> list[CheckOutcome]:
        if entry.type == "http":
            return self._diagnose_http(entry)
        if entry.type == "sqlite":
            return self._diagnose_sqlite(entry)
        return self._diagnose_remote_db(entry, probe_write=probe_write, announce=announce)

    def _deadline(self, entry: ToolEntry) -> float:
        return self._monotonic() + entry.limits.timeout_sec

    def _check_credential(self, entry: ToolEntry) -> CheckOutcome | None:
        if entry.credential_ref is None:
            return None
        cred = load_credential(wiki_root=self._wiki_root, ref=entry.credential_ref)
        if is_err(cred):
            return _ng(
                "credential_resolves",
                cred.error.value if hasattr(cred.error, "value") else str(cred.error),
                "credentials.json の存在・権限(0600)・credential_ref キーを確認",
            )
        return _ok("credential_resolves")

    def _check_delivery(self, entry: ToolEntry) -> CheckOutcome:
        for declared in entry.delivery_allowed_dirs:
            resolved = resolve_declared_dir(
                wiki_root=self._wiki_root, declared=declared
            )
            if is_err(resolved):
                return _ng(
                    "delivery_writable",
                    "delivery_unresolved",
                    f"delivery 先を解決できません: {declared}",
                )
            probe = resolved.value / f".doctor-probe-{secrets.token_hex(4)}"
            try:
                fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                os.unlink(probe)
            except OSError:
                return _ng(
                    "delivery_writable",
                    "delivery_not_writable",
                    f"delivery 先が存在しないか書込不可: {declared}",
                )
        return _ok("delivery_writable")

    # -- sqlite -------------------------------------------------------------

    def _diagnose_sqlite(self, entry: ToolEntry) -> list[CheckOutcome]:
        outcomes: list[CheckOutcome] = []
        cred = self._check_credential(entry)
        if cred is not None:
            outcomes.append(cred)
        provider = self._registry.resolve(entry.type).value
        opened = provider.open(
            entry=entry,
            wiki_root=self._wiki_root,
            deadline_monotonic=self._deadline(entry),
            monotonic=self._monotonic,
        )
        if is_err(opened):
            outcomes.append(
                _ng("connectivity", opened.error, "DB ファイルの存在・パスを確認")
            )
        else:
            opened.value.close()
            outcomes.append(_ok("connectivity"))
        outcomes.append(self._check_delivery(entry))
        return outcomes

    # -- http ---------------------------------------------------------------

    def _diagnose_http(self, entry: ToolEntry) -> list[CheckOutcome]:
        outcomes: list[CheckOutcome] = []
        cred = self._check_credential(entry)
        if cred is not None:
            outcomes.append(cred)

        # allowlist dry-run: 明らかに allowlist 外の endpoint が precheck で
        # 拒否されることを確認する（実送信しない）
        provider = self._registry.resolve(entry.type).value
        import json as _json

        denied_spec = _json.dumps(
            {
                "method": "GET",
                "path": "/__doctor_denied_probe__",
                "records_path": "rows",
                "columns": ["x"],
            }
        )
        result = provider.precheck(entry, denied_spec)
        if is_err(result) and result.error == "http_endpoint_not_allowed":
            outcomes.append(_ok("http_allowlist"))
        else:
            outcomes.append(
                _ng(
                    "http_allowlist",
                    "http_allowlist_not_enforced",
                    "allowed_endpoints の宣言を確認（allowlist 外が拒否されない）",
                )
            )
        outcomes.append(self._check_delivery(entry))
        return outcomes

    # -- postgres / mysql ---------------------------------------------------

    def _diagnose_remote_db(
        self, entry: ToolEntry, *, probe_write: str | None, announce
    ) -> list[CheckOutcome]:
        outcomes: list[CheckOutcome] = []
        conn = entry.connection
        insecure = getattr(conn, "allow_insecure_tls", False)

        announce(f"{entry.tool_id}/credential_resolves を検査中")
        cred = self._check_credential(entry)
        if cred is not None:
            outcomes.append(cred)

        announce(f"{entry.tool_id}/connectivity を検査中")
        provider = self._registry.resolve(entry.type).value
        opened = provider.open(
            entry=entry,
            wiki_root=self._wiki_root,
            deadline_monotonic=self._deadline(entry),
            monotonic=self._monotonic,
        )
        if is_err(opened):
            outcomes.append(
                _ng(
                    "connectivity",
                    opened.error,
                    "host/port/credential/TLS 設定と DB 側の到達性を確認",
                )
            )
            # TLS ネゴシエーション成立は接続成功が前提。接続不能では判定不能
            outcomes.append(
                _skip("tls", "connect_failed", "接続不能のため TLS 成立を未確認")
            )
            # 接続できない場合、以降の introspection は判定できない → SKIP
            for check in ("session_readonly", "role_grants"):
                outcomes.append(
                    _skip(check, "connect_failed", "接続不能のため未検査")
                )
            outcomes.append(self._skip_role_write_denial())
            outcomes.append(self._skip_role_uninspected())
            outcomes.append(self._check_delivery(entry))
            return outcomes

        connector = opened.value
        outcomes.append(_ok("connectivity"))
        # 接続成立 = verify-full / CA+hostname 検証つきの TLS ネゴシエーションが
        # 成立している（緩和時は skip）。allow_insecure_tls は OK にしない
        if insecure:
            outcomes.append(
                _skip(
                    "tls",
                    "tls_relaxed",
                    "allow_insecure_tls が宣言されています（TLS 検証は緩和）",
                )
            )
        else:
            outcomes.append(
                CheckOutcome(
                    check="tls",
                    status=CheckStatus.OK,
                    reason_code="verified_on_connect",
                    hint="",
                )
            )
        try:
            announce(f"{entry.tool_id}/session_readonly を検査中")
            outcomes.append(self._check_session_readonly(entry, connector))
            announce(f"{entry.tool_id}/role_grants を検査中")
            outcomes.append(self._check_role_grants(entry, connector))
        finally:
            connector.close()

        outcomes.append(self._skip_role_write_denial())
        outcomes.append(self._skip_role_uninspected())
        outcomes.append(self._check_delivery(entry))

        if probe_write == entry.tool_id:
            outcomes.append(self._write_probe(entry, announce))
        return outcomes

    def _skip_role_write_denial(self) -> CheckOutcome:
        return _skip(
            "role_write_denial",
            "not_mechanically_verified",
            "role write denial は通常実行では機械検証しない（role_grants 参照）",
        )

    def _skip_role_uninspected(self) -> CheckOutcome:
        return _skip(
            "role_uninspected_privileges",
            "out_of_scope",
            "CREATE への権限・database TEMPORARY・危険 function の EXECUTE は機械検証対象外",
        )

    def _introspect_row(self, connector, sql: str) -> Ok[tuple] | Err[str]:
        result = connector.execute_stream(sql)
        if is_err(result):
            return Err(error=str(result.error), detail=result.detail)
        try:
            with result.value as stream:
                rows = list(stream)
        except ConnectorStreamError as exc:
            return Err(error=exc.reason.value, detail=exc.detail)
        if len(rows) != 1:
            return Err(error="unexpected_rows", detail=f"{len(rows)} 行")
        return Ok(value=rows[0])

    def _check_session_readonly(self, entry: ToolEntry, connector) -> CheckOutcome:
        if entry.type == "postgres":
            sql = "SELECT current_setting('transaction_read_only')"
            expected = "on"
        else:
            sql = "SELECT @@session.transaction_read_only"
            expected = 1
        row = self._introspect_row(connector, sql)
        if is_err(row):
            return _ng(
                "session_readonly",
                "introspection_failed",
                "read-only 状態を取得できません（接続・権限を確認）",
            )
        value = row.value[0]
        # pg は 'on'/'off'、mysql は 1/0
        ok = (value == expected) or (
            entry.type == "postgres" and str(value).lower() == "on"
        ) or (entry.type == "mysql" and value in (1, "1"))
        if ok:
            return _ok("session_readonly")
        return _ng(
            "session_readonly",
            "session_not_readonly",
            "接続 transaction が read-only ではありません（connector の SET 順序を確認）",
        )

    def _check_role_grants(self, entry: ToolEntry, connector) -> CheckOutcome:
        if entry.type == "postgres":
            return self._check_pg_grants(entry, connector)
        return self._check_mysql_grants(entry, connector)

    def _check_pg_grants(self, entry: ToolEntry, connector) -> CheckOutcome:
        relations = sorted(
            canonical_allowlist(
                entry.allowed_tables,
                dialect="postgres",
                default_namespace=entry.connection.default_schema,
            )
        )
        for rel in relations:
            sql = (
                f"SELECT has_table_privilege(current_user, '{rel}', 'INSERT'),"
                f" has_table_privilege(current_user, '{rel}', 'UPDATE'),"
                f" has_table_privilege(current_user, '{rel}', 'DELETE'),"
                f" has_table_privilege(current_user, '{rel}', 'TRUNCATE')"
            )
            row = self._introspect_row(connector, sql)
            if is_err(row):
                return _ng(
                    "role_grants",
                    "introspection_failed",
                    "権限を取得できません（has_table_privilege の実行可否を確認）",
                )
            if any(bool(v) for v in row.value):
                return _ng(
                    "role_grants",
                    "write_privilege_present",
                    f"role が {rel} に書込権限を持ちます（SELECT のみの専用 role にする）",
                )
        return _ok("role_grants")

    def _check_mysql_grants(self, entry: ToolEntry, connector) -> CheckOutcome:
        result = connector.execute_stream("SHOW GRANTS FOR CURRENT_USER()")
        if is_err(result):
            return _ng(
                "role_grants",
                "introspection_failed",
                "SHOW GRANTS を取得できません",
            )
        try:
            with result.value as stream:
                rows = list(stream)
        except ConnectorStreamError:
            return _ng("role_grants", "introspection_failed", "SHOW GRANTS の取得に失敗")

        allowed_privs = {"SELECT", "USAGE"}
        for row in rows:
            grant = str(row[0]).strip()
            if not grant:
                continue
            parsed = _parse_mysql_grant(grant)
            if parsed == "ROLE":
                # role 付与は SHOW GRANTS ... USING での展開が要る。ここでは
                # 実効権限を確定できないため fail-open せず incomplete とする
                return _skip(
                    "role_grants",
                    "role_grants_incomplete",
                    "role 付与を検出（実効権限は SHOW GRANTS ... USING で確認が必要）",
                )
            if parsed is None:
                # 解析不能な非空 grant 行を黙って無視しない（将来の構文差で
                # fail-open にならないよう incomplete として明示する）
                return _skip(
                    "role_grants",
                    "role_grants_incomplete",
                    f"解析できない GRANT 行があります: {grant[:40]}",
                )
            if not parsed <= allowed_privs:
                return _ng(
                    "role_grants",
                    "write_privilege_present",
                    "role が SELECT 以外の権限を持ちます（SELECT のみの専用 user にする）",
                )
        return _ok("role_grants")

    def _write_probe(self, entry: ToolEntry, announce) -> CheckOutcome:
        canary = getattr(entry.connection, "canary_relation", None)
        if not canary:
            return _ng(
                "write_probe",
                "canary_not_declared",
                "connection.canary_relation を宣言してください（未宣言では probe しない）",
            )
        sql = f"INSERT INTO {canary} (doctor_probe) VALUES (1)"
        announce(f"[probe-write] {entry.tool_id}: {sql}（拒否されることを期待）")
        provider = self._registry.resolve(entry.type).value
        opened = provider.open(
            entry=entry,
            wiki_root=self._wiki_root,
            deadline_monotonic=self._deadline(entry),
            monotonic=self._monotonic,
        )
        if is_err(opened):
            return _ng(
                "write_probe", "connect_failed", "probe 用接続に失敗しました"
            )
        connector = opened.value

        def _denied() -> CheckOutcome:
            return CheckOutcome(
                check="write_probe",
                status=CheckStatus.OK,
                reason_code="write_denied",
                hint="canary への書込が拒否されました（session read-only + role の重畳防御）",
            )

        def _inconclusive(reason: object) -> CheckOutcome:
            # 「あらゆる失敗」を拒否成功と誤認しない。接続断・timeout・構文/
            # relation エラー等は書込拒否の確認になっていないため NG とする
            return _ng(
                "write_probe",
                "probe_inconclusive",
                f"probe が書込拒否を確認できませんでした（{_val(reason)}）",
            )

        try:
            result = connector.execute_stream(sql)
            if is_err(result):
                if result.error == ToolConnectorError.NOT_AUTHORIZED:
                    return _denied()
                return _inconclusive(result.error)
            try:
                with result.value as stream:
                    list(stream)
            except ConnectorStreamError as exc:
                if exc.reason == ToolConnectorError.NOT_AUTHORIZED:
                    return _denied()
                return _inconclusive(exc.reason)
            # 拒否されず成功してしまった → role が書込可能（rollback で取消）
            return _ng(
                "write_probe",
                "write_succeeded",
                "canary への INSERT が成功しました（role が書込可能 — 権限を剥奪してください）",
            )
        finally:
            connector.close()  # rollback（commit しない）


def _parse_mysql_grant(grant: str) -> set[str] | str | None:
    """``SHOW GRANTS`` の 1 行を解釈する。

    * ``GRANT <privs> ON <obj> TO ...`` → privilege 集合（ALL PRIVILEGES は
      allowed に含まれない要素として扱い NG にする）
    * ``GRANT `role`@`host` TO ...``（ON なし）→ ``"ROLE"``（role 付与。
      実効権限は展開が必要で、ここでは確定できない）
    * それ以外の解析不能行 → None（呼び出し側で incomplete 扱い）
    """

    text = grant.strip()
    if not text.upper().startswith("GRANT "):
        return None
    after = text[len("GRANT "):]
    on_idx = after.upper().find(" ON ")
    if on_idx < 0:
        # ON を持たない GRANT は role 付与（MySQL 8 roles）
        return "ROLE"
    priv_part = after[:on_idx].strip()
    if priv_part.upper().startswith("ALL PRIVILEGES") or priv_part.upper() == "ALL":
        return {"ALL PRIVILEGES"}
    privs: set[str] = set()
    for token in priv_part.split(","):
        name = token.strip().upper()
        # "SELECT (col1, col2)" のような列指定は権限名だけ取る
        paren = name.find("(")
        if paren >= 0:
            name = name[:paren].strip()
        if name:
            privs.add(name)
    return privs if privs else None

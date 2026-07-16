"""Tests for lib/domain/tool_query.py — proposal bundle と承認状態機械の純粋ロジック.

digest binding は bytes digest（意味的正規化をしない）。状態機械は
draft → approved → consumed（terminal）で、consumed の意味は
「authorization spent」であり実行成功ではない。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from lib.domain.tool_query import (
    DEFAULT_TTL_HOURS,
    MAX_APPROVED_BY_LEN,
    MAX_LABEL_LEN,
    ApprovalAttestation,
    ExecutionReceipt,
    FunnelStep,
    PlanState,
    Proposal,
    RejectReason,
    approve_transition,
    build_plan_id,
    check_rows_in_range,
    compact_timestamp,
    compute_expires_at,
    consume_transition,
    display_digest,
    evaluate_execute_matrix,
    is_expired,
    parse_plan_id,
    parse_rows_range,
    precheck_sql,
    proposal_from_json_dict,
    proposal_to_json_bytes,
    proposal_to_json_dict,
    sha256_hex,
    state_from_json_dict,
    state_to_json_dict,
    validate_approved_by,
    validate_count_labels,
)
from lib.domain.types import is_err, is_ok


NOW = "2026-07-16T12:00:00Z"
LATER = "2026-07-16T13:00:00Z"
EXPIRES = "2026-07-17T12:00:00Z"


def make_proposal(**overrides) -> Proposal:
    base = dict(
        plan_id="20260716120000-ab12-events-db",
        tool_id="events-db",
        catalog_digest="c" * 64,
        delivery_dir="outputs/deliveries",
        key_columns=("user_id",),
        expected_rows_min=10,
        expected_rows_max=100,
        funnel=(
            FunnelStep(label="全登録者", count_sql_digest="a" * 64, row_count=500),
            FunnelStep(label="返金なし", count_sql_digest="b" * 64, row_count=120),
        ),
        sql_digest="d" * 64,
        sql_display_digest="e" * 64,
        created_at=NOW,
        expires_at=EXPIRES,
    )
    base.update(overrides)
    return Proposal(**base)


def draft_state() -> PlanState:
    return PlanState(status="draft")


def approved_state(proposal_digest: str) -> PlanState:
    return PlanState(
        status="approved",
        approved_by="mizumi",
        approved_at=LATER,
        proposal_digest=proposal_digest,
    )


def consumed_state(proposal_digest: str) -> PlanState:
    return PlanState(
        status="consumed",
        approved_by="mizumi",
        approved_at=LATER,
        proposal_digest=proposal_digest,
        consumed_at="2026-07-16T14:00:00Z",
        run_id="20260716140000-cd34-events-db",
    )


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


class TestDigests:
    def test_sha256_hex_matches_hashlib(self) -> None:
        data = "SELECT 1;\n".encode("utf-8")
        assert sha256_hex(data) == hashlib.sha256(data).hexdigest()

    def test_sha256_hex_is_stable_for_same_bytes(self) -> None:
        assert sha256_hex(b"abc") == sha256_hex(b"abc")
        assert sha256_hex(b"abc") != sha256_hex(b"abd")

    def test_display_digest_normalizes_trailing_whitespace_and_newlines(self) -> None:
        a = display_digest("SELECT 1\n")
        assert a == display_digest("  SELECT 1  ")
        assert a == display_digest("SELECT 1\r\n")
        assert a == display_digest("SELECT 1\r")

    def test_display_digest_normalizes_inner_crlf(self) -> None:
        assert display_digest("SELECT 1\r\nFROM t") == display_digest(
            "SELECT 1\nFROM t"
        )

    def test_display_digest_keeps_semantic_differences(self) -> None:
        assert display_digest("SELECT 1") != display_digest("SELECT 2")

    def test_display_digest_does_not_collapse_inner_spaces(self) -> None:
        """保守的正規化のみ（trim + 改行統一）— 内部空白は意味を持ち得る。"""
        assert display_digest("SELECT  1") != display_digest("SELECT 1")


# ---------------------------------------------------------------------------
# SQL 事前チェック
# ---------------------------------------------------------------------------


class TestPrecheckSql:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select * from users",
            "  \n\t SELECT 1",
            "WITH t AS (SELECT 1) SELECT * FROM t",
            "with t as (select 1) select * from t",
        ],
    )
    def test_select_and_with_pass(self, sql: str) -> None:
        assert is_ok(precheck_sql(sql))

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE users SET x = 1",
            "DELETE FROM users",
            "INSERT INTO users VALUES (1)",
            "DROP TABLE users",
            "CREATE TABLE t (x)",
            "ALTER TABLE users ADD COLUMN x",
            "PRAGMA table_info(users)",
            "ATTACH DATABASE 'x' AS y",
            "-- comment first\nSELECT 1",
            "/* comment */ SELECT 1",
            "SELECTX",
            "",
            "   ",
        ],
    )
    def test_non_select_or_comment_start_is_rejected(self, sql: str) -> None:
        result = precheck_sql(sql)
        assert is_err(result)
        assert result.error == RejectReason.SQL_PRECHECK_FAILED


# ---------------------------------------------------------------------------
# plan_id
# ---------------------------------------------------------------------------


class TestPlanId:
    def test_slug_fragment_matches_path_validator_id_space(self) -> None:
        """domain の SLUG_FRAGMENT が service の ID_PATTERN と同じ slug 空間である
        ことを固定する（domain は service を import できないための二重定義）。"""
        import re

        from lib.domain.tool_query import SLUG_FRAGMENT
        from lib.service.path_validator import ID_PATTERN

        fragment_re = re.compile(rf"^{SLUG_FRAGMENT}$")
        id_re = re.compile(ID_PATTERN)
        samples = [
            "a", "ab", "events-db", "a-b_c", "a" * 128, "a" * 129,
            "-a", "a-", "_a", "A", "a b", "", "a--b", "1", "1-2",
        ]
        for sample in samples:
            assert bool(fragment_re.fullmatch(sample)) == bool(
                id_re.fullmatch(sample)
            ), sample

    def test_compact_timestamp_from_iso(self) -> None:
        assert compact_timestamp("2026-07-16T12:34:56Z") == "20260716123456"
        assert compact_timestamp("2026-07-16T12:34:56.789Z") == "20260716123456"

    def test_compact_timestamp_rejects_non_iso(self) -> None:
        with pytest.raises(ValueError):
            compact_timestamp("not-a-timestamp")

    def test_build_and_parse_roundtrip(self) -> None:
        plan_id = build_plan_id(now_iso=NOW, nonce="ab12", tool_id="events-db")
        assert plan_id == "20260716120000-ab12-events-db"
        assert is_ok(parse_plan_id(plan_id))

    def test_build_rejects_bad_nonce(self) -> None:
        for bad in ("", "ab1", "ab123", "AB12", "a/12"):
            with pytest.raises(ValueError):
                build_plan_id(now_iso=NOW, nonce=bad, tool_id="events-db")

    def test_build_rejects_bad_tool_id(self) -> None:
        with pytest.raises(ValueError):
            build_plan_id(now_iso=NOW, nonce="ab12", tool_id="Not A Slug")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "../20260716120000-ab12-events-db",
            "/etc/passwd",
            "20260716120000-ab12-events-db/../x",
            "20260716120000-ab12-Events-DB",
            "20260716120000-ab12-",
            "20260716120000--events-db",
            "2026071612000-ab12-events-db",
            "20260716120000-ab12-events-db\n",
            "20260716120000-ab12-events-db\x00",
            "20260716120000-AB12-events-db",
            # Unicode 数字（アラビア数字）— \d だと通ってしまう
            "٢٠٢٦٠٧١٦١٢٠٠٠٠-ab12-events-db",
            # カレンダー不正（月13・時99）
            "20261399129999-ab12-events-db",
            "20260230120000-ab12-events-db",
        ],
    )
    def test_parse_rejects_malformed_ids(self, bad: str) -> None:
        result = parse_plan_id(bad)
        assert is_err(result)
        assert result.error == RejectReason.INVALID_PLAN_ID


# ---------------------------------------------------------------------------
# label / approved_by / rows range
# ---------------------------------------------------------------------------


class TestLabels:
    def test_valid_labels_pass(self) -> None:
        assert is_ok(validate_count_labels(["全登録者", "返金なし"]))

    def test_empty_label_is_rejected(self) -> None:
        assert is_err(validate_count_labels([""]))

    def test_too_long_label_is_rejected(self) -> None:
        assert is_ok(validate_count_labels(["x" * MAX_LABEL_LEN]))
        result = validate_count_labels(["x" * (MAX_LABEL_LEN + 1)])
        assert is_err(result)
        assert result.error == RejectReason.INVALID_LABEL

    def test_control_char_label_is_rejected(self) -> None:
        for bad in ("a\nb", "a\tb", "a\x1bb", "a\x00b"):
            assert is_err(validate_count_labels([bad])), repr(bad)

    def test_duplicate_labels_are_rejected(self) -> None:
        result = validate_count_labels(["全登録者", "全登録者"])
        assert is_err(result)
        assert result.error == RejectReason.INVALID_LABEL


class TestApprovedBy:
    def test_trims_and_returns_name(self) -> None:
        result = validate_approved_by("  mizumi ")
        assert is_ok(result)
        assert result.value == "mizumi"

    def test_empty_after_trim_is_rejected(self) -> None:
        for bad in ("", "   ", "\t"):
            result = validate_approved_by(bad)
            assert is_err(result)
            assert result.error == RejectReason.INVALID_APPROVED_BY

    def test_control_char_is_rejected(self) -> None:
        assert is_err(validate_approved_by("mizu\nmi"))

    def test_too_long_is_rejected(self) -> None:
        assert is_ok(validate_approved_by("x" * MAX_APPROVED_BY_LEN))
        assert is_err(validate_approved_by("x" * (MAX_APPROVED_BY_LEN + 1)))


class TestRowsRange:
    def test_valid_range(self) -> None:
        result = parse_rows_range("10:100")
        assert is_ok(result)
        assert result.value == (10, 100)

    def test_zero_to_zero_is_valid(self) -> None:
        assert parse_rows_range("0:0").value == (0, 0)

    @pytest.mark.parametrize("bad", ["100:10", "abc", "-1:5", "10", "10:", ":10", "1:2:3", ""])
    def test_malformed_range_is_rejected(self, bad: str) -> None:
        result = parse_rows_range(bad)
        assert is_err(result)
        assert result.error == RejectReason.INVALID_ROWS_RANGE

    def test_check_rows_in_range_boundaries(self) -> None:
        assert is_ok(check_rows_in_range(row_count=10, min_rows=10, max_rows=100))
        assert is_ok(check_rows_in_range(row_count=100, min_rows=10, max_rows=100))
        for out in (9, 101):
            result = check_rows_in_range(row_count=out, min_rows=10, max_rows=100)
            assert is_err(result)
            assert result.error == RejectReason.ROWS_OUT_OF_RANGE


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_compute_expires_at_adds_ttl(self) -> None:
        assert compute_expires_at(created_at=NOW) == EXPIRES
        assert DEFAULT_TTL_HOURS == 24

    def test_not_expired_within_ttl(self) -> None:
        assert is_expired(now="2026-07-17T11:59:59Z", expires_at=EXPIRES) is False

    def test_expired_at_boundary_and_after(self) -> None:
        assert is_expired(now=EXPIRES, expires_at=EXPIRES) is True
        assert is_expired(now="2026-07-17T12:00:01Z", expires_at=EXPIRES) is True

    def test_subsecond_now_is_handled(self) -> None:
        assert is_expired(now="2026-07-17T11:59:59.500Z", expires_at=EXPIRES) is False


# ---------------------------------------------------------------------------
# 状態遷移
# ---------------------------------------------------------------------------


class TestTransitions:
    ATTESTATION = ApprovalAttestation(
        approved_by="mizumi", approved_at=LATER, proposal_digest="f" * 64
    )

    def test_approve_from_draft_records_attestation(self) -> None:
        result = approve_transition(draft_state(), self.ATTESTATION)
        assert is_ok(result)
        state = result.value
        assert state.status == "approved"
        assert state.approved_by == "mizumi"
        assert state.approved_at == LATER
        assert state.proposal_digest == "f" * 64
        assert state.consumed_at is None
        assert state.run_id is None

    def test_approve_from_approved_is_rejected(self) -> None:
        result = approve_transition(approved_state("f" * 64), self.ATTESTATION)
        assert is_err(result)
        assert result.error == RejectReason.ALREADY_APPROVED

    def test_approve_from_consumed_is_rejected(self) -> None:
        result = approve_transition(consumed_state("f" * 64), self.ATTESTATION)
        assert is_err(result)
        assert result.error == RejectReason.ALREADY_CONSUMED

    def test_consume_from_approved_is_terminal(self) -> None:
        result = consume_transition(
            approved_state("f" * 64),
            consumed_at="2026-07-16T14:00:00Z",
            run_id="20260716140000-cd34-events-db",
        )
        assert is_ok(result)
        state = result.value
        assert state.status == "consumed"
        assert state.consumed_at == "2026-07-16T14:00:00Z"
        assert state.run_id == "20260716140000-cd34-events-db"
        # 承認の記録は維持される
        assert state.approved_by == "mizumi"

    def test_consume_from_draft_is_rejected(self) -> None:
        result = consume_transition(
            draft_state(), consumed_at=LATER, run_id="20260716140000-cd34-events-db"
        )
        assert is_err(result)
        assert result.error == RejectReason.NOT_APPROVED

    def test_consume_from_consumed_is_replay(self) -> None:
        result = consume_transition(
            consumed_state("f" * 64),
            consumed_at=LATER,
            run_id="20260716150000-ef56-events-db",
        )
        assert is_err(result)
        assert result.error == RejectReason.ALREADY_CONSUMED


# ---------------------------------------------------------------------------
# execute 検証マトリクス
# ---------------------------------------------------------------------------


class TestExecuteMatrix:
    def _happy_args(self) -> dict:
        proposal = make_proposal()
        digest = sha256_hex(proposal_to_json_bytes(proposal))
        return dict(
            state=approved_state(digest),
            proposal=proposal,
            proposal_digest_now=digest,
            sql_digest_now=proposal.sql_digest,
            count_sql_digests_now=tuple(
                step.count_sql_digest for step in proposal.funnel
            ),
            catalog_digest_now=proposal.catalog_digest,
            now="2026-07-16T18:00:00Z",
        )

    def test_happy_path_passes(self) -> None:
        assert is_ok(evaluate_execute_matrix(**self._happy_args()))

    def test_draft_state_is_not_approved(self) -> None:
        args = self._happy_args()
        args["state"] = draft_state()
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.NOT_APPROVED

    def test_consumed_state_is_replay(self) -> None:
        args = self._happy_args()
        args["state"] = consumed_state(args["proposal_digest_now"])
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.ALREADY_CONSUMED

    def test_sql_tampering_in_bundle_is_detected(self) -> None:
        args = self._happy_args()
        args["sql_digest_now"] = "0" * 64
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.SQL_DIGEST_MISMATCH

    def test_count_sql_tampering_is_detected(self) -> None:
        args = self._happy_args()
        digests = list(args["count_sql_digests_now"])
        digests[1] = "0" * 64
        args["count_sql_digests_now"] = tuple(digests)
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.COUNT_SQL_DIGEST_MISMATCH

    def test_count_sql_cardinality_change_is_detected(self) -> None:
        args = self._happy_args()
        args["count_sql_digests_now"] = args["count_sql_digests_now"][:1]
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.COUNT_SQL_DIGEST_MISMATCH

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(delivery_dir="outputs/elsewhere"),
            lambda d: d.update(key_columns=["email"]),
            lambda d: d.update(expected_rows_min=0, expected_rows_max=999999),
            lambda d: d.update(expires_at="2036-01-01T00:00:00Z"),
            lambda d: d["funnel"][0].update(label="書き換えたラベル"),
        ],
        ids=["delivery_dir", "key_columns", "rows_range", "expires_at", "funnel_label"],
    )
    def test_post_approval_proposal_tampering_is_detected(self, mutate) -> None:
        """承認後に proposal.json のどのフィールドを書き換えても
        proposal_digest 再計算照合で検出される。"""
        args = self._happy_args()
        tampered = proposal_to_json_dict(args["proposal"])
        mutate(tampered)
        tampered_proposal = proposal_from_json_dict(tampered).value
        args["proposal"] = tampered_proposal
        args["proposal_digest_now"] = sha256_hex(
            proposal_to_json_bytes(tampered_proposal)
        )
        # expires_at 延長改竄も TTL 判定に到達する前に digest 照合で落ちる
        # （マトリクスは digest 検査 → TTL 検査の順）
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.PROPOSAL_DIGEST_MISMATCH

    def test_catalog_change_after_prepare_is_detected(self) -> None:
        args = self._happy_args()
        args["catalog_digest_now"] = "1" * 64
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.CATALOG_DIGEST_MISMATCH

    def test_ttl_expiry_is_rejected(self) -> None:
        args = self._happy_args()
        args["now"] = "2026-07-18T00:00:00Z"
        result = evaluate_execute_matrix(**args)
        assert is_err(result)
        assert result.error == RejectReason.TTL_EXPIRED


# ---------------------------------------------------------------------------
# 直列化（proposal.json / state.json）
# ---------------------------------------------------------------------------


class TestProposalSerialization:
    def test_roundtrip(self) -> None:
        proposal = make_proposal()
        data = json.loads(proposal_to_json_bytes(proposal).decode("utf-8"))
        result = proposal_from_json_dict(data)
        assert is_ok(result)
        assert result.value == proposal

    def test_json_bytes_are_deterministic(self) -> None:
        assert proposal_to_json_bytes(make_proposal()) == proposal_to_json_bytes(
            make_proposal()
        )

    def test_missing_field_is_malformed(self) -> None:
        data = proposal_to_json_dict(make_proposal())
        del data["sql_digest"]
        result = proposal_from_json_dict(data)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_PROPOSAL

    def test_unknown_field_is_malformed(self) -> None:
        data = proposal_to_json_dict(make_proposal())
        data["extra"] = 1
        result = proposal_from_json_dict(data)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_PROPOSAL

    def test_wrong_type_is_malformed(self) -> None:
        data = proposal_to_json_dict(make_proposal())
        data["expected_rows_min"] = "10"
        assert is_err(proposal_from_json_dict(data))

    def test_malformed_funnel_is_rejected(self) -> None:
        data = proposal_to_json_dict(make_proposal())
        data["funnel"] = [{"label": "x"}]
        assert is_err(proposal_from_json_dict(data))

    @pytest.mark.parametrize(
        "corrupt",
        [
            lambda d: d.update(plan_id="../x"),
            lambda d: d.update(plan_id="20261399129999-ab12-events-db"),
            lambda d: d.update(tool_id="Not A Slug"),
            lambda d: d.update(expected_rows_min=100, expected_rows_max=10),
            lambda d: d.update(sql_digest="not-sha256"),
            lambda d: d.update(sql_display_digest="A" * 64),  # uppercase hex
            lambda d: d.update(catalog_digest="c" * 63),
            lambda d: d.update(expires_at="not-a-time"),
            lambda d: d.update(created_at="2026-13-99T99:99:99Z"),
            lambda d: d["funnel"][0].update(label="改行\n入り"),
            lambda d: d["funnel"][0].update(count_sql_digest="zz"),
            lambda d: d.update(
                funnel=[
                    {"label": "同じ", "count_sql_digest": "a" * 64, "row_count": 1},
                    {"label": "同じ", "count_sql_digest": "b" * 64, "row_count": 2},
                ]
            ),
        ],
        ids=[
            "traversal_plan_id",
            "calendar_invalid_plan_id",
            "bad_tool_id",
            "reversed_range",
            "non_sha256_digest",
            "uppercase_digest",
            "short_digest",
            "bad_expires_at",
            "calendar_invalid_created_at",
            "control_char_label",
            "non_hex_count_digest",
            "duplicate_labels",
        ],
    )
    def test_domain_constraints_are_enforced_on_deserialize(self, corrupt) -> None:
        """永続 JSON からの復元時も単体 validator と同じ制約が強制される。"""
        data = proposal_to_json_dict(make_proposal())
        corrupt(data)
        result = proposal_from_json_dict(data)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_PROPOSAL

    def test_matrix_does_not_raise_on_directly_built_bad_expires_at(self) -> None:
        """deserialize を経ない不正 expires_at でも例外ではなく fail closed。"""
        proposal = make_proposal(expires_at="not-a-time")
        digest = sha256_hex(proposal_to_json_bytes(proposal))
        result = evaluate_execute_matrix(
            state=approved_state(digest),
            proposal=proposal,
            proposal_digest_now=digest,
            sql_digest_now=proposal.sql_digest,
            count_sql_digests_now=tuple(
                step.count_sql_digest for step in proposal.funnel
            ),
            catalog_digest_now=proposal.catalog_digest,
            now=NOW,
        )
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_PROPOSAL


class TestStateSerialization:
    def test_draft_roundtrip(self) -> None:
        state = draft_state()
        result = state_from_json_dict(state_to_json_dict(state))
        assert is_ok(result)
        assert result.value == state

    def test_approved_roundtrip(self) -> None:
        state = approved_state("f" * 64)
        assert state_from_json_dict(state_to_json_dict(state)).value == state

    def test_consumed_roundtrip(self) -> None:
        state = consumed_state("f" * 64)
        assert state_from_json_dict(state_to_json_dict(state)).value == state

    @pytest.mark.parametrize(
        "corrupt",
        [
            lambda d: d.update(status="banana"),
            lambda d: d.pop("status"),
            lambda d: d.update(extra=1),
            lambda d: d.update(status="approved", approved_by=None),
            lambda d: d.update(status="consumed", run_id=None),
            lambda d: d.update(status="draft", approved_by="x"),
        ],
        ids=[
            "unknown_status",
            "missing_status",
            "unknown_key",
            "approved_without_attestation",
            "consumed_without_run_id",
            "draft_with_leftover_approval",
        ],
    )
    def test_malformed_state_fails_closed(self, corrupt) -> None:
        data = state_to_json_dict(approved_state("f" * 64))
        corrupt(data)
        result = state_from_json_dict(data)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_STATE

    def test_non_dict_is_malformed(self) -> None:
        assert is_err(state_from_json_dict([]))
        assert is_err(state_from_json_dict("draft"))

    @pytest.mark.parametrize(
        "corrupt",
        [
            lambda d: d.update(approved_at="bad-time"),
            lambda d: d.update(proposal_digest="x"),
            lambda d: d.update(approved_by="mizu\nmi"),
            lambda d: d.update(approved_by="   "),
        ],
        ids=["bad_timestamp", "non_sha256_digest", "control_char_name", "blank_name"],
    )
    def test_approved_state_values_are_validated(self, corrupt) -> None:
        data = state_to_json_dict(approved_state("f" * 64))
        corrupt(data)
        result = state_from_json_dict(data)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_STATE

    @pytest.mark.parametrize(
        "corrupt",
        [
            lambda d: d.update(run_id="../x"),
            lambda d: d.update(consumed_at="bad-time"),
        ],
        ids=["traversal_run_id", "bad_consumed_at"],
    )
    def test_consumed_state_values_are_validated(self, corrupt) -> None:
        data = state_to_json_dict(consumed_state("f" * 64))
        corrupt(data)
        result = state_from_json_dict(data)
        assert is_err(result)
        assert result.error == RejectReason.MALFORMED_STATE


class TestExecutionReceipt:
    def test_receipt_holds_run_metadata(self) -> None:
        receipt = ExecutionReceipt(
            run_id="20260716140000-cd34-events-db",
            row_count=42,
            csv_sha256="9" * 64,
            published_at="2026-07-16T14:00:05Z",
            delivery_dir="outputs/deliveries",
        )
        assert receipt.row_count == 42

"""Tests for tool_sql_gate.py — sqlglot による静的 relation allowlist.

pg / mysql には sqlite の authorizer に相当する「実行エンジン自身の判定」が
ないため、この静的検査層が第2防御になる。契約:

* statement 種別検査 — 単一 SELECT / WITH のみ（複文・DML・DDL・SET・
  SELECT INTO・FOR UPDATE・CTE 内 DML を拒否）
* relation 抽出 — JOIN・サブクエリ・CTE・view 名を含む全実 relation
* canonical ID 照合 — dialect 別の完全修飾形式（pg: schema.table /
  mysql: db.table）。未修飾名は既定名前空間に展開
* fail closed — parse 失敗・未対応構文・解決できない relation は拒否
"""

from __future__ import annotations

import pytest

from lib.domain.types import is_err, is_ok
from lib.service.tool_sql_gate import (
    SqlGateError,
    canonical_allowlist,
    check_sql,
)


# ---------------------------------------------------------------------------
# 関数呼び出しの allowlist 迂回防止（Codex BLOCK 1 の回帰ガード）
# ---------------------------------------------------------------------------


class TestFunctionGate:
    def test_lateral_table_function_is_rejected(self) -> None:
        result = pg_check("SELECT * FROM LATERAL secret_fn() AS s")
        assert is_err(result)
        assert result.error == SqlGateError.FUNCTION_NOT_ALLOWED

    def test_pg_file_read_function_is_rejected(self) -> None:
        """pg_read_file 等のファイル読取関数を relation なしで通さない。"""
        result = pg_check("SELECT pg_read_file('/etc/passwd')")
        assert is_err(result)
        assert result.error == SqlGateError.FUNCTION_NOT_ALLOWED

    def test_mysql_load_file_is_rejected(self) -> None:
        result = my_check("SELECT LOAD_FILE('/etc/passwd')")
        assert is_err(result)
        assert result.error == SqlGateError.FUNCTION_NOT_ALLOWED

    def test_json_table_function_is_rejected(self) -> None:
        result = my_check(
            "SELECT * FROM JSON_TABLE('[]', '$[*]' "
            "COLUMNS(a INT PATH '$.a')) AS jt"
        )
        assert is_err(result)
        assert result.error in (
            SqlGateError.FUNCTION_NOT_ALLOWED,
            SqlGateError.RELATION_UNRESOLVED,
        )

    def test_schema_qualified_function_is_rejected(self) -> None:
        result = pg_check("SELECT admin.dangerous_fn(1)")
        assert is_err(result)
        assert result.error == SqlGateError.FUNCTION_NOT_ALLOWED

    def test_function_in_lateral_subquery_is_rejected(self) -> None:
        result = pg_check(
            "SELECT * FROM users u, LATERAL (SELECT pg_sleep(10)) x"
        )
        assert is_err(result)
        assert result.error == SqlGateError.FUNCTION_NOT_ALLOWED

    def test_hidden_function_in_where_is_rejected(self) -> None:
        result = pg_check(
            "SELECT * FROM users WHERE name = pg_read_file('/etc/passwd')"
        )
        assert is_err(result)
        assert result.error == SqlGateError.FUNCTION_NOT_ALLOWED

    def test_recognized_builtin_functions_are_allowed(self) -> None:
        """count/sum/upper 等の組み込み集計・スカラー関数は通す（過剰拒否しない）。"""
        assert is_ok(
            pg_check(
                "SELECT count(*), sum(amount), upper(name), "
                "coalesce(email, '') FROM users"
            )
        )

    def test_window_function_is_allowed(self) -> None:
        assert is_ok(
            pg_check("SELECT row_number() OVER (ORDER BY user_id) FROM users")
        )


def pg_check(sql: str, tables=("users", "registrations", "refunds"), schema="public"):
    return check_sql(
        sql,
        dialect="postgres",
        default_namespace=schema,
        allowed_tables=tables,
    )


def my_check(sql: str, tables=("users", "registrations"), db="appdb"):
    return check_sql(
        sql,
        dialect="mysql",
        default_namespace=db,
        allowed_tables=tables,
    )


# ---------------------------------------------------------------------------
# statement 種別検査
# ---------------------------------------------------------------------------


class TestStatementGate:
    def test_plain_select_is_allowed(self) -> None:
        assert is_ok(pg_check("SELECT id, name FROM users"))

    def test_with_cte_select_is_allowed(self) -> None:
        sql = "WITH recent AS (SELECT * FROM registrations) SELECT count(*) FROM recent"
        assert is_ok(pg_check(sql))

    def test_union_of_selects_is_allowed(self) -> None:
        assert is_ok(pg_check("SELECT id FROM users UNION SELECT id FROM refunds"))

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users VALUES (1)",
            "UPDATE users SET name = 'x'",
            "DELETE FROM users",
            "DROP TABLE users",
            "CREATE TABLE t (id int)",
            "ALTER TABLE users ADD COLUMN x int",
            "TRUNCATE users",
            "SET search_path TO evil",
            "GRANT SELECT ON users TO evil",
            "VACUUM",
        ],
    )
    def test_dml_ddl_set_are_rejected(self, sql: str) -> None:
        result = pg_check(sql)
        assert is_err(result), sql
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED

    def test_multiple_statements_are_rejected(self) -> None:
        result = pg_check("SELECT 1; SELECT 2")
        assert is_err(result)
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED

    def test_select_hiding_dml_after_semicolon_is_rejected(self) -> None:
        result = pg_check("SELECT id FROM users; DELETE FROM users")
        assert is_err(result)
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED

    def test_select_into_is_rejected(self) -> None:
        result = pg_check("SELECT * INTO newtab FROM users")
        assert is_err(result)
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED

    def test_select_for_update_is_rejected(self) -> None:
        result = pg_check("SELECT * FROM users FOR UPDATE")
        assert is_err(result)
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED

    def test_data_modifying_cte_is_rejected(self) -> None:
        """pg は WITH 内の DELETE ... RETURNING を許すため必ず walk で検出する。"""
        sql = "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x"
        result = pg_check(sql)
        assert is_err(result)
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED

    def test_empty_sql_is_rejected(self) -> None:
        assert is_err(pg_check(""))
        assert is_err(pg_check("   \n  "))


# ---------------------------------------------------------------------------
# relation 抽出（JOIN・サブクエリ・CTE・alias）
# ---------------------------------------------------------------------------


class TestRelationExtraction:
    def test_join_relations_are_all_checked(self) -> None:
        sql = "SELECT * FROM users u JOIN registrations r ON u.id = r.user_id"
        result = pg_check(sql)
        assert is_ok(result)
        assert set(result.value.relations) == {"public.users", "public.registrations"}

    def test_subquery_relation_outside_allowlist_is_rejected(self) -> None:
        sql = "SELECT * FROM users WHERE id IN (SELECT user_id FROM admin_keys)"
        result = pg_check(sql)
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED
        assert "admin_keys" in result.detail

    def test_join_relation_outside_allowlist_is_rejected(self) -> None:
        sql = "SELECT * FROM users u JOIN secrets s ON u.id = s.uid"
        result = pg_check(sql)
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED

    def test_cte_name_is_not_a_relation(self) -> None:
        sql = "WITH tmp AS (SELECT * FROM users) SELECT * FROM tmp"
        result = pg_check(sql)
        assert is_ok(result)
        assert set(result.value.relations) == {"public.users"}

    def test_cte_alias_shadowing_real_table_checks_underlying_relation(self) -> None:
        """CTE 名が allowlist 外の実テーブル名を shadow しても、照合対象は
        CTE の underlying 実 relation のみ。"""
        sql = "WITH secrets AS (SELECT * FROM users) SELECT * FROM secrets"
        result = pg_check(sql)
        assert is_ok(result)
        assert set(result.value.relations) == {"public.users"}

    def test_derived_table_alias_is_not_a_relation(self) -> None:
        sql = "SELECT * FROM (SELECT id FROM users) AS derived"
        result = pg_check(sql)
        assert is_ok(result)
        assert set(result.value.relations) == {"public.users"}

    def test_string_literal_and_comment_do_not_trigger(self) -> None:
        sql = (
            "SELECT 'FROM admin_keys' AS s, u.name FROM users u -- JOIN admin_keys\n"
        )
        result = pg_check(sql)
        assert is_ok(result)
        assert set(result.value.relations) == {"public.users"}


# ---------------------------------------------------------------------------
# canonical ID（pg: 未修飾展開・quote・case folding）
# ---------------------------------------------------------------------------


class TestPostgresCanonicalization:
    def test_unqualified_expands_to_default_schema(self) -> None:
        result = pg_check("SELECT * FROM users")
        assert is_ok(result)
        assert result.value.relations == ("public.users",)

    def test_custom_default_schema_is_used(self) -> None:
        result = pg_check(
            "SELECT * FROM users", tables=("analytics.users",), schema="analytics"
        )
        assert is_ok(result)
        assert result.value.relations == ("analytics.users",)

    def test_qualified_reference_matches_qualified_allowlist(self) -> None:
        result = pg_check("SELECT * FROM public.users")
        assert is_ok(result)

    def test_other_schema_is_rejected(self) -> None:
        result = pg_check("SELECT * FROM private.users")
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED

    def test_unquoted_identifier_is_case_folded(self) -> None:
        """pg の unquoted 識別子は小文字化される（Users == users）。"""
        assert is_ok(pg_check("SELECT * FROM Users"))

    def test_quoted_mixed_case_identifier_is_preserved(self) -> None:
        """quoted "Users" は小文字化されず allowlist（users）と一致しない。"""
        result = pg_check('SELECT * FROM "Users"')
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED

    def test_allowlist_entries_are_case_folded_like_unquoted(self) -> None:
        """catalog の allowlist 宣言も unquoted 扱いで小文字化して照合する。"""
        assert is_ok(pg_check("SELECT * FROM users", tables=("Users",)))

    def test_three_part_name_is_rejected(self) -> None:
        result = pg_check("SELECT * FROM otherdb.public.users")
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_UNRESOLVED

    def test_system_catalog_is_rejected_by_allowlist(self) -> None:
        result = pg_check("SELECT * FROM pg_catalog.pg_tables")
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED


# ---------------------------------------------------------------------------
# mysql dialect（backquote・database.table・case-sensitive 照合）
# ---------------------------------------------------------------------------


class TestMySqlDialect:
    def test_unqualified_expands_to_default_database(self) -> None:
        result = my_check("SELECT * FROM users")
        assert is_ok(result)
        assert result.value.relations == ("appdb.users",)

    def test_backquoted_qualified_reference(self) -> None:
        result = my_check("SELECT * FROM `appdb`.`users`")
        assert is_ok(result)
        assert result.value.relations == ("appdb.users",)

    def test_other_database_is_rejected(self) -> None:
        result = my_check("SELECT * FROM otherdb.users")
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED

    def test_matching_is_case_sensitive(self) -> None:
        """mysql の table 名比較は設定依存のため case-sensitive 照合を既定とする。"""
        result = my_check("SELECT * FROM Users")
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_NOT_ALLOWED

    def test_mysql_dml_is_rejected(self) -> None:
        result = my_check("REPLACE INTO users VALUES (1)")
        assert is_err(result)
        assert result.error == SqlGateError.STATEMENT_NOT_ALLOWED


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_parse_failure_is_rejected(self) -> None:
        result = pg_check("SELECT FROM WHERE !!!")
        assert is_err(result)
        assert result.error == SqlGateError.PARSE_FAILED

    def test_table_function_source_is_rejected(self) -> None:
        """generate_series 等の table 関数は relation として解決できない → 拒否。"""
        result = pg_check("SELECT * FROM generate_series(1, 10)")
        assert is_err(result)
        assert result.error == SqlGateError.RELATION_UNRESOLVED

    def test_unknown_dialect_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            check_sql(
                "SELECT 1",
                dialect="oracle",
                default_namespace="public",
                allowed_tables=("users",),
            )

    def test_relation_free_select_is_allowed(self) -> None:
        """SELECT 1 のような relation を持たないクエリは保護対象がないため許可。"""
        result = pg_check("SELECT 1")
        assert is_ok(result)
        assert result.value.relations == ()


# ---------------------------------------------------------------------------
# canonical_allowlist（catalog 宣言の正規化）
# ---------------------------------------------------------------------------


class TestCanonicalAllowlist:
    def test_pg_unqualified_expands_and_folds_case(self) -> None:
        allow = canonical_allowlist(
            ("Users", "analytics.Events"), dialect="postgres", default_namespace="public"
        )
        assert allow == frozenset({"public.users", "analytics.events"})

    def test_mysql_preserves_case(self) -> None:
        allow = canonical_allowlist(
            ("Users", "other.Events"), dialect="mysql", default_namespace="AppDb"
        )
        assert allow == frozenset({"AppDb.Users", "other.Events"})

    def test_unknown_dialect_raises(self) -> None:
        with pytest.raises(ValueError):
            canonical_allowlist(("users",), dialect="sqlite", default_namespace="main")

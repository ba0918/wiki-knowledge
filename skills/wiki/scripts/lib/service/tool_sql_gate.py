"""sqlglot による静的 SQL 検査層 — pg / mysql の authorizer 代替.

sqlite の authorizer は「実行エンジン自身の判定」だったが、postgres / mysql
にはクライアント側から同等のフックがない。この gate は3層防御の第2層:

* 第1防御 — DB 側 read-only role（catalog 契約。guide に設定手順）
* **第2防御 — 本 gate**: statement 種別検査 + relation allowlist 照合
* 第3防御 — session 属性（read-only transaction + timeout、connector 所有）

契約:

* 単一の SELECT / WITH ... SELECT のみ（複文・DML・DDL・SET・SELECT INTO・
  FOR UPDATE・CTE 内 DML を拒否 — pg は ``WITH x AS (DELETE ...)`` を許す
  ため top-level 判定だけでは足りず、tree 全体を walk する）
* relation は dialect 別の完全修飾 canonical ID（pg: ``schema.table`` /
  mysql: ``db.table``）で照合。未修飾名は既定名前空間へ静的展開する
  （session の search_path には依存しない）
* case folding は dialect 規則: pg は unquoted を小文字化（quoted は保持）、
  mysql は設定依存のため case-sensitive 照合を既定とする
* **fail closed**: parse 失敗・未対応構文・解決できない relation
  （テーブル関数・3部修飾名）はすべて拒否
* **関数呼び出しは「sqlglot が型として認識する組み込み関数」のみ許可**:
  sqlglot が型を持たない未知関数（``exp.Anonymous``）は fail closed で
  拒否する。これは `pg_read_file()` / `LOAD_FILE()` 等のファイル読取関数や、
  SECURITY DEFINER のユーザー定義関数を静的層で塞ぐため（DB 側 role が
  第一防御だが、role が緩い環境でも allowlist 層を迂回させない）。
  ``LATERAL fn()`` / scalar function のどちらの位置でも同じ規則が効く
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from lib.domain.types import Err, Ok


GATE_DIALECTS = ("postgres", "mysql")

# 見つけたら即拒否する node。未対応構文は sqlglot が exp.Command に
# fallback するため（REPLACE / CALL / EXPLAIN 等）、Command を含めることで
# 「parser が理解できない文は通らない」= fail closed になる
_FORBIDDEN_NODES: tuple[type, ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Set,
    exp.Use,
    exp.Show,
    exp.Kill,
    exp.Copy,
    exp.Pragma,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Command,
    exp.Lock,  # SELECT ... FOR UPDATE / FOR SHARE（行ロック取得）
    exp.Into,  # SELECT INTO（テーブル生成）
)


class SqlGateError(str, Enum):
    """Discriminator for SQL gate rejections（監査 reason にもそのまま使う）。"""

    PARSE_FAILED = "sql_gate_parse_failed"
    STATEMENT_NOT_ALLOWED = "sql_gate_statement_not_allowed"
    RELATION_NOT_ALLOWED = "sql_gate_relation_not_allowed"
    RELATION_UNRESOLVED = "sql_gate_relation_unresolved"
    FUNCTION_NOT_ALLOWED = "sql_gate_function_not_allowed"


@dataclass(frozen=True)
class SqlGateReport:
    """検査通過の証跡 — 実際に参照される relation の canonical ID（出現順）。"""

    relations: tuple[str, ...]


def _fold(dialect: str, name: str) -> str:
    """catalog 宣言（unquoted 扱い）の case folding。"""

    return name.lower() if dialect == "postgres" else name


def canonical_allowlist(
    tables: Sequence[str], *, dialect: str, default_namespace: str
) -> frozenset[str]:
    """catalog の allowed_tables を canonical ID 集合に正規化する。

    引数は catalog 検証済みであるべきで、不正（未知 dialect・3部修飾）は
    プログラミングエラーとして ValueError にする。
    """

    if dialect not in GATE_DIALECTS:
        raise ValueError(f"未対応 dialect: {dialect!r}")
    ns = _fold(dialect, default_namespace)
    out: set[str] = set()
    for table in tables:
        parts = table.split(".")
        if len(parts) == 1:
            out.add(f"{ns}.{_fold(dialect, parts[0])}")
        elif len(parts) == 2:
            out.add(f"{_fold(dialect, parts[0])}.{_fold(dialect, parts[1])}")
        else:
            raise ValueError(f"allowlist に3部修飾名: {table!r}")
    return frozenset(out)


def check_sql(
    sql: str,
    *,
    dialect: str,
    default_namespace: str,
    allowed_tables: Sequence[str],
) -> Ok[SqlGateReport] | Err[SqlGateError]:
    """SQL を静的検査し、通過なら参照 relation の一覧を返す（fail closed）。"""

    allowed = canonical_allowlist(
        allowed_tables, dialect=dialect, default_namespace=default_namespace
    )

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as exc:
        return Err(error=SqlGateError.PARSE_FAILED, detail=str(exc)[:300])
    if len(statements) != 1:
        return Err(
            error=SqlGateError.STATEMENT_NOT_ALLOWED,
            detail=f"単一の SELECT 文のみ実行できます（{len(statements)} 文）",
        )
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        return Err(
            error=SqlGateError.STATEMENT_NOT_ALLOWED,
            detail=f"SELECT 以外の文は実行できません: {type(tree).__name__}",
        )
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return Err(
                error=SqlGateError.STATEMENT_NOT_ALLOWED,
                detail=f"禁止構文を含みます: {type(node).__name__}",
            )
        # sqlglot が型を持たない未知関数（Anonymous）は fail closed。
        # FROM 句のテーブル関数（LATERAL fn()）も SELECT 句の scalar function
        # （pg_read_file / LOAD_FILE 等）も同じ経路で拒否する
        if isinstance(node, exp.Anonymous):
            return Err(
                error=SqlGateError.FUNCTION_NOT_ALLOWED,
                detail=(
                    f"未知の関数呼び出しは許可されていません: {node.name}"
                    "（sqlglot が組み込みとして認識する関数のみ許可）"
                ),
            )

    try:
        normalized = normalize_identifiers(tree, dialect=dialect)
        scopes = traverse_scope(normalized)
    except SqlglotError as exc:
        return Err(error=SqlGateError.PARSE_FAILED, detail=str(exc)[:300])

    ns = _fold(dialect, default_namespace)
    relations: dict[str, None] = {}
    for scope in scopes:
        for source in scope.sources.values():
            if isinstance(source, Scope):
                # CTE / derived table — underlying relation は自身の scope で検査済み
                continue
            if not isinstance(source, exp.Table):
                return Err(
                    error=SqlGateError.RELATION_UNRESOLVED,
                    detail=f"解決できない FROM 句: {type(source).__name__}",
                )
            if source.args.get("catalog"):
                return Err(
                    error=SqlGateError.RELATION_UNRESOLVED,
                    detail=f"3部修飾名は未対応（fail closed）: {source.sql()}",
                )
            ident = source.args.get("this")
            if not isinstance(ident, exp.Identifier) or not source.name:
                return Err(
                    error=SqlGateError.RELATION_UNRESOLVED,
                    detail="テーブル関数等は relation として解決できません",
                )
            db = source.text("db") or ns
            relations[f"{db}.{source.name}"] = None

    denied = [r for r in relations if r not in allowed]
    if denied:
        return Err(
            error=SqlGateError.RELATION_NOT_ALLOWED,
            detail="allowlist 外の relation: " + ", ".join(denied),
        )
    return Ok(value=SqlGateReport(relations=tuple(relations)))

# Tool Query ガイド — dry-run 承認手順と Selection Recipe

wiki-tool-query スキルの詳細リファレンス。運用フローの本体は
`skills/wiki-tool-query/SKILL.md`、実行契約の真実源は `{wiki_root}/tools/catalog.json`
（schema: `{wiki_root}/schema/tool-catalog-schema.json`）。

## ファネル提示フォーマット

承認依頼はデータの絞り込み過程が一目で追える形で提示する。**各段で何件落ちたか**が
承認判断の中心情報になる:

```
対象定義: ev-2026 参加登録者のうち返金を受けていない人（補填対象）
inclusion:
  - registrations.event = 'ev-2026'
exclusion:
  - refunds に user_id が存在する人
ファネル:
  ev-2026 登録者: 412 件
  → 返金なし: 397 件（-15）
想定件数レンジ: 380〜410 件
delivery 先: outputs/deliveries
tool: events-db / plan_id: 20260716... / sql_digest: ab12...
```

<details> で SQL 本文を添付（本文は bundle の `query.sql` が唯一の実行対象。表示突合には
`sql_display_digest` = trim + 改行統一のみの保守的正規化 digest を使える）。

### COUNT SQL の組み方

- 1 段 = 1 ファイル。本実行 SQL の WHERE 条件を先頭から 1 つずつ足していく
- 各 COUNT は「1 行 1 列の非負整数」を返すこと（それ以外は `count_result_invalid` で拒否）
- label は 64 文字以内・制御文字なし・重複なし。bundle 内ファイル名には使われない（連番 `counts/{nn}.sql`）

### 想定件数レンジ（expected_rows）

- 実行時制約。本実行の実測件数がレンジ外だと **publish されず** `rows_out_of_range` で拒否される
- ファネル最終段の COUNT を中心に、prepare〜execute 間のデータ変動を見込んだ幅を取る
  （min = max にすると完全一致を要求できる）

## 失敗時の reason code 早見表

| reason | 意味 | 次の一手 |
|---|---|---|
| `not_approved` | 未承認（draft）で execute | approve を依頼する |
| `already_consumed` | replay（二重 execute） | 新 prepare → 再承認 |
| `ttl_expired` | plan の期限切れ（**prepare 起算 24h**。承認時刻ではない） | 新 prepare → 再承認 |
| `sql_digest_mismatch` / `count_sql_digest_mismatch` | bundle 内 SQL の改変検出 | bundle を破棄し新 prepare |
| `proposal_digest_mismatch` | 承認後の proposal 書き換え検出 | 同上 |
| `catalog_digest_mismatch` | catalog が prepare 後に変更された | 新 prepare（新しい契約で承認し直す） |
| `rows_out_of_range` | 実測件数がレンジ外 | 原因を確認しレンジ or 条件を見直して新 prepare |
| `row_limit_exceeded` 等 | catalog の limits 超過 | 条件で絞るか catalog 変更を PR で提案 |
| `delivery_conflict` | delivery 先に同名 run が存在 | そのまま再 execute は不可（承認消費済み）。新 prepare |
| `audit_write_failed` | 監査が書けない（fail closed） | ディスク・権限を確認。DB アクセス前なら承認は未消費 |

## Connector 別の書き方（Phase A2）

catalog の `type` で connector が決まる: `sqlite` / `postgres` / `mysql` / `http`。
承認フロー（prepare → approve → execute）・監査・delivery・single-use は**全 type 共通**で、
type 差は接続と enforcement 層だけに閉じる。

### postgres / mysql（SQL 系リモート DB）

- 接続は catalog の field から組み立てる（`host` / `port` / `dbname` / `user` 必須）。
  **ユーザー入力の DSN 文字列は受け付けない**（injection 面の縮小）
- `credential_ref` は **必須**。解決するのは password のみ（user は catalog field で宣言）
- SQL は sqlite と同じ `--sql-file` / `--count-sql` で渡す。**静的 SQL 検査層**（sqlglot）が
  接続前に (1) 単一 SELECT / WITH のみ (2) relation allowlist 照合 (3) 未知関数の拒否 を行う
- `allowed_tables` は**完全修飾**（`schema.table` / `db.table`）または**未修飾**で宣言:
  - postgres: 未修飾名は `connection.default_schema`（既定 `public`）へ静的展開。unquoted 識別子は
    小文字化して照合（`Users` == `users`）。quoted `"Users"` は別物として扱う
  - mysql: `connection.dbname` を既定 database として展開。table 名照合は**大文字小文字を区別**する
    （MySQL の設定依存を避けるため case-sensitive を既定とする）
  - JOIN・サブクエリ・CTE・view の実 relation もすべて allowlist 照合される。CTE 名・derived alias は
    relation 扱いしない（underlying の実テーブルのみ照合）
- **関数は sqlglot が組み込みとして認識するもののみ許可**。未知関数（`pg_read_file` /
  `LOAD_FILE` / ユーザー定義関数 / LATERAL テーブル関数）は fail closed で拒否される
  （`sql_gate_function_not_allowed`）。`count` / `sum` / `upper` / `coalesce` / window 関数等は通る

#### read-only role の設定（第一防御 — 必ず設定する）

pg / mysql には sqlite の authorizer（実行エンジン自身の判定）に相当するものがない。
**DB 側の read-only 専用 role が第一防御**であり、静的 SQL 検査層 + session read-only は
その補完。専用 role を必ず用意すること:

```sql
-- PostgreSQL: SELECT 権限のみの専用 role
CREATE ROLE wiki_readonly LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE analytics TO wiki_readonly;
GRANT USAGE ON SCHEMA public TO wiki_readonly;
GRANT SELECT ON public.users, public.registrations TO wiki_readonly;
-- INSERT/UPDATE/DELETE/TRUNCATE/CREATE は付与しない
```

```sql
-- MySQL: SELECT 権限のみの専用ユーザー
CREATE USER 'wiki_readonly'@'%' IDENTIFIED BY '...';
GRANT SELECT ON billing.users TO 'wiki_readonly'@'%';
GRANT SELECT ON billing.invoices TO 'wiki_readonly'@'%';
-- CREATE TEMPORARY TABLES は付与しない（後述の一時テーブル穴を塞ぐため）
```

`doctor` サブコマンドがこの role の read-only 性を introspection で機械検証する（後述）。

#### read-only session の実行順序契約

- **postgres**: 接続直後・transaction 未開始の時点で `Connection.read_only = True` を設定し、
  その後に開始される明示 transaction 内で named cursor（server-side cursor）を開く。
  `statement_timeout` と `search_path=<default_schema>` は接続オプションで渡し、静的 SQL gate と
  実行時の未修飾 relation の解決先を一致させる
- **mysql**: autocommit 状態（transaction 開始前）で `SET SESSION TRANSACTION READ ONLY` +
  `max_execution_time` を発行し、その後 `START TRANSACTION` → `SSCursor`（unbuffered）で実行
- 巨大結果は server-side cursor で行数上限まで fetch した時点で打ち切る（client 全バッファしない）

#### 保証範囲の限定（pg / mysql）

- **保証範囲の変化**: sqlite の authorizer は「実行エンジン自身の判定」だったが、pg / mysql は
  「DB 側 read-only role（第一防御）+ 静的 SQL 検査 + session read-only」の**組み合わせ**。
  role を SELECT 専用にしないと防御が静的層 + session のみに縮む
- **MySQL の一時テーブル穴**: MySQL の read-only transaction は**一時テーブルへの DML を許容する**。
  この穴は session 層では塞げないため、`CREATE TEMPORARY TABLES` 権限を role に付与しないことで防ぐ
  （上記 role 設定を守れば問題にならない）
- **MariaDB は保証範囲外**: MySQLConnector は **MySQL** を対象にテストしている。MariaDB でも動く
  可能性はあるが検証しない

### TLS（pg / mysql）

- 既定は**安全側**: postgres は `sslmode=verify-full`、mysql は CA + hostname 検証
- CA は `connection.tls_ca_file`（wiki_root 相対 or 絶対、symlink 全拒否。省略時はシステム CA）
- 緩和は `connection.allow_insecure_tls: true` の明示 opt-in で、**host が localhost /
  127.0.0.1 / ::1 の場合のみ**受理される（`tls_ca_file` との同時宣言は不可）
- `doctor` は TLS ネゴシエーションの成立を確認し、緩和が宣言されている場合は SKIP（警告）表示する

### http（Redash / Kibana(ES) / 社内 API）

SQL の代わりに **request spec ファイル**（JSON）を `--request-file` / `--count-request` で渡す
（`--sql-file` / `--count-sql` は SQL 系専用。http tool に SQL flag を渡すとエラーで案内される）:

```json
{
  "method": "POST",
  "path": "/api/queries/42/results",
  "body": { "max_age": 0 },
  "records_path": "query_result.data.rows",
  "columns": ["user_id", "email"]
}
```

- **request spec は JSON Schema で検証**（`{wiki_root}/schema/tool-request-spec-schema.json`、未知キー拒否）。
  行取得は `records_path` + `columns`、ファネル COUNT は `count_path`（単一の非負整数）— 両者は排他
- `records_path` / `count_path` は dot-path（`a.b.c`。配列 index・ワイルドカードなし）。各 record は
  object（columns で射影）または配列（位置で射影）。型は None/int/float/str に正規化し、bool は int、
  nested object の混入は型逸脱として拒否
- **catalog（type: http）**: `base_url`（origin のみ。https 必須。http は localhost 限定の
  `allow_insecure` opt-in）/ `allowed_endpoints`（method + path_prefix の allowlist）/
  `auth_header_name` + `auth_header_template`（`{credential}` を秘密値で置換して注入）/
  `limits.max_response_bytes`
- **URL は canonicalize してから allowlist 照合**: encoded separator（`%2f` / `%5c` / `%2e%2e` /
  NUL・control）は decode せず拒否、二重 / 不正 encoding は fail closed、`.` / `..` を解決、
  `//` / backslash / 絶対 URL / userinfo / fragment を拒否。照合は origin 完全一致 +
  **segment 境界の** path prefix（`/api/query` は `/api/query/42` に一致するが `/api/query-delete`
  には一致しない）+ method 一致
- **リダイレクトは拒否**（allowlist 迂回防止）。CLI 表示は中立な `request_digest`
  （bundle 内部 field は Phase A 互換の `sql_digest` を維持）

#### 代表レスポンス例

- **Redash**: `POST /api/queries/{id}/results` → `records_path: "query_result.data.rows"`,
  各行は `{"user_id": .., "email": ..}` の object
- **Kibana (Elasticsearch) search**: `records_path: "hits.hits"`, `columns: ["_id", "_score"]`。
  `_source`（object）を column に指定すると型逸脱として拒否される（nested 投影は保証範囲外）
- **Elasticsearch count**: `count_path: "count"`（`_count` エンドポイントの応答）

#### メモリモデルと max_response_bytes（保証範囲）

- `Accept-Encoding: identity` を固定送信し圧縮転送を使わない（wire バイト = 実体サイズとなり
  `max_response_bytes` の streaming 遮断が実効になる。サーバが圧縮を返したら Content-Encoding で拒否）
- 読み込みは chunk 単位で `max_response_bytes` を検査し、超過時点で全量確保前に切断
- **JSON parse 後は document 全体 + 正規化後 rows が同時にメモリへ載る**。これは設計上の保証範囲で、
  `max_response_bytes` はメモリ予算に対して十分小さく（既定推奨 **8 MiB**）設定する。大きな結果が
  必要なら「クエリ側で絞る」が正しい対処（本ツールの用途は要約・ファネルであり大量転送ではない）
- **保証範囲外**: 非同期 job / polling（Redash の query 実行 job 等。one-shot JSON API のみ対応）、
  streaming JSON parser、レスポンス DSL（ES / Redash のクエリ内容）の静的検証

## doctor サブコマンド — 接続とリモート enforcement の事前診断

リモート接続が入ると「実際に read-only role で繋がるか」を実行前に確かめたくなる。`doctor` は
実データに触れず（COUNT すら実行しない）接続・read-only・delivery を診断する:

```bash
python3 skills/wiki/scripts/tool_query_run.py doctor --wiki-root .wiki [--tool <id>] [--probe-write <tool-id>]
```

- 出力は固定列 `tool / check / status(OK|NG|SKIP) / reason_code / hint`（`--format table|json`）
- **read-only は独立 check に分解**される（同一接続では session と role を区別できないため）:
  - `session_readonly` — 実クエリと同じ transaction 内の read-only 状態を introspection
  - `role_grants` — pg: allowlist relation 全件で table-level INSERT/UPDATE/DELETE/TRUNCATE と
    column-level INSERT/UPDATE が false /
    mysql: `SHOW GRANTS` を parse し SELECT 以外の権限がないこと。**role 付与（MySQL 8 roles）や
    解析できない grant 行がある場合は `role_grants_incomplete` の SKIP**（fail-open せず、
    実効権限は `SHOW GRANTS ... USING` で確認が必要な旨を示す）
  - `role_write_denial` — 通常実行では機械検証しない（**SKIP** 既定）
  - `role_uninspected_privileges` — CREATE / TEMPORARY / EXECUTE 等は機械検証対象外（**SKIP** 明示）
- その他: `credential_resolves` / `tls` / `connectivity` / http は `http_allowlist`（dry-run、実送信しない）/
  `delivery_writable`（temp probe → 即削除）/ `audit`（doctor イベントの監査ログ書込可否。
  書込失敗は `audit_write_failed` の NG として計上し、exit 0 で無言に流さない）
- **exit code**: 0 = NG なし（SKIP は失敗扱いにしない） / 1 = NG あり / 2 = usage / 130 = 中断。
  summary に SKIP 件数を必ず含め、必須 check に SKIP があれば未検証件数を明示する。
  JSON は対象 check 名を `required_skips` に列挙する
- **`--probe-write <tool-id>`（二重 opt-in）**: `connection.canary_relation` を宣言した tool に対してのみ、
  canary への INSERT を試行し「拒否されること」を確認する。canary 未宣言なら probe 自体を拒否。
  対象が未知 tool / postgres・mysql 以外 / `--tool` と不一致の場合は usage エラー（exit 2）。
  拒否が **権限拒否（NOT_AUTHORIZED）** の場合のみ OK とし、接続断・timeout・relation 不在などの
  「その他の失敗」は `probe_inconclusive` の NG（あらゆる失敗を書込拒否成功と誤認しない）。
  本 connector は read-only session 専用のため、この probe は session read-only + role の**重畳**での
  拒否を確認する（role 単独の書込拒否を session から分離しては検証しない — role 側の第一情報源は
  `role_grants`）。MySQL の canary relation はトランザクショナルエンジン（InnoDB）必須であり、
  非トランザクショナルエンジンでは rollback しても probe の書込が残り得る
- **TLS check**: 接続成立をもって「verify-full / CA+hostname 検証つきの TLS ネゴシエーション成立」を
  OK とする（緩和宣言時は SKIP、接続不能時も SKIP — 設定値だけで OK にしない）

## Selection Recipe の書き方

Recipe は「何をどう判断して取得するか」の説明層。`{wiki_root}/concepts/` に通常の記事として置く。

- **category**: `practices` / **tags**: `selection-recipe` + ドメインタグ
- テンプレート: `skills/wiki/assets/selection-recipe-template.md`
- **source_refs の埋め方（必須 — page-template.json は minItems: 1）**: Recipe の出典は
  「初回実施時の依頼内容・判断メモ」。依頼の要約と裁定の経緯を
  `{wiki_root}/raw/articles/{slug}.md` に immutable に保存し、そのパスを source_refs に
  書く（wiki-ingest のテキスト取り込みと同じ手順）。既存の raw ソースに判断根拠が
  ある場合はそれを指してもよい。空配列は lint（missing frontmatter / format violation）で
  落ちる
- 必ず書くこと:
  - 対象定義（業務言葉で 1 行）と、それを SQL 条件に落とすときの判断（なぜこの条件で表現するか）
  - **除外条件とその理由**（Why not — 「なぜ○○は含めないか」が Recipe の最重要情報）
  - ファネル構成（どの順で条件を足すと承認者が検算しやすいか）
  - tool_id と主要テーブル・key_columns
  - 実施ログ（日付 / plan_id / 件数 / 依頼受領〜引き渡しの所要時間 / 気づき）
- 書かないこと: 接続情報・上限値（catalog の写しは陳腐化する。tool_id で参照するだけにする）

### 昇格基準（いつ Recipe 記事にするか）

1. **2 回目の同種依頼が来た時点で必ず作る**（1 回目はセッション内メモでよい）
2. 1 回目でも、除外条件の判断に業務知識（例: 「テスト用アカウントは `email LIKE '%@example.com'` で除外」）
   が必要だった場合は作る — その判断こそが外在化する価値のある資産
3. 実行のたびに実施ログ節へ追記し、判断が変わったら本文を更新する（履歴は git が持つ）

## サンプル catalog のセットアップ

`.wiki/tools/catalog.json` の `sample-events-db` は**形見本**であり、そのままでは
実行できない（DB ファイルと delivery 先が存在しない）。試すには:

```bash
# 1. DB fixture を作る（catalog の connection.path に合わせる）
python3 - <<'EOF'
import sqlite3, pathlib
pathlib.Path(".wiki/data").mkdir(exist_ok=True)
conn = sqlite3.connect(".wiki/data/sample-events.sqlite3")
conn.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, email TEXT);
CREATE TABLE IF NOT EXISTS registrations (user_id INTEGER, event TEXT);
CREATE TABLE IF NOT EXISTS refunds (user_id INTEGER, amount INTEGER);
""")
conn.commit(); conn.close()
EOF

# 2. delivery 先を作る（catalog の delivery.allowed_dirs に合わせる）
mkdir -p .wiki/outputs/deliveries

# 3. 検証
python3 skills/wiki/scripts/tool_query_run.py catalog-validate --wiki-root .wiki
```

実データを扱う tool を登録する場合は、既存 DB への path（または base_dir）を宣言し、
`allowed_tables` を必要最小限にして PR レビューを経る。

## catalog の変更手順

catalog は git 管理の実行契約。変更（テーブル追加・上限緩和・delivery 先追加）は:

1. `.wiki/tools/catalog.json` を編集
2. `python3 skills/wiki/scripts/tool_query_run.py catalog-validate --wiki-root .wiki` で検証
3. **通常の PR / commit レビューを経る**（Wiki 記事の編集では安全境界を変更できない、が設計原則）

catalog 変更後、既存の承認済み plan は `catalog_digest_mismatch` で実行不能になる（意図した挙動）。

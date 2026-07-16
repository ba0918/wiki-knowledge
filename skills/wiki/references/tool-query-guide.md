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

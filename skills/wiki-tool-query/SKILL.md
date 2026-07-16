---
name: wiki-tool-query
description: >
  catalog 登録済みデータソース（sqlite）への制約・監査付きアドホック集計を、dry-run 承認フローで実行する。
  「補填対象者を抽出して」「DB から対象者リストを出して」「アドホック集計」「tool query」で使用する。
  Selection Recipe 記事（practices）を参照して SQL を組み、prepare → 人間承認 → execute で結果 CSV を delivery する。
---

# Wiki Tool Query

catalog 登録済みデータソースへの「自由な質問 + 制約された実行」。LLM が SQL を組んでよいが、
実行前に dry-run 計画（対象定義・選定ファネル・想定件数レンジ）を人間が承認する。

**wiki_root の取得**: `AGENTS.md` の `wiki_root:` フィールドを読む（未設定なら wiki-init を案内）。

**前提**: `{wiki_root}/tools/catalog.json`（git 管理）に対象 tool が登録済みであること。
catalog が実行契約の真実源であり、Wiki 記事（Selection Recipe）は説明層 — 記事の編集では
接続先・allowlist・上限などの安全境界は変わらない。

## プロセス

### 1. Recipe 参照と SQL 組み立て

1. 依頼内容に対応する Selection Recipe 記事（`category: practices`、tags に `selection-recipe`）を
   `{wiki_root}/index.md` から探して読む。初回（Recipe がない）場合は依頼者への質問で
   対象定義・除外条件を確定する
2. Recipe とユーザー依頼から本実行 SQL と**選定ファネル COUNT SQL**（条件を1段ずつ足した件数見積もり）を組む
3. SQL はファイルに書く（`{wiki_root}/.cache/` などの一時パスでよい。bundle に bytes コピーされるため以後の編集は無関係）
4. 書き方の詳細: [tool-query-guide.md](../wiki/references/tool-query-guide.md)

### 2. prepare（dry-run）

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py prepare \
  --wiki-root {wiki_root} --tool <tool_id> \
  --sql-file <main.sql> \
  --count-sql "<段の説明>=<count1.sql>" --count-sql "<次の段>=<count2.sql>" \
  --key-columns <col>... --expected-rows <min>:<max> --deliver-to <dir> --format json
```

- **承認前に COUNT だけは走る**: prepare のファネル COUNT は本実行と同じ enforcement
  （read-only 三重防御 / allowed_tables / timeout）と監査記録を通る。このことをユーザーに明示する
- 生成された immutable proposal bundle（`outputs/toolquery-plans/{plan_id}/`）が以後の唯一の実行対象

### 3. 承認依頼（summary-first で提示）

ユーザーに以下の順で提示する。SQL 本文は折りたたみ（`<details>`）で添付:

```
対象定義: <1行で>
inclusion / exclusion: <箇条書き>
ファネル: <label>: <count> 件 → <label>: <count> 件 → …
想定件数レンジ: <min>〜<max> 件
delivery 先: <dir>
tool: <tool_id> / plan_id: <plan_id> / sql_digest: <digest>
```

選択肢は **3 択のみ**（自動承認となるデフォルトは設けない）:

1. **実行** → ユーザー本人に approve コマンドを案内する（下記）
2. **条件を修正** → SQL を直して prepare からやり直し（= 新 plan_id）。旧 plan の扱いは
   事実どおりに伝える: 未承認なら draft のまま実行不能。**承認済みで TTL 内なら実行可能なまま残る**
   ため、破棄したい場合は「実行しない」運用であることを提示する
3. **中止**

### 4. approve（人間が実行する — LLM は代行しない）

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py approve \
  --wiki-root {wiki_root} --plan <plan_id> --approved-by <名前>
```

- **LLM は approve コマンドを実行しない**。ユーザーに `! <コマンド>` などでの自己実行を依頼する
- approve は summary（plan_id / tool / sql_digest / 想定件数 / delivery / expires_at / ファネル）を
  再表示し、確認プロンプト（TTY 必須、stderr）で `yes` 入力を求める。JSON の手編集は不要
- 承認の有効期限は prepare から 24 時間（expires_at）

### 5. execute と完了報告

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/wiki/scripts/tool_query_run.py execute \
  --wiki-root {wiki_root} --plan <plan_id> --format json
```

完了テンプレート（値は execute の JSON 出力から埋める）:

```
✅ 実行完了
- 取得件数: <row_count> 件（想定 <min>〜<max> 件の範囲内）
- manifest: 重複 key <duplicate_key_count> 件 / NULL <要点> / csv_sha256: <digest>
- 無害化したセル: <sanitized_cell_count> 件
- delivery: <dir>/<run_id>/（result.csv + manifest.json）
- published_at: <published_at> / plan_id: <plan_id>
```

execute の JSON 出力に `warnings` が含まれる場合（published 監査イベントや
receipt の記録失敗）は、その旨をテンプレートに追記して報告する — publish 自体は
成功しており、監査の欠損は Phase B の reconcile 対象。「監査済み」と誇張しない。

**失敗テンプレートの適用判定**: execute が失敗したら、まず
`{wiki_root}/outputs/toolquery-plans/{plan_id}/state.json` の `status` を確認する:

- `status: approved` のまま → 承認は未消費（検証マトリクスで拒否された等）。
  原因を直せば**同じ plan を再 execute できる**（TTL 内なら）
- `status: consumed` → 承認は消費済み。以下のテンプレートで報告する:

```
❌ 実行失敗: <reason>
この plan の承認は消費済みです（consumed = 承認の消費であり実行成功ではない）。
再実行するには新しい prepare → 承認が必要です。条件はそのままで再 prepare しますか？
```

### 6. 実施記録と Recipe 昇格

- 案件完了後、Recipe 記事の新規作成・更新を提案する（判断・除外条件・ファネル構成の変化を反映）。
  テンプレート: `${CLAUDE_PLUGIN_ROOT}/skills/wiki/assets/selection-recipe-template.md`、
  昇格基準: [tool-query-guide.md](../wiki/references/tool-query-guide.md)
- 依頼受領時刻を実施記録（Recipe 記事の実施ログ節）に残す（所要時間計測の起点。終点は監査ログの published、
  承認待ちは approved イベントで控除）

## 保証範囲（ユーザーに聞かれたら答える・誇張しない）

**守る**: 承認後の SQL・ファネル・delivery 先の*偶発的*変更・取り違え・陳腐化（catalog 変更後の実行）・
再実行（replay）の検出と拒否。read-only 逸脱・allowlist 外アクセス・上限超過の拒否。

**守らない（PoC の限定）**:

- 同一 OS ユーザーで動く悪意あるプロセスによる proposal/approval ファイル改竄の検出
  （権限分離がないため暗号学的な真正性証明はしない）。人間承認の真正性は本スキルの運用フロー +
  git 管理 catalog のレビューで担保する**運用上の性質**であり、スクリプトが証明する性質ではない
- DB スナップショットの束縛（prepare の COUNT と execute の間に DB 状態は変わり得る。
  manifest の `data_as_of` と想定件数レンジ照合が乖離の検出線）
- credential は prompt・argv・stdout/stderr・監査ログ・エラーメッセージに載せないが、
  同一 OS ユーザー権限でのファイル読み取り自体は防げない
- delivery 先の no-clobber 保証は「全 writer が本スクリプト経由」である前提

## 制約（スクリプトが enforcement する）

- SELECT / WITH のみ（複文・コメント開始不可）。read-only 三重防御 + relation allowlist（catalog の `allowed_tables`）
- 出力上限: catalog の `limits`（max_rows / max_result_bytes / max_cell_bytes / timeout_sec）
- 結果データは delivery 先に引き渡して非保持。監査ログ（`outputs/toolquery-audit.jsonl`）は
  値を含まないメタデータのみ
- CSV は式インジェクション無害化（OWASP 準拠）済み

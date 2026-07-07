# Schema 体制の裁定 — v0 を schema-of-record に、v1 は条件付き standby 資産に

**Cycle ID:** 20260707194819
**Type:** Decision + Hardening
**Created:** 2026-07-07 19:48:19
**Status:** 🟢 Complete（2026-07-07 全 Work Item 完了、493 tests pass）
**Related:** docs/plans/20260408163658_source-agnostic-knowledge-pipeline.md（Paused）/ docs/plans/20260703224551_repo-ingest-mvp.md / docs/ideas/20260703222307_cross-repo-wiki-gitlab-fetcher.md

## Overview

リポジトリには2つのスキーマ体制が併存している:

- **v0（稼働中）**: `page-template.json`。全12記事、全消費側スクリプト（lint / graph_gen / trust_score / gap_detect / wikilink_render）、SKILL.md の compile 手順、wiki-init テンプレートが依拠する。
- **v1（休眠中）**: `page-template-v1.json` + `lib/domain/types.py` の `Article` 集約 + `lib/service/schema.py` / `wiki_repo.py` / `migrations/` 一式（テスト完備、実配線ゼロ）。source-agnostic pipeline（Phase 0.10 で Paused）の基礎工事。

repo-ingest MVP は v1 との `source_version` 意味衝突を**解決せず回避**した（`source_revision` へ改名、マッピング表は「Phase 1 で確定」と先送り）。新機能が増えるたびにこの回避コストが再発するため、5リポジトリ実適用の前に体制を1本化する裁定を行った。

## 裁定

**v0 を schema-of-record と正式宣言する。v1 は削除も全面採用もせず、「採用トリガー付きの standby 資産」として再定義する。**

### 採用トリガー（日付ではなく不変条件）

> **concepts/ に「raw/ から再導出できない状態」を書き込む最初の機能（`wiki review resolve` / claim 仲裁 / 出典なし promote 等）は、v1 migration（migrate.py CLI + 全記事昇格 + 消費側の v1 対応）と同一サイクルでリリースしなければならない。**

このトリガーが発火するまで、concepts/ は v0 のままでよい。5リポジトリ実適用は v0 のまま進めてよい（本裁定が公式にアンブロックする）。

## 根拠

1. **v1 最大主義の前提は concepts/ を一次データとみなしているが、本アーキテクチャでは concepts/ は派生物。** 「元データを壊さず、後から読む側を賢くする」原則により、真に不可逆なのは raw/（immutable）であり、concepts/ は raw から再コンパイル可能。「後から migration 困難」（pipeline plan の Q12-1）は、記事がレビュー裁定・claim 仲裁など再導出不能な状態を蓄積し始めて初めて成立する。それを書き込む機能は現在ひとつも存在しない。
2. **5リポジトリ適用が必要とする不可逆性の防衛は raw/ 側で既に完了している。** 差分 re-compile（将来）は「repo HEAD vs `source_revision`」の比較で動き、raw フロントマターの revision 固定（repo-ingest MVP 実装済み）だけを要求する。記事レベルの `sources[].content_hash` が今すぐ守るものはない。
3. **最大主義でもスキーマ変更は防げなかった。** v1 は Slack を第1号 Fetcher と想定して設計されたが、実際に先に来たのは repo であり、`Source` に `revision` フィールドが欠けていた。未来は設計を裏切る — だからこそ「必要になった時に採用」が正しい（gist の「まず使え、ツールは必要になってから」とも一致）。
4. **v0 継続の実害2件は外科的に解消できる**（下記 Work Items）。全面採用の場合は lint(842行)・graph・trust・gap・SKILL.md compile・wiki-init の全 v1 対応が必要で、validator / claims 未実装のため即時の実益はほぼゼロのまま5リポジトリ適用が後ろ倒しになる。

## Work Items

| # | 内容 | 種別 |
|---|------|------|
| 1 | 本 decision record の作成 | docs |
| 2 | `Source.revision: str \| None` を v1 スキーマに正式追加（`types.py` + `schema.py` round-trip + `page-template-v1.json`、TDD） — repo-ingest の回避を過去のものにする | code |
| 3 | lint に schema_version ガード追加（format_violations のサブチェック `schema_version_unadopted`。v1 記事混入時は意味不明な violation の山ではなく、migration 案内付きエラー1件を出し当該記事の v0 規範チェックをスキップ、TDD） | code |
| 4 | `references/frontmatter-schemas.md` のマッピング表を「Phase 1 で追加予定」→「確定」に更新 | docs |
| 5 | `references/architecture.md` に Schema 体制の節を追加（v0 = schema-of-record / v1 standby + 採用トリガー / lib/ の現役部分と待機部分の区分） | docs |
| 6 | pipeline plan（20260408163658）に裁定注記、status.md の Paused エントリ更新、CLAUDE.md に Schema 体制1行 + format_violations 説明更新、SKILL.md lint 節の format_violations 説明更新 | docs |

### lib/ の地位区分（Work Item 5 の要旨）

- **現役（sanctioned 共有 service 層）**: `path_validator.py` / `clock.py` / `file_lock.py` / `lib/domain/repo.py` / `repo_clone.py` — repo_ingest が既に消費。今後の決定論スクリプト（SKILL.md からの抽出等）もここを使う
- **standby（採用トリガーで起動）**: `lib/domain/types.py` の Article 集約 / `schema.py` の v1 load・dump / `wiki_repo.py` / `migrations/` 一式 — 削除しない。トリガー発火時に v1.1 として再検証（claims の要否等はその時点で再訪）

## Acceptance Criteria

- [x] `Source` に `revision` が追加され、YAML round-trip テストが通る（省略時は出力に現れない）
- [x] `schema_version` を持つ記事を lint すると `schema_version_unadopted` エラー1件のみ（type_violation / missing_frontmatter 等のカスケードなし）
- [x] 既存 486 tests + 新規テスト全 pass（493 passed / 1 xfailed、実 Wiki lint も No findings・exit 0）
- [x] マッピング表・architecture.md・status.md・CLAUDE.md・SKILL.md・pipeline plan が裁定と整合
- [x] issue との整合: 本裁定は issue 20260703213243（graph 統合リファクタ）のスコープに触れない（並行可能）

## Non-Goals

- migrate.py CLI の実装（採用トリガー発火時に実施）
- claims / knowledge_time / status モデルの再設計（同上 — 今直すと再度陳腐化するため）
- trust_score 鮮度モデルのスナップショット方針との矛盾解消（Pitch 2 のスコープ）
- SKILL.md の決定論ロジック抽出（Pitch 3 のスコープ）

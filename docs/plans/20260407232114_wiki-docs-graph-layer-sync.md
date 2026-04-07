# wiki スキル ドキュメント整合性修正（graph layer MVP 反映）

**Cycle ID:** 20260407232114
**Type:** Enhancement / Documentation fix
**Created:** 2026-04-07 23:21:14

## Overview

graph layer MVP（コミット d1a58ff）の導入後、実装は健全だがドキュメント（SKILL.md / CLAUDE.md / references）が graph layer を反映していない。investigate スキルで Critical 4件 / Warning 4件が検出済み。本 plan は docs 4ファイルを実コードと一致させ、cycle 実行時に graph_gen が正しく流れる状態にすることを目的とする。

## Goals

- Claude が SKILL.md / CLAUDE.md を読んで graph layer の存在と lint 実行手順を正しく理解できる
- cycle 実行が graph_gen ステップ込みで成功する（exit 2 で死なない）
- 3層構造（concepts → inventory → graph）が architecture.md で明示される
- lint-procedure.md の検出方法説明が現行コードと一致する
- SKILL.md lint 節の検出項目数が 8 に修正される

## Non-Goals

- W3: page-template.json category enum 固定（別 issue）
- 新規 reference ファイル追加（architecture.md 拡張で吸収）
- Layer 3（cache, coref-detect 等）の前倒し

## Design Decision: W1（graph 欠如時の扱い）

選択肢:
- (a) `lint-wiki.py` 側で graph 欠如時に自動生成フォールバック
- (b) `wiki-cycle` 側で明示的に `graph_gen` を呼ぶ
- (c) 両方

**決定: (c) 両方 — ただし責務を明確に分離する。**

根拠:
- 層責務の観点では (b) が自然。前回 graph layer plan で「lint からの層越境を撤廃、graph 欠如時は exit 2 で誘導」とした経緯があり、cycle は orchestrator として graph_gen → lint の順に明示的に呼ぶべき。
- ただし、ユーザーが `wiki-lint` を単独実行したときに exit 2 + 手動 `graph_gen` 実行を要求するのは UX が悪い。pure 関数原則を破らない範囲でフォールバックを提供する価値がある。
- 妥協案: lint 側のフォールバックは**オプトイン**（`--auto-graph` フラグ、デフォルト OFF）にする。デフォルト動作は exit 2 維持（層越境なし）、ユーザーが明示的に opt-in した場合のみ graph_gen を subprocess 呼び出しする。これで cycle 側は (b) の明示呼出を堅持しつつ、単独実行ユーザーは `--auto-graph` で救済される。
- cycle 側 (b) は必須。lint フォールバックは補助的なセーフティネット。

## Architecture / Layer Analysis

対象レイヤー:
- **Documentation layer**: CLAUDE.md, SKILL.md, references/*.md — 実コードの写像
- **Orchestrator layer (commands)**: wiki-cycle.md, wiki-lint.md — 実行手順を記述
- **Script layer**: lint-wiki.py — `--auto-graph` オプトインフラグ追加（最小変更）

依存方向は既存を維持（commands → SKILL.md → scripts）。層越境は発生しない。

## Implementation Steps

### Step 1: architecture.md に 3層構造を明示（C4）

**File:** `skills/wiki/references/architecture.md`

- 既存の 3層構造図を更新し、`concepts`（source of truth）→ `inventory`（派生インデックス）→ `graph`（派生グラフ）の派生関係を明示
- graph layer の役割（dead_link / orphan 検出の基盤）を1段落で追記
- graph_gen の位置づけ（compile の後、lint の前に実行される派生生成ステップ）を図に反映

### Step 2: lint-procedure.md 更新（C3, W2）

**File:** `skills/wiki/references/lint-procedure.md`

- dead_link / orphan の検出方法セクションを「graph layer 経由」に書き換え
- 実スクリプトの 8 項目（dead_link, orphan, missing_source, missing_frontmatter, coverage_gap, link_quality, article_quality, format_violations）を列挙
- graph 欠如時の挙動（デフォルト exit 2 / `--auto-graph` でフォールバック）を記述
- Trust Score / Gap Detection への言及を追記（SKILL.md と整合）

### Step 3: SKILL.md lint 節更新（C1, W4）

**File:** `skills/wiki/SKILL.md`

- `## lint` セクションを graph 経由説明に書き換え
- 検出項目を 3 → 8 に修正
- `--use-graph` デフォルト ON / exit 2 / `--auto-graph` オプトインを明記
- cycle 節に graph_gen ステップを compile と lint の間に追加

### Step 4: CLAUDE.md 更新（C2）

**File:** `CLAUDE.md`

- `## Lint` セクションに `--use-graph` デフォルト ON / exit 2 / graph_gen 事前実行を追記
- `## Graph Layer` セクション新設（graph_gen スクリプト場所・出力先・役割・再生成コマンド）
- `## Articles` / 他セクションは変更なし

### Step 5: lint-wiki.py に `--auto-graph` フラグ追加（W1 の補助）

**File:** `skills/wiki/scripts/lint-wiki.py`

- argparse に `--auto-graph`（store_true, default False）追加
- graph ファイル欠如かつ `--auto-graph` 時のみ graph_gen を subprocess 呼び出し
- デフォルト挙動（exit 2 で誘導）は維持 — 既存テスト破壊なし
- 追加テスト: `--auto-graph` 指定時の fallback パス / 未指定時の exit 2 維持

### Step 6: commands/wiki-cycle.md, wiki-lint.md 追記（W1 本体）

**File:** `commands/wiki-cycle.md`

- cycle フロー記述に「compile → **graph_gen** → lint」のステップを明示

**File:** `commands/wiki-lint.md`

- graph 欠如時の 2 択（事前に graph_gen を手動実行、または `--auto-graph` 指定）を記述

## Test List

### Documentation（手動検証）

- CLAUDE.md を再読し、graph layer の存在・lint 実行手順が把握できるか
- SKILL.md の cycle 節の手順通りに graph_gen → lint が流れるか
- architecture.md の図で concepts / inventory / graph の派生関係が読み取れるか
- lint-procedure.md の検出方法説明と `lint-wiki.py` の実装が一致するか

### Automated（lint-wiki.py）

- `test_lint_wiki.py` に以下を追加:
  - graph 欠如 + `--auto-graph` 未指定 → exit 2
  - graph 欠如 + `--auto-graph` 指定 → graph_gen が呼ばれ、その後通常 lint 実行
  - graph 存在 + `--auto-graph` 指定 → graph_gen は呼ばれない（スキップ）

### Integration（手動）

- `/wiki-cycle` を実走させ、compile → graph_gen → lint が exit 0 で完走すること

## Security Checklist

- `--auto-graph` の subprocess 呼び出しは固定パス（同梱 `graph_gen` スクリプト）のみ、ユーザー入力経由のパス展開なし
- 既存の path traversal ガードは維持
- ドキュメント変更のみのステップ（1-4, 6）はセキュリティ影響なし

## Progress Tracking

| Step | Description | Status |
|------|-------------|--------|
| 1 | architecture.md 3層構造図更新 | 🟢 |
| 2 | lint-procedure.md graph 経由記述 + Trust/Gap 言及 | 🟢 |
| 3 | SKILL.md lint/cycle 節更新 | 🟢 |
| 4 | CLAUDE.md Lint 節修正 + Graph Layer セクション新設 | 🟢 |
| 5 | lint-wiki.py `--auto-graph` フラグ追加 + テスト | 🟢 |
| 6 | commands/wiki-cycle.md, wiki-lint.md 追記 | 🟢 |

## Risks / Open Questions

- `--auto-graph` フォールバックが層越境の議論を再燃させる可能性 → opt-in かつデフォルト OFF で責務を明確化することで緩和
- graph_gen スクリプトの CLI インターフェース確認が必要（Step 5 実装時に確認）

## Related

- 前回 plan: graph layer MVP 導入（コミット d1a58ff）
- investigate レポート: Critical 4 / Warning 4

# Project Status

**Last Updated:** 2026-07-03 23:10:00

---

## 🎯 Current Session

| Cycle ID | Feature | Started | Phase | Plan |
|----------|---------|---------|-------|------|
| `20260703224551` | repo ingest MVP（5リポジトリ横断 Q&A wiki の入口） | 2026-07-03 22:45:51 | 🟢 Complete | [plan](./plans/20260703224551_repo-ingest-mvp.md) |

**Current Focus:** repo ingest MVP 完了（A: 手動 dogfooding ゲート通過 → B: compile 規範 → C: TDD 実装 486 tests → D: ripgrep+fd 横断 wikilink 機械検証）。次の候補: 仕事の5リポジトリへの実適用、raw slug 命名統一（plan の Phase D 摩擦リスト参照）、pipeline Phase 0 再開。

## ⏸️ Paused

| Cycle ID | Feature | Phase | Note |
|----------|---------|-------|------|
| `20260408163658` | Source-Agnostic Knowledge Pipeline | 🔵 Phase 0 途中（0.10 まで commit 済み） | migrate.py CLI + 12記事 v1 昇格（Phase 0.11-0.13）が未了。repo ingest MVP と型境界を共有（plan v2 参照） |

---

## 📌 Phase 2+ ロードマップ

| ID | 機能 | 優先度 | Status |
|----|------|--------|--------|
| 2a | QueryLog 蓄積 | **P0** | 🟢 Complete |
| 3a | Trust Score | P1 | 🟢 Complete |
| 2b+2c | Gap Detection + Auto Ingest 提案 | P2 | 🟢 Complete |
| 3b | Lint 強化 | P3 | 🟢 Complete |
| 4-5 | Multi-Resolution / Portal 等 | 保留 | — |

詳細: [Phase 2+ 分解メモ](./ideas/20260405_phase2-roadmap-decomposition.md)

## 📝 Design Decisions（このセッションで決まったこと）

- **単一ディレクトリ集約**: Wiki データは `.wiki/`（デフォルト）に集約。`wiki-init --path` でカスタマイズ可能
- **CLAUDE.md の wiki_root**: YAML フロントマターで宣言（テーブル形式は LLM が読みにくいため却下）
- **パス解決ルール**: フロントマター = `{wiki_root}` 基準、本文 Markdown リンク = ファイルからの相対パス
- **compile 対象選択**: デフォルト = 未コンパイル自動検出、パス指定、`--all` の3パターン
- **明示的呼び出し**: `/wiki` スラッシュコマンド方式（description ワードトリガーは信用しない）
- **1スキル統合**: SKILL.md 297行（~2.5K tokens）で init/ingest/compile/query/lint/cycle を統合。誤差レベル

## 📝 Design Decisions（このセッションで追加）

- **プラグイン化**: `.claude-plugin/plugin.json`（name: `wiki`）で独立プラグインとして登録。claude-skills に混ぜない
- **サブコマンドパターン**: `commands/wiki-{workflow}.md` → `wiki:wiki` に `$ARGUMENTS` ルーティング。既存 claude-skills と同じパターン
- **`$ARGUMENTS` ルーティング**: SKILL.md 内で先頭キーワード（init/ingest/compile/query/lint/cycle）で分岐。引数なしはヘルプ表示

## 📂 成果物一覧

```
.claude-plugin/
├── plugin.json                 (プラグインメタデータ)
└── marketplace.json            (マーケットプレイス登録)

commands/
├── wiki.md                     (親コマンド — /wiki)
├── wiki-init.md                (/wiki-init)
├── wiki-ingest.md              (/wiki-ingest)
├── wiki-compile.md             (/wiki-compile)
├── wiki-query.md               (/wiki-query)
├── wiki-lint.md                (/wiki-lint)
└── wiki-cycle.md               (/wiki-cycle)

skills/wiki/
├── SKILL.md                    (297行 — 全6操作のルーティング + 手順)
├── references/
│   ├── architecture.md         (3層構造、4相パイプライン)
│   ├── compilation-guide.md    (語調、wikilink密度、出典ルール)
│   ├── frontmatter-schemas.md  (各種フロントマター定義)
│   ├── lint-procedure.md       (6つのLLM駆動チェック + 修復フロー)
│   └── prompts.md              (各フェーズのプロンプトテンプレート)
├── scripts/
│   ├── lint-wiki.py            (自動lint: 8チェック + --format table/json/report)
│   ├── test_lint_wiki.py       (lint-wiki テスト — 44テスト)
│   ├── querylog_stats.py       (QueryLog 集計)
│   ├── querylog-stats.py       (↑へのシンボリックリンク)
│   ├── test_querylog_stats.py  (querylog_stats テスト — 15テスト)
│   ├── trust_score.py          (Trust Score 算出)
│   ├── test_trust_score.py     (trust_score テスト)
│   ├── gap_detect.py           (Gap Detection + Auto Ingest 提案)
│   └── test_gap_detect.py      (gap_detect テスト)
└── assets/
    ├── wiki-article-template.md
    ├── index-template.md
    ├── log-template.md
    └── claude-md-template.md

.wiki/                          (実データ — 記事7つ)
├── .gitignore                  (querylog.jsonl を git 管理外に)
├── concepts/*.md               (4記事)
├── raw/articles/*.md           (ソース3件)
├── schema/{page-template,categories,querylog-schema}.json
├── index.md
└── log.md
```

---

## 📜 Session History

_Archived sessions can be found in [session-history.md](./session-history.md)._

---

## 🔗 Quick Links

- [Implementation Plan](./plans/20260406053703_lint-enhancement.md)
- [Idea Memo](./ideas/20260405183234_llm-wiki-knowledge-base-as-claude-skill.md)
- [Project Root](../)

---

**Note:** This file is auto-managed by the `plan` skill.

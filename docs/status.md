# Project Status

**Last Updated:** 2026-04-06 03:35:00

---

## 🎯 Current Session

_No active session_

---

## 📌 Phase 2+ ロードマップ

| ID | 機能 | 優先度 | Status |
|----|------|--------|--------|
| 2a | QueryLog 蓄積 | **P0** | 🟢 Complete |
| 3a | Trust Score | P1 | 🟢 Complete |
| 2b+2c | Gap Detection + Auto Ingest 提案 | P2 | 🟢 Complete |
| 3b | Lint 強化 | P3 | ⚪ Pending |
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
│   ├── lint-wiki.py            (自動lint: dead link/orphan/missing source — テスト済み)
│   ├── querylog_stats.py       (QueryLog 集計 — 15テスト)
│   ├── querylog-stats.py       (↑へのシンボリックリンク)
│   └── test_querylog_stats.py  (querylog_stats テスト)
└── assets/
    ├── wiki-article-template.md
    ├── index-template.md
    ├── log-template.md
    └── claude-md-template.md

.wiki/                          (実データ — 記事4つ)
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

- [Implementation Plan](./plans/20260405185738_llm-wiki-skill.md)
- [Idea Memo](./ideas/20260405183234_llm-wiki-knowledge-base-as-claude-skill.md)
- [Project Root](../)

---

**Note:** This file is auto-managed by the `plan` skill.

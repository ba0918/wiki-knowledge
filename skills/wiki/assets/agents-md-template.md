# AGENTS.md

This file is the shared project instruction source for Claude Code, Codex CLI, and other agents.
`CLAUDE.md` must stay a thin wrapper that imports this file with `@AGENTS.md`.

## Wiki Knowledge Base

このプロジェクトは LLM Wiki Knowledge Base です。

wiki_root: WIKI_ROOT_PATH

## Scope

SCOPE_DESCRIPTION

## Conventions

- Wiki記事は `{wiki_root}/concepts/` にフラットに配置（`{slug}.md`）
- ソースドキュメントは `{wiki_root}/raw/` に immutable に保存
- 相互参照は `[[wikilink]]` 記法を使用
- フロントマターは `{wiki_root}/schema/page-template.json` に準拠
- カテゴリは `{wiki_root}/schema/categories.json` で管理

## Articles

_まだ記事がありません_

## Research Gaps

_未調査のトピックがあればここに記録_

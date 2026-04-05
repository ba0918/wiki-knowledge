---
wiki_root: .wiki
created: 2026-04-05
---

# Wiki Knowledge Base

このプロジェクトは LLM Wiki Knowledge Base です。

## Scope

LLM向けの知識ベース構築の仕組みを実験するプロジェクト。Karpathy の LLM Wiki コンセプトを Claude Skill として実装し、既存プロジェクトに導入可能な形で提供する。

## Conventions

- Wiki記事は `.wiki/concepts/` にフラットに配置（`{slug}.md`）
- ソースドキュメントは `.wiki/raw/` に immutable に保存
- 相互参照は `[[wikilink]]` 記法を使用
- フロントマターは `.wiki/schema/page-template.json` に準拠
- カテゴリは `.wiki/schema/categories.json` で管理

## Articles

- [[llm-wiki-knowledge-base]] — LLM Wiki Knowledge Base（concepts）

## Research Gaps

_未調査のトピックがあればここに記録_

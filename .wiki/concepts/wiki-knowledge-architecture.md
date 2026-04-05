---
title: Wiki ナレッジ構築アーキテクチャ
type: wiki
source_refs:
  - "raw/articles/20260405-wiki-knowledge-architecture.md"
created: 2026-04-05
updated: 2026-04-05
category: concepts
tags: [wiki, architecture, ingest, compile, index, knowledge-base]
related:
  - "concepts/llm-wiki-knowledge-base.md"
---

# Wiki ナレッジ構築アーキテクチャ

> このプロジェクトの知識ベースは **Ingest → Compile → Index** の3層構造で構築される。人間がソースをキュレーションし、LLM が構造化を担う。

## 3層アーキテクチャ

### 1. Ingest（ソース取り込み）

生のドキュメント（URL、ファイル、記事）を `.wiki/raw/` に **immutable** で保存する層。

- フロントマターに `source_url` と `scraped` 日付を記録し、取り込み履歴を追跡
- 一度取り込んだソースは変更しない（immutable 原則）
- ここが「事実の原典」として機能する

### 2. Compile（記事生成）

取り込んだソースを LLM が読み込み、`.wiki/concepts/` に構造化 Wiki 記事を生成する層。

コンパイル時のルール：

- **スキーマ準拠**: `page-template.json` で定義されたフロントマター（title, type, source_refs, created, updated, category, tags, related）
- **出典追跡**: `source_refs` で記事とソースの対応を明示
- **相互参照**: `[[wikilink]]` 記法で記事間リンク
- **カテゴリ分類**: `categories.json` の4カテゴリ（concepts / tools / practices / references）
- **フラット配置**: `{slug}.md` でネストなし

### 3. Index（インデックス管理）

記事カタログと変更履歴を管理する層。

- `.wiki/index.md` — 全記事をカテゴリ別に一覧化
- `.wiki/log.md` — 変更履歴の追跡

## ディレクトリ構造

| パス | 役割 |
|------|------|
| `.wiki/raw/` | ソースドキュメント（immutable） |
| `.wiki/concepts/` | コンパイル済み Wiki 記事 |
| `.wiki/schema/` | スキーマ・カテゴリ定義 |
| `.wiki/index.md` | 全記事カタログ |
| `.wiki/log.md` | 変更履歴 |

## 設計思想

[[llm-wiki-knowledge-base]] の Karpathy コンセプトに基づき、**人間はキュレーションと質問に集中し、構造化は LLM に委譲する** というアプローチを取る。Claude Skill として実装されているため、任意のプロジェクトに導入可能。

## 出典

- [Wiki ナレッジ構築アーキテクチャ — 3層構造の解説](../raw/articles/20260405-wiki-knowledge-architecture.md)

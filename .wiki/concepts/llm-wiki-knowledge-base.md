---
title: LLM Wiki Knowledge Base
type: wiki
source_refs:
  - "raw/articles/20260405-llm-wiki-knowledge-base.md"
created: 2026-04-05
updated: 2026-04-05
category: concepts
tags: [llm, wiki, knowledge-base, karpathy, claude-skill]
related:
  - "concepts/wiki-knowledge-architecture.md"
---

# LLM Wiki Knowledge Base

> Karpathy が提唱した LLM Wiki コンセプトを Claude Skill として実装し、既存プロジェクトに導入可能な知識ベース構築の仕組みを提供する。

## 概要

LLM 向けの知識ベース（ナレッジ）を構築するための仕組み。LLM がソースドキュメントを読み込み、相互参照付きの Markdown Wiki としてコンパイル・メンテナンスする。人間はソースのキュレーションと質問に集中し、構造化は LLM に委譲するというアプローチを取る。

## 背景

Karpathy の [LLM Wiki Gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) で提唱されたコンセプト。Gist のコメント欄には既に様々なパターンでの実装・検証事例が報告されている。

## 設計方針

- **Claude Skill として提供**: 構築フローをスキル化し、既存プロジェクトに導入可能にする
- **先人の実装を参照**: 複数の既存実装の良いところを取り入れて構築する
- **実験的アプローチ**: 仕組みが適切に機能するかどうかを検証する

## 出典

- [LLM Wiki Knowledge Base — プロジェクト構想](../raw/articles/20260405-llm-wiki-knowledge-base.md)

---
title: LLM Wiki Knowledge Base
type: wiki
source_refs:
  - "raw/articles/20260405-llm-wiki-knowledge-base.md"
  - "raw/articles/20260405-karpathy-llm-wiki-pattern.md"
created: 2026-04-05
updated: 2026-04-05
category: concepts
tags: [llm, wiki, knowledge-base, karpathy, claude-skill, rag, memex, persistent-artifact]
related:
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/llm-wiki-use-cases.md"
  - "concepts/llm-wiki-tooling.md"
---

# LLM Wiki Knowledge Base

> LLM がソースドキュメントを読み込み、相互参照付きの永続的な Markdown Wiki を構築・維持する。RAG のように毎回ゼロから知識を再導出するのではなく、知識を一度コンパイルして蓄積し続ける。

## コアアイデア：RAG との根本的な違い

従来の RAG（NotebookLM、ChatGPT ファイルアップロード等）は、質問のたびに生のドキュメントからチャンクを検索して回答を生成する。5つのドキュメントを横断する微妙な質問には、毎回断片をかき集めて組み立て直す必要がある。**知識の蓄積がない。**

LLM Wiki のアプローチは根本的に異なる：

- LLM が **永続的な Wiki を漸進的に構築・維持** する
- 新しいソースが追加されると、既存のページを更新し、矛盾を指摘し、合成を進化させる
- 知識は **一度コンパイルされ、最新に保たれる** — 毎回再導出されない
- 相互参照は既にそこにあり、矛盾は既にフラグされ、合成は読んだすべてを反映している

**Wiki は永続的で複利的なアーティファクト** である。ソースを追加し質問するたびに、リッチになり続ける。

## 役割分担

| 人間の役割 | LLM の役割 |
|-----------|-----------|
| ソースのキュレーション | 要約・構造化 |
| 探索と質問 | 相互参照の維持 |
| 分析の方向づけ | 整合性チェック |
| 意味の解釈 | ファイリングと記帳 |

> Obsidian は IDE、LLM はプログラマ、Wiki はコードベース — Karpathy

## 3層アーキテクチャ

詳細は [[wiki-knowledge-architecture]] を参照。

1. **Raw Sources** — キュレーションされたソースドキュメント。immutable。LLM は読むだけで変更しない。事実の原典。
2. **The Wiki** — LLM が生成する構造化 Markdown ファイル群。エンティティページ、コンセプトページ、比較、概要、合成。LLM が完全に所有。
3. **The Schema** — Wiki の構造・規約・ワークフローを LLM に伝える設定ドキュメント（CLAUDE.md 等）。ドメインに合わせて共同進化させる。

## 操作（Operations）

- **Ingest**: ソースを取り込み、要約ページを作成し、関連する既存ページを横断的に更新。1つのソースが 10-15 ページに影響しうる。
- **Query**: Wiki に対して質問し、引用付きの合成回答を得る。**良い回答は Wiki に新ページとして還元** できる — 探索も知識ベースに複利的に蓄積される。
- **Lint**: 定期的にヘルスチェック。矛盾、古くなった主張、孤立ページ、欠けている相互参照、調査すべきギャップを検出。

## なぜ機能するのか

知識ベースの維持で大変なのは、読むことや考えることではなく **記帳作業** — 相互参照の更新、要約の最新化、新旧データの矛盾の記録、ページ間の整合性維持。人間は維持コストが価値を上回ると Wiki を放棄する。LLM は飽きず、相互参照の更新を忘れず、1パスで 15 ファイルに手を入れられる。**維持コストがほぼゼロだから、Wiki は維持され続ける。**

## 歴史的背景

Vannevar Bush の Memex（1945）— 文書間の連想的な辿り（associative trails）を持つ個人的でキュレーションされた知識ストア — と精神的に近い。Bush のビジョンは Web よりもこちらに近かった：プライベートで、能動的にキュレーションされ、文書間のつながりが文書自体と同じくらい価値がある。Bush が解決できなかった「誰がメンテナンスするか」を LLM が担う。

## 設計方針（このプロジェクト）

- **Claude Skill として提供**: 構築フローをスキル化し、既存プロジェクトに導入可能にする
- **先人の実装を参照**: 複数の既存実装の良いところを取り入れて構築する
- **実験的アプローチ**: 仕組みが適切に機能するかどうかを検証する

## 出典

- [LLM Wiki Knowledge Base — プロジェクト構想](../raw/articles/20260405-llm-wiki-knowledge-base.md)
- [LLM Wiki — Karpathy's Original Pattern Document](../raw/articles/20260405-karpathy-llm-wiki-pattern.md)

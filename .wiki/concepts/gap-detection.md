---
title: Gap Detection — Wiki 知識ギャップの自動検出と Ingest 提案
type: wiki
source_refs:
  - "raw/articles/20260406-gap-detection-feature.md"
created: 2026-04-06
updated: 2026-04-06
category: concepts
tags: [gap-detection, auto-ingest, querylog, coverage, tokenization, knowledge-gap, phase2b]
related:
  - "concepts/querylog.md"
  - "concepts/trust-score.md"
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/llm-wiki-knowledge-base.md"
  - "concepts/graphify-knowledge-graph-concepts.md"
---

# Gap Detection

Gap Detection は、[[querylog]] ([↗](querylog.md)) に蓄積されたクエリデータを分析し、Wiki にまだ存在しない知識のギャップを自動検出する機能である。検出されたギャップに対して優先度付きの Ingest 提案（Auto Ingest 提案）を生成する。Phase 2b+2c として実装された。

## 目的と背景

[[llm-wiki-knowledge-base]] ([↗](llm-wiki-knowledge-base.md)) では、ユーザーのクエリに対して既存記事から回答を合成する。しかし Wiki の知識範囲には限界があり、回答できないトピック（ギャップ）が発生する。[[querylog]] ([↗](querylog.md)) の `gap_noted` フラグと `gap_topics` フィールドは、query 実行時にこれらのギャップを構造化データとして記録する。

Gap Detection はこのデータを集約・分析し、「最も頻繁にギャップとして検出されているトピック」を可視化する。Wiki の自律的な成長エンジンとしての役割を担う。

## カバレッジ計算

gap_topics に記録されたトピックが、実際に既存記事でカバーされていないかを確認するプロセス。

1. **トークン化**: トピック文字列と各記事（title, tags, slug, body）をトークンに分解
2. **重複率計算**: `len(topic_tokens & article_tokens) / len(topic_tokens)`
3. **閾値判定**: 全記事中の最大重複率が threshold（デフォルト 0.8）未満のトピックをギャップとして確定

例えば「RAG architecture」というギャップトピックが既に「RAG」について詳しく書かれた記事に含まれている場合、重複率が高くなり、ギャップとしては除外される。

## トークン化戦略

英語と日本語で異なるアプローチをとる:

| テキスト種別 | 戦略 | 例 |
|-------------|------|-----|
| 英語 | 小文字化 + スペース/ハイフン/アンダースコア分割 | `"RAG architecture"` → `{"rag", "architecture"}` |
| 日本語 | 文字 bigram | `"知識ベース"` → `{"知識", "識ベ", "ベー", "ース"}` |
| 混在 | ASCII/マルチバイトを分離して各戦略を適用 | 両方のトークンを union |

外部形態素解析器への依存を避けるため、日本語は簡易的な bigram 方式を採用した。`extract_tokens(text: str) -> frozenset[str]` のインターフェースで隔離されており、将来的に形態素解析器ベースの実装に差し替え可能。

## Auto Ingest 提案

確定したギャップごとに提案を生成する:

- **Priority**: `frequency × (1 - coverage)` を 0.0〜1.0 に min-max 正規化。頻度が高く、カバレッジが低いほど優先度が高い
- **Suggested Queries**: トピックに基づく検索クエリ候補
- **Related Articles**: 部分的に関連する既存記事のスラッグ（coverage が threshold/2 以上の記事）

提案は「提案のみ」で自動実行しない。ユーザーが `wiki ingest` で明示的に取り込む設計とした。

## データ型

全データ型は `frozen=True` で不変性を保証し、リストフィールドは `tuple` を使用する。[[trust-score]] ([↗](trust-score.md)) と同じ immutability 方針。

- **ConfirmedGap**: topic, frequency, coverage, related_articles
- **IngestProposal**: topic, priority, suggested_queries, related_articles

## 実装構造

コアロジックは全て純粋関数で実装されている。[[trust-score]] ([↗](trust-score.md)) と同じく、I/O は `load_articles()` と CLI エントリポイント `main()` に隔離。

既存の `load_querylog()`（querylog_stats.py）と `parse_frontmatter()`（lint-wiki.py）をインポートして再利用する。ハイフン付きファイル名のインポートは [[trust-score]] ([↗](trust-score.md)) と同じ `importlib` パターン。

### CLI

```
python3 gap_detect.py --wiki-root .wiki [--format table|json|report] [--threshold 0.8]
```

3フォーマット出力（table / json / report）は [[trust-score]] ([↗](trust-score.md)) と同パターン。report 形式では `{wiki_root}/outputs/reports/{YYYYMMDD}-gap-detect.md` に出力する。

## lint ワークフロー統合

[[wiki-knowledge-architecture]] ([↗](wiki-knowledge-architecture.md)) の Lint フェーズに統合され、[[trust-score]] ([↗](trust-score.md)) チェックの後に実行される。Priority が 0.7 以上の提案は lint レポートの 🔵 Info として記載される。

## Phase 2+ での位置づけ

[[querylog]] ([↗](querylog.md))・[[trust-score]] ([↗](trust-score.md)) と連携して Wiki の自律的成長サイクルを構成する:

```
QueryLog (P0) → Trust Score (P1) → Gap Detection + Auto Ingest (P2)
```

- **[[querylog]] ([↗](querylog.md))**: クエリデータを蓄積（gap_topics が Gap Detection の入力データ）
- **[[trust-score]] ([↗](trust-score.md))**: 記事品質を評価（低スコア記事の改善優先度を把握）
- **Gap Detection**: ギャップを検出し成長方向を提案（ingest → compile で記事追加）

## テスト

21件のテストケースで、トークン化（英語・日本語・混在）、カバレッジ計算の境界値、ギャップ検出の threshold 制御、提案の priority 正規化、出力フォーマットの構造をカバーしている。

## 関連

Leiden クラスタリングや共引用ベースの missing edge 検出による将来的な強化案は [[graphify-knowledge-graph-concepts]] ([↗](graphify-knowledge-graph-concepts.md)) を参照。

## 出典

- [Gap Detection + Auto Ingest 提案 — 設計と実装の詳細](../raw/articles/20260406-gap-detection-feature.md)

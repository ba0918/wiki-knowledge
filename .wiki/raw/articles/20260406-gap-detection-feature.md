---
title: Gap Detection + Auto Ingest 提案 — Wiki 知識ギャップの自動検出機能
scraped: 2026-04-06
tags: [gap-detection, auto-ingest, wiki, querylog, knowledge-gap, phase2b, phase2c]
---

# Gap Detection + Auto Ingest 提案 — Wiki 知識ギャップの自動検出機能

## 概要

Gap Detection は、QueryLog に蓄積されたクエリデータを分析し、Wiki にまだ存在しない知識のギャップを自動検出する機能である。検出されたギャップに対して、優先度付きの ingest 提案（Auto Ingest 提案）を生成する。Phase 2b+2c として実装された。

Wiki が自律的に成長するための「成長エンジン」としての役割を担う。

## 設計思想

### なぜ Gap Detection が必要か

LLM Wiki では、ユーザーのクエリに対して既存記事から回答を合成する。しかし、Wiki の知識範囲には限りがあり、回答できないトピック（ギャップ）が必然的に発生する。

QueryLog の `gap_noted` フラグと `gap_topics` フィールドは、query 実行時にこれらのギャップを構造化データとして記録する。Gap Detection はこのデータを集約・分析し、「どのトピックが最も頻繁にギャップとして検出されているか」を可視化する。

### カバレッジ計算によるギャップの確定

gap_topics に記録されたトピックが、実際に既存記事でカバーされていないかを確認するプロセスがある。

1. **トークン化**: トピック文字列と各記事（title, tags, slug, body）をトークンに分解
2. **重複率計算**: `len(topic_tokens & article_tokens) / len(topic_tokens)`
3. **閾値判定**: 最大重複率が threshold（デフォルト 0.8）未満のトピックをギャップとして確定

これにより、「RAG architecture」というギャップトピックが既に「RAG」について詳しく書かれた記事でカバーされている場合は、重複として除外できる。

### Auto Ingest 提案

確定したギャップごとに、以下の情報を含む提案を生成する：

- **Priority**: `frequency × (1 - coverage)` を 0.0〜1.0 に正規化。頻度が高く、既存記事でのカバレッジが低いほど優先度が高い
- **Suggested Queries**: トピックに基づく検索クエリ候補（`"{topic} wiki"`, `"{topic} overview"`, `"{topic} tutorial"`）
- **Related Articles**: 部分的に関連する既存記事のスラッグ

提案は「提案のみ」であり、自動実行しない。ユーザーが `wiki ingest` で明示的に取り込む設計とした。

## トークン化戦略

### 英語テキスト

小文字化し、スペース・ハイフン・アンダースコアで分割。各単語をトークンとして扱う。

### 日本語テキスト

外部形態素解析器への依存を避けるため、文字単位の bigram（2文字連続）で分割する。例えば「知識ベース」は `{"知識", "識ベ", "ベー", "ース"}` となる。

精度は形態素解析に劣るが、外部依存ゼロで動作するメリットがある。インターフェース `extract_tokens(text: str) -> frozenset[str]` で隔離されているため、将来的に形態素解析器ベースの実装に差し替え可能。

### 混在テキスト

ASCII 部分とマルチバイト部分を正規表現で分離し、それぞれの戦略を適用して union する。全角英数字は NFKC 正規化で半角に変換。

## データ型

### ConfirmedGap（確定ギャップ）

```python
@dataclass(frozen=True)
class ConfirmedGap:
    topic: str           # gap_topics から取得したトピック文字列
    frequency: int       # QueryLog での出現回数
    coverage: float      # 既存記事によるカバー率 0.0-1.0
    related_articles: tuple[str, ...]  # 部分的に関連する記事スラッグ
```

### IngestProposal（取り込み提案）

```python
@dataclass(frozen=True)
class IngestProposal:
    topic: str
    priority: float      # 正規化された優先度 0.0-1.0
    suggested_queries: tuple[str, ...]  # 検索クエリ候補
    related_articles: tuple[str, ...]
```

全てのデータ型は `frozen=True` で不変性を保証。リストフィールドは `tuple` を使用。

## アーキテクチャ

### 純粋関数設計

コアロジックは全て純粋関数として実装：

- `extract_tokens(text) -> frozenset[str]` — トークン化
- `compute_coverage(topic_tokens, articles, threshold) -> tuple[float, tuple[str, ...]]` — カバレッジ計算
- `detect_gaps(entries, articles, threshold) -> list[ConfirmedGap]` — ギャップ検出
- `generate_proposals(gaps) -> list[IngestProposal]` — 提案生成

I/O は `load_articles()` と CLI エントリポイント `main()` に隔離。

### 既存コード再利用

- `load_querylog()` — querylog_stats.py から import（JSONL 読み込み）
- `parse_frontmatter()` — lint-wiki.py から import（記事メタデータ解析）
- `importlib` による動的インポート — trust_score.py と同パターン（ハイフン付きファイル名対応）

### 3フォーマット出力

trust_score.py と同じ出力パターン：

- **table**: CLI 向け ASCII テーブル（デフォルト）
- **json**: プログラム連携向け JSON
- **report**: Markdown レポート → `.wiki/outputs/reports/{YYYYMMDD}-gap-detect.md`

## lint ワークフロー統合

SKILL.md の lint セクションで、Trust Score チェックの後に Gap Detection チェックを実行：

```bash
python3 scripts/gap_detect.py --wiki-root {wiki_root}
```

Priority が 0.7 以上の提案は lint レポートの 🔵 Info として記載される。

## CLI 使用法

```bash
python3 gap_detect.py --wiki-root .wiki                     # table 出力（デフォルト）
python3 gap_detect.py --wiki-root .wiki --format json       # JSON 出力
python3 gap_detect.py --wiki-root .wiki --format report     # Markdown レポート生成
python3 gap_detect.py --wiki-root .wiki --threshold 0.5     # 閾値変更
```

## Phase 2+ ロードマップにおける位置づけ

```
QueryLog (2a) ✅ → Trust Score (3a) ✅ → Gap Detection + Auto Ingest (2b+2c) ✅
```

QueryLog がクエリデータを蓄積し、Trust Score が記事品質を評価し、Gap Detection がギャップを検出して成長方向を提案する。この3機能が連携することで、Wiki の自律的な品質向上と成長のサイクルが完成する。

## テスト

21 テスト（全パス）：

- `TestExtractTokens` (5件) — 英語、ハイフン、空文字、日本語 bigram、混在テキスト
- `TestComputeCoverage` (4件) — 完全一致、部分一致、不一致、単一トークン
- `TestDetectGaps` (5件) — 通常ケース、全カバー済、空 querylog、threshold 制御、related
- `TestGenerateProposals` (3件) — priority 計算、suggested_queries、空リスト
- `TestFormatters` (2件) — JSON 構造、report ヘッダー
- `TestIntegration` (2件) — tmp_path フルパイプライン、空 querylog

## 将来の拡張

- 形態素解析器ベースのトークン化（MeCab 等）への差し替え
- Web 検索 API との連携による自動ソース候補取得
- query 実行時のリアルタイムギャップ検出と即時提案
- gap_topics の同義語クラスタリング（「RAG architecture」と「RAG アーキテクチャ」の統合）

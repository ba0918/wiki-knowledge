---
title: QueryLog — Wiki Query のメタデータログ基盤
type: wiki
source_refs:
  - "raw/articles/20260406-querylog-feature.md"
created: 2026-04-06
updated: 2026-04-06
category: concepts
tags: [querylog, jsonl, gap-detection, trust-score, wiki-query, phase2]
related:
  - "concepts/wiki-knowledge-architecture.md"
  - "concepts/llm-wiki-knowledge-base.md"
---

# QueryLog — Wiki Query のメタデータログ基盤

> wiki-query 実行時にクエリのメタデータを JSONL で蓄積する仕組み。後続の Trust Score・Gap Detection・Auto Ingest 提案の基盤データレイヤーとして機能する。

## 目的と背景

[[wiki-knowledge-architecture]] の4相パイプライン（Ingest → Compile → Query → Lint）において、Query フェーズは回答を合成するだけでなく、**Wiki の知識ギャップを検出する観測点**でもある。

従来の wiki-query は `log.md` に1行ログ、`outputs/queries/` にマークダウン回答を保存していたが、機械的に集計可能な構造化データがなかった。QueryLog はこのギャップを埋め、query 実行のたびに以下を記録する:

- どの記事が参照されたか（`sources_consulted`）
- どの記事が実際に引用されたか（`sources_cited`）
- Wiki にない知識が指摘されたか（`gap_noted` / `gap_topics`）
- 回答が記事に昇格したか（`promoted`）

## データ形式

JSONL（JSON Lines）形式で `{wiki_root}/outputs/querylog.jsonl` に append-only で記録される。各行が1つの JSON オブジェクト。

```json
{
  "id": "q_20260405T223000",
  "timestamp": "2026-04-05T22:30:00+09:00",
  "question": "Ingest と Compile の違いは？",
  "sources_consulted": ["concepts/llm-wiki-knowledge-base.md"],
  "sources_cited": ["concepts/llm-wiki-knowledge-base.md"],
  "gap_noted": false,
  "gap_topics": [],
  "promoted": false,
  "promoted_to": null
}
```

スキーマは `.wiki/schema/querylog-schema.json`（JSON Schema draft-07）で厳密に定義されている。

## 設計判断

### JSONL を選んだ理由

- `jq` や Python で直接パースでき、`log.md` の grep ベース集計より正確
- 1行追記するだけで既存データに影響しない（ファイルロック不要）
- 既存の `log.md` / `outputs/queries/` と並存する追加レイヤーであり、既存フローを破壊しない

### タイムスタンプベース ID

`q_{YYYYMMDDTHHMMSS}` 形式を採用。seq 番号方式（`q_20260405_001`）は read-before-write が必要で脆弱なため不採用。人間が手動で query を実行する運用では同一秒衝突は無視できる。

### sources_cited の抽出

回答テキストから `[[wikilink]]` を正規表現で抽出する方式を採用。既存の `lint-wiki.py` の `find_wikilinks()` と同じロジック。プロンプトで別途リストを出力させる方式は回答フォーマットが複雑になるため不採用。

### git 管理ポリシー

`question` フィールドにユーザの質問がそのまま記録されるため、機密情報漏洩リスクがある。デフォルトで `.wiki/.gitignore` による git 管理対象外。

## gap_topics — 知識ギャップの構造化

QueryLog の核心的価値。query のたびに「Wiki にまだない知識」がトピック名として構造化・蓄積される。

活用先:
- **Gap Detection**: 頻度集計で「最もよく聞かれるがまだ記事がないトピック」を自動検出
- **Auto Ingest 提案**: 高頻度ギャップに対して取り込むべきソースを提案
- **成長方向の可視化**: どの領域の知識が不足しているかをデータで把握

これは [[llm-wiki-knowledge-base]] の「Query も知識ベースに複利的に蓄積される」思想を、メタデータレベルで実現するもの。

## querylog-stats.py

人間向けの統計集計ツール。I/O（ファイル読み込み）と純粋関数（統計計算）を分離した3関数構成。

```bash
python3 querylog-stats.py --wiki-root .wiki
```

出力: 総クエリ数、記事の参照率、未参照記事一覧、ギャップ率、頻出ギャップトピック、promote 率。

`never_consulted`（一度も参照されていない記事）の検出は、Wiki の健全性指標として有用 — 読まれない記事は存在価値が低い可能性がある。

## Phase 2+ での位置づけ

QueryLog は [[wiki-knowledge-architecture]] の Output 層に属し、Phase 2+ 全機能の基盤:

```
QueryLog (P0) → Trust Score (P1) → Gap Detection + Auto Ingest (P2)
```

- **Trust Score**: 参照頻度（sources_consulted/cited の出現回数）を記事ごとの信頼度に変換
- **Gap Detection**: gap_topics の頻度集計で知識ギャップを構造化
- **Auto Ingest 提案**: 高頻度ギャップに対するソース候補の自動生成

## 出典

- [QueryLog 蓄積機能 — 設計と実装の詳細](../raw/articles/20260406-querylog-feature.md)

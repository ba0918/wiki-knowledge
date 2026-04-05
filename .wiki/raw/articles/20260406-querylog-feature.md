---
title: QueryLog 蓄積機能 — Wiki Query のメタデータログ基盤
scraped: 2026-04-06
tags: [querylog, phase2, gap-detection, trust-score, wiki-query]
---

# QueryLog 蓄積機能

## 概要

QueryLog は wiki-query 実行時にクエリのメタデータを構造化ログとして蓄積する機能である。JSONL（JSON Lines）形式で `{wiki_root}/outputs/querylog.jsonl` に append-only で記録される。

現在の wiki-query は `log.md` に1行の操作ログ、`outputs/queries/` にマークダウン回答を保存しているが、機械的に集計可能な構造化データがなかった。QueryLog はこのギャップを埋め、後続の Phase 2+ 機能（Trust Score、Gap Detection、Auto Ingest 提案）の基盤となるデータレイヤーを提供する。

## 設計判断

### なぜ JSONL か

- **集計しやすさ**: JSON であれば `jq` や Python で直接パースできる。log.md の grep ベースの集計よりも正確
- **Append-only**: 1行追記するだけで既存データに影響しない。ファイルロックも不要
- **既存フローと独立**: log.md と outputs/queries/ はそのまま維持。QueryLog は追加のデータレイヤーとして並存する

### ID 採番方式

`q_{YYYYMMDDTHHMMSS}` 形式のタイムスタンプベース ID を採用。当初検討した seq 番号方式（`q_20260405_001`）は、前のエントリを読んで次の番号を決める read-before-write が必要で脆弱だったため不採用。同一秒の衝突は、人間が手動で query を実行する運用上無視できる。

### sources_cited の抽出方法

回答テキストから `[[wikilink]]` パターンを正規表現 `\[\[([a-z0-9-]+)\]\]` で抽出する方式を採用。プロンプトで別途引用リストを出力させる方式も検討したが、回答フォーマットが複雑になるため不採用。既存の lint-wiki.py の `find_wikilinks()` 関数と同じロジックを再利用する。

### git 管理ポリシー

`querylog.jsonl` はユーザの質問文（`question` フィールド）をそのまま記録するため、機密情報が含まれる可能性がある。デフォルトでは `.wiki/.gitignore` で git 管理対象外に設定。ユーザが明示的に管理したい場合は `.gitignore` から除外できる。

## エントリ構造

各行が1つの JSON オブジェクト。スキーマは `.wiki/schema/querylog-schema.json`（JSON Schema draft-07）で定義。

```json
{
  "id": "q_20260405T223000",
  "timestamp": "2026-04-05T22:30:00+09:00",
  "question": "Ingest と Compile の違いは？",
  "sources_consulted": ["concepts/llm-wiki-knowledge-base.md", "concepts/wiki-knowledge-architecture.md"],
  "sources_cited": ["concepts/llm-wiki-knowledge-base.md"],
  "gap_noted": false,
  "gap_topics": [],
  "promoted": false,
  "promoted_to": null
}
```

### フィールド説明

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | string | タイムスタンプベース ID |
| `timestamp` | string | ISO 8601 タイムスタンプ |
| `question` | string | ユーザの質問文（全文） |
| `sources_consulted` | string[] | 読み込んだ記事パス |
| `sources_cited` | string[] | 回答で引用した記事パス |
| `gap_noted` | boolean | ギャップ指摘の有無 |
| `gap_topics` | string[] | ギャップトピック名 |
| `promoted` | boolean | 回答を記事に昇格したか |
| `promoted_to` | string/null | 昇格先パス |

## query フローへの統合

既存フローの末尾に QueryLog 追記ステップを追加。既存の動作は一切変更しない。

```
1. Index スキャン → 関連記事特定
2. 記事読み込み
3. 回答合成（gap_topics も抽出）
4. 保存提案 → outputs/queries/ or concepts/ に保存
5. log.md 追記
6. 【新規】QueryLog エントリを querylog.jsonl に追記
```

回答合成時に「Wiki にない情報」をギャップとして指摘し、トピック名を構造化して記録する。プロンプトテンプレートに Knowledge Gaps セクションを追加し、LLM が自然にギャップを抽出できるようにした。

## gap_topics — Gap Detection の種

QueryLog の核心的な価値は `gap_topics` フィールドにある。query のたびに「Wiki にまだない知識」が構造化されて蓄積される。これにより:

- **Gap Detection**: gap_topics の頻度集計で「最もよく聞かれるがまだ記事がないトピック」を自動検出
- **Auto Ingest 提案**: 高頻度のギャップトピックに対して「このソースを取り込んだら？」と提案
- **Wiki の成長方向の可視化**: どの領域の知識が不足しているか、データで把握できる

## querylog-stats.py — 集計スクリプト

人間向けの統計集計ツール。I/O と純粋関数を分離した3関数構成。

```bash
python3 querylog-stats.py --wiki-root .wiki
```

出力例:
```json
{
  "total_queries": 42,
  "sources": {
    "total_concepts": 12,
    "consulted_unique": 8,
    "never_consulted": ["concept-a.md"],
    "consultation_rate": 0.667
  },
  "gaps": {
    "queries_with_gaps": 15,
    "gap_rate": 0.357,
    "top_topics": [{"topic": "RAG architecture", "count": 5}]
  },
  "promotions": {
    "promoted_count": 3,
    "promotion_rate": 0.071
  }
}
```

`never_consulted` は concepts/ に存在するが一度も参照されていない記事を検出する。これも Wiki の健全性指標として有用。

## Phase 2+ ロードマップにおける位置づけ

QueryLog は Phase 2+ の全機能の基盤:

```
QueryLog (P0, 完了) → Trust Score (P1) → Gap Detection + Auto Ingest (P2)
```

- **Trust Score**: QueryLog の参照頻度（sources_consulted/cited の出現回数）を記事ごとの信頼度スコアに変換
- **Gap Detection**: gap_topics の頻度集計 + クラスタリングで知識ギャップを構造化
- **Auto Ingest 提案**: 高頻度ギャップに対するソース提案を自動生成

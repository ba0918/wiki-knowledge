# Frontmatter Schemas

各ファイル種別のフロントマター定義。

## Wiki 記事（concepts/*.md）

正式な JSON Schema は `{wiki_root}/schema/page-template.json` にある。

```yaml
---
title: ページタイトル           # 必須
type: wiki                      # 必須、固定値
source_refs:                    # 必須、{wiki_root}からの相対パス
  - "raw/articles/xxx.md"
created: 2026-04-05             # 必須、YYYY-MM-DD
updated: 2026-04-05             # 必須、YYYY-MM-DD
category: concepts              # 必須、categories.json のslug
tags: [tag1, tag2]              # 必須
related:                        # 任意、{wiki_root}からの相対パス
  - "concepts/yyy.md"
---
```

## Raw ソース（raw/articles/*.md）

```yaml
---
title: ドキュメントタイトル      # 必須
source_url: https://example.com  # 任意（URLからの取り込み時）
scraped: 2026-04-05              # 必須、取り込み日
tags: [tag1, tag2]               # 必須
---
```

## Query 出力（outputs/queries/*.md）

```yaml
---
title: 質問の要約
type: query
question: 元の質問文
answered: 2026-04-05
sources_consulted:
  - "concepts/xxx.md"
promoted: false                  # true の場合 concepts/ にコピー済み
---
```

## Lint レポート（outputs/reports/*.md）

```yaml
---
title: Lint Report YYYY-MM-DD
type: lint
date: 2026-04-05
summary:
  error: 0
  warning: 0
  info: 0
---
```

## QueryLog エントリ（outputs/querylog.jsonl）

各行が1つの JSON オブジェクト（JSONL 形式）。

```jsonl
{"id":"q_20260405T223000","timestamp":"2026-04-05T22:30:00+09:00","question":"Ingest と Compile の違いは？","sources_consulted":["concepts/llm-wiki-knowledge-base.md"],"sources_cited":["concepts/llm-wiki-knowledge-base.md"],"gap_noted":false,"gap_topics":[],"promoted":false,"promoted_to":null}
```

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `id` | string | Yes | `q_{YYYYMMDDTHHMMSS}` 形式（タイムスタンプベース） |
| `timestamp` | string | Yes | ISO 8601 タイムスタンプ |
| `question` | string | Yes | ユーザの質問文（全文） |
| `sources_consulted` | string[] | Yes | index スキャンで読み込んだ記事パス |
| `sources_cited` | string[] | Yes | 回答テキストから `[[wikilink]]` を正規表現抽出して収集 |
| `gap_noted` | boolean | Yes | 回答中に「Wiki にない情報」を指摘したか |
| `gap_topics` | string[] | Yes | ギャップとして指摘したトピック（空配列可） |
| `promoted` | boolean | Yes | 回答を concepts/ に昇格したか |
| `promoted_to` | string\|null | Yes | 昇格先のパス（promoted=false なら null） |

**注意:** `question` にはユーザの質問文がそのまま記録される。機密情報が含まれる可能性があるため、デフォルトでは git 管理対象外。

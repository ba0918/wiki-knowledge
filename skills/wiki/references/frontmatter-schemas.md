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

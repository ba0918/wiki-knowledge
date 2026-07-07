---
title: Trust Score v2（絶対スケール化）と Query Retrieval Pre-pass の設計
scraped: 2026-07-07
tags: [trust-score, query, retrieval, graph-layer, design]
---

# Trust Score v2（絶対スケール化）と Query Retrieval Pre-pass の設計

出典: docs/plans/20260707200608_query-derived-layer-consumer.md（実装同日）

## Trust Score v2 — 絶対スケール化

v1 の min-max 正規化はスコアを「wiki 内相対評価」にしていた。どんな完璧な wiki でも各要素の最下位は 0.00 に張り付く一方、lint / SKILL.md の警告閾値 0.30 は絶対値として扱われており、意味論が破綻していた（実測: 12記事中10記事が 0.30 未満）。

v2 では min-max 正規化を廃止し、全4要素を絶対的な飽和カーブに変更した:

| 要素 | 重み | v2 算式 | カーブの意味 |
|------|------|---------|-------------|
| ソース数 | 0.30 | `n / (n + 1)` | 1件=0.50、2件=0.67、3件=0.75 — 逓減リターン |
| 鮮度 | 0.20 | `0.5 ** (elapsed_days / 365)` | 半減期365日。1年=0.50、2年=0.25。0にならない |
| 引用頻度 | 0.30 | `c / (c + 2)` | 1回=0.33、2回=0.50、6回=0.75 |
| backlink数 | 0.20 | `b / (b + 2)` | 同上 |

鮮度が指数減衰で 0 に到達しないのは意図的な設計である。記事は `source_revision` 等で「取得時点の事実」に固定される（スナップショット方針）ため、経過時間は「上流と乖離しているリスクの漸増」を表すのであって「無効化」ではない。`updated` フィールドが無い記事のみ鮮度 0.0 とする。

QueryLog 空時のフォールバック再配分（ソース 0.40 / 鮮度 0.30 / backlink 0.30）は v1 から不変。`ArticleScore` の `*_norm` フィールド名は出力互換のため維持され、意味が「正規化値」から「絶対要素スコア」に変わった。

## Query Retrieval Pre-pass（query_retrieve.py）

query ワークフローの「index.md 全読 + 勘での記事選定」を決定論スクリプトに置き換える。LLM はキーワード選定と最終判断のみを担う。

処理フロー:

1. **Seed 選定**: concepts 記事の slug / title / tags / 本文へのキーワードマッチ（大文字小文字非区別）。重み: slug/title=3、tags=2、本文=1（キーワードごと、フィールドごとに1回）
2. **Graph 展開**: graph.json の edges から seed の outbound + inbound（backlink）を1ホップ展開。**backlink 方向は記事本文からは見えない情報であり、graph layer だけが提供できる** — 質問に直接マッチしないが seed を参照している記事（横断フロー記事が典型）を候補に浮上させる
3. **Degree 正規化**: seed の影響力は接続数で分配される（`seed × 0.5 / degree`、PageRank 的減衰）。密結合 wiki で展開スコアが seed の差を飲み込みランキングが平坦化する現象（実測: 52 edges / 12 nodes で6記事が同点）への対策
4. **Trust 注釈**: 各候補に Trust Score v2 を付与。回答合成時、trust 0.30 未満の記事の引用には「（信頼度低）」を付す

graph.json 不在時は exit 2 で停止し graph_gen.py の実行を案内する（lint-wiki.py と同じ層契約 — retrieval 側で graph を再生成しない）。

## スケール根拠

キーワードマッチ + graph 展開は全て in-memory で記事数百本まで余裕がある。LLM が読む量は「候補 limit 件のメタデータ + 選定した記事の本文」に抑えられ、index.md の肥大と無関係になる。5リポジトリ横断 wiki（数十〜百記事規模）を想定した設計である。

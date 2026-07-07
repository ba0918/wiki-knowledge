# Query を derived layer の消費者に再設計する — retrieval script + Trust Score v2

**Cycle ID:** 20260707200608
**Type:** Feature + Fix
**Created:** 2026-07-07 20:06:08
**Status:** 🟢 Complete（2026-07-07 全 Work Item 完了、513 tests pass）
**Related:** docs/plans/20260406012002_trust-score.md / docs/plans/20260407183028_wiki-graph-layer-mvp.md / docs/plans/20260707194819_schema-regime-decision.md / docs/issues/20260703213243_unify-trust-gap-detection-on-graph-layer.md

## Overview

このプロジェクトは4つの派生サブシステム（graph / trust / gap / querylog）を構築したが、**唯一のユーザー向け操作である query はそのどれも消費していない**:

- SKILL.md の query は「index.md を読む → 関連記事を勘で特定 → wikilink を1段だけ勘で辿る」。graph.json（12 nodes / 52 edges）への言及ゼロ
- **backlink（被リンク）方向の探索が構造的に不可能** — 記事本文からは outbound リンクしか見えない。graph だけが両方向を持つ
- 引用時に記事の信頼度を知らない — trust-score 記事自身が「query 時: 引用元の信頼度を把握（Phase 3b で統合予定）」と予告したまま未統合
- index.md 全読 + 勘の選定は、5リポジトリ適用（記事数十〜百本規模）でオリエンテーション段階が破綻する

さらに Trust Score 自体に、統合前に直すべき意味論バグが2つある:

1. **min-max 正規化がスコアを wiki 内相対評価にしている**: どんな完璧な wiki でも各要素の最下位は 0.00 に張り付く（必ず誰かが沈む）。一方 lint / SKILL.md の警告閾値 0.30 は絶対値の顔をしている。相対スコア × 絶対閾値 = 意味不明（実測: 12記事中10記事が 0.30 未満、うち2記事は 0.00）
2. **鮮度の線形減衰（365日で 0.0）がスナップショット方針と矛盾**: repo 記事は `source_revision` で「取得時点の事実」に固定される。1年経過 = 信頼度ゼロではなく「上流と乖離しているリスクの増加」でしかない

## Design

### 1. Trust Score v2 — 絶対スケール化（trust_score.py 改修）

min-max 正規化を廃止し、全4要素を**絶対的な飽和カーブ**にする。0.30 閾値が記事単体で意味を持つようになる:

| 要素 | v1（相対） | v2（絶対） | 根拠 |
|------|-----------|-----------|------|
| ソース数 | raw=件数 → min-max | `n / (n + 1)` | 1件=0.50、2件=0.67、3件=0.75 — 逓減リターン |
| 鮮度 | `max(0, 1 - d/365)` → min-max | `0.5 ** (d / 365)` | 半減期365日。90日=0.84、1年=0.50、2年=0.25。**0にならない** — スナップショット意味論（乖離リスクの漸増であって無効化ではない）。`updated` 無しのみ 0.0 |
| 引用頻度 | raw=回数 → min-max | `c / (c + 2)` | 1回=0.33、2回=0.50、6回=0.75 |
| backlink | raw=件数 → min-max | `b / (b + 2)` | 同上 |

- 重み配分（0.30/0.20/0.30/0.20、QueryLog 空時 0.40/0.30/0.30）は不変
- `ArticleScore` のフィールド名（`*_norm`）は互換維持、意味が「正規化値」→「絶対要素スコア」に変わる（docstring 明記）
- `normalize_scores()` は削除（相対化の根そのもの）
- 将来: revision-pinned ソースの「上流 HEAD 乖離検出」は差分 re-compile 導入時に鮮度へ統合（本 cycle では扱わない）

### 2. query_retrieve.py — 決定論 retrieval pre-pass（新規）

LLM の「index 全読 + 勘」を置き換える候補選定スクリプト。**LLM はキーワード選定と最終判断だけを担い、候補列挙・グラフ展開・信頼度注釈は決定論に落とす**:

```bash
python3 skills/wiki/scripts/query_retrieve.py --wiki-root .wiki --keywords trust score 信頼度 [--limit 12] [--format table|json]
```

処理フロー（pure core + 薄い CLI、graph_gen / lint と同型）:

1. **Seed 選定**: concepts/*.md の title / slug / tags / 本文にキーワードマッチ（大文字小文字非区別）。重み: title/slug=3、tag=2、本文=1（キーワードごと加算）
2. **Graph 展開**: graph.json の edges から seed の **outbound + inbound（backlink）** を1ホップ展開。隣接記事のスコア = 接続元 seed スコア × 0.5 × 接続数
3. **Trust 注釈**: trust_score v2 を in-process 呼び出し（`import trust_score` — underscore 名なので通常 import 可）で各候補に trust を付与
4. **出力**: スコア降順 `--limit` 件。各行 = slug / retrieval score / trust / 選定理由（matched: どのフィールドにどのキーワード / via: どの seed からの outbound/backlink）

graph.json 不在時は lint と同じ規約で **exit 2** + `graph_gen.py` 実行案内（層越境しない）。

### 3. SKILL.md query 手順の書き換え

- ステップ1「index.md スキャン」→「質問からキーワードを抽出し `query_retrieve.py` を実行、候補リストを得る」（graph.json 不在なら先に graph_gen）
- ステップ2「関連記事を読む」→ 候補リストから LLM が判断して Read（従来の「回答の正確性が上がる場合のみ辿る」基準は維持。retrieve が返さない記事を index.md から補うのも可 — script は候補提示であり検閲ではない）
- **Trust-aware 引用ルール（新設）**: trust < 0.30 の記事を引用する場合、回答内の当該引用箇所に「（信頼度低: {score}）」を付す
- querylog 追記・promote・完了メッセージは不変

### 4. スケール根拠（5リポジトリ適用）

- キーワードマッチ + graph 展開は記事数百本で余裕（全て in-memory、graph.json は既に derived）
- LLM が読む量は「候補 limit 件のメタデータ + 選んだ記事の本文」に抑えられ、index.md の肥大と無関係になる
- backlink 展開により「質問に直接マッチしない が seed を参照している記事」（横断フロー記事が典型）が候補に浮上する — 5リポジトリ wiki の最重要記事型を素通ししない

## Work Items

| # | 内容 | 種別 |
|---|------|------|
| 1 | 本 plan 作成 | docs |
| 2 | Trust Score v2（絶対スケール化 + 鮮度半減期化、TDD、既存テスト改修） | code |
| 3 | query_retrieve.py 新規実装（TDD: seed 選定 / graph 展開 / trust 注釈 / formatter / CLI） | code |
| 4 | SKILL.md query 節の書き換え（retrieval pre-pass + trust-aware 引用ルール） | docs |
| 5 | 整合更新: CLAUDE.md（Query 節新設 + Trust Score 節）/ architecture.md（graph 消費者に query_retrieve 追加）/ .wiki/concepts/trust-score.md（v2 算式に追従、「Phase 3b 統合予定」を解消） | docs |
| 6 | 実 Wiki での動作確認（query_retrieve 実行 + trust v2 実測 + lint 回帰） | verify |

## Acceptance Criteria

- [x] trust v2: min-max 正規化が消え、全要素が絶対スケール。均質な wiki で全記事が同スコアになり 0 に沈む記事が出ない（実測: 12記事が 0.63〜0.78 に分布、0.30 未満ゼロ — v1 では10記事が未満だった）
- [x] trust v2: 鮮度が半減期カーブ（1年=0.50、0にならない。実測: 90日経過記事 = 0.84）
- [x] query_retrieve: キーワードから seed 選定 → graph 1ホップ展開（inbound 含む）→ trust 注釈付きランキングを返す。**実装中の実測で密結合 wiki のランキング平坦化（6記事同点）を検出し、degree 正規化（seed 影響の接続数分配）を追加**
- [x] query_retrieve: graph.json 不在時 exit 2 + graph_gen 案内（実測確認済み）
- [x] SKILL.md / CLAUDE.md / architecture.md / trust-score 記事が新設計と整合（記事は raw ソース `20260707-trust-score-v2-query-retrieve.md` を ingest して規範どおり更新）
- [x] 全テスト pass（513 passed / 1 xfailed、query_retrieve 18件 + trust v2 改修）+ 実 Wiki lint No findings

## Follow-up（スコープ外として記録）

- query_retrieve を紹介する wiki 記事（`query-retrieval` 等）の compile — 今回は trust-score 記事の更新のみ
- 2ホップ展開・BM25 バックエンドは記事数が増えて必要になってから

## Non-Goals

- BM25 / embedding 検索（キーワード + graph で足りなくなってから。Strategy 化は将来）
- gap_detect の query 内呼び出し（gap は querylog 経由で非同期に検出される設計を維持）
- trust/gap の graph layer 統合リファクタ（issue 20260703213243 — 並行可能な別作業）
- 差分 re-compile / 上流乖離検出（スナップショット方針の価値実証後）

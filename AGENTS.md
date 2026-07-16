# AGENTS.md

This file is the shared project instruction source for Claude Code, Codex CLI, and other agents.
`CLAUDE.md` must stay a thin wrapper that imports this file with `@AGENTS.md`.

## Wiki Knowledge Base

このプロジェクトは LLM Wiki Knowledge Base です。

wiki_root: .wiki

## Scope

LLM向けの知識ベース構築の仕組みを実験するプロジェクト。Karpathy の LLM Wiki コンセプトをエージェントスキルとして実装し、既存プロジェクトに導入可能な形で提供する。

## Conventions

- Wiki記事は `.wiki/concepts/` にフラットに配置（`{slug}.md`）
- ソースドキュメントは `.wiki/raw/` に immutable に保存
- 相互参照は `[[wikilink]]` 記法を使用
- フロントマターは `.wiki/schema/page-template.json` に準拠
- カテゴリは `.wiki/schema/categories.json` で管理
- **Schema 体制**: v0（`page-template.json`）が schema-of-record。v1（`page-template-v1.json` + `lib/` migrations）は採用トリガー付き standby 資産 — 裁定: `.agents/artifacts/plans/20260707194819_schema-regime-decision.md`

## Articles

- [[llm-wiki-knowledge-base]] — LLM Wiki Knowledge Base（concepts）
- [[wiki-knowledge-architecture]] — Wiki ナレッジ構築アーキテクチャ（concepts）
- [[llm-wiki-use-cases]] — LLM Wiki ユースケース（concepts）
- [[llm-wiki-tooling]] — LLM Wiki ツーリング（tools）
- [[querylog]] — QueryLog メタデータログ基盤（concepts）
- [[trust-score]] — Trust Score 記事信頼度スコア（concepts）
- [[gap-detection]] — Gap Detection 知識ギャップ検出と Ingest 提案（concepts）
- [[graphify-knowledge-graph-concepts]] — graphify 知識グラフ構築パターンと適用判断（concepts）
- [[wikilink-github-interop]] — GitHub と wikilink 記法の相互運用性（concepts）
- [[wikilink-reader-comparison]] — wikilink リーダー実装比較（tools）
- [[wikilink-conversion-strategies]] — wikilink ↔ 標準 Markdown link 変換戦略（practices）
- [[wikilink-link-parser-spec]] — lint-wiki.py wikilink パーサ仕様（references）

## QueryLog

- wiki-query 実行時にクエリメタデータを `.wiki/outputs/querylog.jsonl` に蓄積する（JSONL、append-only）
- スキーマ: `.wiki/schema/querylog-schema.json`
- 追記: `python3 skills/wiki/scripts/querylog_append.py --wiki-root .wiki --question <q> --consulted <paths>... --answer-file <path>` — id 生成・`sources_cited` の wikilink 抽出・schema 検証・flock 付き JSONL 追記をスクリプトが担う（LLM は JSON を手組みしない）。テストが schema の required と `REQUIRED_FIELDS` の同期を機械検証
- 集計: `python3 skills/wiki/scripts/querylog-stats.py --wiki-root .wiki`
- querylog.jsonl はデフォルト git 管理外（`.wiki/.gitignore`）

## Query Retrieval

- wiki-query は retrieval pre-pass を使う: `python3 skills/wiki/scripts/query_retrieve.py --wiki-root .wiki --keywords <kw>...`
- graph.json（outbound + backlink 両方向の1ホップ展開、seed 影響は degree 正規化で分配）と Trust Score を消費し、trust 注釈つき候補リストを返す
- `outputs/graph.json` 必須（不在時 exit 2 で graph_gen.py を案内）。出力形式: `--format table`（デフォルト）/ `json`
- 回答で trust < 0.30 の記事を引用する際は「（信頼度低）」を付す（wiki-query スキル）

## Trust Score

- 記事ごとの信頼度スコア（0.0〜1.0）を4要素（ソース数・鮮度・引用頻度・backlink数）で算出
- **v2（絶対スケール）**: 各要素は絶対飽和カーブ（ソース n/(n+1)、引用 c/(c+2)、backlink b/(b+2)）。min-max 正規化は廃止 — 0.30 閾値が記事単体で意味を持つ
- 鮮度は半減期365日の指数減衰（1年=0.50、2年=0.25、0にならない）— スナップショット方針（source_revision 固定）と整合
- 実行: `python3 skills/wiki/scripts/trust_score.py --wiki-root .wiki`
- 出力形式: `--format table`（デフォルト）/ `json` / `report`（Markdown レポート出力）
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-trust-score.md`
- QueryLog が空の場合は引用頻度を除外し、残り3要素で再配分
- Trust Score は derived value のためフロントマターには保存しない

## Gap Detection

- QueryLog の `gap_topics` を集計し、既存記事とのカバレッジを照合してナレッジギャップを検出
- 実行: `python3 skills/wiki/scripts/gap_detect.py --wiki-root .wiki`
- 出力形式: `--format table`（デフォルト）/ `json` / `report`（Markdown レポート出力）
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-gap-detect.md`
- `--threshold` でカバレッジ閾値を調整（デフォルト: 0.8）

## Lint

- Wiki 記事の品質・整合性を10項目で自動チェック
- `dead_link` / `orphan` は graph layer 経由で検出するため、**lint 実行前に `graph_gen.py` を実行する必要がある**
- `--use-graph` はデフォルト ON。`.wiki/outputs/graph.json` 不在時は **exit 2** で停止し、`graph_gen.py` の実行を案内する
- 単独実行を救済する opt-in フラグ: `--auto-graph`（graph 欠如時に lint 側が graph_gen を subprocess 呼び出し）
- legacy パス: `--no-graph`（inventory から直接 dead_link/orphan を再計算）
- 実行: `python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki && python3 skills/wiki/scripts/lint-wiki.py --wiki-root .wiki`
- 出力形式: `--format table`（デフォルト）/ `json` / `report`（Markdown レポート出力）
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-lint.md`
- チェック項目:
  - Dead link — `[[slug]]` の参照先が存在しない
  - Orphan — 被リンクなしの孤立記事
  - Missing source — `source_refs` のファイルが存在しない
  - Missing frontmatter — 必須フィールド欠損
  - Coverage gap — 2回以上参照されているが記事がない
  - Link quality — 一方向リンク、`related` と本文 wikilink の不一致
  - Article quality — 50 words 未満の短記事、推測ブロック 30% 超
  - Format violations — slug 命名規則、page-template.json 準拠、category/type/date/tags 検証、未採用 v1 記事の混入検出（schema_version_unadopted）
  - Wikilink rendering — `[[slug]]` に GitHub Web UI 用併記 `([↗](slug.md))` が付いていない（`python3 skills/wiki/scripts/wikilink_render.py --write .wiki/concepts/` で修正、compile に自動統合）
  - Index sync — `.wiki/index.md` と `concepts/` の乖離（未掲載記事・存在しない記事の掲載）を検出

## Repo Ingest

- git リポジトリ（URL / ローカルパス）を自動 clone して knowledge source 化する入口
- 実行: `python3 skills/wiki/scripts/repo_ingest.py <url-or-path>... --wiki-root .wiki`（複数リポジトリ一括可）
- clone: `ghq get --shallow` 優先 / `git clone --depth 1` フォールバック（`{wiki_root}/.cache/repos/`、gitignore 対象）
- 出力: manifest（`{wiki_root}/.cache/manifests/{slug}.json`、構造メタ + docs 候補ティア）+ 機械生成 `repo-inventory.md`（`raw/files/{slug}/`）
- raw フロントマター拡張: `source_revision`（commit hash）/ `source_path`（`source_version` は pipeline の int 型と衝突するため不使用）
- 複数リポジトリは「全 clone → 全 ingest → 一括 compile」の3段で処理（横断 wikilink のため）。手順: wiki-ingest スキル「repo ソースの ingest」、compile 規範: `references/compilation-guide.md`「repo ソースの compile」
- セキュリティ: positive-match allowlist（ext::/file:// 拒否）、`GIT_ALLOW_PROTOCOL` 制限、userinfo 除去、2 base パス封じ込め
- オプション: `--max-docs`（既定50）/ `--full-clone` / `--refresh` / `--output`。exit: 0=全成功 / 1=一部失敗 / 2=引数エラー / 130=中断

## Discover（ソースコードからドメイン知識抽出）

- repo ingest 済みリポジトリのソースコードから LLM がドメイン知識を自動抽出し、`concepts/` に記事を直接生成する compile のコードソース対応モード
- 前提: repo ingest 済み（manifest が `.wiki/.cache/manifests/{slug}.json` に存在すること）
- ソース分類スクリプト: `python3 skills/wiki/scripts/source_scan.py --wiki-root .wiki --slug {slug}`（6カテゴリ: schema / routes / rules / state / tests / entry）
- 生成記事: `{slug}-architecture` / `{slug}-db-schema` / `{slug}-api-routes` / `{slug}-business-rules` / `{slug}-state-machines` / `{slug}-glossary`（内容に応じて取捨選択）
- discover 記事の識別: tags に `discover` を含む + `source_refs` に `raw/files/{slug}/repo-inventory.md`
- 読解ガイド: `skills/wiki/references/discover-guide.md`、プロンプト: `skills/wiki/references/prompts.md` の Discover 節
- パイプライン: `wiki-ingest → wiki-compile discover → wiki-compile → graph_gen → wiki-lint`（discover はオプショナル、wiki-cycle で一括実行可能）

## Security Scan（ingest）

- ingest 時のセキュリティチェック 3 項目（パス traversal / 機密データ / プロンプトインジェクション）はスクリプトが単一の真実源
- 実行: `python3 skills/wiki/scripts/security_scan.py <file>... --filename <保存名>`（テキスト直接入力は `--stdin`）
- exit: 0=クリーン / 1=検出あり（ingest 中断） / 2=引数エラー。`--format json` あり

## Operation Log（log.md）

- `## [YYYY-MM-DD] {op} | ...` の定型追記はスクリプトが担う（単複の使い分け等のフォーマットドリフト防止）
- 実行: `python3 skills/wiki/scripts/log_append.py {ingest|compile|promote|query|lint} --wiki-root .wiki <op別フィールド>`
- 共通オプション: `--date`（省略時は今日）/ `--note`（末尾に「 — {note}」を付す自由記述）

## Graph Layer

- `concepts/*.md` から派生する読み取り専用グラフ（nodes / edges / metadata.dangling_links）
- スクリプト: `skills/wiki/scripts/graph_gen.py`
- 出力先: `.wiki/outputs/graph.json`
- 役割: lint の `dead_link` / `orphan` 検出基盤。検出ロジックの二重実装を排除し、層越境を防ぐ
- 再生成: `python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki`
- cycle 実行時は `compile → graph_gen → lint` の順で orchestrator が明示的に呼び出す
- graph layer は派生物（derived）のため git 管理外運用も可能（必要に応じて `.wiki/.gitignore` で除外）

## Research Gaps

_未調査のトピックがあればここに記録_

# LLM Wiki Knowledge Base as Claude Skill

**Created:** 2026-04-05 18:32:34
**Status:** 💡 Idea
**Tags:** `llm-wiki`, `claude-skill`, `knowledge-base`, `karpathy`
**Mode:** Team Brainstorm
**Rounds:** 3

---

## Summary

Karpathy の「LLM Wiki」コンセプト（Raw Sources → Wiki → Schema の3層構造、Ingest/Query/Lint の3操作）を参考に、LLM向け知識ベースを Claude Skill として設計する。3ラウンドの壁打ちで MVP 設計が完全に固まり、wiki-ingest の SKILL.md 骨格、依存関係グラフ、コンテキストバジェット配分、Phase 0-4 のロードマップまで具体化された。

## Key Discussion Points

- イベントソーシング（JSONL or git commit）を全設計のバックボーンとして採用
- Schema は人間管理・LLM読み取り専用 + 提案APIで柔軟性と安全性を両立
- 検索は Strategy Pattern で grep → BM25 → ハイブリッドを DI で段階的に差し替え
- Lint は検出・レポートのみ（副作用ゼロ）、修正は severity 別に人間承認
- シングルエージェントで開始、パイプライン拡張可能な構造だけ準備
- 他スキル連携は MVP では commit のみ、Phase 2 でアクセス制御設計とセットで拡張

## Dispute Memory

### Accepted

全ロールが「面白い」と認めた案。

| # | Idea | Proposed By | Why Accepted |
|---|------|-------------|--------------|
| 1 | イベントソーシング型 Wiki | Explorer, Connector, Domain Expert | 3ロールが独立にJSONLイベントログ+コンパイル方式を提案。イミュータブル・リプレイ可能・テスタブル |
| 2 | ファイルシステム3層構造（sources/wiki/schema/） | Grounded | シンプルかつ外部依存ゼロ。全案の基盤。YAGNI原則に最適 |
| 3 | 3スキル分離（wiki-ingest/query/lint） | Grounded | 既存 claude-skills パターンとの整合性、単一責任原則 |
| 4 | 入力汚染検出（Ingest Sanitization Gate） | Challenger | プロンプトインジェクション・パストラバーサル対策。セキュリティは設計制約 |
| 5 | ソース追跡（Provenance Chain） | Challenger, Domain Expert | 回答の根拠を辿れる仕組み。ハルシネーション対策の基本 |
| 6 | 機密データ自動マスキング | Challenger | ソースに含まれる認証情報がWiki経由で漏洩するリスクへの対策 |
| 7 | 構造化Lint（JSON Schema + 構造化出力） | Connector, Domain Expert | Schema を型として活用し、Lint結果を機械的に処理可能に |
| 8 | Schema 権限分離（人間管理+提案API） | 全員合意（R2） | LLM読み取り専用 + schema-proposals/ に隔離。自己参照的腐敗を防止 |
| 9 | 検索の段階的昇格（Strategy Pattern） | Explorer, Domain Expert, Grounded（R2） | grep→BM25→ハイブリッドをDIで差し替え可能。争点を設計パターンで解消 |
| 10 | Lint は検出のみ・修正は人間承認 | Challenger, Connector, Domain Expert（R2） | severity 3段階（CRITICAL/WARNING/INFO）。自動修正はINFOのみ |
| 11 | シングルエージェントから開始 | Grounded, Domain Expert, Connector（R2） | MVP はシングル。パイプライン拡張可能な構造だけ準備 |
| 12 | Ingest 監査ログ（Audit Trail） | Challenger（R2） | git commit をイベントログ代替に使い監査可能性を担保 |
| 13 | プロンプト設計4パターン | Domain Expert（R2） | ロール固定、CoT出典明記、構造化Lint出力、差分Ingest |
| 14 | 3段階コンテキストロード | Domain Expert（R2） | Level1(目次2K)→Level2(サマリー20K)→Level3(本文100K) |
| 15 | 具体的ファイル構造 + MVP実装順 | Grounded（R2） | 7段階の実装優先順位 + フロントマター形式 + ディレクトリレイアウト |
| 16 | 他スキル連携: MVP=commit のみ | 全員合意（R3） | Phase 2以降の接続順も確定（investigate→cycle→doc-write→brainstorm-wrap） |
| 17 | wiki-ingest SKILL.md 骨格 | Grounded（R3） | 5フェーズ（入力検証→Schema読込→Wiki生成→保存→commit）の具体的手順書 |
| 18 | MVP実装順（Grounded+Connector統合版） | Grounded+Connector（R3） | Step 1〜7 + 依存関係グラフで完全整合検証済み |

### Controversial

ロール間で意見が分かれていた案。全て Round 2-3 で解決済み。

| # | Idea | For | Against | Core Tension | Resolution |
|---|------|-----|---------|--------------|------------|
| 1 | Schema の権限分離 | Challenger: 自己参照的腐敗を防ぐ | Grounded: MVP では過剰 | 自動化 vs 安全性 | R2で解決: 人間管理+提案API（Accepted #8に昇格） |
| 2 | BM25+ハイブリッド検索 | Domain Expert: 精度向上 | Grounded: grepで十分 | 初期複雑度 vs 検索品質 | R2で解決: Strategy Patternで段階的昇格（Accepted #9に昇格） |
| 3 | マルチエージェント分業 | Domain Expert: コンテキスト分散 | Challenger: 通信複雑さ | 分業メリット vs 統合コスト | R2で解決: シングルから開始（Accepted #11に昇格） |
| 4 | Lint の停止条件 | Challenger: 自動修正は危険 | Explorer: 自己修復と矛盾 | 自律性の範囲 | R2で解決: 検出のみ+severity別承認（Accepted #10に昇格） |
| 5 | 他スキル連携の範囲 | Connector: 統合メリット | Grounded: スコープ外 | 統合範囲 | R3で解決: commit連携のみ（Accepted #16に昇格） |

### Frontier

実装方法は不明だが革新的な可能性がある案。全てロードマップ化済み。

| # | Idea | Proposed By | Potential | Unknown | Roadmap Phase |
|---|------|-------------|-----------|---------|---------------|
| 1 | クエリドリブン型Wiki | Explorer | 需要駆動型進化、シード問いでコールドスタート解決 | シード問いの粒度、クラスタリング精度 | Phase 1（1-2ヶ月後） |
| 2 | 生命体型Wiki信頼度スコア | Explorer | query_success_rateの行動ログ逆算型、ページ品質の自動評価 | ログ蓄積前のスコア精度 | Phase 2（3ヶ月後） |
| 3 | ポータル型Wiki | Explorer | 外部システムアダプタ、セルフヒーリング | アダプタ品質保証 | Phase 4（6ヶ月+） |

## Round History

### Round 1

**Phase 1 (Independent Divergence):**
- Challenger: 入力汚染検出、Schema権限分離、Provenance Chain、機密データマスキング、Lint暴走防止（5件）
- Explorer: 生命体型Wiki、イベントソーシング型、ポータル型、クエリドリブン型、スペクトラム型スキーマ（5件）
- Connector: イベントソーシング×Ingest、クエリパターン×Lint、Schema×型安全、Obsidian双方向リンク、キャッシュ×Skill合成（5件）
- Grounded: シェル1コマンドIngest、ファイルシステム3層構造、grep MVP Query、Lintスキル化、3スキルエントリポイント（5件）
- Domain Expert: コンテキスト圧縮型更新、BM25+エンベディングハイブリッド、構造化Lint、イベントログ駆動、マルチエージェント分業（5件）

**Phase 2 (Classification):**
- Accepted: 7 ideas
- Controversial: 5 ideas
- Frontier: 4 ideas

**Phase 3 (User Feedback + Deep Dive):**
- User feedback: 次のラウンドで深掘りを希望
- Team response: Controversial と Frontier を中心に Round 2 へ

### Round 2

**Phase 1 (Deep Dive):**
- Challenger: Controversial 全5件への立場明確化 + Frontier 安全版4件 + 監査ログ新規提案
- Explorer: Frontier「制約1つ外し」具体化 + 汎用設計パターン5件 + 統合案3件（Reactive Wiki Engine等）
- Connector: Accepted 接続マップ + Controversial「第三の道」5件 + 既存スキル接続ポイント6件
- Grounded: MVP 実装優先順位7段階 + Controversial 妥協点5件 + 具体的ファイル構造・フロントマター
- Domain Expert: Controversial LLM推奨5件 + プロンプト設計4パターン + コンテキストウィンドウ管理5戦略

**Phase 2 (Classification):**
- Accepted: 15 ideas (+8、Controversial 4件が昇格、新規4件)
- Controversial: 1 idea (-4)
- Frontier: 3 ideas (-1、スペクトラム型がAcceptedに吸収)

**Phase 3 (User Feedback + Deep Dive):**
- User feedback: もう一ラウンド深掘りを希望
- Team response: 残りの Controversial + Frontier + セキュリティ俯瞰で Round 3 へ

### Round 3

**Phase 1 (Final Deep Dive):**
- Challenger: 他スキル連携最終見解 + Frontier 最悪ケース3件 + セキュリティ見落とし5件（API キー漏洩、パス traversal、間接プロンプトインジェクション、ログ改ざん、サプライチェーン）
- Explorer: Frontier Phase 2 優先順位 + 検証仮説 + Reactive Wiki Engine ロードマップ（Phase 0-4） + 根本的な問い3つ
- Connector: MVP/Phase 2 連携分離 + Accepted 15件の依存関係グラフ + Grounded 実装順との整合検証（完全整合+1件補足）
- Grounded: commit連携のみの根拠3つ + wiki-ingest SKILL.md 骨格 + 最小検証シナリオ2件 + やらないことリスト10件
- Domain Expert: Claude API 実装コード3件 + wiki-ingest 完全プロンプト例 + コンテキストバジェット数値（ingest 30K/query 63K/lint 123K per batch）

**Phase 2 (Classification):**
- Accepted: 18 ideas (+3)
- Controversial: 0 ideas (完全消滅)
- Frontier: 3 ideas (全てロードマップ化)

## Decisions & Conclusions

- MVP は wiki-ingest スキルのみで開始し、1ファイルの変換品質を検証する
- git commit をイベントログの代替として使い、JSONL は後回し
- 外部依存ゼロ（ベクトルDB不要、追加ライブラリ不要）
- セキュリティ（入力汚染検出・機密マスキング・パス正規化）は MVP から組み込む
- 検索・Lint・マルチエージェントは全て DI/Strategy Pattern で後から差し替え可能な設計

## Open Questions

- Wikiは誰のための知識か？（人間向けMarkdown vs LLM向けStructured Claim集）
- 知識の忘却はバグか仕様か？（イベントソーシング基盤+UIでの非表示で両立可能）
- WikiスキルはOSかAppか？（Read-only APIの「知識レジストリ」として疎結合に設計）
- シード問いの最適な粒度（「1シード問い = 1つの意図」が原則だが実証が必要）
- LLM API キーの管理方針（環境変数 vs Secret Manager）

## Next Steps

- `/claude-skills:plan-create` で MVP 実装計画を作成する
- Step 1: sources/ wiki/ schema/ ディレクトリ作成
- Step 2: note.md を sources/ に移動
- Step 3: schema/page-template.json 作成
- Step 3.5: Schema 作成（Connector 補足）
- Step 4-5: 入力汚染検出・機密マスキングの仕組みを Ingest に組み込む
- Step 6: wiki-ingest SKILL.md 実装
- 検証: 基本変換テスト + 機密データ検出テストの2シナリオで成功判定

## Appendix: MVP Implementation Details

### ディレクトリレイアウト

```
wiki-knowladge/
├── sources/                  # Raw Sources（人間がキュレーション）
│   └── 20260405_llm-wiki-idea.md
├── wiki/                     # LLM生成マークダウン（相互参照付き）
│   ├── index.md
│   ├── concepts/
│   ├── how-to/
│   └── reference/
├── schema/                   # 構造定義（静的JSON、人間管理）
│   ├── page-template.json
│   └── categories.json
└── docs/
    └── ideas/
```

### Wiki ページフロントマター

```yaml
---
title: ページタイトル
source_ref: sources/YYYYMMDD_slug.md
created: YYYY-MM-DD
updated: YYYY-MM-DD
category: concepts|how-to|reference|archive
related: []
---
```

### コンテキストバジェット配分

| スキル | 通常コスト | 最大コスト | 主なボトルネック |
|--------|-----------|-----------|----------------|
| wiki-ingest | ~30,000 tokens | 180,000 tokens | Raw source サイズ |
| wiki-query | ~63,000 tokens | 180,000 tokens | 関連ページ本文ロード数 |
| wiki-lint | ~123,000 tokens/batch | 180,000 tokens/batch | 比較対象ページ数 |

### Reactive Wiki Engine ロードマップ

```
Phase 0 (今)   : Event Log + Wiki + Seed Query + grep検索
Phase 1 (1-2M) : + QueryLog + Gap Detection + Auto Ingest提案
Phase 2 (3M)   : + Trust Score + Lint強化
Phase 3 (4-5M) : + Multi-Resolution + Intent Detection
Phase 4 (6M+)  : + Portal Adapter + Self-Healing Adapter
```

### セキュリティ見落とし（Challenger R3 指摘）

| # | リスク | 深刻度 | MVP対策 |
|---|--------|--------|---------|
| 1 | LLM API キーの漏洩 | HIGH | 環境変数取得、ログ出力禁止テスト |
| 2 | Wiki ファイルのパス traversal | HIGH | ファイル名サニタイズ（英数字+ハイフンのみ） |
| 3 | Lint の間接プロンプトインジェクション | MEDIUM | Wikiコンテンツを「データ」として渡すフレーミング |
| 4 | イベントログの改ざん | LOW (MVP) | git commit が改ざん検知の代替 |
| 5 | 依存ライブラリのサプライチェーン | LOW (MVP) | MVP は外部依存ゼロ |

## Appendix: 先人の実装分析

Karpathy Gist のコメント欄に投稿された実装群を clone して分析した結果。
リポジトリは `examples/` に配置（.gitignore 対象）。

### 分析対象リポジトリ一覧

| リポジトリ | 言語/形態 | 概要 |
|-----------|----------|------|
| [pedronauck/skills](https://github.com/pedronauck/skills) (karpathy-kb) | Claude Skill | 最も完成度の高い Skill 実装。4相パイプライン、Query→Wiki promote、Obsidian統合 |
| [kfchou/wiki-skills](https://github.com/kfchou/wiki-skills) | Claude Plugin | 5スキル構成（init/ingest/query/lint/update）。SCHEMA.md 中心設計 |
| [xoai/sage-wiki](https://github.com/xoai/sage-wiki) | Go CLI/MCP | RRF検索（BM25+Vector+Tag+Recency）、SQLite FTS5、Ontologyグラフ、14 MCPツール |
| [mpazik/binder](https://github.com/mpazik/binder) | TypeScript | トランザクションログ→グラフDB→Markdown の3層。LSP/MCP/CLI の3インターフェース |
| [flyersworder/lens](https://github.com/flyersworder/lens) | Python | SQLite+sqlite-vec、FTS5+RRF ハイブリッド検索、TRIZ矛盾行列 |
| [Astro-Han/karpathy-llm-wiki](https://github.com/Astro-Han/karpathy-llm-wiki) | Claude Skill | npx add-skill でインストール可能。URL取り込み対応 |
| [ractive/hyalo](https://github.com/ractive/hyalo) | CLI | frontmatter検索、リンク自動修復 |
| [Okohedeki/NANTA](https://github.com/Okohedeki/NANTA) | - | TikTok/YouTube等の多元ソース取り込み |
| [hrishikeshs/magnus](https://github.com/hrishikeshs/magnus) | - | Claude Code統合、Emacs連携 |
| [VictorVVedtion/vibe-sensei](https://github.com/VictorVVedtion/vibe-sensei) | - | JSONL+Markdown ハイブリッド |

### 比較マトリックス

| 観点 | pedronauck (Skill) | wiki-skills (Skill) | sage-wiki (Go) | binder (TS) | lens (Python) |
|------|-------------------|---------------------|----------------|-------------|---------------|
| **コア操作** | 4相: ingest→compile→query→lint | 5スキル: init/ingest/query/lint/update | 5相: diff→summary→concept→write→image | CRUD+トランザクション | 4相: acquire→extract→build→analyze |
| **検索** | コンテキスト全文ロード | index.mdスキャン | RRF (BM25+Vector+Tag+Recency) | グラフクエリ+フィルタ | FTS5+sqlite-vec RRF |
| **永続化** | Markdown+git | Markdown+git | SQLite (FTS5+BLOB vector) | SQLite+トランザクションログ | SQLite+sqlite-vec |
| **知識グラフ** | wikilink | wikilink | Ontology (8種リレーション) | 型付きグラフDB | 矛盾行列+進化木 |
| **スキーマ** | CLAUDE.md (topic別) | SCHEMA.md (中央) | config.yaml+manifest | 型付きスキーマ=データ | vocabularyテーブル |
| **Query→Wiki昇格** | あり (promote) | あり (保存提案) | あり (auto-file) | - | - |
| **Backlink audit** | 必須 (3回言及) | 必須 (Step 7) | 自動 (compile時) | 逆関係自動展開 | - |
| **監査ログ** | log.md (grep可) | log.md (append-only) | CHANGELOG自動 | イミュータブルTxログ | event_logテーブル |
| **規模想定** | 100+記事, 400K語 | 中小規模 | 中〜大規模 | 任意 | 論文数百〜数千 |

### 取り入れるべき設計パターン（優先順）

#### 1. Query → Wiki Promote パターン (pedronauck)

壁打ちでは出なかった重要概念。Query の回答を `outputs/queries/` に保存し、品質が十分なら `wiki/concepts/` に「昇格」させる。知識がQueryからも複利的に成長する Karpathy 思想の核心。

- Phase A: wiki を読んで回答を合成（全引用に `[[wikilink]]`）
- Phase B: 回答をファイルに保存、矛盾があれば既存記事を再コンパイル
- 昇格条件: 比較表、トレードオフ分析、合成概念など「持続的な価値」がある回答

#### 2. Ingest と Compile の分離 (pedronauck)

壁打ちの設計では wiki-ingest 1つにまとめていたが、pedronauck は明確に分離:
- **Ingest**: ソースを `raw/` にステージング（immutable、firecrawl/手動）
- **Compile**: `raw/` から `wiki/concepts/` に記事を生成（backlink audit付き）

メリット:
- バッチ取り込み→後から一括コンパイルが可能
- raw/ の不変性で再現性確保
- マルチトピック vault での非同期コンパイル

#### 3. RRF ハイブリッド検索 (sage-wiki, lens)

Phase 2 の検索改善で直接使えるレシピ:
```
score = 1/(60 + bm25_rank) + 1/(60 + vector_rank) + tagBoost + recencyDecay
```

sage-wiki (Go):
- Pure Go実装（CGOなし）、SQLite FTS5 + BLOB vector
- Tag Boost: +3%/tag (cap 15%)
- Recency Decay: 14日半減期、max +5%

lens (Python):
- sqlite-vec 拡張で cosine距離
- FTS5 + vec0 を SQL の WITH句で結合
- SPECTER2 埋め込み（科学論文特化768次元）

#### 4. Ontology グラフ — 型付きリレーション (sage-wiki, binder)

wikilink を超えた構造化:

sage-wiki — 8種リレーション:
- implements, extends, optimizes, contradicts, cites, prerequisite_of, trades_off, derived_from
- BFS/DFS で深さ5まで探索可能
- MCP経由で `wiki_ontology_query(entity, relation, direction, depth)` として公開

binder — 型付きグラフDB:
- エンティティ・フィールド・参照の3層モデル
- 逆関係自動展開（`children` ↔ `parent`）
- トランザクションログで全変更追跡

#### 5. CLAUDE.md をスキーマドキュメントとして活用 (pedronauck)

トピックごとに CLAUDE.md を配置:
- Topic scope（何を含み何を含まないか）
- 規約（frontmatter、wikilink形式）
- 現在の記事一覧
- 研究ギャップ（次に取り込むべき領域）

LLM が自然に読むファイルをスキーマ代わりにする。あーしたちの `schema/page-template.json` と相補的。

### 追加で参考にすべきパターン

#### log.md の Unix クエリ可能な形式 (pedronauck, wiki-skills)

```bash
grep "^## \[" log.md | tail -10       # 直近10件
grep "^## \[.*compile" log.md | wc -l # コンパイル回数
```

git commit log より軽量で、Skill 内から直接クエリ可能。

#### Backlink Audit の必須化 (pedronauck, wiki-skills)

両方のSkill実装が「最もスキップされがちだが最も重要なステップ」として強調:
- 新記事のタイトル・エンティティで既存ページを grep
- マッチした箇所に `[[新記事]]` の双方向リンクを追加
- これがないと wiki は blog に退化する

#### compile パイプラインの5段階チェックポイント (sage-wiki)

`.sage/compile-state.json` で進捗保存。失敗時に途中から再開可能（idempotent）。
大規模プロジェクトでの中断・再開が容易。

#### トランザクションログの Changeset 4操作 (binder)

- **Apply**: 状態 + 変更 = 新状態
- **Inverse**: 変更の反転（完全な取消）
- **Squash**: 複数変更の圧縮（履歴圧縮）
- **Rebase**: 競合する変更の調整（マージ・同期）

イベントソーシングの本格実装として Phase 2 以降の参考に。

#### 語彙駆動抽出 — クラスタリング排除 (lens)

LLM が抽出時に直接概念を正規化。`NEW:` 接頭辞で未知概念を標識:
- 1回の LLM 呼び出しで3タイプを同時抽出
- クラスタリングの「ブラックボックス性」を排除
- 語彙リストをプロンプトに inject して用語統一

### 設計への影響: MVP 修正提案

壁打ちの MVP 設計に対する先人分析からの修正提案:

| 項目 | 壁打ち時の設計 | 先人から学んだ修正 |
|------|-------------|------------------|
| wiki-ingest | 1スキルで取り込み+Wiki生成 | **Ingest（raw/ステージング）と Compile（wiki/生成）を分離** |
| wiki-query | 検索+回答のみ | **回答の保存（outputs/queries/）と Wiki への promote 機能を追加** |
| 監査ログ | git commit で代替 | **log.md（grep可能な append-only）も併用** |
| Backlink | Lint で検出 | **Compile 時の必須ステップとして組み込む** |
| スキーマ | schema/page-template.json | **CLAUDE.md をスキーマドキュメントとして併用**（scope・ギャップ管理） |

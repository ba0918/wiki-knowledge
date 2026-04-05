# Gap Detection + Auto Ingest 提案

**Cycle ID:** `20260406030813`
**Started:** 2026-04-06 03:08:13
**Status:** 🔵 Implementing

---

## 📝 What & Why

QueryLog に蓄積されたクエリデータから「Wiki にまだ無い知識のギャップ」を検出し、ingest 候補を自動提案する機能を実装する。Wiki が自律的に成長するためのエンジンとなる Phase 2b+2c の中核機能。

## 🎯 Goals

- QueryLog の `gap_topics` を集計し、既存記事とのカバレッジを計算してギャップを確定する
- 確定したギャップごとに優先度付きの ingest 提案を生成する
- lint ワークフローに統合し、定期的にギャップを検出できるようにする

## 📐 Design

### アーキテクチャ

```
QueryLog (gap_topics)
    ↓
gap_detect.py [Pure Functions]
    ├── detect_gaps()     — トピック集計 + 記事カバレッジ照合
    └── generate_proposals() — 優先度計算 + 検索クエリ候補生成
    ↓
Output: table / json / report
    ↓
SKILL.md lint セクション統合（Trust Score チェックの後）
```

### データ型

```python
@dataclass(frozen=True)
class ConfirmedGap:
    topic: str           # gap_topics から
    frequency: int       # 出現回数
    coverage: float      # 既存記事によるカバー率 0.0-1.0
    related_articles: tuple[str, ...]  # 部分的に関連する記事スラッグ（immutable）

@dataclass(frozen=True)
class IngestProposal:
    topic: str
    priority: float      # frequency × (1 - coverage)、正規化 0.0-1.0
    suggested_queries: tuple[str, ...]  # 検索クエリ候補（immutable）
    related_articles: tuple[str, ...]   # （immutable）
```

### カバレッジ計算アルゴリズム

1. gap topic をトークン化（`extract_tokens` — 下記参照）
2. 各記事の title, tags, slug, body text をトークン化し **記事ごとにキャッシュ**（同じ記事を複数 topic で再計算しない）
3. トークン重複率 = `len(topic_tokens & article_tokens) / len(topic_tokens)`
4. 全記事中の最大重複率をカバレッジとして採用
5. threshold（デフォルト 0.8）未満のトピックをギャップとして確定

### トークン化戦略（`extract_tokens`）

- **英語**: 小文字化 → スペース/ハイフン/アンダースコアで分割
- **日本語**: 文字単位の n-gram（bigram）で分割。外部形態素解析器への依存を避けるため、簡易的な文字 bigram を採用
- **混在テキスト**: ASCII 部分とマルチバイト部分を分離し、それぞれの戦略を適用して union
- **正規化**: 全角英数字は半角に変換、記号は除去

この戦略は将来的に形態素解析器ベースの実装に差し替え可能なよう、`extract_tokens(text: str) -> frozenset[str]` のインターフェースで隔離する。

### Files to Change

```
skills/wiki/scripts/
  gap_detect.py         - 新規: Gap Detection + Auto Ingest 提案のコアスクリプト (~350行)
  test_gap_detect.py    - 新規: テスト (~350行, ~21テスト)

skills/wiki/
  SKILL.md              - 編集: lint セクションに Gap Detection チェック追加 (+15行)

CLAUDE.md               - 編集: Gap Detection の説明追加 (+5行)
```

### Key Points

- **既存コード再利用**: `load_querylog`（querylog_stats.py）、`parse_frontmatter` / `find_wikilinks`（lint-wiki.py）を import。trust_score.py と同じ `importlib` パターンを使用
- **`load_articles` は新規実装**: trust_score.py の `parse_article_metadata` は body text / tags を返さないため、gap_detect 用に body + tags を含む `ArticleInfo` を新規定義。将来的に共通化の余地はあるが、現時点では責務が異なるため別実装とする
- **提案のみ、自動実行しない**: ユーザーが `wiki ingest` で明示的に取り込む設計。安全性を優先
- **3フォーマット出力**: table（CLI）/ json（プログラム連携）/ report（Markdown レポート）— trust_score.py と同パターン
- **空 QueryLog 対応**: グレースフルに "ギャップデータなし" を返す
- **矛盾データ対応**: `gap_noted: true` かつ `gap_topics: []` のエントリはスキップ（集計対象外）。`gap_noted: false` のエントリも同様にスキップ

## 🔧 Implementation Steps

### Step 1: gap_detect.py — コアロジック実装

**File:** `skills/wiki/scripts/gap_detect.py`

1. importlib で `lint-wiki.py` と `querylog_stats.py` を読み込み（trust_score.py パターン踏襲）
2. `ArticleInfo(frozen=True)` データクラス定義 — slug, title, tags: tuple[str, ...], body, tokens: frozenset[str]
3. `load_articles(concepts_dir) -> list[ArticleInfo]` — 記事読み込み + トークン事前計算（I/O 層）
4. `extract_tokens(text: str) -> frozenset[str]` — テキストをトークン集合に変換（純粋関数、日本語 bigram 対応）
5. `compute_coverage(topic_tokens: frozenset[str], articles: list[ArticleInfo]) -> tuple[float, tuple[str, ...]]` — カバレッジ計算（純粋関数）。戻り値は `(max_coverage, related_slugs)`。各記事のトークンとの重複率を計算し、最大値をカバレッジ、threshold の半分以上の記事を related として返す
6. `detect_gaps(entries, articles: list[ArticleInfo], threshold) -> list[ConfirmedGap]` — ギャップ検出（純粋関数、`gap_noted: true` かつ `gap_topics` が非空のエントリのみ処理）
7. `generate_proposals(gaps) -> list[IngestProposal]` — 提案生成（純粋関数）

### Step 2: gap_detect.py — 出力フォーマッタ

1. `format_table(gaps, proposals)` — ASCII テーブル
2. `format_json(gaps, proposals)` — JSON 出力
3. `format_report(gaps, proposals, today)` — Markdown レポート → `{wiki_root}/outputs/reports/{YYYYMMDD}-gap-detect.md`

**注**: レポート出力時のディレクトリ作成（`mkdir -p`）は CLI エントリポイント（`main()`）で実行する（trust_score.py と同じパターン）。

### Step 3: gap_detect.py — CLI エントリポイント

```bash
python3 gap_detect.py --wiki-root .wiki [--format table|json|report] [--threshold 0.8]
```

### Step 4: test_gap_detect.py — テスト実装

| テストクラス | テスト数 | 内容 |
|-------------|---------|------|
| TestExtractTokens | 5 | 基本分割、ハイフン処理、空文字、日本語 bigram、混在テキスト |
| TestComputeCoverage | 4 | 完全一致、部分一致、不一致、単トークン |
| TestDetectGaps | 5 | 通常ケース、全カバー済、空querylog、threshold制御、related |
| TestGenerateProposals | 3 | priority計算、suggested_queries、空リスト |
| TestFormatters | 2 | JSON構造、report ヘッダー |
| TestIntegration | 2 | tmp_path でフルパイプライン、空querylog |

### Step 5: SKILL.md lint セクション更新

Trust Score チェックの後に追加:

```markdown
### Gap Detection チェック（gap_detect.py）

Trust Score チェックの後に `scripts/gap_detect.py` を実行する:

python3 scripts/gap_detect.py --wiki-root {wiki_root}

Priority が **0.7 以上** の Ingest Proposal は lint レポートの 🔵 Info として記載:

> 🔵 Info:「{topic}」が {frequency} 回ギャップとして検出（Priority: {priority}）。
> `wiki ingest` による取り込みを検討してください。
```

### Step 6: CLAUDE.md 更新

Gap Detection の説明を追加。

## ✅ Tests

- [ ] extract_tokens: 基本的なトークン分割（英語）
- [ ] extract_tokens: ハイフン付き文字列の処理
- [ ] extract_tokens: 空文字列
- [ ] extract_tokens: 日本語テキストの bigram 分割
- [ ] extract_tokens: 英日混在テキスト
- [ ] compute_coverage: 完全一致 → 1.0
- [ ] compute_coverage: 部分一致 → 0.5
- [ ] compute_coverage: 不一致 → 0.0
- [ ] compute_coverage: 単一トークン
- [ ] detect_gaps: カバーされていないトピックのみ返す
- [ ] detect_gaps: 全トピックカバー済み → 空リスト
- [ ] detect_gaps: 空 querylog → 空リスト
- [ ] detect_gaps: threshold パラメータで制御
- [ ] detect_gaps: related_articles の正確性
- [ ] generate_proposals: priority = frequency × (1 - coverage) の正規化
- [ ] generate_proposals: suggested_queries にトピック文字列を含む
- [ ] generate_proposals: 空リスト → 空リスト
- [ ] format_json: 有効な JSON 構造
- [ ] format_report: 期待する Markdown ヘッダーを含む
- [ ] integration: tmp_path でフルパイプライン
- [ ] integration: 空 querylog で有効な出力

## 📊 Progress

| Step | Status |
|------|--------|
| Step 1: コアロジック | 🟢 |
| Step 2: フォーマッタ | 🟢 |
| Step 3: CLI | 🟢 |
| Step 4: テスト | 🟢 |
| Step 5: SKILL.md 統合 | ⚪ |
| Step 6: CLAUDE.md 更新 | ⚪ |
| Commit | ⚪ |

**Legend:** ⚪ Pending · 🟡 In Progress · 🟢 Done

---

**Next:** Write tests → Implement → Commit with `claude-skills:commit` 🚀

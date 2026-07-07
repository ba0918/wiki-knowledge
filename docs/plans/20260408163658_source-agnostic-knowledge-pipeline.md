# Source-Agnostic Knowledge Pipeline

**Cycle ID:** `20260408163658`
**Started:** 2026-04-08 16:36:58
**Status:** ⏸️ Paused — schema 体制の裁定により Phase 0.11-0.13（migrate.py CLI + 12記事 v1 昇格）は**採用トリガー発火時に実施**へ変更（2026-07-07）

> **⚖️ 裁定注記（2026-07-07）**: v0 を schema-of-record と宣言し、本 plan の v1 スキーマ + migrations は「採用トリガー付き standby 資産」に再定義された。採用トリガー: 「concepts/ に raw/ から再導出できない状態を書き込む最初の機能（review resolve / claim 仲裁 / 出典なし promote）は v1 migration と同一サイクルでリリースする」。`Source.revision` は repo-ingest との整合のため先行して正式追加済み。詳細: [docs/plans/20260707194819_schema-regime-decision.md](20260707194819_schema-regime-decision.md)

---

## 📝 What & Why

Slack 検索性の破綻により埋もれている業務ドメイン知識（意思決定・サポート対応）を LLM で引ける検索可能な Wiki に変換する。ただし Slack にロックインせず、**任意の一次情報を構造化知識へ昇華する汎用パイプライン**として設計する。Slack は最初の Fetcher 実装にすぎず、将来 Teams / GitHub Issues / Email / 会議録等を追加可能にする。個人ドッグフーディングで価値実証 → チーム資産化の段階導入。

**データ規模前提**: 実運用 Slack は稼働 5〜10 年分、**重要スレッドに絞れば 500〜2,000 スレッド程度**を想定する。将来 20 年分スケールに拡張する際のための「保存形式の不可逆性」だけは最初から守るが、初期 MVP では 500〜2,000 スレッド規模を前提にシンプルな実装で良い（Performance Design Document 不要）。

背景詳細・設計議論の全記録: [docs/ideas/archives/20260408161026_source-agnostic-knowledge-pipeline.md](../ideas/archives/20260408161026_source-agnostic-knowledge-pipeline.md)

### scope cut: 初期実装から除外した項目（Q12-2 A 案採用）

以下 4 件は忘却防止のため docs/issues/ に登録済み。着手判断基準は各 issue を参照:

- `docs/issues/20260408202825_compiler-api-phase-2-5.md` — Compiler API 実装 (Phase 2.5 送り)
- `docs/issues/20260408202826_validator-llm-conflict-phase-5.md` — Validator 第 2 段 LLM ベース矛盾検出 (Phase 5+ 送り)
- `docs/issues/20260408202827_compiler-intent-classifier-llm.md` — Compiler intent_classifier の LLM 化検討 (当初 rule-based)
- `docs/issues/20260408202828_token-keyring-phase-2-5.md` — Token 管理の keyring 化 (Phase 2.5+ 送り)

## 🎯 Goals

- **基礎工事の不可逆性を確保**: schema_version / article_id / sources[] / knowledge_time / claims[] / claim_refs[] / extensions{} / generated_by を最初から固定し、後から migration 不要にする
- **ソース非依存の抽象レイヤを導入**: Orchestrator / Fetcher / Normalizer / Compiler / Validator の 5 層（型契約）、registry パターンで Fetcher を差し替え可能に。**実装モジュールは 3 層 (Domain / Service / Handler) に集約可**
- **記事型 4 種の導入**: decision / runbook / reference / concept。型は「view + required fields set」として扱い、article_type は可変・article_id は不変
- **Slack を第 1 号 Fetcher として実装**: slack_thread / slack:// スキーム対応、User Token 方式（多層フォールバック）、増分更新
- **競合解決と時効管理**: 現行 + 経緯ハイブリッド構造、rule-based 矛盾検出、「自動で警報、手動で裁定」原則
- **v0 → v1 Migration コマンドの整備**: 既存 12 記事の schema 追従を保証する再実行可能な migration 基盤（dry-run / backup / rollback 対応）
- **個人ドッグフーディングで価値実証**: 評価メトリクス（一次情報到達率・検索時間短縮・重複質問削減）を定量化

## 📐 Design

### 設計原則（不可侵）

- **Schema は最大主義、Implementation は最小主義**: schema フィールドは最初から完全に入れる（後から migration 困難）、実装ロジックは scope cut OK（後から追加しても既存記事に触らない）
- **Testability Above All**: 全ての domain 関数は pure function、外部依存（fcntl / time / LLM / filesystem / network / env）は全て DI 化
- **基礎工事はケチらない**: 「段階的実装」は UI・推論・検索の高度化にのみ許され、保存形式には適用しない
- **Immutability by Default**: domain types は全て `@dataclass(frozen=True)`（TypedDict 不使用、既存 `lib/inventory.py` の precedent に合わせる）
- **Result 型で明示的エラーハンドリング**: 期待される失敗（token 解決失敗 / source 取得失敗 / LLM 失敗 / 矛盾検出）は `Result[T, Error]` 型で返却する。例外は「本当に例外的な事態」にのみ使用
- **article_type は view、article_id は不変**: 型変更は migration 不要
- **会話は segments[] のまま後段へ**: 発言者・時系列を本文文字列に潰さない（後段の型分類精度が落ちる）
- **LLM は候補抽出と説明生成のみ**: 最終判定器にしない。「自動で警報、手動で裁定」
- **記事編集は LLM 経由のみ**: 人間による直接手動編集は禁止。記事改変は wiki-compile / migrate / review コマンド経由に統一する。これにより provenance / generated_by の一貫性を保つ
- **Provenance の二段構成**: plain data は domain types（timestamp は文字列としてフィールドに持つだけ）、生成ロジックは `lib/service/provenance.py`（time.now() 等の I/O を担当）。これで domain 層の純粋性を保つ
- **Plugin は継承より registry**: callable を `@register_fetcher("kind")` で登録
- **Domain(pure) → Service(I/O) → Handler(CLI)** の依存方向を守る。**逆方向依存・層間スキップは禁止**
- **単一 canonical path validator**: `lib/service/path_validator.py` を全 I/O 箇所（wiki_repo / fetchers / migrations / token_resolver）で再利用する
- **既存スクリプト再編は並行アプローチ**: 新機能は新構造、既存は徐々に移行
- **元データを壊さず、後から読む側を賢くする**: 一度取り込んだ `.wiki/raw/` は immutable。解釈の改善は compile 側で吸収する

### 5 層型契約 ↔ 3 層実装モジュールの対応（明示）

**型契約は 5 層分を維持**（層越境を防ぎ、各層の責務を明確化）、**実装モジュールは 3 層に集約**（過剰分割を避ける）という二層構造を採る。Orchestrator は「独立層」ではなく **Service 層の concrete module** (`lib/service/orchestrator.py`) として実装する。

```
型契約 (5 layers, 抽象)          実装モジュール (3 layers, 具象)
────────────────────             ──────────────────────────
Orchestrator                     ↘
Fetcher     (registry plugin)    ↘
Normalizer  (pure rules)         →  Service (lib/service/)
Compiler    (LLM 必須、DI)        ↗
Validator   (rule-based + future LLM) ↗

                                 ↓ 依存方向
                                 Domain (lib/domain/, pure)

                                 ↑ 呼び出し
                                 Handler (skills/wiki/scripts/*.py, thin CLI)
```

**依存方向（厳守）**:
```
Handler (CLI)  ──calls──>  Service (Orchestrator, Fetchers, Compiler services, ...)
                              │
                              └──depends_on──>  Domain (pure functions, types)
```

- Handler は Service のみを import、Domain を直接触らない（Service 経由）
- Service は Domain を import、Handler を import しない
- Domain は何も import しない（stdlib のみ、副作用ゼロ）
- Provenance は Domain type + Service generator の二段構成

### 基礎工事フロントマター（Phase 0 で確定・以後不変）

```yaml
---
# 不変・基礎工事
schema_version: 1
article_id: 20260408163658-customer-a-ops  # YYYYMMDDHHMMSS-{slug}、衝突時 -2 サフィックス（atomic allocation）
article_type: runbook                        # 可変 view (decision | runbook | reference | concept)
title: 顧客A 運用フロー

# 時間軸・来歴
captured_at: 2026-04-08
knowledge_time:
  valid_from: 2023-04-01
  valid_to: null   # null = "still current, no known end"（将来明示的に終了を記録）
status: current    # current | historical | disputed | unverified （4 値に固定）

# 出典（構造化）— Phase 0 で sources[] 全体を確定
sources:
  - id: src-001
    type: slack_thread
    ref: .wiki/raw/slack/20230401-thread.md
    permalink: https://...          # optional
    source_version: 1                # 増分更新ごとに +1（不可逆、Fetcher 管理）
    content_hash: sha256:abcd1234    # 差分検出・改竄検出の両用
    fetched_at: 2026-04-08T09:12:00Z # UTC ISO8601、Fetcher 取得時刻

# 型横断グラフ — superseded_by は relations で管理（status では持たない）
relations:
  supersedes: []
  superseded_by: null
  caused_by: []
  derived_from: []
  implements: []
  depends_on: []
  related_to: []

# claim スロット（Phase 0 で schema 確定、Phase 3 compile 時に埋まる）
claims: []

# claim 参照スロット（Phase 0 で確定、初期は empty。Phase 3 で書き込みロジック追加）
claim_refs: []

# 生成来歴（LLM 経由編集前提を明示）
generated_by:
  tool: wiki-compile
  version: 1                         # compile ロジックのバージョン
  generated_at: 2026-04-08T09:12:00Z

# 名前空間付き拡張点
extensions: {}

# 型固有フィールドはここから（type 別）
tags: []
---
```

**重要**: `claims[]` / `claim_refs[]` / `generated_by` / `source_version` / `content_hash` / `fetched_at` はすべて Phase 0 で schema に入れる。初期実装では空のまま保持されるが、後から追加する際の migration を避けるため前倒しする（Q12-1 確定）。

### status 状態モデル（単一ソース）

- **永続化される status 値**: `current | historical | disputed | unverified` のこの 4 値のみ
- **Phase 4 Validator が矛盾を検出したとき**: `status = disputed` に変更（`conflicted` という別語彙は使わない）
- **情報不足時**: `status = unverified` を使う（`draft` という別語彙は使わない）
- **置換関係**: `superseded_by` は `relations.superseded_by: <article_id>` で管理、`status = historical` とセットで設定する
- **CLI (`wiki review resolve`) の --status 引数**: 上記 4 値のみを受け付け、`--superseded-by <id>` は別フラグで提供
- **型表現**: `Status = Literal["current", "historical", "disputed", "unverified"]`

### audit trail の保存場所（schema との整合）

- `generated_by` は **tool / version / generated_at の 3 項目のみ**（schema も `GeneratedBy` 型も一致）
- **review resolve の監査記録は `extensions["review"]["audit"]` 配下に追記**する
  - 型: `tuple[ReviewAuditEntry, ...]`（dict 側は read-only 運用規約で担保）
  - 追記のみ、既存 entry は書き換え不可
- `extensions` 名前空間の予約: `extensions["review"]` は review.py 専用、他モジュールは使わない
- schema file `.wiki/schema/page-template-v1.json` にも `extensions.review.audit` の JSON schema を明記

### CLI dispatcher 方針

- **既存の `skills/wiki/SKILL.md` の `wiki <subcommand>` ルーティングを継承**する（`wiki ingest | wiki compile | wiki query | wiki lint | wiki cycle` 等）
- 新規追加する subcommand は **独立 Python スクリプトとして `skills/wiki/scripts/` 配下に配置**し、SKILL.md 側でどの script を呼ぶかを定義する（単一 entrypoint `wiki.py` は作らない）
- 追加される subcommand: `wiki migrate` / `wiki slack ingest` / `wiki slack check-auth` / `wiki review list|show|resolve` / `wiki stats`
- 共通の CLI 規約（`--quiet` / `--json` / `--no-color` / exit code 0/1/2/130）は `references/cli-conventions.md` に集約、各 script から参照
- ネスト namespace (`wiki slack <verb>`) は SKILL.md 側で dispatcher ロジックを持たせる

### 本文骨格（共通部分）

```markdown
## Summary
## Current Understanding
## History / Changes
## Evidence Notes
## Open Questions
```

型固有セクションは Current Understanding の後に挿入。

### Domain 型 (frozen dataclass)

```python
# lib/domain/types.py より抜粋（全て frozen dataclass）
from dataclasses import dataclass, field
from typing import Literal, TypeVar, Generic

ArticleType = Literal["decision", "runbook", "reference", "concept"]
Status = Literal["current", "historical", "disputed", "unverified"]
SchemaVersion = Literal[1]  # 追加時は Literal[1, 2] に拡張

@dataclass(frozen=True)
class Source:
    id: str
    type: str                 # e.g., "slack_thread", "file", "url"
    ref: str                  # 相対パス（.wiki/ 内）
    source_version: int
    content_hash: str         # "sha256:..." 形式
    fetched_at: str           # ISO8601 UTC
    permalink: str | None = None

@dataclass(frozen=True)
class GeneratedBy:
    tool: str
    version: int
    generated_at: str         # ISO8601 UTC (Service layer で生成・注入)

@dataclass(frozen=True)
class Segment:
    speaker: str
    speaker_type: Literal["user", "bot", "system"]
    ts: str                   # Slack ts など
    content: str
    edited_at: str | None = None
    deleted: bool = False
    orphan: bool = False      # 親 message 欠落 reply
    reply_to: str | None = None

@dataclass(frozen=True)
class KnowledgeTime:
    valid_from: str | None    # ISO date
    valid_to: str | None       # None = "still current, no known end"

@dataclass(frozen=True)
class Relations:
    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None
    caused_by: tuple[str, ...] = ()
    derived_from: tuple[str, ...] = ()
    implements: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    related_to: tuple[str, ...] = ()

@dataclass(frozen=True)
class Claim:
    claim_id: str              # deterministic: {article_id}#c-{sha256(canonical(subject, attribute, period, predicate))[:8]}
                               # 同一入力 → 同一 ID を保証（再 compile で ID 漂流しない）
                               # canonical(): subject/attribute/predicate を NFC normalize + trim、period は ISO8601、全体を JSON canonical form で hash
    subject: str               # e.g., "Customer A", "auth-service"
    attribute: str             # e.g., "runbook_exists", "mttr_hours", "owner"
    period: KnowledgeTime      # いつ有効な claim か
    predicate: str             # 人間可読の事実記述
    source_refs: tuple[str, ...]  # sources[].id の list（どの出典から抽出したか）

@dataclass(frozen=True)
class ReviewAuditEntry:
    resolver: str              # ユーザー名 or "system"
    resolved_at: str           # ISO8601 UTC
    status_before: Status
    status_after: Status
    reason: str = ""
    superseded_by_id: str | None = None

@dataclass(frozen=True)
class Article:
    schema_version: int
    article_id: str           # 不変
    article_type: ArticleType
    title: str
    captured_at: str
    knowledge_time: KnowledgeTime
    status: Status
    sources: tuple[Source, ...]
    relations: Relations
    claims: tuple[Claim, ...]
    claim_refs: tuple[str, ...]           # 他記事の claim_id を参照
    generated_by: GeneratedBy              # tool / version / generated_at のみ
    extensions: dict[str, object]          # read-only 運用（frozen dataclass 外側で変更しない規約、運用で担保）
                                           # 既知 namespace: extensions["review"]["audit"]: tuple[ReviewAuditEntry, ...]
    tags: tuple[str, ...]
    body: str

# Result 型（期待される失敗に使用）
T = TypeVar("T")
E = TypeVar("E")

@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T

@dataclass(frozen=True)
class Err(Generic[E]):
    error: E
    detail: str = ""

Result = Ok[T] | Err[E]
```

### LLM Client DI 設計

```python
# lib/service/llm_client.py より抜粋
from typing import Protocol, Callable

class LLMClient(Protocol):
    def complete(self, prompt: str, *, temperature: float = 0.0, seed: int | None = None) -> str: ...

# シンプルな test double — callable を直接受ける
LLMCompleter = Callable[[str], str]

class FixedResponseLLM:
    """全呼び出しで同じ文字列を返す最小限のテスト double。"""
    def __init__(self, response: str) -> None: self._r = response
    def complete(self, prompt, **_): return self._r

class MappedResponseLLM:
    """prompt substring match でマップ応答を返す。"""
    def __init__(self, mapping: dict[str, str], default: str = "") -> None: ...
    def complete(self, prompt, **_): ...

# Domain 層は LLMClient Protocol のみを知る。実装は service/handler で注入。
# Compiler は Callable[[str], str] も受け付ける（簡易 DI）。
```

### File Lock / Time DI

```python
# lib/service/file_lock.py
from typing import Protocol, ContextManager

class FileLock(Protocol):
    def acquire(self, path: str) -> ContextManager[None]: ...

# 本番: filelock ライブラリを wrap（fcntl 直呼びは避け、cross-platform 耐性を確保）
# テスト: FakeFileLock（衝突シナリオを注入可能）

# lib/service/clock.py
class Clock(Protocol):
    def now(self) -> str: ...    # ISO8601 UTC

class SystemClock: ...
class FixedClock:  # テスト用、固定時刻を返す
    def __init__(self, ts: str): ...
```

### Files to Change

```
skills/wiki/scripts/
├── lib/
│   ├── domain/                    # 【Phase 0 開始】pure functions, frozen dataclass
│   │   ├── __init__.py
│   │   ├── types.py               # 【Phase 0】ArticleType / Status / SchemaVersion / Source / GeneratedBy / Segment / Article / KnowledgeTime / Relations / Claim / Result (Ok|Err)
│   │   ├── normalizer/            # 【Phase 1】conversation 正規化
│   │   │   ├── __init__.py
│   │   │   └── conversation.py
│   │   ├── compiler/              # 【Phase 3】記事生成 pure
│   │   │   ├── __init__.py
│   │   │   ├── claim_extractor.py    # 【Phase 3】segments[] → claims[] 抽出 (LLM DI)
│   │   │   ├── intent_classifier.py  # 【Phase 3, rule-based】article candidate 分類
│   │   │   ├── renderer.py           # 【Phase 3】型別レンダリング
│   │   │   └── prompts.py            # 【Phase 3】型別プロンプトテンプレート
│   │   └── validator/             # 【Phase 4】rule-based 矛盾検出
│   │       ├── __init__.py
│   │       ├── conflict.py        # claim subject×attribute×期間 matcher
│   │       ├── lifecycle.py       # 時効チェック
│   │       └── integrity.py       # 型整合性チェック
│   ├── service/                   # I/O adapters
│   │   ├── __init__.py
│   │   ├── path_validator.py      # 【Phase 0】単一 canonical — sanitize_id / resolve_safe_path
│   │   ├── file_lock.py           # 【Phase 0】FileLock Protocol + filelock 実装
│   │   ├── clock.py               # 【Phase 0】Clock Protocol + SystemClock / FixedClock
│   │   ├── schema.py              # 【Phase 0】schema_version validation + YAML frontmatter I/O (python-frontmatter)
│   │   ├── wiki_repo.py           # 【Phase 0】記事 CRUD + atomic allocation（FileLock 注入）
│   │   ├── migrations/            # 【Phase 0】v0→v1 以降の migration 基盤
│   │   │   ├── __init__.py
│   │   │   ├── registry.py        # @register_migration("v0", "v1") デコレータ
│   │   │   ├── base.py            # Migration Protocol (up / down / validate → Result)
│   │   │   ├── backup.py          # 簡素な cp -r ベース（.wiki/backups/{timestamp}/concepts/）
│   │   │   └── v0_to_v1.py        # 既存 12 記事用、最初の使用例
│   │   ├── token_resolver.py      # 【Phase 2】Token 多層フォールバック解決（path_validator 経由）
│   │   ├── querylog.py            # 【Phase 5 新規】 querylog.jsonl の read / append / aggregate（has_provenance 拡張含む）
│   │   ├── fetchers/              # 【Phase 1+】
│   │   │   ├── __init__.py
│   │   │   ├── registry.py        # 【Phase 1】@register_fetcher デコレータ
│   │   │   ├── file.py            # 【Phase 1】既存 file ingest の新構造実装
│   │   │   ├── url.py             # 【Phase 1】既存 URL ingest の新構造実装（SSRF guard 付き）
│   │   │   └── slack.py           # 【Phase 2】Slack User Token 実装（多層フォールバック）
│   │   ├── llm_client.py          # 【Phase 1】LLMClient Protocol + FixedResponseLLM + MappedResponseLLM
│   │   ├── provenance.py          # 【Phase 1】GeneratedBy 生成（Clock 注入）+ Source ビルダー
│   │   └── orchestrator.py        # 【Phase 1】pipeline 実行制御（retry / partial failure / Result 統合）
│   ├── inventory.py               # 既存（pure、そのまま活用）
│   └── ...
├── ingest.py                      # 【Phase 1 handler】orchestrator を呼ぶ薄い CLI
├── compile.py                     # 【Phase 1 handler】同上（Phase 2 は Export JSON のみ、API は Phase 2.5）
├── validate.py                    # 【Phase 1 handler】同上
├── migrate.py                     # 【Phase 0 handler】migration 専用 CLI
├── review.py                      # 【Phase 4 handler】CLI ベース裁定 UI
├── stats.py                       # 【Phase 5 新規 handler】wiki stats CLI（querylog 集計 + 評価メトリクス表示）
├── graph_gen.py                   # 【既存→段階移行】handler 薄化 + v0+v1 混在 pass-through
├── lint-wiki.py                   # 【既存→段階移行】handler 薄化 + v0+v1 混在 pass-through
├── trust_score.py                 # 【既存→段階移行】handler 薄化 + v0+v1 混在 pass-through
├── gap_detect.py                  # 【既存→段階移行】handler 薄化 + v0+v1 混在 pass-through
└── tests/
    ├── domain/                    # 純粋関数テスト（モック不要）
    ├── service/                   # fake filesystem / fake LLM / fake lock / fake clock
    └── integration/               # pipeline 結合テスト

requirements.txt                   # 【Phase 0 新規】 依存ライブラリバージョン固定
                                   #   python-frontmatter>=1.1,<2
                                   #   pyyaml>=6.0,<7
                                   #   filelock>=3.12,<4            # cross-platform FileLock
                                   #   slack_sdk>=3.27,<4           # Phase 2 で使用
                                   #   tenacity>=8.2,<9             # rate limiter fallback（任意）
                                   #   pytest>=8,<10                # 既存 .venv の pytest 9 系に整合
                                   #   typing-extensions>=4.10,<6   # 既存 .venv の 4.15 系に整合
                                   # 実行環境: .venv (uv 管理) に uv pip install -r で集約
                                   # 将来 issue: intervaltree (現 MVP は不要)

.wiki/schema/
├── page-template.json             # 【Phase 0 更新】基礎工事フィールド追加
├── page-template-v1.json          # 【Phase 0 新規】schema_version: 1 の canonical（extensions.review.audit 含む）
├── querylog-schema.json           # 【Phase 5 更新】has_provenance: bool を追加（既存スキーマに非破壊追加）
├── article-types/
│   ├── decision.json              # 【Phase 0 新規】型固有スキーマ
│   ├── runbook.json
│   ├── reference.json
│   └── concept.json
└── migrations/
    └── v0-to-v1.md                # 【Phase 0 新規】 field mapping 仕様書（実装は lib/service/migrations/v0_to_v1.py）
                                   # v0.type:"wiki" → v1.article_type:<推定 or fallback "concept">
                                   # v0.category → v1.tags[] or extensions
                                   # v0.source_refs[] → v1.sources[]（path のみ、他フィールドは空で初期化）
                                   # 等、全 v0 field の移行先を明示

.wiki/config/                      # 【Phase 0 新規】secrets 等のローカル設定
└── secrets.env                    # 【Phase 2 使用、Phase 0 で .gitignore 追加】 Token 等

.wiki/concepts/                    # 既存 12 記事 → v1 migration（migrate.py --apply 経由）
.wiki/raw/                         # 【Phase 0 新規】ソース保存先ルート
└── slack/                         # 【Phase 2】Slack ソース保存先
.wiki/backups/                     # 【Phase 0 新規】migrate backup
.wiki/.gitignore                   # 【Phase 0 更新】.wiki/raw/、.wiki/config/secrets.env、.wiki/backups/ を追加

skills/wiki/SKILL.md               # 【更新】ingest 入力仕様拡張、記事型説明、手動編集禁止ルール明記
skills/wiki/references/
├── architecture.md                # 【更新】5 層型契約 + 3 層実装モジュールの対応図
├── frontmatter-schemas.md         # 【更新】v1 schema 仕様
├── prompts.md                     # 【更新】型別 compile プロンプト
├── slack-fetcher-guide.md         # 【新規】Slack App 設定手順
├── token-resolution.md            # 【新規】Token 多層フォールバック仕様 + エラー出力例
└── cli-conventions.md             # 【新規】CLI 共通フラグ (--quiet / --json / --no-color) と exit code (0/1/2/130)

CLAUDE.md                          # 【更新】新 schema / 4 記事型 / 記事編集は LLM 経由のみ / 記事数 12 本（Articles 欄実態と同期）
```

### Phase 分け

#### Phase 0: 基礎工事 + Migration 基盤 + Domain 最小セット（最重要・ケチ厳禁）

**目的**: 後から migration 不要な保存形式を確定し、既存 12 記事を v1 へ追従させる。Phase 1 以降が依存する最小の domain types と service utilities もここで整備する。

- **依存管理**: `requirements.txt` を新規作成（python-frontmatter / pyyaml / filelock / intervaltree / tenacity / slack_sdk / pytest 等をバージョン固定）
- **新フロントマター v1 schema 確定**:
  - `sources[]` 内に `source_version` / `content_hash` / `fetched_at`
  - top-level に `claim_refs[]`（初期 empty）
  - `generated_by: { tool, version, generated_at }`
  - `valid_to: null` は "still current, no known end" と仕様書に明記
  - `status` は 4 値 `current | historical | disputed | unverified` に固定
- **4 記事型固有フィールド定義** (`.wiki/schema/article-types/*.json`)
- **本文骨格の確定**
- **Domain 最小セット（`lib/domain/types.py`）**:
  - frozen dataclass で ArticleType / Status / SchemaVersion / Source / GeneratedBy / Segment / Article / KnowledgeTime / Relations / Claim / Result (Ok/Err)
  - Phase 1 以降の service/migrations が依存するため Phase 0 で着地
- **Service utilities（Phase 0 必須）**:
  - `lib/service/path_validator.py` — 単一 canonical sanitize
  - `lib/service/file_lock.py` — FileLock Protocol + filelock 実装 + FakeFileLock
  - `lib/service/clock.py` — Clock Protocol + SystemClock + FixedClock
  - `lib/service/schema.py` — schema_version validation + python-frontmatter 経由の YAML I/O
  - `lib/service/wiki_repo.py` — 記事 CRUD + **atomic allocation**（FileLock DI）
- **Migration 基盤**:
  - `lib/service/migrations/{registry, base, backup, v0_to_v1}.py`
  - `base.py` の Migration Protocol は `up / down / validate` を全て `Result[Article, MigrationError]` で返却
  - **backup 戦略は簡素**: `cp -r .wiki/concepts .wiki/backups/{timestamp}/concepts/`（JSONL checkpoint は 12 記事規模では過剰、Phase 5+ で必要になってから追加）
  - **Backup tamper detection**: `.wiki/backups/{timestamp}/.meta.json` に `{timestamp, cli_version, article_count, tree_sha256}` を記録。`--rollback` 実行時に `.meta.json` の `tree_sha256` と実際のバックアップ内容の hash を照合し、不一致なら警告（dogfooding 期は hard block しない、チーム共有フェーズで hard block に昇格）
  - dry-run デフォルト、`--apply` で実行、`--rollback <timestamp>` で復元、`--auto` で schema_version 自動検出
  - **checkpoint / timestamp の validation**: `^[0-9]{8}T[0-9]{6}Z$` 準拠（`.meta.json` の timestamp 形式）、path traversal 禁止、`path_validator.resolve_safe_path` で `.wiki/backups/` 配下に閉じ込め
  - **Ctrl+C (SIGINT) handling**: migrate.py は signal.signal(SIGINT, handler) を登録し、進行中の記事 1 件の書き込みが完了した時点で中断する（atomic per article）。中断後は **backup が残っているので `wiki migrate --rollback <timestamp>` で完全復元可能**、`--resume` 機能は 12 記事規模では不要（Phase 5+ で issue 化）
  - exit code: 0 (success) / 1 (user abort after SIGINT) / 2 (validation error) / 130 (SIGINT during critical section)
- **v0 → v1 schema field mapping document**（`.wiki/schema/migrations/v0-to-v1.md`）を先に書く:
  - 既存 `.wiki/schema/page-template.json`（v0）の全フィールドに対して v1 移行先を明示
  - v0 `type: "wiki"` は v1 では **migration 時に手動 or ヒューリスティック分類**（初期は fallback `article_type: "concept"` + `tags: ["legacy"]`、後工程で `review` コマンドで型変更可能）
  - v0 `created` / `updated` → v1 `captured_at` / `knowledge_time.valid_from`
  - v0 `source_refs[]` → v1 `sources[]`（`source_version=1`, `content_hash=sha256(ref ファイル内容)`, `fetched_at=migration 実行日時`）
- **既存 12 記事**の v0 → v1 migration 実装と実行 (`lib/service/migrations/v0_to_v1.py`)
- `schema_version` validation の導入
- **Mixed v0+v1 状態の取り扱い**（migration 中断時 or partial apply 時）:
  - `schema.py` は `schema_version` フィールドを読み取り、未指定 (v0) と `1` (v1) の両方を検証可能
  - 既存の `inventory.py` / `graph_gen.py` / `lint-wiki.py` / `trust_score.py` / `gap_detect.py` を minimal 変更で対応:
    - `schema_version` が無い記事 (v0) は既存ロジックで処理（従来動作）
    - `schema_version: 1` の記事 (v1) は新フィールド (sources[], claims[], etc.) を無視して pass-through（Phase 0 では新フィールド活用ロジックは入れない）
  - Phase 0 test に「partial migration 中に graph_gen / lint-wiki / trust_score / gap_detect を実行して crash しないこと」を追加
  - migrate.py はコマンド実行時に `wiki concepts status` 相当で現状（v0 件数 / v1 件数 / 混在 count）を表示
- **`.wiki/.gitignore` に `.wiki/raw/`・`.wiki/config/secrets.env`・`.wiki/config/`・`.wiki/backups/` を追加**
- **atomic allocation 実装**: `wiki_repo.py` 内で FileLock 注入、article_id 採番時に衝突回避
- **fcntl → filelock ライブラリ採用**: WSL2 edge case 回避、cross-platform 耐性確保（直接 fcntl 呼び出しはしない）
- **file permission 強制**: `secrets.env` 作成時に `os.chmod(path, 0o600)`
- **CLAUDE.md 更新**: Articles 欄の 12 件を実態と同期、手動編集禁止ルール、新 schema の要約追記、記事数の定義（`.wiki/concepts/` の `.md` ファイル数）を明文化

#### Phase 1: 抽象レイヤ骨格

**前提**: 既存の `ingest / compile` は **独立スクリプトとしては存在せず、`skills/wiki/SKILL.md` に手順定義のみ** が書かれている状態。Phase 1 では「既存を新構造へ接続」ではなく、**`ingest.py` / `compile.py` / `validate.py` を新規作成し、SKILL.md の手順を新構造の薄い CLI で置き換える** 作業となる。

- `lib/domain/normalizer/conversation.py`: 会話ログ正規化ルール（Phase 2 の Slack fetcher が呼ぶ）
- `lib/service/fetchers/registry.py`: registry パターン実装（`@register_fetcher("kind")`）
- `lib/service/fetchers/file.py` / `lib/service/fetchers/url.py`: 既存の file / URL ingest 手順を新構造に実装
  - url.py には **SSRF guard**（127.x / 10.x / 169.254.x / localhost / metadata.google.internal ブロック、http/https のみ許可、response size cap 50MB）
- `lib/service/llm_client.py`: `LLMClient` Protocol + `FixedResponseLLM` + `MappedResponseLLM`
- `lib/service/provenance.py`: `GeneratedBy` 生成（`Clock` 注入）+ `Source` ビルダー
- `lib/service/orchestrator.py`: pipeline 実行制御の骨格（retry bounded: max_retries=3 / max_backoff=300s / Result 統合）
- `ingest.py` / `compile.py` / `validate.py` を新規作成（`orchestrator` を呼ぶ薄い handler）
- 既存スクリプト（`graph_gen / lint-wiki / trust_score / gap_detect`）は触らず、並行して `lib/` を育てる

#### Phase 2: Slack Fetcher（MVP 実装）

**scope**: Q12-2 A 案を採用。Phase 2 は **Export JSON のみ提供**、プログラマティック API は Phase 2.5 送り（issue 登録済み）。

- Slack App 作成手順ドキュメント（`references/slack-fetcher-guide.md`）
- **Token 多層フォールバック解決**（`lib/service/token_resolver.py`）— 優先順位：
  1. `--token-file <path>` CLI 明示指定（最優先、シェル環境非依存、`path_validator` で sanitize）
  2. `$SLACK_USER_TOKEN` 環境変数
  3. `.wiki/config/secrets.env`（プロジェクトローカル、`.gitignore` 対象、作成時 0600）
  4. `~/.config/wiki/secrets.env`（ユーザーグローバル）
  - `TokenResolver` Protocol を定義し、本番 `DefaultTokenResolver` / テスト `FakeTokenResolver` を用意（全て DI）
  - `secrets.env` は `KEY=VALUE` 形式、シェル実行不要の dotenv loader で読む（インタラクティブ/非インタラクティブの shell profile 罠を完全回避）
- **Token 未発見時のエラー出力例**（`references/token-resolution.md` に正規仕様として記載）:
  ```
  Error: SLACK_USER_TOKEN not found.

  Searched (priority order):
    1. --token-file: not specified
    2. $SLACK_USER_TOKEN: not set
    3. .wiki/config/secrets.env: file not found
    4. ~/.config/wiki/secrets.env: file not found

  Fix:
    • Create .wiki/config/secrets.env:
        printf 'SLACK_USER_TOKEN=xoxp-...\n' > .wiki/config/secrets.env
        chmod 600 .wiki/config/secrets.env
    • Or export the environment variable:
        export SLACK_USER_TOKEN=xoxp-...
    • Or pass via CLI:
        wiki slack ingest --token-file /path/to/secrets.env

  Documentation: skills/wiki/references/token-resolution.md
  ```
  - エラー出力にパス名は含めるが、**Token 値や前後の文字列は一切含めない**
- **`wiki slack check-auth` 診断コマンド**:
  - 正常時出力例:
    ```
    ✓ Token source: .wiki/config/secrets.env (SLACK_USER_TOKEN)
    ✓ Token permission: 0600 (secure)
    ✓ auth.test: team=@my-workspace, user=@mizumi, team_id=T0123456
    ✓ Required scopes:
      ✓ channels:history
      ✓ groups:history
      ✓ users:read
    ```
  - スコープ不足時出力例:
    ```
    ✓ Token source: .wiki/config/secrets.env (SLACK_USER_TOKEN)
    ✓ auth.test: team=@my-workspace, user=@mizumi
    ✗ Missing scope: users:read (required for speaker resolution)

    Fix: Re-install the Slack App after adding users:read scope at
         https://api.slack.com/apps → Your App → OAuth & Permissions
    ```
  - `auth.test` は 1 コール、scope 判定はレスポンス内 `scope` フィールドの string match（追加 API コールなし）
- **keyring 化は Phase 2.5+ 送り**（issue 登録済み）
- `service/fetchers/slack.py`: `fetch(source_spec) -> Result[list[RawSource], FetchError]` の registry 実装
- Slack permalink URL パーサ、`slack://` スキーム対応
- **シンプルな rate limiter**（slack_sdk 内蔵を優先活用、追加の tenacity ラッパはオプション、Retry-After ヘッダ尊重）
- `domain/normalizer/conversation.py`: Slack thread → NormalizedSource (tuple[Segment, ...])
  - **Normalizer edge case 仕様**:
    - 削除メッセージ: `Segment(deleted=True, content="[deleted]")`
    - 編集メッセージ: `Segment(edited_at=...)`, 最終版のみ content、履歴は `extensions`
    - Bot 発言: `speaker_type="bot"` 付与、除外はしない（運用判断で後段 filter 可能）
    - 空スレッド（本文 1 件のみ返信なし）: warning を出すが記事化は継続、`len(segments)=1`
    - Orphaned reply（親 message 欠損）: `Segment(orphan=True)`、continue、provenance に anomaly 記録
    - 大規模スレッド (10k messages / >10MB JSON): streaming 解析、`raw_payload` は NormalizedSource 生成後に明示的に解放 (`del payload` or context manager)
- 増分更新（`source_id` 不変、`source_version` 増分、`content_hash` 比較、`fetched_at` 記録）
- **`.wiki/raw/slack/` 保存戦略**: single-file immutable — 再取得時は **新しいファイルを `{id}-v{n}.md` として追加**（前版は消さない）、`sources[].ref` は最新版のみを指す、`source_version` と `content_hash` で来歴追跡
- **compile 出力は JSON export のみ**（Phase 2 範囲）
- **Progress feedback**: `wiki slack ingest --source <spec>` は default で `Fetched M/N threads` を stderr に出力、`--quiet` で抑制、`--json` で NDJSON 進捗

#### Phase 3: Compiler 強化

**scope**: `intent_classifier` は **当初 rule-based** で実装（Q12-2 A 案、issue 登録済み）。**claims[] の生成は Phase 3 で行う**（Phase 4 Validator が依存するため必須）。

- `domain/compiler/claim_extractor.py`: **LLM DI (`LLMClient`) 経由で segments[] から claims[] を抽出**
  - claims[] の粒度: `(subject, attribute, period, predicate, source_refs)` の tuple
  - 抽出失敗時は `Result[list[Claim], ExtractionError]` で返却、空 claims でも記事生成は続行
  - 「claims を保存できない設計は避けろ」原則（Phase 0 で schema 確定済み）を満たす
- `domain/compiler/intent_classifier.py`: **rule-based 実装** — 1 ソース → article candidates[] 分類
  - キーワード/パターンマッチング + 簡易ヒューリスティクス
  - 誤分類率の計測ポイントを埋め込み（運用評価用）
  - LLM 化判断基準は `docs/issues/20260408202827_compiler-intent-classifier-llm.md` を参照
- `domain/compiler/renderer.py`: 4 記事型別レンダリング
- `domain/compiler/prompts.py`: 型別プロンプトテンプレート
- 1 ソース → 複数記事分割（Slack 障害スレッド → incident + runbook + decision）
- 全記事が同じ source provenance を共有する保証
- `claim_refs[]` を Article に書き込み、他記事の claim を参照できるようにする（Phase 4 差分 merge の足場）
- **raw_payload メモリ解放**: Compiler は `Fetcher → Normalizer → raw_payload 解放 → segments のみ参照` を厳守

#### Phase 4: Validator

**scope**: 第 2 段（LLM ベース矛盾検出）は **Phase 5+ 送り**（Q12-2 A 案、issue 登録済み）。Phase 4 は rule-based 3 種 + CLI 裁定 UI に集中する。

- `domain/validator/conflict.py`: claim の subject × attribute × 期間マッチ
  - **MVP 実装**: `Dict[subject][attribute] = list[(period_start, period_end, claim_id)]` を採用、新規 claim 追加時に線形走査（500-2000 claim 規模では < 10ms）
  - 将来 **IntervalTree への移行**: claim 数が 5,000 を超えた / プロファイリングで conflict 検出 > 50ms のいずれかで移行判断（issue 化候補）
  - 起動時は全件再スキャンで in-memory index を再構築（破損・不整合時も自動復旧）
- `domain/validator/lifecycle.py`: 5 年超 `要検証` タグ付与（運用手順・組織名など限定）
- `domain/validator/integrity.py`: 型別必須フィールドチェック
- 矛盾検出時は `status=disputed` に遷移（`conflicted` 語彙は使わない、schema 単一モデル）
- 情報不足時は `status=unverified` に遷移（`draft` 語彙は使わない）
- **第 2 段 LLM interface のみ用意**（実装は Phase 5+、issue 参照）
- lint との統合（既存 lint-wiki に Validator 警告を連携）
- **CLI ベースの簡易裁定 UI**（C5 降格対応）: `review.py`
  - `wiki review list [--status <status>] [--format table|json] [--limit N] [--offset N]` — 警報一覧
    - デフォルト table 出力: `article_id | article_type | status | title (truncated 40 chars) | claim_count`
    - `--format json` は各行を JSON オブジェクトで出力（NDJSON）
  - `wiki review show <article_id> [--audit]` — 詳細と全 claim 表示
    - 標準出力: title / status / article_type / claims のリスト
    - `--audit` 指定時は `extensions.review.audit[]` の全 entry を時系列表示
  - `wiki review resolve <article_id> --status {current|historical|disputed|unverified} [--superseded-by <id>] [--reason <text>] [--yes]` — 裁定結果反映
    - `--yes` 無指定時は y/N 確認プロンプト
    - 実行時に `ReviewAuditEntry(resolver=os.getenv("USER"), resolved_at=Clock.now(), status_before=old, status_after=new, reason=text, superseded_by_id=...)` を生成
    - **冪等性保証**: 現在値と同一な resolve（status / superseded_by_id / reason が前回と完全一致）は no-op として扱い、audit entry も追記しない（ユーザーには「no change」と報告）。差分がある場合のみ audit entry を 1 件追加する
    - `extensions["review"]["audit"]` に append（既存 tuple を copy して新規 tuple を作成、frozen 保持）
    - `wiki_repo.update_article()` 経由で .md ファイルに書き戻し、`generated_by.generated_at` も更新

#### Phase 5: ドッグフーディング

- **評価メトリクスの operationalization（実装仕様）**:
  - 一次情報到達率: `provenance_coverage = 回答内に wikilink/引用付きの割合` を querylog から抽出
    - querylog スキーマ拡張: 既存 `.wiki/schema/querylog-schema.json` に `has_provenance: bool` を追加（non-breaking、optional、default=false）
    - 集計: `lib/service/querylog.py` の `aggregate_provenance_coverage(since, period) -> float` 関数で算出
  - 検索時間短縮: 手動記録（CLI `wiki stats log-search-time --wiki <seconds> --slack <seconds>` で querylog に特殊 entry 追記）
  - 重複質問削減: querylog の `gap_topics` + 簡易 tf-idf クラスタリング（既存 `gap_detect` 拡張）
- `skills/wiki/scripts/stats.py` — `wiki stats` CLI handler
  - `wiki stats [--format table|json] [--since <date>] [--period week|month]` — メトリクス表示
  - `wiki stats report [--output <path>]` — Markdown レポート生成
- `lib/service/querylog.py` — querylog JSONL の read / append / aggregate
  - **並行書き込み対策**: `FileLock` DI（Phase 0 の `lib/service/file_lock.py` を流用）を適用、1 行 append は atomic（lock → write → fsync → unlock）
  - JSONL 破損防止: 各 append は `line + "\n"` を一括 write、`os.fsync(fd)` で disk flush、部分書き込み時は next append で末尾改行を確認して修復
  - stats.py から呼ぶ薄い構造化 I/O 層、business logic は持たない
- レポート出力先: `.wiki/outputs/reports/{YYYYMMDD}-stats.md`
  - フォーマット: Summary table → Coverage trend (週次) → Top gap topics → Search time delta
- 評価用質問セット 20〜30 個の整備
- 週 1 回の「裁定セッション」運用（`wiki review list` → 人間判断 → `wiki review resolve`）
- Phase 2+ ロードマップへの統合（Compiler API / LLM Validator 第 2 段 / intent_classifier LLM 化 / keyring / IntervalTree 移行 の各 issue の着手判定）

### Key Points

- **基礎工事と MVP は別物**: 再解釈不能な保存形式は後から直せないので最初から固める（schema 最大主義）
- **Implementation は最小主義**: 初期は rule-based / Export JSON / 環境変数ベース + linear scan で十分。LLM / API / keyring / IntervalTree は issue で追跡し、必要になってから着手
- **Testability Above All**: 全 domain 関数は pure、外部依存は全て DI（filelock / clock / LLM / filesystem / env）
- **会話構造保持**: Segment の speaker / speaker_type / ts / reactions / reply_to / edited_at / deleted / orphan を normalize 段階で残す
- **claims vs claim_refs の責務分離**: `claims[]` は当該記事が主張する事実、`claim_refs[]` は他記事の claim への参照。両方 Phase 0 で schema 確定、Phase 3 で書き込みロジック実装
- **status 単一モデル**: 永続化される status 値は schema の 4 値のみ。内部中間状態や CLI 語彙を分岐させない
- **差分 merge の単位は claim / article candidate**: source 単位だと過剰再生成、full recompile は大規模で破綻
- **Validator 2 段構え**: rule-based 第 1 段で大半を処理、LLM 第 2 段は Phase 5+ で疑わしいやつだけ
- **registry + Protocol**: Fetcher を継承階層にせず、`fetch(source_spec) -> Result[list[RawSource], FetchError]` を満たす callable として登録
- **Migration 基盤は Phase 0 必須**: 既存 12 記事を v1 に追従させる。dry-run デフォルト、cp -r backup + rollback で安全性を担保。複雑な checkpoint JSONL は Phase 5+ 必要時まで延期
- **エラー境界**: source 消失 / provenance 欠落 / article_id 衝突は停止、LLM 一時失敗 / ネットワークエラーは継続（Result 型で分岐）
- **既存スクリプト再編は並行**: 新機能は新構造で書き、既存は動くまま徐々に lib/ を参照させる
- **Token 多層フォールバック**: shell 環境のインタラクティブ/非インタラクティブ問題を構造的に回避する設計
- **raw/ パスは `.wiki/raw/` に統一**: gitignore パスと実際の保存先が一致、誤コミット防止

## ✅ Tests

### Phase 0（基礎工事 + Migration 基盤 + Domain 最小セット）
- [ ] `requirements.txt` 経由で全依存が `pip install` できる
- [ ] Domain types の frozen 保証（代入で FrozenInstanceError）
- [ ] `Result[T, E]` の Ok / Err 判定網羅パターン
- [ ] 新 v1 schema を満たすフロントマターのバリデーション
- [ ] 型別必須フィールドチェック（decision / runbook / reference / concept）
- [ ] article_id の衝突検出とサフィックス採番
- [ ] **atomic allocation**: 並行採番時に衝突しない（`FakeFileLock` で並行シナリオを注入）
- [ ] **filelock の WSL2 互換性テスト**: 2 プロセス同時ロック取得で必ず 1 つだけ成功
- [ ] v0 → v1 migration の純粋関数テスト（既存 12 記事を題材）
- [ ] v0→v1 schema field mapping 網羅テスト（v0 の全フィールドが v1 のいずれかに落ちる）
- [ ] migrate.py dry-run / apply / rollback の結合テスト
- [ ] **cp -r backup からの rollback テスト**（.wiki/backups/{timestamp}/ → .wiki/concepts/）
- [ ] schema_version 不一致の検出
- [ ] `claim_refs[]` / `claims[]` / `generated_by` / `source_version` / `content_hash` / `fetched_at` の存在と空許容の確認
- [ ] `valid_to: null` が "still current" として扱われるテスト
- [ ] `status` が 4 値以外を拒否するテスト
- [ ] **Mixed v0+v1 state**: schema.py が両版を validate 可能、`inventory.py` / `graph_gen.py` / `lint-wiki.py` / `trust_score.py` / `gap_detect.py` が混在状態で crash しないこと（各スクリプトごとに pass-through テスト）
- [ ] **python-frontmatter nested structure round-trip**: `knowledge_time` / `sources[]` / `claims[]` / `relations` / `extensions.review.audit[]` / `generated_by` の全 nested フィールドを load → dump → load で等価性確認
- [ ] **Backup tamper detection**: `.meta.json` の tree_sha256 mismatch で rollback 時に警告出力（hard block しない、dogfooding 期は継続可）
- [ ] **SIGINT handling**: migrate --apply 中に SIGINT → 進行中の 1 記事の atomic 書き込み完了後に中断 → exit code 130 / 部分状態が backup から完全復元可能
- [ ] **path_validator**: `..` / 絶対パス / 空文字 / NUL / 1024 文字超 / Unicode を安全に拒否 or normalize
- [ ] **secrets.env ファイルパーミッション**: 作成時 0600、読み取り時 permission 警告
- [ ] **checkpoint/backup ID validation**: `^[a-z0-9_-]{1,128}$` 準拠、path traversal 拒否
- [ ] CLAUDE.md Articles 欄と `.wiki/concepts/*.md` 実態数の同期確認スクリプト

### Phase 1（抽象レイヤ骨格）
- [ ] Source / Segment / Article / Provenance 型の不変性（frozen）
- [ ] Provenance の全層伝搬（pipeline 統合テスト）
- [ ] registry パターンの登録・解決
- [ ] `FixedResponseLLM` / `MappedResponseLLM` の基本動作
- [ ] LLMClient Protocol の DI テスト（Domain 層は Protocol のみに依存）
- [ ] orchestrator の partial failure 制御（`Result[T, E]` 統合、max_retries=3 / max_backoff=300s 上限）
- [ ] url.py SSRF guard（localhost / RFC1918 / metadata.google.internal / data: / file: を拒否、response 50MB cap）
- [ ] handler → orchestrator → service → domain の依存方向テスト（逆依存禁止）

### Phase 2（Slack Fetcher）
- [ ] Slack permalink URL パーサ（正常系・異常系）
- [ ] `slack://` スキームパーサ
- [ ] Slack API レスポンスのモックからの RawSource 生成
- [ ] **Token 多層フォールバック解決**: 4 段階の優先順位テスト（`FakeTokenResolver` / fake filesystem + 環境変数モック）
- [ ] Token 未発見時のエラーメッセージに全探索箇所が列挙されること、Token 値が漏れないことの確認
- [ ] `wiki slack check-auth` 診断コマンド: 正常系 / Token 不在 / 無効 / スコープ不足 / auth.test 1 コールのみ確認
- [ ] NormalizedSource への segment 分解（1 message = 1 segment）
- [ ] **Normalizer edge case**: 削除 msg / 編集 msg / bot 発言 / 空スレッド / orphaned reply / **大規模スレッド (5MB mock)** の各シナリオ
- [ ] **raw_payload メモリ解放**: 5MB mock スレッドで NormalizedSource 生成後に payload が解放される (weakref or gc 確認)
- [ ] content_hash ベースの差分検出
- [ ] source_version 増分ロジック
- [ ] `fetched_at` の正しい記録
- [ ] `.wiki/raw/slack/` への保存パスが `.gitignore` に含まれることの確認
- [ ] rate limiter の基本動作（Retry-After ヘッダ尊重、指数バックオフ、slack_sdk 内蔵優先）
- [ ] Progress output: default / --quiet / --json の 3 モード

### Phase 3（Compiler）
- [ ] intent_classifier **rule-based** の型判定（固定パターン入力）
- [ ] 誤分類率計測ポイントの出力確認
- [ ] **claim_extractor**: LLM DI 経由で segments[] → claims[] 生成、失敗時は空 claims で継続
- [ ] **Claim 型 shape 保証**: `claim_id` の deterministic 採番、subject/attribute/period の必須性、source_refs の `sources[].id` への参照整合性
- [ ] **claim_id 決定性**: 同一入力 (同じ segments[]) を 2 回 compile しても同じ claim_id が生成されること（ID 漂流防止）
- [ ] **review resolve 冪等性**: 現在値と同一な resolve が no-op として扱われ audit が重複しないこと
- [ ] **allow_external_api=False 時の Compiler 挙動**: LLMClient 呼び出し直前で `Err[PolicyError]` 返却、Compiler は空 article を生成せずに中断、handler はエラーメッセージで原因と切替方法を出力
- [ ] **raw_payload メモリ解放**: 5MB mock スレッドで NormalizedSource 生成後に payload が解放される（同 Phase 2 重複、Compiler 側も確認）
- [ ] 4 記事型それぞれのレンダリング出力
- [ ] 1 ソース → 複数記事分割（Slack 障害スレッドを題材）
- [ ] 分割後の provenance 共有
- [ ] claim_refs[] の書き込みロジック

### Phase 4（Validator）
- [ ] 同一 subject × attribute × 期間重複の検出（linear scan MVP）
- [ ] 期間非重複時は衝突と判定しない
- [ ] 起動時の in-memory index 再構築（12 記事 + 仮 claims で挙動確認）
- [ ] 時効チェック（5 年超の運用手順系記事にタグ付与）
- [ ] 矛盾検出時の status 遷移 (`disputed`)
- [ ] 情報不足時の status 遷移 (`unverified`)
- [ ] status が schema の 4 値以外に遷移しないことの検証
- [ ] lint との統合テスト
- [ ] `review.py` CLI: list / show / resolve の各コマンド動作確認、`--yes` なしの確認プロンプト
- [ ] **review resolve 後の audit trail**: `extensions["review"]["audit"]` に ReviewAuditEntry が正しく append され、既存 entry が破壊されないこと（frozen + copy-on-append）
- [ ] **audit trail の disk persistence**: resolve 実行後 `.md` ファイルを re-read して audit[] が persist していること

### Phase 5（ドッグフーディング）
- [ ] 評価メトリクス計測スクリプトの単体動作
- [ ] querylog からの重複質問検出（既存 gap_detect の拡張）
- [ ] **querylog スキーマ拡張**: `has_provenance: bool` フィールドが既存 entry と non-breaking で共存することを確認（optional / default false、レガシー entry は has_provenance=false として集計）
- [ ] **querylog 並行書き込み**: 2 プロセス同時 append で JSONL が破損しないこと（FileLock + fsync の組み合わせ）
- [ ] provenance 到達率の集計 (`lib/service/querylog.py` の `aggregate_provenance_coverage` 関数)
- [ ] `wiki stats` CLI 出力確認: `--format table` / `--format json` / `wiki stats report --output <path>`
- [ ] `.wiki/outputs/reports/{YYYYMMDD}-stats.md` の構造（Summary table / Coverage trend / Top gap topics / Search time delta）が生成されること

## 🔒 Security

- [ ] **Slack Token の安全保管**: 多層フォールバック解決（`lib/service/token_resolver.py`）。`.wiki/config/secrets.env` は `.gitignore` で保護、作成時 `chmod 0600`、読み取り時も permission 検査して警告。keyring 化は Phase 2.5+
- [ ] **LLM Provider への業務情報送信の明文化 + opt-out ゲート**: `references/prompts.md` と `CLAUDE.md` に「業務情報を Compiler / Validator 経由で外部 LLM に送信する設計」を明記。`lib/service/llm_client.py` に `allow_external_api: bool = True` のゲートを設け、`False` 時は Compiler が `Err[PolicyError]` で停止。**CLI 露出**: `wiki compile [--allow-external-api | --no-allow-external-api]` および `.wiki/config/settings.json` の `llm.allow_external_api` で上書き可能。**Compiler 起動時の警告表示**: `allow_external_api=True` の場合、stderr に `⚠️  Business data will be sent to external LLM. Use --no-allow-external-api to opt out.` を 1 回表示（`--quiet` で抑制）。チーム共有フェーズ前にデフォルト値・オプトイン UX を再検討
- [ ] **`.wiki/raw/` の git 隔離**: Phase 0 で `.wiki/.gitignore` に追加、誤コミット防止。`.wiki/raw/` 全体 + `.wiki/config/` + `.wiki/backups/` を対象
- [ ] **Slack スコープ最小化**: `channels:history / groups:history / users:read` に限定、`im:history / mpim:history` は Phase 対象外
- [ ] **単一 canonical path validator** (`lib/service/path_validator.py`) を wiki_repo / fetchers / migrations / token_resolver で再利用。`..` / 絶対パス / NUL / symlink escape を統一的に拒否
- [ ] **LLM へ送信するコンテンツの境界明示**: fetch した `raw_payload` を Compiler 以外に流さない。Compiler 呼び出し後に明示的に解放
- [ ] **article_id 衝突時の安全策**: `filelock` 経由の atomic allocation でレース条件を排除、サフィックス採番で既存記事を上書きしない
- [ ] **schema_version 不一致時の保護**: 未知 version の記事は read-only 扱いで crash しない
- [ ] **手動編集禁止の徹底**: 記事編集は `wiki-compile / migrate / review` コマンド経由に統一。CLAUDE.md / SKILL.md に明記。`validate.py` に `--strict` フラグで `generated_by.generated_at` の recency をチェック（警告のみ、hard block しない）
- [ ] **Migration 実行時の安全策**: dry-run デフォルト、cp -r backup、timestamp validation、rollback 経路を全て提供。backup timestamp は `path_validator.resolve_safe_path` で `.wiki/backups/` 配下に閉じ込め
- [ ] **Backup tamper detection**: `.wiki/backups/{timestamp}/.meta.json` に `{timestamp, cli_version, article_count, tree_sha256}` を記録、restore 時に SHA256 照合、mismatch で警告出力（dogfooding は警告のみ、チーム共有フェーズで hard block へ昇格）
- [ ] **SIGINT handling**: migrate.py は atomic per article 書き込みを保証、中断時は backup から完全復元可能
- [ ] **URL Fetcher SSRF guard**: `service/fetchers/url.py` に `validate_url()` を組み込み、localhost / RFC1918 / metadata エンドポイント / file:// / data: / ftp:// を拒否。response size cap 50MB
- [ ] **エラーメッセージ漏洩防止**: token_resolver / check-auth のエラー出力は path 名は OK だが、Token 値・前後文字列・環境変数値は一切含めない

## 📊 Progress

| Phase | Status |
|-------|--------|
| Phase 0: 基礎工事 + Migration 基盤 + Domain 最小セット | ⚪ |
| Phase 1: 抽象レイヤ骨格 | ⚪ |
| Phase 2: Slack Fetcher (Export JSON のみ) | ⚪ |
| Phase 3: Compiler 強化 (rule-based intent_classifier + claim 抽出) | ⚪ |
| Phase 4: Validator + CLI 裁定 UI | ⚪ |
| Phase 5: ドッグフーディング | ⚪ |

| Step | Status |
|------|--------|
| Tests | ⚪ |
| Implementation | ⚪ |
| Commit | ⚪ |

**Legend:** ⚪ Pending · 🟡 In Progress · 🟢 Done

### 後続 issue（scope cut 項目、着手判定は Phase 5 時）

- [ ] `docs/issues/20260408202825_compiler-api-phase-2-5.md` — Compiler API 実装
- [ ] `docs/issues/20260408202826_validator-llm-conflict-phase-5.md` — Validator 第 2 段 LLM ベース矛盾検出
- [ ] `docs/issues/20260408202827_compiler-intent-classifier-llm.md` — Compiler intent_classifier の LLM 化検討
- [ ] `docs/issues/20260408202828_token-keyring-phase-2-5.md` — Token 管理の keyring 化
- [ ] (追加候補) Validator conflict index を IntervalTree 化 — claim 数 5,000 超 or 計測値 >50ms で起動

---

**Next:** Phase 0 から着手 → 基礎工事 + Migration 基盤 + Domain 最小セット完了まで他 Phase に手を出さない → `claude-skills:commit` でコミット 🚀

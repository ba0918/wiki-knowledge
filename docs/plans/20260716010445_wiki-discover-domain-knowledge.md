# wiki-discover: コードベースからのドメイン知識自動抽出

**Cycle ID:** 20260716010445
**Type:** Feature
**Created:** 2026-07-16 01:04:45
**Status:** 🟢 Complete（Phase A-C 実装完了、Phase D dogfooding は実リポジトリ適用時に実施）
**Idea:** docs/ideas/20260716004727_wiki-discover-domain-knowledge.md
**Related Idea:** docs/ideas/20260703222307_cross-repo-wiki-gitlab-fetcher.md

## Overview

`wiki discover` サブコマンドを新設し、git リポジトリのソースコードから LLM がドメイン知識を自動抽出して `concepts/` に直接記事化する。discover は compile のコードソース対応モードとして位置付け、既存の compile インフラ（出典規約 `path@8hash`、Backlink Audit、wikilink_render、index 同期）を丸ごと再利用する。

### なぜ必要か

- 既存の `repo ingest` はファイルの中身を一切読まない静的スキャナ（パターンマッチでドキュメントファイルを発見するだけ）
- ソースコードに埋まっている DB スキーマ、API ルート、ビジネスルール、状態遷移、用語は ingest では取り込めない
- 特にテストコードは仕様の体現であり、テスト名からビジネスルールを逆引きできる最良のソース
- cross-repo-wiki-gitlab-fetcher（5リポジトリ横断 wiki）の実現手段として、discover が横断ドメイン知識の抽出を担う

### アーキテクチャ裁定（Codex レビュー由来）

discover の出力先について3択を検討した結果、**選択肢 A: compile のコードソース対応モード**を採用。

**却下した案と理由**:
- `raw/` に書く案: architecture.md の不変条件（「Source 層は immutable。LLM は Knowledge 層と Output 層のみ変更する」）に正面から違反。repo-inventory.md が raw/ に置けるのは「決定論的・LLM 解釈なし」だから。discover 出力は非決定的・ハルシネーションしうる LLM 生成物であり前例にならない
- `outputs/` 中間層案: raw 不変性は守れるが、「LLM が散文を書く → 自分の散文を再読して concepts 散文を書く」二重 LLM パスが残り lossy。中間層の管理コストも増加

**選択肢 A の利点**: raw 不変条件を破らない / 二重 LLM パスなし / 既存 compile インフラ（出典規約 `path@8hash`、Backlink Audit、wikilink_render、index 同期）を丸ごと再利用 / source_refs は不要（コード出典は本文内 `path@hash` で完結）

### Goals

- **Phase A**: ソース走査スクリプト `source_scan.py` — リポジトリのソースコードからドメイン知識の候補ファイルを分類・抽出する決定論的スキャナ
- **Phase B**: discover ワークフロー定義 + 縦切り検証 — LLM がソースコードを読解し記事を生成する SKILL.md フロー定義。1記事タイプで目視検証を先行
- **Phase C**: SKILL.md + CLAUDE.md 統合 — `wiki discover` サブコマンドのルーティング追加と既存パイプラインとの接続
- **Phase D**: dogfooding — 6カテゴリを踏む公開リポジトリで discover → query を通し、記事品質を検証

### Non-Goals

- YAML schema / formal completeness package（mino 的な重さは不要）
- 差分 re-discover（初回はスナップショット。`source_revision` pin で将来対応）
- GUI / Web UI
- discover 単体での clone 機能（repo_ingest の clone を再利用）

## Architecture Design

### パイプライン上の位置

```
[既存] repo_ingest: clone → doc ファイル発見 → manifest 生成
        ↓ manifest + clone 済みリポジトリパスを引き継ぐ
[新規] source_scan: ソースコードのドメイン知識候補を分類・抽出
        ↓ scan result（ファイルリスト + 分類）
[新規] discover (LLM workflow): ソースコードを読解 → concepts/ に記事を直接生成
        ↓
[既存] graph_gen → lint → query
```

discover は compile のコードソース対応モード。repo_ingest の clone 結果と manifest を再利用するため、discover 単体での clone 機能は持たない（ingest 未実行なら ingest を先に案内）。discover が concepts/ に直接書くため、従来の compile ステップは不要（discover 自体が compile の役割を兼ねる）。

### 物理配置

```
skills/wiki/scripts/
├── source_scan.py                     (CLI — ソースコード分類スキャナ)
├── test_source_scan.py
├── lib/domain/source_scan.py          (pure — 分類ロジック)
├── lib/domain/test_source_scan.py
├── lib/service/source_scan_io.py      (I/O — git ls-files 実行、manifest 読み込み、DI)
├── lib/service/test_source_scan_io.py
```

repo_ingest と同じ層分離: domain(pure) → service(I/O + DI) → CLI(薄い handler)。`git ls-files` の I/O は service 層に置く（manifest は top_level_dirs / 拡張子統計 / entrypoints のみで全ファイルリストを持たないため、ls-files 再取得が必要）。

### source_scan.py が分類する対象

repo_ingest の `discover_docs()` がドキュメントファイル（.md, config）を分類するのに対し、source_scan はソースコードを分類する。対象が重複するケース（docs 配下の非 md、routes ディレクトリの README 等）は **precedence 規則**で解決する: discover_docs で既に分類済みのファイルは source_scan の対象外とする（manifest の docs リストで除外）。

| カテゴリ | 狙うファイル | 抽出するドメイン知識 |
|---|---|---|
| `schema` | migration ファイル、ORM モデル定義（`models/`, `schema/`, `migrations/`） | DB スキーマ、テーブル関連、制約 |
| `routes` | ルート定義、コントローラー（`routes/`, `controllers/`, `handlers/`, `views/`） | API エンドポイント、リクエスト/レスポンス構造 |
| `rules` | バリデーション、ドメインロジック、定数（`validators/`, `rules/`, `constants/`） | ビジネスルール、制約条件 |
| `state` | enum、状態遷移、ステータス管理（`enums/`, `states/`, `status`） | 状態遷移図、ライフサイクル |
| `tests` | テストファイル（`test_*`, `*_test.*`, `*.spec.*`, `__tests__/`） | 仕様、境界条件、例外ケース、用語 |
| `entry` | エントリポイント（`main.*`, `app.*`, `index.*`, `server.*`） | アーキテクチャ、データフロー |

### 分類ロジック（pure 関数）

```python
@dataclass(frozen=True)
class SourceCandidate:
    path: str
    category: str          # schema | routes | rules | state | tests | entry
    confidence: float      # 0.0-1.0（パスパターン一致度、clamp 済み）
    size_bytes: int
    large_file_warning: bool  # > 100KB

@dataclass(frozen=True)
class ScanResult:
    slug: str
    revision: str
    candidates: tuple[SourceCandidate, ...]
    stats: dict            # カテゴリ別ファイル数
    skipped_count: int     # 分類不能ファイル数
```

- 分類はファイルパスのパターンマッチ（LLM 不使用、決定論的）
- confidence は複数パターン一致で加算後 **min(1.0, score) で clamp**
- 巨大ファイル（> 100KB）は候補に残すが `large_file_warning = True`
- binary ファイルは除外
- 1ファイルが複数カテゴリに該当する場合、最も confidence の高いカテゴリに分類（同点なら precedence: schema > routes > rules > state > tests > entry）

### CLI インターフェース

```bash
python3 scripts/source_scan.py --wiki-root .wiki --slug {slug} [--categories schema,routes,tests] [--max-files 100] [--format table|json]
```

- `--slug`: repo_ingest で生成された manifest のリポジトリ slug（manifest からクローンパスを解決）
- `--categories`: 走査対象カテゴリの絞り込み（デフォルト: 全カテゴリ）
- `--max-files`: カテゴリあたりの最大ファイル数（デフォルト: 100）
- `--format`: 出力形式（デフォルト: table）
- exit: 0=成功（候補なしも含む、空結果は正常）/ 1=失敗（manifest 不在、clone パス解決不能等）/ 2=引数エラー

### discover ワークフロー（SKILL.md 定義、LLM 実行）

source_scan の結果を受けて LLM がソースコードを読解し、**concepts/ に記事を直接生成**する。compile のコードソース対応モードとして、既存の compile 規約（語調、wikilink 密度、出典ルール）に従う。

**入力**: scan result（ファイルリスト + 分類）
**出力**: `concepts/` に以下の記事を生成（リポジトリの内容に応じて取捨選択）

| 記事 slug | 生成条件 | 抽出元カテゴリ |
|---|---|---|
| `{slug}-architecture` | 常に生成 | entry + 全体構造 |
| `{slug}-db-schema` | schema 候補あり | schema |
| `{slug}-api-routes` | routes 候補あり | routes |
| `{slug}-business-rules` | rules 候補あり + tests から補強 | rules, tests |
| `{slug}-state-machines` | state 候補あり | state |
| `{slug}-glossary` | 用語が一定数以上抽出された場合のみ | 全カテゴリから用語を収集 |

glossary は常時生成ではなく **用語が一定数（5語以上）抽出された場合のみ生成**。ドメイン用語の薄いリポジトリで 50 words 未満の短記事を生んで lint の article_quality に引っかかることを防ぐ。

**記事のフロントマター**（page-template.json 準拠、`additionalProperties: false`）:

```yaml
---
title: "{slug} DB スキーマ"
type: "wiki"
category: "references"
tags: ["{slug}", "db-schema", "discover"]
created: "{date}"
updated: "{date}"
source_refs:
  - "raw/files/{slug}/repo-inventory.md"
related:
  - "concepts/{slug}-architecture.md"
---
```

- `type` は schema 上 `const: "wiki"` に固定。discover 記事の識別は **タグ `discover`** の存在で行う
- `source_refs` は必須（`minItems: 1`）。repo-inventory.md を必ず含める
- `slug` フィールドは schema 外のため使わない（ファイル名 = slug）
- コード由来の事実は本文内に `path@8hash` 形式の出典を付ける（compilation-guide.md の repo 出典規約に準拠）

**discover 済み検出**: concepts/ 内の記事に `discover` タグが含まれ、かつ `source_refs` に当該リポジトリの `raw/files/{slug}/repo-inventory.md` が含まれていれば discover 済みとみなす。再 discover 時は既存記事を上書き更新する（`updated` 日付を更新）。

**読解プロトコル**（compilation-guide.md の段階的読解プロトコルを拡張）:

1. manifest + repo-inventory.md で全体構造を把握
2. entry カテゴリのファイル冒頭を読んでデータフローを把握
3. カテゴリごとに候補ファイルを confidence 順に読解（高 → 低）
4. tests カテゴリからビジネスルール・境界条件・用語を補強
5. 不足箇所だけ追加 Read
6. 記事末尾に読解カバレッジの限界を明記する

**mino-skills から盗む4視点**（discover プロンプトに散文で埋め込む）:

- **actor + purpose**: 同じ名詞でも context で意味が違うケースを発見する
- **term ledger**: 用語集を作成し、多義語は文脈別に定義する
- **context boundary**: 意味・ルール・状態が変わる境界を特定する
- **invisible concepts**: 名詞ではなく判断・制約・失敗をモデル化する

**確認対話**: discover が記事を生成した後、AskUserQuestion で記事サマリを提示して「この理解で合っている？」と確認する。修正があれば記事を更新。**非対話モード**: `--yes` フラグ（または cycle 内実行時）は確認をスキップしてそのまま保存。cycle headless 実行との互換性を確保。

### SKILL.md への追加

操作ルーティングテーブルに `discover` を追加:

| キーワード | ワークフロー | 説明 |
|-----------|-------------|------|
| `discover` | **discover** | ソースコードからドメイン知識を抽出（compile のコードソース対応モード） |

cycle の拡張: `ingest → discover → compile → graph_gen → lint` のフルパイプライン。discover はオプショナル（repo ソース + manifest がある場合のみ）。discover 済み判定はタグ `discover` + `source_refs` 内の `raw/files/{slug}/repo-inventory.md` の存在で行う。

**wikilink_render の適用**: discover が concepts/ に記事を書いた後、graph_gen の前に `wikilink_render.py --write` を実行する（既存の compile フローと同じタイミング）。

### セキュリティ

- discover が読むソースコードは untrusted data（compilation-guide.md の untrusted 取り扱いに準拠）
- 生成した記事には保存前に security_scan.py を適用（secret 漏れ検出として有効。injection 対策は LLM 読解時のプロンプト防御が本質 — scan は二重防御）
- ソースコード中の指示めいた文言には従わない

## Steps

### Phase A: source_scan.py — ソースコード分類スキャナ

- [x] A1. `lib/domain/source_scan.py` — pure 分類ロジック（`SourceCandidate`, `ScanResult`, `classify_source_files()`, カテゴリ別パターン定義、confidence clamp、precedence 規則）
- [x] A2. `lib/domain/test_source_scan.py` — 分類ロジックの単体テスト（各カテゴリのパターンマッチ、confidence clamp 境界値、precedence 解決、巨大ファイル warning、binary 除外、discover_docs 既分類ファイルの除外）
- [x] A3. `lib/service/source_scan_io.py` — I/O 層（`git ls-files` 実行、manifest JSON 読み込み、clone パス解決。SubprocessRunner 互換の DI）
- [x] A4. `lib/service/test_source_scan_io.py` — I/O 層のテスト（manifest 読み込み、ls-files パース、DI モック）
- [x] A5. `source_scan.py` — 薄い CLI ハンドラ（argparse, domain + service 層の合成, table/json 出力, exit code mapping）
- [x] A6. `test_source_scan.py` — CLI の統合テスト（exit code 規約、出力形式、エラーケース）

### Phase B: discover ワークフロー定義 + 縦切り検証

- [x] B1. `references/discover-guide.md` — discover 読解プロンプトガイド（カテゴリ別の読解戦略、mino 4視点の散文プロンプト、記事テンプレート、確認対話 + `--yes` 非対話モードの仕様）
- [x] B2. `references/prompts.md` に discover 用プロンプトテンプレートを **1種（architecture）だけ**先行追加
- [x] B3. **縦切り検証**: 公開リポジトリ1つで source_scan → architecture 記事1つを生成 → 目視で品質評価。6記事タイプを一括で書く前にフィードバックを得る
- [x] B4. B3 の結果を踏まえて残り5種のプロンプトテンプレートを追加（db-schema / api-routes / business-rules / state-machines / glossary）

### Phase C: SKILL.md + CLAUDE.md 統合

- [x] C1. SKILL.md に `discover` セクションを追加（操作ルーティング、ワークフロー手順、source_scan CLI 呼び出し、読解プロトコル、確認対話 / `--yes`、記事保存、wikilink_render、index.md 更新、log.md 追記）
- [x] C2. SKILL.md の cycle セクションに discover オプション統合（repo ソース + manifest がある場合にオプショナルで discover を挟む）
- [x] C3. CLAUDE.md に discover 関連の記載追加（サブコマンド説明、source_scan スクリプト、discover-guide.md 参照）
- [x] C4. `log_append.py` に discover 操作の追加（`python3 log_append.py discover --wiki-root .wiki --slug {slug} --articles N`）

### Phase D: dogfooding — 実リポジトリで検証

- [ ] D1. **6カテゴリを踏む公開リポジトリ**（Rails / Django / Express 等のフルスタックアプリ）で discover を実行。本リポジトリは schema/routes/state がほぼ空のため主要カテゴリを exercise できない
- [ ] D2. 記事品質の評価（coverage、正確性、用語の一貫性、wikilink の有用性）
- [ ] D3. lint 通過の確認（graph_gen → lint。article_quality / dead_link / orphan 等）
- [ ] D4. query で横断質問に答えられるか検証（discover 生成記事 + 既存記事の連携）
- [ ] D5. 摩擦リスト作成（source_scan の分類精度、読解の深さ、確認対話の UX、セキュリティスキャン誤検知）
- [ ] D6. 摩擦に基づく修正（Phase A-C へのフィードバックループ）

## Risks & Mitigations

| リスク | 影響 | 対策 |
|---|---|---|
| source_scan のパターンが言語・フレームワークごとに異なりすぎる | 分類精度の低下 | 主要フレームワーク（Rails, Django, Express, Spring, Next.js）のパターンを Phase A で網羅。未知のパターンは `unknown` カテゴリに分類して LLM に判断を委ねる |
| LLM のコンテキストウィンドウに収まらない巨大リポジトリ | 読解の品質低下 | source_scan の `--max-files` で制限 + 段階的読解プロトコルで最小限の Read に抑える |
| discover が生成する記事のハルシネーション | 不正確な知識の蓄積 | 確認対話で人間がチェック + 出典規約（コード由来の事実にはファイルパス@8hash 出典）で検証可能性を担保 |
| security_scan の誤検知（ソースコード内のテストデータ等） | discover フローの中断 | cross-repo-wiki-gitlab-fetcher のアイデアで既に認識済み。閾値・許可リスト調整を Phase D で実測 |
| glossary が 50 words 未満の短記事になる | lint の article_quality 違反 | 用語が一定数（5語以上）抽出された場合のみ生成する条件付きにした |
| discover 記事が既存 compile 記事と slug 衝突 | 上書きリスク | `{slug}-` prefix で名前空間を分離。discover は既存 slug を上書きしない（衝突時は warning + スキップ） |

## Codex レビュー反映ログ

| 指摘 | 対応 |
|---|---|
| 🔴 raw/ に LLM 生成物を書くのはアーキテクチャ違反 | discover = compile のコードソース対応モード（選択肢 A）に変更。concepts/ に直接生成 |
| source_type "repo-ingest" はコードベースに存在しない | source_type フィールド自体を廃止。page-template.json 準拠のフロントマターに変更 |
| service 層が欠けている（git ls-files は I/O） | `lib/service/source_scan_io.py` を追加。repo_ingest と同じ層分離 |
| source_path "(multiple)" は 1file-1path 規約違反 | source_path を廃止。コード出典は本文内 `path@8hash` で完結 |
| exit code 1=候補なし は既存規約と衝突 | 0=成功（候補なし含む）/ 1=失敗 / 2=引数エラー に変更 |
| cycle headless と確認対話が矛盾 | `--yes` 非対話モードを追加 |
| 「互いに排他的」は過大主張 | precedence 規則に変更（manifest docs リストで除外 + confidence 最大カテゴリ） |
| confidence が 1.0 超過しうる | `min(1.0, score)` で clamp |
| glossary 常時生成は薄いリポで短記事を生む | 用語5語以上で条件付き生成に変更 |
| Phase B が最高リスクなのに検証が D まで無い | B3 に縦切り検証ステップを挿入（architecture 1記事で先行検証） |
| 本リポジトリは dogfooding 先として不適切 | Phase D をフルスタック公開リポジトリに変更 |
| security_scan の injection 対策境界 | scan は secret 漏れ検出として有効、injection は読解時プロンプト防御が本質と明記 |

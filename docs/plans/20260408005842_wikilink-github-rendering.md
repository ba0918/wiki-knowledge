# wikilink GitHub 追跡性改善 — 自動併記スクリプトと lint チェック

**Cycle ID:** 20260408005842
**Type:** Enhancement
**Created:** 2026-04-08 00:58:42
**Status:** 🟡 Planning

## Overview

wiki-knowladge は `[[slug]]` 記法を真実とする core asset だが、GitHub Flavored Markdown は wikilink を解釈しないため、GitHub Web UI / PR レビュー時にリンクが踏めない。本計画では **案 C-1（併記方式）** を採用し、`[[slug]]` を `[[slug]] ([↗](slug.md))` に自動変換するスクリプトと lint チェックを導入する。

### Goals

- GitHub Web UI で `.wiki/concepts/*.md` を開いたとき、すべてのリンクが踏める
- 既存 lint / trust-score / graph_gen パイプラインに影響を与えない（wikilink 自体は残す）
- 変換は決定論的（idempotent、同じ入力で同じ出力）
- 新規記事作成時に手動併記を意識しなくて済む（wiki-compile に統合）
- pure 関数中心、副作用は CLI 層に分離（Design Principle 準拠）

### Non-Goals

- 案 A（wikilink 廃止）への移行
- Obsidian / Foam reader 対応設定
- gh-pages rendered branch 方式

## Architecture Design

### Layer Analysis

> **重要な前提修正**: wikilink 抽出の pure 関数 `find_wikilinks()` は既に `skills/wiki/scripts/lib/inventory.py` L36/L50 に存在し、`lint-wiki.py` も既にそこから import している（L34）。テストも `lib/test_inventory.py` 完備。したがって本計画は **新規 parser を作らず、既存 `lib/inventory.py` を再利用** する。

```
domain (pure / 既存)
  lib/inventory.py:find_wikilinks  — 既存。再利用のみ。改修なし
                                     既知の限界: チルダフェンス `~~~` / インデントコードブロック / HTML コメント未除外（[[wikilink-link-parser-spec]] 参照）

domain (pure / 新規)
  wikilink_render.py    — pure 変換関数 render_wikilinks(text) → text。idempotent
                          抽出は lib.inventory.find_wikilinks を流用せず、変換用に別 regex を持つ
                          （find_wikilinks は slug 列挙用、render は位置情報＋置換が必要なため責務分離）
                          ただし slug 制約 `[a-z0-9-]+` と code-fence/inline-code 除外ルールは inventory.py と完全一致させる

service
  wikilink_render.py CLI 層 — ファイル I/O、--check/--write モード、.wiki/ 配下制限

integration
  lint-wiki.py            — 新チェック「wikilink-rendering」のみ追加。既存 import は触らない
  SKILL.md (wiki-compile) — 最終ステップで wikilink_render.py --write を実行
```

### File Structure

```
skills/wiki/scripts/
├── lib/
│   ├── inventory.py                  (変更なし — find_wikilinks を再利用)
│   └── test_inventory.py             (変更なし)
├── wikilink_render.py                (新規 — pure + CLI)
├── test_wikilink_render.py           (新規)
└── lint-wiki.py                      (修正 — 新チェック「wikilink-rendering」のみ追加)

skills/wiki/
├── SKILL.md                          (修正 — wiki-compile 節に変換ステップ追記)
└── references/
    ├── architecture.md               (修正 — 併記方式を記載)
    └── lint-procedure.md             (修正 — wikilink-rendering チェック追記)

CLAUDE.md                             (修正 — Lint チェック項目に追加)
.wiki/concepts/*.md                   (修正 — 初期適用)
```

## Design Decisions

| 論点 | 決定 | 理由 |
|------|------|------|
| 配置場所 | wiki-compile の最終ステップ（pre-commit はオプション） | compile が wiki ライフサイクルの責務層。pre-commit は project ごとに導入条件異なる |
| 変換形式 | `[[slug]] ([↗](slug.md))` | wikilink を残し併記。lint/trust-score 無改修 |
| Idempotency | 既存 `[[…]] ([↗](…))` パターンを正規表現で検出してスキップ | 二重化防止。テスト強制 |
| エイリアス順序 | **Obsidian/Foam 順** `[[slug\|表示]]` を採用（Dendron の `[[表示\|slug]]` 順は不採用） | 既存 `lib/inventory.py` の `_WIKILINK_RE` がパイプ前を slug として扱っており、本プロジェクトは [[wikilink-reader-comparison]] 4ツール最小公倍数互換のうち Obsidian/Foam 寄りを既定としている。Dendron 順は Non-Goals |
| slug 文字種 | `[a-z0-9-]+`（既存制約と完全一致） | reader 4ツール最小公倍数互換の維持。`render_wikilinks` の regex も同制約 |
| display text | `[[slug\|表示]]` → `[[slug\|表示]] ([↗](slug.md))` | slug 部分のみ参照、表示テキストは保持 |
| dead link | そのまま変換（slug.md は存在しなくても link 化） | 変換器は存在チェックしない（責務分離）。dead link は lint 側で検出済み |
| コードブロック除外 | バッククォートフェンス + インラインコードのみ（既存 `lib/inventory.py` と一致） | チルダフェンス `~~~` / インデントコードブロック / HTML コメント内は **defer**（既存 inventory.py の既知の限界を継承し、本計画では解消しない。問題化したら別 plan） |
| `## 関連` 節との重複 | 本文 wikilink にも `## 関連` 内 wikilink にも一律 `([↗](slug.md))` を併記 | `## 関連` も markdown 上は本文と同等。区別を入れると idempotency が壊れやすい。冗長性は許容 |
| 逆変換 | スコープ外（必要になったら別 plan） | YAGNI |
| lint チェック粒度 | 警告（warning）レベル | 強制すると初期段階で全記事 fail。compile 統合で実質的に強制される |

## Implementation Steps

> Step 1〜3（旧計画）の「parser 切り出し」は **削除** — 既に `lib/inventory.py` に存在するため不要。

1. **wikilink_render.py（pure 関数）** — `render_wikilinks(text: str) → str` を実装
   - 既存 `lib/inventory.py` の code-fence / inline-code 除外ロジックと**同じ regex 定数**を共有（`lib/inventory.py` から定数のみ import するか、定数を `lib/inventory.py` の公開シンボルに昇格）
   - slug 制約 `[a-z0-9-]+` を維持
   - 既存併記 `([↗](slug.md))` パターンを検出して二重化防止
   - チルダフェンス未対応は既知の限界として継承（テストに XFAIL で記録）
2. **test_wikilink_render.py** — idempotent / コードスパン除外 / 二重化防止 / display text / dead link / 複数 wikilink 同一行 / `## 関連` 内の wikilink も変換される / チルダフェンス XFAIL
3. **wikilink_render.py CLI 層** — `--write` / `--check` / ファイル/ディレクトリ引数。パスは `.wiki/` 配下制限
4. **lint-wiki.py 新チェック追加** — `wikilink-rendering` カテゴリで「併記が剥がれている wikilink」を warning として報告。既存 import 構造は触らない
5. **test_lint_wiki.py 追加テスト** — 新チェックの正常系 / 異常系
6. **SKILL.md（wiki-compile 節）修正** — compile の最終ステップに `python3 skills/wiki/scripts/wikilink_render.py --write .wiki/concepts/` を追加
7. **`.wiki/concepts/*.md` 初期適用** — スクリプト走らせて全記事を併記化（`git diff` の規模見積り: 8 記事 × 平均 5〜10 wikilink ≈ 40〜80 行変更想定）
8. **ドキュメント更新** — `references/lint-procedure.md` / `references/architecture.md` / `CLAUDE.md` の Lint 節

## Test List

### Pure (wikilink_render)
- [ ] `[[slug]]` → `[[slug]] ([↗](slug.md))`
- [ ] 既に併記済みの行は変更なし（idempotent）
- [ ] 二重実行で結果が変わらない（idempotent 強制）
- [ ] コードスパン内は変換しない
- [ ] コードフェンス内は変換しない
- [ ] `[[slug|表示]]` → `[[slug|表示]] ([↗](slug.md))`
- [ ] 同一行の複数 wikilink すべて変換
- [ ] 存在しない slug もそのまま変換（責務外）
- [ ] `## 関連` セクション内の `[[slug]]` も変換される
- [ ] チルダフェンス `~~~` 内は変換されてしまう（XFAIL — 既知の限界として記録）

### Integration (lint-wiki)
- [ ] 既存 44 テストが green
- [ ] 新チェック「wikilink-rendering」が剥がれた wikilink を検出
- [ ] 併記済み記事は warning 0

### CLI
- [ ] `--check` で差分があれば exit 1
- [ ] `--write` でファイル更新
- [ ] ディレクトリ再帰

## Security Checklist

- [ ] パストラバーサル: CLI で受け取るパスを `.wiki/` 配下に制限（または現状 lint-wiki.py と同等のチェック）
- [ ] 入力検証: ファイルサイズ上限なし（小さい markdown のみ想定、明示的に注記）
- [ ] 副作用は `--write` 指定時のみ。デフォルトは dry-run（stdout）

## Progress Tracking

| Step | 状態 |
|------|------|
| 1. wikilink_render.py pure | ⬜ |
| 2. test_wikilink_render.py | ⬜ |
| 3. wikilink_render.py CLI | ⬜ |
| 4. lint 新チェック追加 | ⬜ |
| 5. test_lint_wiki.py 追加テスト | ⬜ |
| 6. SKILL.md compile 節更新 | ⬜ |
| 7. .wiki/concepts/*.md 初期適用 | ⬜ |
| 8. ドキュメント更新 | ⬜ |

## Alternatives Considered

- **Pandoc 3.0 `wikilinks_title_after_pipe` 拡張**: 不採用。pandoc 依存追加（現状 pure-Python のみ）と、変換が「ファイル書き換え型」となり執筆体験を壊すため。本プロジェクトは [[wikilink-conversion-strategies]] の戦略 3（併記方式）を採る。
- **CI rendered branch**: スコープ外（[[wikilink-conversion-strategies]] 戦略 2）。将来必要なら別 plan。
- **`lib/inventory.py:find_wikilinks` を render 用にも流用**: スパン位置が必要なため不採用（findall は位置情報を返さない）。代わりに regex 定数のみ共有して責務分離。

## References

- 直前の wiki-query 比較結果（案 A/B/C-1）
- [[wiki-knowledge-architecture]]
- [[wikilink-github-interop]] — GFM が wikilink 非対応である根拠
- [[wikilink-reader-comparison]] — Dendron エイリアス順序の罠
- [[wikilink-conversion-strategies]] — 戦略 3 採用根拠 + Pandoc 3.0 代替案
- [[wikilink-link-parser-spec]] — 既存 `lib/inventory.py:find_wikilinks` 仕様 + チルダフェンス未対応の既知限界
- `skills/wiki/scripts/lib/inventory.py` L36, L50（既存 `_WIKILINK_RE` / `find_wikilinks`）
- `skills/wiki/scripts/lint-wiki.py` L34（既存 import）
- Design Principles: Pure Functions / DI / Layer Separation

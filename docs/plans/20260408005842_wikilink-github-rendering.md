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

```
domain (pure)
  wikilink_parser.py    — wikilink 抽出（コードスパン除外）の純粋関数。lint-wiki.py から切り出して共通化
  wikilink_render.py    — pure 変換関数 render_wikilinks(text) → text。idempotent

service
  wikilink_render.py CLI 層 — ファイル I/O、--check/--write モード

integration
  lint-wiki.py            — wikilink_parser.py を import に置き換え、新チェック「wikilink-rendering」を追加
  SKILL.md (wiki-compile) — 最終ステップで wikilink_render.py --write を実行
```

### File Structure

```
skills/wiki/scripts/
├── lib/
│   ├── __init__.py
│   └── wikilink_parser.py            (新規 — lint-wiki.py から抽出)
├── wikilink_render.py                (新規 — pure + CLI)
├── test_wikilink_parser.py           (新規)
├── test_wikilink_render.py           (新規)
└── lint-wiki.py                      (修正 — parser を lib 経由 + 新チェック)

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
| display text | `[[slug\|表示]]` → `[[slug\|表示]] ([↗](slug.md))` | slug 部分のみ参照、表示テキストは保持 |
| dead link | そのまま変換（slug.md は存在しなくても link 化） | 変換器は存在チェックしない（責務分離）。dead link は lint 側で検出済み |
| 逆変換 | スコープ外（必要になったら別 plan） | YAGNI |
| lint チェック粒度 | 警告（warning）レベル | 強制すると初期段階で全記事 fail。compile 統合で実質的に強制される |

## Implementation Steps

1. **lib/wikilink_parser.py 切り出し** — `lint-wiki.py` から wikilink 抽出ロジック（コードスパン/コードフェンス除外）を pure 関数として抽出。`extract_wikilinks(text) → list[Wikilink]` を公開
2. **test_wikilink_parser.py** — コードスパン除外、フェンス除外、display text、複数行のテスト
3. **lint-wiki.py 修正** — `lib.wikilink_parser` を import するよう置き換え。既存テスト（44）が green であることを確認
4. **wikilink_render.py（pure 関数）** — `render_wikilinks(text: str) → str` を実装。既存併記検出 + 変換 + idempotency
5. **test_wikilink_render.py** — idempotent / コードスパン除外 / 二重化防止 / display text / dead link / 複数 wikilink 同一行
6. **wikilink_render.py CLI 層** — `--write` / `--check` / ファイル/ディレクトリ引数
7. **lint-wiki.py 新チェック追加** — `wikilink-rendering` カテゴリで「併記が剥がれている wikilink」を warning として報告
8. **SKILL.md（wiki-compile 節）修正** — compile の最終ステップに `python3 skills/wiki/scripts/wikilink_render.py --write .wiki/concepts/` を追加
9. **`.wiki/concepts/*.md` 初期適用** — スクリプト走らせて全記事を併記化
10. **ドキュメント更新** — `references/lint-procedure.md` / `references/architecture.md` / `CLAUDE.md` の Lint 節

## Test List

### Pure (wikilink_parser)
- [ ] 通常の `[[slug]]` を抽出
- [ ] `[[slug|display]]` で slug を抽出、display を保持
- [ ] バッククォート内の `[[slug]]` は除外
- [ ] コードフェンス内の `[[slug]]` は除外
- [ ] 同一行に複数 wikilink

### Pure (wikilink_render)
- [ ] `[[slug]]` → `[[slug]] ([↗](slug.md))`
- [ ] 既に併記済みの行は変更なし（idempotent）
- [ ] 二重実行で結果が変わらない（idempotent 強制）
- [ ] コードスパン内は変換しない
- [ ] コードフェンス内は変換しない
- [ ] `[[slug|表示]]` → `[[slug|表示]] ([↗](slug.md))`
- [ ] 同一行の複数 wikilink すべて変換
- [ ] 存在しない slug もそのまま変換（責務外）

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
| 1. lib/wikilink_parser.py 切り出し | ⬜ |
| 2. test_wikilink_parser.py | ⬜ |
| 3. lint-wiki.py を parser に置換 | ⬜ |
| 4. wikilink_render.py pure | ⬜ |
| 5. test_wikilink_render.py | ⬜ |
| 6. wikilink_render.py CLI | ⬜ |
| 7. lint 新チェック追加 | ⬜ |
| 8. SKILL.md compile 節更新 | ⬜ |
| 9. .wiki/concepts/*.md 初期適用 | ⬜ |
| 10. ドキュメント更新 | ⬜ |

## References

- 直前の wiki-query 比較結果（案 A/B/C-1）
- [[wiki-knowledge-architecture]]
- `skills/wiki/scripts/lint-wiki.py` L450-500 付近の wikilink 抽出ロジック
- Design Principles: Pure Functions / DI / Layer Separation

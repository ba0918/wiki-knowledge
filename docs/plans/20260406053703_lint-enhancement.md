# 3b Lint 強化

**Cycle ID:** `20260406053703`
**Started:** 2026-04-06 05:37:03
**Status:** 🔵 Implementing

---

## 📝 What & Why

既存の `lint-wiki.py` を拡張し、リンク品質・記事品質・フォーマット違反チェックとレポート出力を追加する。記事が少ない今のうちにルールを整備し、後から補正が大変になるのを防ぐ。

## 🎯 Goals

- 他スクリプト（trust_score.py, gap_detect.py）と統一された `--format table|json|report` 出力
- リンク品質チェック（一方向リンク検出、`related` と本文 wikilink の不一致）
- 記事品質チェック（短記事、出典なし主張、推測ブロック過多）
- フォーマット違反チェック（slug 命名規則、page-template.json 準拠、出典パス検証）
- 全チェック項目のユニットテスト

## 📐 Design

### アーキテクチャ方針

既存の `lint-wiki.py` は `lint()` 関数に全チェックがフラットに並んでいる。チェック項目が倍増するため、**チェック関数を個別に分離**し、`lint()` がそれらを呼び出すオーケストレータになるよう再構成する。

```
lint-wiki.py
├── lint()                    # オーケストレータ（ロジックなし、チェック呼び出しのみ）
├── _build_inventory()        # 記事インベントリ構築（既存ロジック抽出）
├── _check_dead_links()       # 既存: dead link 検出
├── _check_orphans()          # 既存: orphan 検出
├── _check_missing_sources()  # 既存: missing source 検出
├── _check_missing_fm()       # 既存: missing frontmatter 検出
├── _check_coverage_gaps()    # 既存: coverage gap 検出
├── _check_link_quality()     # 新規: 一方向リンク、related 不一致
├── _check_article_quality()  # 新規: 短記事、出典なし、推測過多
├── _check_format()           # 新規: slug 命名、schema 準拠、出典パス
├── format_table()            # 新規: table 出力
├── format_json()             # 既存 JSON 出力を関数化
├── format_report()           # 新規: Markdown レポート出力
└── main()                    # CLI: --wiki-root, --format
```

### Files to Change

```
skills/wiki/scripts/
  lint-wiki.py          - チェック関数分離 + 新規3チェック + --format 対応
  test_lint_wiki.py     - 新規: 全チェックのユニットテスト
```

### Key Points

- **チェック関数の純粋性**: 各 `_check_*` 関数は `(inventory, wiki_root)` を受け取り `list[Finding]` を返す純粋関数
- **Finding 型**: `dataclass(frozen=True)` で severity, check, slug, message, details(dict|None) を持つ。details は check 固有の追加情報（target, source_ref, missing_fields 等）を格納。既存の dict 形式は全て Finding に統一移行する
- **Inventory 型**: `dataclass(frozen=True)` で slug, path, frontmatter(dict), wikilinks(list[str]), text(str), body(str) を持つ。`_build_inventory()` は `dict[str, ArticleInventory]` を返す
- **CLI統一**: `--wiki-root` 引数に変更（trust_score.py/gap_detect.py と統一）。後方互換は `argparse` の位置引数 `nargs='?'` で実装し、`--wiki-root` 未指定時に位置引数をフォールバックとして使用
- **レポート出力先**: `.wiki/outputs/reports/{YYYYMMDD}-lint.md`（lint-procedure.md 準拠）
- **page-template.json 読み込み**: schema ファイルを `_build_inventory()` 時に一度だけ読み込み、inventory と共にチェック関数へ渡す。ファイル不在時は schema チェックをスキップし Warning を出力
- **categories.json 読み込み**: 同様に一度だけ読み込み。不在時はカテゴリチェックをスキップし Warning を出力
- **related FM の slug 正規化**: `related` FM の値は `{wiki_root}` からの相対パス形式（例: `concepts/foo.md`）のため、比較時はベア slug に正規化する。正規化ロジック（`_normalize_slug()`）は lint-wiki.py に定義し、trust_score.py からも import する形に統一（lint-wiki.py が基盤モジュールのため循環依存なし）
- **schema/categories の注入**: `page-template.json` と `categories.json` は `lint()` オーケストレータが一度だけ読み込み、`_check_format(inventory, wiki_root, schema, categories)` へ引数として注入する（inventory に混ぜない）

### 新規チェック詳細

#### Check 6: Link Quality (`_check_link_quality`)

| 検出 | Severity | ロジック |
|------|----------|---------|
| 一方向リンク | 🟡 Warning | A→B の wikilink はあるが B→A の wikilink も related もない |
| related 不一致 | 🟡 Warning | `related` FM にあるが本文 `[[wikilink]]` にない、またはその逆 |

#### Check 7: Article Quality (`_check_article_quality`)

| 検出 | Severity | ロジック |
|------|----------|---------|
| 短記事 | 🟡 Warning | 本文（FM除く）が 50 words 未満 |
| 推測過多 | 🟡 Warning | `> [推測]` ブロックが本文行数の 30% 超 |

※「出典なし主張」は LLM 駆動チェック（lint-procedure.md §6）のため、自動チェックスコープ外とする。

#### Check 8: Format Violations (`_check_format`)

| 検出 | Severity | ロジック |
|------|----------|---------|
| slug 命名違反 | 🔴 Error | ファイル名が `^[a-z0-9]+(-[a-z0-9]+)*$` に非準拠 |
| schema 非準拠 | 🟡 Warning | page-template.json の required/type/const/additionalProperties 違反 |
| category 不正 | 🟡 Warning | categories.json に存在しない category 値 |
| type 不正 | 🔴 Error | `type` が `wiki` でない |
| date 形式不正 | 🟡 Warning | `created`/`updated` が YYYY-MM-DD 形式でない |
| tags 形式不正 | 🟡 Warning | tags 要素が小文字ハイフン区切り `^[a-z0-9-]+$` に非準拠 |
| source_refs 空 | 🔴 Error | `source_refs` が空配列（minItems: 1 違反） |
| related 型不正 | 🟡 Warning | `related` が配列でない、または要素が string でない |

## ✅ Tests

### 既存チェックのテスト

- [ ] dead link: 存在しないリンクを検出
- [ ] dead link: 正常リンクは検出しない
- [ ] orphan: 被リンクなし記事を検出
- [ ] orphan: 被リンクあり記事は検出しない
- [ ] missing source: 存在しないソースを検出
- [ ] missing frontmatter: 必須フィールド欠損を検出
- [ ] coverage gap: 2回以上参照されてる未存在ページを検出

### 新規チェックのテスト

- [ ] link quality: 一方向リンクを検出
- [ ] link quality: 双方向リンクは検出しない
- [ ] link quality: related FM と本文 wikilink の不一致を検出
- [ ] article quality: 50 words 未満の記事を検出
- [ ] article quality: 推測ブロック 30% 超を検出
- [ ] format: slug 命名違反を検出（大文字、スペース、連続ハイフン）
- [ ] format: schema 非準拠を検出（unknown field, missing required, wrong type）
- [ ] format: 不正 category を検出
- [ ] format: type が wiki 以外を検出
- [ ] format: date 形式不正を検出
- [ ] format: tags 形式不正を検出
- [ ] format: source_refs 空を検出
- [ ] format: related 型不正を検出
- [ ] format: schema/categories.json 不在時にスキップ+Warning

### 出力フォーマットのテスト

- [ ] format_table: 正しい table 文字列を返す
- [ ] format_json: 正しい JSON 構造を返す
- [ ] format_report: Markdown レポートのヘッダー・セクション構造が正しい
- [ ] CLI: --wiki-root と --format の引数パース

### インベントリ構築のテスト

- [ ] _build_inventory: 記事のメタデータを正しく抽出
- [ ] _build_inventory: concepts/ が存在しない場合にエラー

## 📊 Progress

| Step | Status |
|------|--------|
| Tests | ⚪ |
| Implementation | ⚪ |
| Commit | ⚪ |

**Legend:** ⚪ Pending · 🟡 In Progress · 🟢 Done

---

**Next:** Write tests → Implement → Commit with `claude-skills:commit` 🚀

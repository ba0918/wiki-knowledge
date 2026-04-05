# Architecture

## 設計思想

LLM Wiki Knowledge Base は3つの層で構成される。

### 3層構造

| 層 | 場所 | 責務 | 変更者 |
|----|------|------|--------|
| Source 層 | `{wiki_root}/raw/` | 不変のソースドキュメント | 人間（キュレーション） |
| Knowledge 層 | `{wiki_root}/concepts/` | 相互参照付き Wiki 記事 | LLM（compile/promote） |
| Output 層 | `{wiki_root}/outputs/` | Query 回答・Lint レポート | LLM（query/lint） |

**原則**: Source 層は immutable。LLM は Knowledge 層と Output 層のみ変更する。

### 4相パイプライン

```
Ingest → Compile → Query → Lint → (back to Ingest)
```

| Phase | 入力 | 出力 | トリガー |
|-------|------|------|---------|
| Ingest | ファイル / URL | `raw/` にステージング | ユーザがソースを追加 |
| Compile | `raw/` のソース | `concepts/` に記事生成 | Ingest 後 or ユーザ指示 |
| Query | ユーザの質問 | 回答（→ promote で記事化も） | ユーザが質問 |
| Lint | Wiki 全体 | レポート + 修復提案 | 定期 or ユーザ指示 |

### パス解決

全スキルは CLAUDE.md の YAML フロントマターから `wiki_root` を取得する。

```yaml
---
wiki_root: .wiki
---
```

Wiki 内のパスは全て `{wiki_root}` からの相対パス。

## Backlink Audit

Compile / Promote 時の必須ステップ。新記事を追加したら、既存記事を走査して双方向リンクを確立する。

なぜ必須か: 一方向リンクのみだと Wiki が blog に退化する。双方向リンクがあることで、どの記事からでも関連情報に辿り着ける。

### 手順

1. 新記事のタイトル・タグ・キーワードを抽出
2. `{wiki_root}/concepts/` 内の全記事を `grep` で走査
3. 関連性が高い既存記事に `[[new-slug]]` リンクを追加
4. 既存記事の `related` フロントマターにも追加

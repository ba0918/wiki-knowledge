# Architecture

## 設計思想

LLM Wiki Knowledge Base は3つの層で構成される。

### 3層構造

| 層 | 場所 | 責務 | 変更者 |
|----|------|------|--------|
| Source 層 | `{wiki_root}/raw/` | 不変のソースドキュメント | 人間（キュレーション） |
| Knowledge 層 | `{wiki_root}/concepts/` | 相互参照付き Wiki 記事 | LLM（compile/promote） |
| Output 層 | `{wiki_root}/outputs/` | Query 回答・Lint レポート・派生グラフ | LLM / scripts |

**原則**: Source 層は immutable。LLM は Knowledge 層と Output 層のみ変更する。

### Knowledge 層内の派生関係（concepts → inventory → graph）

Knowledge 層は **source of truth（concepts）→ 派生インデックス（inventory）→ 派生グラフ（graph）** の単方向派生で構成される。

```
concepts/*.md   (source of truth — 人間/LLM が編集)
     │
     ▼ parse (lib/inventory.py)
ArticleInventory  (派生インデックス — in-memory のみ、永続化しない)
     │
     ▼ graph_gen.py
outputs/graph.json  (派生グラフ — nodes / edges / metadata.dangling_links)
     │
     ▼ lint-wiki.py --use-graph (デフォルト ON)
Lint Findings  (dead_link / orphan は graph layer 経由で検出)
```

graph layer の役割は **dead_link / orphan を一箇所で計算する基盤** となること。lint-wiki.py は inventory を再走査せず `outputs/graph.json` の `metadata.dangling_links` と `edges` から派生情報を読むため、検出ロジックの二重実装が排除される。

### 4相パイプライン（派生生成ステップを含む）

```
Ingest → Compile → graph_gen → Lint → (back to Ingest)
                       ▲
                       └ compile の後、lint の前に必ず実行する派生生成ステップ
```

`graph_gen` は派生生成ステップであり、独立フェーズではなく compile と lint の橋渡しとして位置付ける。`wiki cycle` は orchestrator として `compile → graph_gen → lint` を明示的に呼び出す。

### 4相パイプライン

```
Ingest → Compile → Query → Lint → (back to Ingest)
```

| Phase | 入力 | 出力 | トリガー |
|-------|------|------|---------|
| Ingest | ファイル / URL | `raw/` にステージング | ユーザがソースを追加 |
| Compile | `raw/` のソース | `concepts/` に記事生成 | Ingest 後 or ユーザ指示 |
| graph_gen | `concepts/*.md` | `outputs/graph.json` | compile の後・lint の前（派生生成ステップ） |
| Query | ユーザの質問 | 回答（→ promote で記事化も） | ユーザが質問 |
| Lint | Wiki 全体 + `outputs/graph.json` | レポート + 修復提案 | 定期 or ユーザ指示 |

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

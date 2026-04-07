# Wiki Graph Layer (MVP)

**Cycle ID:** `20260407183028`
**Started:** 2026-04-07 18:30:28
**Status:** 🟡 Planning

---

## 📝 What & Why

graphify (examples/graphify) の知識グラフ概念を wiki-knowladge に取り込む第一歩として、`inventory.json` → `graph.json` → `lint 高速化` の3層 MVP を実装する。team-brainstorm 3 ラウンド (Challenger / Explorer / Connector / Grounded / KG-Expert) の合意に基づき、cache と coref-detect は defer、graph 所有権は新規 `graph_gen.py` に確定。

## 🎯 Goals

- `.wiki/concepts/*.md` から決定論的に `inventory.json` を派生させる中間層を作る (二重管理回避)
- `inventory.json` を入力として `graph.json` (read-only view) を都度生成する `graph_gen.py` を新設
- `lint-wiki.py` を `graph.json` の consumer に書き換え、Dead Link / Orphan / backlink 検出を graph 経由に統一
- 拡張余地 (`_custom`, `claim_id`, `co_citation_*`) をスキーマに予約し、Layer 3 への破壊的変更を防ぐ
- 全体を pure function 中心で実装し、決定性テスト (`sha256sum` 二回一致) で保証

## 📐 Design

### アーキテクチャ階層

```
.wiki/concepts/*.md  (source of truth)
        ↓ inventory.py (pure)
.wiki/outputs/inventory.json  (derived, deterministic)
        ↓ graph_gen.py (pure)
.wiki/outputs/graph.json  (read-only view, .gitignore)
        ↓ consumer
lint-wiki.py / (将来) gap_detect / trust_score
```

- **Source of truth は記事テキスト + frontmatter + wikilink のみ**。inventory / graph は全て derived。
- **graph.json は read-only VIEW**。手編集禁止、`.wiki/.gitignore` 対象、compile 直後に都度再生成。
- **単一責任原則**: inventory.py = parse、graph_gen.py = graph build、lint-wiki.py = constraint check。

### Files to Change

```
skills/wiki/scripts/
  lib/
    __init__.py                       - 新規
    inventory.py                      - 新規: ArticleInventory dataclass + load_inventory()
    test_inventory.py                 - 新規: pure function 単体テスト
    graph_schema.py                   - 新規: GraphNode/GraphEdge dataclass + version 定数
  graph_gen.py                        - 新規: inventory.json → graph.json (CLI + library)
  test_graph_gen.py                   - 新規: 決定性テスト + スキーマ検証
  lint-wiki.py                        - 修正: graph.json を consumer として利用 (dead link / orphan / backlink)
  test_lint_wiki.py                   - 修正: 既存 44 テストを graph 経由でも pass させる

.wiki/
  .gitignore                          - 修正: outputs/graph.json, outputs/inventory.json を追加
  schema/
    inventory-schema.json             - 新規: inventory.json の JSON Schema
    graph-schema.json                 - 新規: graph.json の JSON Schema (version 1.0)

docs/
  status.md                           - 更新
```

### inventory.json スキーマ

```json
{
  "version": "1.0",
  "generated_at": "2026-04-07T18:30:28Z",
  "wiki_root": ".wiki",
  "articles": [
    {
      "slug": "trust-score",
      "path": "concepts/trust-score.md",
      "sha256": "abc123...",
      "title": "Trust Score",
      "category": "concepts",
      "type": "concept",
      "updated": "2026-04-06",
      "tags": ["quality", "metrics"],
      "wikilinks": ["querylog", "gap-detection"],
      "source_refs": ["raw/articles/karpathy-llm-wiki.md"],
      "frontmatter": { /* raw frontmatter dict */ }
    }
  ]
}
```

### graph.json スキーマ (version 1.0)

```json
{
  "version": "1.0",
  "generated_at": "2026-04-07T18:30:28Z",
  "metadata": {
    "node_count": 7,
    "edge_count": 12,
    "source_inventory_sha256": "..."
  },
  "nodes": [
    {
      "id": "trust-score",
      "slug": "trust-score",
      "type": "article",
      "category": "concepts",
      "_custom": {}
    }
  ],
  "edges": [
    {
      "source": "trust-score",
      "target": "querylog",
      "relation_type": "wikilink",
      "weight": 1.0,
      "co_citation_count": 0,
      "co_citation_frequency": 0.0,
      "confidence": 1.0,
      "sources": ["wikilink"],
      "claim_id": null,
      "_custom": {}
    }
  ]
}
```

### Key Points

- **inventory.py は pure**: `parse_articles(wiki_root: Path) -> list[ArticleInventory]` に I/O を分離注入。テストは fixture markdown で完結。
- **graph_gen.py は pure**: `build_graph(inventory: Inventory, querylog: Optional[QueryLog]) -> Graph`。CLI ラッパーが I/O を担当。
- **決定性保証**: dict ordering を保証 (`sort_keys=True`)、article は slug 順、edge は (source, target, relation_type) 順でソート。
- **lint-wiki.py の差分**: `--use-graph` フラグ (default ON) で graph.json 経由、`--no-graph` で旧パス。後方互換。
- **graph.json は無ければ作る**: lint 実行時に inventory/graph が無ければ自動生成 (compile 不要で動く)。
- **querylog optional**: なくても build 可能。あれば co_citation_count を後で埋める準備だけ整える (実検出は Layer 3)。

## ✅ Tests

### inventory.py
- [ ] `parse_article()` が単一記事から正しい ArticleInventory を作る (frontmatter / wikilink / sha256)
- [ ] `parse_articles()` が複数記事を slug 順でソートして返す
- [ ] 同じ入力に対して `to_json()` の SHA256 が二回一致する (決定性)
- [ ] frontmatter 欠損記事もエラーにせず最低限の inventory を生成する
- [ ] wikilink `[[slug]]` / `[[slug|alias]]` 両方を正しく抽出
- [ ] CRLF / LF 違いで sha256 がブレない (改行正規化)

### graph_gen.py
- [ ] `build_graph()` が inventory から nodes / edges を生成する
- [ ] querylog なしでも build 可能 (co_citation_* は 0 で初期化)
- [ ] 決定性テスト: 同じ inventory → 二回 build → SHA256 一致
- [ ] schema version が "1.0"、`_custom` / `claim_id` 予約フィールドが存在
- [ ] CLI: `python graph_gen.py --wiki-root .wiki` で `.wiki/outputs/graph.json` を出力
- [ ] dead link (存在しない slug への wikilink) は edge に含めず metadata.dangling_links に記録

### lint-wiki.py (graph consumer 化)
- [ ] 既存 44 テストが全て pass
- [ ] `--use-graph` モードで dead link / orphan の結果が旧モードと完全一致
- [ ] graph.json が無ければ自動生成して続行
- [ ] backlink 数取得が graph 経由で動く

### Integration
- [ ] `.wiki/` 実データに対して: inventory → graph → lint がエラーなく完走
- [ ] `sha256sum .wiki/outputs/graph.json` を二回比較して一致

## 🔒 Security

- [ ] パストラバーサル防止: `wiki_root` 外の path を inventory に含めない
- [ ] 信頼できない frontmatter (YAML) のパースは `yaml.safe_load` を使う
- [ ] graph.json / inventory.json は `.gitignore` に追加 (cache 的扱い、commit させない)

## 📊 Progress

| Step | Status |
|------|--------|
| Tests (inventory) | ⚪ |
| Implementation (inventory) | ⚪ |
| Tests (graph_gen) | ⚪ |
| Implementation (graph_gen) | ⚪ |
| Lint refactor | ⚪ |
| Integration check | ⚪ |
| Commit | ⚪ |

**Legend:** ⚪ Pending · 🟡 In Progress · 🟢 Done

---

## 🗒 Day 1 朝に確認すべき事項 (team-brainstorm より)

1. `.wiki/outputs/querylog.jsonl` に実データはあるか (なくても MVP は動く)
2. inventory.json スキーマの最終確認 (上記で OK か)
3. graph.json determinism: `sha256sum .wiki/outputs/graph.json` を二回実行して一致確認

## 🚫 Defer (Layer 3 以降)

- SHA256 cache (determinism test 完了後)
- coref-detect / missing edge 検出 (querylog 蓄積 + 偽陽性 3段階フィルタ検証後)
- claim provenance chain
- God node isolation risk
- Leiden clustering
- hyperedge / graph-constraints DSL / graph_check.py

## 📚 References

- 元議論: team-brainstorm セッション Round 1-3 の合意 (本会話内)
- 参考実装: `examples/graphify/{ARCHITECTURE.md, validate.py:1-63, cache.py:77-116, report.py:7-100}`
- 設計原則: `~/develop/claude-skills/rules/design-principles.md` (testability above all)

---

**Next:** Write tests → Implement → Commit with `claude-skills:commit` 🚀

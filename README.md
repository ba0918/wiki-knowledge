# Wiki Knowledge Base

LLM がソースドキュメントを知識ベース（相互参照付き Markdown Wiki）にコンパイル・メンテナンスする Claude Code プラグイン。

[Karpathy の LLM Wiki コンセプト](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)を Claude Skill として実装し、既存プロジェクトに導入可能な形で提供する。

## 4相パイプライン

```
Ingest → Compile → Query → Lint
```

| Phase | コマンド | 説明 |
|-------|---------|------|
| Ingest | `/wiki-ingest` | ソースドキュメントを `raw/` に取り込む（immutable） |
| Compile | `/wiki-compile` | `raw/` → `concepts/` に Wiki 記事を生成（Backlink Audit 付き） |
| Query | `/wiki-query` | Wiki の知識に基づいて質問に回答・記事に昇格 |
| Lint | `/wiki-lint` | 品質チェック（dead link, orphan, 矛盾検出等） |

その他:

| コマンド | 説明 |
|---------|------|
| `/wiki-init` | プロジェクトに Wiki 構造をブートストラップ |
| `/wiki-cycle` | Ingest → Compile → Graph Gen → Lint を一括実行 |
| `/wiki` | サブコマンド一覧・ルーティング |

## インストール

```bash
claude mcp add wiki -- /path/to/wiki-knowladge
```

## 使い方

### 1. Wiki を初期化

```
/wiki-init
```

プロジェクトに `.wiki/` ディレクトリと `CLAUDE.md` の `wiki_root` 設定が作られる。

### 2. ソースを取り込む

```
/wiki-ingest path/to/article.md
```

セキュリティチェック（機密データスキャン、プロンプトインジェクション検出）後、`raw/` に保存。

### 3. Wiki 記事を生成

```
/wiki-compile
```

未コンパイルのソースを自動検出して記事を生成。`[[wikilink]]` による相互参照と Backlink Audit を自動実行。

### 4. Wiki に質問する

```
/wiki-query なぜ Ingest と Compile を分離するのか？
```

Wiki の記事を情報源として回答を合成。品質が高ければ `concepts/` に昇格可能。

### 5. 品質チェック

```
/wiki-lint
```

自動チェック（dead link, orphan, missing source）+ LLM 駆動チェック（矛盾, 陳腐化, カバレッジギャップ）。

## Wiki ディレクトリ構造

```
.wiki/
├── raw/                  # immutable なソースドキュメント
│   ├── articles/         # Web 記事、ブログ、論文
│   └── files/            # ローカルファイル、PDF 等
├── concepts/             # LLM 生成 Wiki 記事（相互参照付き）
├── outputs/
│   ├── queries/          # Query 回答
│   └── reports/          # Lint レポート
├── schema/
│   ├── page-template.json
│   └── categories.json
├── index.md              # 全ページカタログ
└── log.md                # 操作ログ（append-only）
```

## 設計思想

- **Ingest/Compile 分離**: raw/ は immutable。バッチ取り込み後に一括コンパイル可能
- **Backlink Audit 必須**: Compile 時に既存記事を走査して双方向リンクを追加。skip すると Wiki が blog に退化する
- **Query → Wiki Promote**: 回答を記事に昇格させることで知識の複利的成長を実現
- **セキュリティ**: Ingest 時の入力汚染検出 + 機密データマスキング
- **明示的呼び出し**: `/wiki-*` スラッシュコマンド方式（description ワードトリガーに依存しない）

## ロードマップ

| Phase | 内容 |
|-------|------|
| **0-1** | MVP + 4相パイプライン + スキル登録 (**完了**) |
| **2** | QueryLog 蓄積 + Gap Detection + Auto Ingest 提案 |
| **3** | Trust Score + Lint 強化 |
| **4** | Multi-Resolution + Intent Detection |
| **5** | Portal Adapter + Self-Healing Adapter |

## License

MIT

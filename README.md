# Wiki Knowledge Base

LLM がソースドキュメントを知識ベース（相互参照付き Markdown Wiki）にコンパイルし、メンテナンスする Claude Code プラグイン。

[Karpathy の LLM Wiki コンセプト](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)を Claude Skill として実装し、既存プロジェクトに導入可能な形で提供する。

知識を貯めるだけでなく、貯めた知識を使う入口も持つ。
`/wiki-query` は Wiki を情報源として質問に答え、`/wiki-tool-query` は Wiki に蓄積した抽出手順（Selection Recipe）を使って、登録済みの外部データソースから承認付きでデータを取り出す。

## インストール

Claude Code プラグインとしてインストールする（MCP サーバではない）。

```
# Claude Code 内で marketplace を登録（GitHub リポジトリ）
/plugin marketplace add ba0918/wiki-knowladge

# プラグインをインストール（plugin名@marketplace名）
/plugin install wiki@wiki-knowladge
```

ローカルクローンから登録する場合は `/plugin marketplace add /path/to/wiki-knowladge`。

## スキル一覧

| スキル | 役割 |
|--------|------|
| `/wiki-init` | プロジェクトに Wiki 構造をブートストラップ |
| `/wiki-ingest` | ソース（ファイル、URL、git リポジトリ）を `raw/` に取り込む |
| `/wiki-compile` | `raw/` から `concepts/` に Wiki 記事を生成。ソースコードからのドメイン知識抽出（discover）もここで行う |
| `/wiki-query` | Wiki の知識に基づいて質問に回答し、良質な回答を記事に昇格する |
| `/wiki-lint` | 品質チェック（dead link、orphan、矛盾検出など 10 項目 + Trust Score + Gap Detection） |
| `/wiki-cycle` | Ingest から Lint までを一括実行するオーケストレータ |
| `/wiki-tool-query` | catalog 登録済みデータソースへの承認付きアドホック集計 |

## 使い方

### Wiki を育てる（Ingest → Compile → Lint）

**1. 初期化**

```
/wiki-init
```

プロジェクトに `.wiki/` ディレクトリと `AGENTS.md` の `wiki_root` 設定が作られる。

**2. ソースを取り込む**

```
/wiki-ingest path/to/article.md
```

セキュリティチェック（機密データスキャン、プロンプトインジェクション検出）を通過したソースが `raw/` に immutable に保存される。

git リポジトリも取り込める（複数一括可）。

```
/wiki-ingest https://github.com/owner/repo https://gitlab.example.com/team/api
```

clone キャッシュは `{wiki_root}/.cache/repos/` に置かれ、`rm -rf` で安全に削除できる（`ghq` 管理下の clone は削除しない）。

**3. 記事を生成する**

```
/wiki-compile
```

未コンパイルのソースを自動検出して記事を生成する。
`[[wikilink]]` による相互参照と Backlink Audit を自動実行する。

repo ingest 済みのリポジトリからは、ソースコードを読んでドメイン知識（アーキテクチャ、DB スキーマ、業務ルールなど）を記事化する **discover** モードも使える。

```
/wiki-compile discover
```

**4. 品質を保つ**

```
/wiki-lint
```

自動チェック 10 項目（dead link、orphan、missing source、format violations など）に加えて、記事ごとの信頼度を測る **Trust Score** と、質問されたのに記事がないトピックを検出する **Gap Detection** を実行する。

**5. 一括実行**

```
/wiki-cycle
```

Ingest → Compile → Graph 生成 → Lint を一気に回す。

### Wiki に質問する

```
/wiki-query なぜ Ingest と Compile を分離するのか？
```

retrieval pre-pass（リンクグラフの 1 ホップ展開 + Trust Score 注釈）で候補記事を絞り、Wiki の記事を情報源として出典付きで回答を合成する。
質問メタデータは QueryLog に蓄積され、Gap Detection の入力になる。
品質が高い回答は `concepts/` に昇格できる。

### データ抽出を任せる（wiki-tool-query）

「イベント補填の対象者を抽出して」のような突発のデータ抽出依頼を、Wiki の知識と外部データソースへのアクセスを組み合わせて処理する。
LLM が抽出計画を組み、人間が承認してから実行する（自由な質問 + 制約された実行）。

対象は LLM からアクセスできるデータソース全般を想定している。
DB に限らず、API、管理ツール、Kibana や Redash のような分析ツールでも、データが取り出せる手段なら catalog に登録して使えるようにするのがゴールである。
現在の実装（Phase A）は sqlite connector のみで、接続手段は Connector protocol として固定してあるため、他のデータソースは adapter の追加で対応していく。

事前準備は次の 2 つ。

- `{wiki_root}/tools/catalog.json` に接続先を登録する（git 管理。接続先、テーブル allowlist、行数上限などの実行契約はここが真実源）
- 認証情報が必要な接続先は `{wiki_root}/.local/credentials.json` に置く（git 管理外。sqlite は不要）

実行フローは 3 段階に分かれる。

```
prepare（dry-run） → approve（人間が実行） → execute
```

1. **prepare**: LLM が Selection Recipe 記事を参照して SQL を組み、選定ファネル（条件を 1 段ずつ足した件数の推移）と想定件数レンジを提示する
2. **approve**: 内容を確認した人間が承認コマンドを実行する。LLM は代行しない
3. **execute**: 承認内容と SQL の一致を digest で検証してから実行し、結果 CSV と検証マニフェスト（件数、重複、NULL）を指定ディレクトリに引き渡す

安全設計の要点:

- **read-only の三重防御**: read-only 接続 + `PRAGMA query_only` + SQLite authorizer によるテーブル allowlist 照合
- **single-use 承認**: 1 回の承認で実行できるのは 1 回だけ。承認後に SQL や配送先を変更すると digest 不一致で拒否される
- **結果データ非保持**: 結果は delivery 先に引き渡して破棄する。監査ログには値を含まないメタデータだけが残る

案件が終わったら、判断や除外条件を **Selection Recipe** 記事として Wiki に残す。
次に同種の依頼が来たとき、LLM は Recipe を読むだけで同じ品質の抽出を再現できる。
手を動かした記録が集合知に変わっていくのが、このスキルと Wiki を同居させる理由である。

## Wiki ディレクトリ構造

```
.wiki/
├── raw/                       # immutable なソースドキュメント
│   ├── articles/              # Web 記事、ブログ、論文
│   └── files/                 # ローカルファイル、repo inventory 等
├── concepts/                  # LLM 生成 Wiki 記事（相互参照付き）
├── tools/
│   └── catalog.json           # tool-query の接続先定義（実行契約の真実源）
├── outputs/
│   ├── queries/               # Query 回答
│   ├── reports/               # Lint / Trust Score / Gap Detection レポート
│   ├── graph.json             # リンクグラフ（derived、lint と query が消費）
│   ├── querylog.jsonl         # クエリメタデータログ（git 管理外）
│   ├── toolquery-plans/       # tool-query の proposal bundle（git 管理外）
│   └── toolquery-audit.jsonl  # tool-query 監査ログ（git 管理外）
├── schema/                    # page-template / categories / querylog / tool-catalog
├── .cache/                    # repo ingest の clone とマニフェスト（git 管理外）
├── .local/                    # 認証情報（git 管理外）
├── index.md                   # 全ページカタログ
└── log.md                     # 操作ログ（append-only）
```

## 設計思想

- **Ingest/Compile 分離**: raw/ は immutable。バッチ取り込み後に一括コンパイルできる
- **Backlink Audit 必須**: Compile 時に既存記事を走査して双方向リンクを追加する。省略すると Wiki が blog に退化する
- **Query → Wiki Promote**: 回答を記事に昇格させることで知識が複利的に成長する
- **derived layer**: リンクグラフ、Trust Score、Gap Detection は記事から再導出できる派生物として扱い、フロントマターには保存しない
- **スクリプトが真実源**: JSONL 追記、schema 検証、セキュリティチェック、SQL 実行の enforcement は Python スクリプトが担い、LLM は構造化データを手組みしない
- **安全境界は git 管理ファイルに置く**: tool-query の実行契約は catalog.json にあり、Wiki 記事の編集では変えられない
- **明示的呼び出し**: `/wiki-*` スラッシュコマンド方式（description ワードトリガーに依存しない）

## ロードマップ

| Phase | 内容 | 状態 |
|-------|------|------|
| 0-1 | MVP + 4相パイプライン + スキル登録 | 完了 |
| 2 | QueryLog 蓄積 + Gap Detection + Auto Ingest 提案 | 完了 |
| 3 | Trust Score + Lint 強化 | 完了 |
| repo ingest / discover | git リポジトリ取り込みとドメイン知識抽出 | 完了 |
| tool-query Phase A | 承認付きアドホック集計（sqlite） | 完了 |
| tool-query Phase B | doctor 診断、コネクタ追加（postgres、API、管理ツール等）、監査 reconcile | 予定 |
| 4-5 | Multi-Resolution / Portal Adapter 等 | 保留 |

## License

MIT

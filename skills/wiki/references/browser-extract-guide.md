# Browser Extract ガイド — 設計裁定・登録時壁打ち・tier 判定

wiki-browser-extract 系統（browser-extract 別系統）の設計リファレンス兼裁定書。
実行契約の真実源は git 管理の **catalog**（`{wiki_root}/tools/catalog.json` の
`type: browser` エントリ、schema: `{wiki_root}/schema/browser-extract-catalog-schema.json`）
と **固定フローコード**（`{wiki_root}/tools/flows/{tool_id}.py`、PR レビュー必須）。
Wiki 記事（Selection Recipe）は説明層であり、自然言語編集ではこの安全境界を変更できない。

このドキュメントは実装ステップ（Step 2〜7）が参照する「決まっていること」を固定する。
Open Question を残さない — 曖昧なまま実装に入ると誤成功（正しく見えるが違うデータ）を招く。

---

## 0. 最重要裁定 — seal-at-prepare の承認完全性（監査アンカー照合）

refine フェーズで最後まで WARN が残った箇所。ここを最初に固定する。

### 問題

seal-at-prepare モデルでは、prepare が認証済みセッションで抽出を完了し、成果物 +
manifest を隔離 bundle（spool = `outputs/browser-plans/{plan_id}/`）に封印する。
approve は人間が TTY で成果物プレビューを見て承認する。**spool はエージェント可読
かつ書込み可能なローカルファイル**であるため、prepare 後〜approve 前に spool 内の
artifact / manifest / プレビューを整合的に書き換えると「人間が承認済みの搬出」を
偽造できてしまう。

### 裁定

approve の TTY 表示は **prepare 時に保存されたプレビューファイルを一切信用しない**。
表示時に以下を行う:

1. **封印 artifact 本体からハッシュとプレビュー（先頭 N 行）を再計算**する
2. 再導出ハッシュを **spool 外のアンカー = `prepared` 監査イベントに記録済みの封印
   ハッシュと fail-closed 比較**する
   - 照合スコープは **artifact ハッシュ + manifest ハッシュの両方**
   - `prepared` 監査イベントに `artifact_digest`（result 本体の SHA-256）と
     `manifest_digest`（manifest.json bytes の SHA-256）を記録する
3. **不一致は「表示への反映」ではなく「拒否」**（exit 1、`readback_mismatch` 相当の
   専用 reason `seal_mismatch`）
   - manifest の中身（件数・anchor 照合結果）は prepare 時の live ページに対する
     実行事実であり artifact からは再導出できない。その完全性を担保するのは
     **この監査アンカー照合だけ**。したがって manifest 改変も「拒否」のみが合格実装。
     偽造 anchor 結果を「検証済みの顔」で表示に反映する実装は**不合格**
4. 一致した場合のみ表示し、`approved` 監査イベントにも pin したハッシュを記録する
5. execute は承認レコード（state.json の `sealed_artifact_digest` /
   `sealed_manifest_digest`）と `prepared` 監査アンカーの**両方**に照合してから
   delivery を解放する

これにより「prepare 後に artifact + manifest を整合的に書き換えて人間承認済み搬出を
成立させる」経路を消す。

### 受容する残余（既知の限界）

監査 JSONL（`browser-audit.jsonl`）自体もエージェント可読環境にある。攻撃者が
`prepared` イベントの digest も書き換えれば照合を通せる。本裁定は
**「攻撃者は監査履歴も書き換えねばならない」まで bar を上げる**に留まる。これは既存
tool-query family が受容済みの残余（監査ログの可書性）と同格であり、guide の
「既知の限界」節に記載する。OS 層の append-only / 別ホスト集約は将来オプション。

### 信頼境界の honest scoping（交換条件）

SQL 系（approve-then-execute）と異なり、**seal-at-prepare の人間承認はデータアクセスを
門番しない**。prepare は承認前に認証済みセッションで抽出を完了し、封印 bundle は
エージェント可読なローカルファイルになる。approve が守るのは **delivery（マシン外への
搬出）のみ**で、機密性境界は「そのマシン」に後退する。

- 棄却代替: **二段階承認**（承認 → 抽出 → 再承認）= 摩擦倍増、かつ承認材料が実データに
  ならない一段目は形骸化する
- 棄却代替: **approve-then-execute**（SQL 系流用）= 承認材料が実抽出データにならず、
  prepare→execute 間（最大 24h）にデータ・権限・UI・session が変わる TOCTOU が再発する
- 採用理由: 承認対象 = 配布物の同一性をハッシュで固定することで「人間が見たものと
  出て行くものが常にバイト同一」を保証する。B2 の「人手確認必須」も approve が
  その確認点になり、確認前 publish が構造的に起きない

安価なガード（seal-at-prepare の帰結を補強）:

- `prepared` を **row_count 付きの第一級監査イベント**にする（未承認でも抽出事実が残る）
- tool ごとの **未承認 bundle 数上限**（catalog `limits.max_unapproved_bundles`）を設け、
  超過時は prepare を拒否する
- 未承認 bundle も **TTL janitor の回収対象**に含める（放置された機密 spool を残さない）

この裁定は tier 保証マトリクス（§5）と approve TTY 文言（§10）にも明示する。

---

## 1. 系統分離の方針

既存 tool-query に `type: browser` として統合**しない**。保証水準が異なる:

- SQL 系 = 静的検査 + DB role で **機械保証**
- browser 系 = 封じ込め + 証跡（**強制ではなく honest scoping**）

**catalog スキーマ規約と監査 JSONL 形式だけ共有する別系統**として実装する。
共有可能な service 層（承認 bundle・single-use consume・TTL・delivery・パス封じ込め・
credential 解決）は既存モジュールを import で再利用し、二重実装しない（§11 の再利用境界）。

---

## 2. enforcement 機構の裁定 — なぜ制限 DSL でなく capability API + AST ゲートか

### 保証水準の言明（honest scoping）

固定フローコードは in-process Python である。**Python に対する構造的封じ込めは達成不能**
（`().__class__.__bases__[0].__subclasses__()` 等の言語機構は塞ぎ切れない）。したがって
**悪意フローへの構造的境界は主張しない**。担保は次の3層による**事故防止とレビュー支援**:

1. **catalog の SHA-256 pin**（不一致 = 実行拒否）
   - catalog の browser エントリは `flow.sha256` でフローファイルの内容ハッシュを宣言する
   - runner はロード時にフローファイルを読み、ハッシュ照合する。不一致は `flow_pin_mismatch`
     で実行拒否。「未追跡コード拒否」はこの hash 不一致の言い換えであり、**実行時に git を
     照会しない**（git 非依存。ハッシュだけが真実源）
2. **ロード時 AST 静的ゲート**（§3 で許可文法を定義）
   - `import` / `from import` 文、`exec` / `eval` 呼び出し、dunder 属性アクセス
     （`__globals__` 等）を拒否する
3. **PR レビュー**（フロー・検証契約の変更は必ず人間レビューを経る）

### 棄却した代替

- **record/replay**: 記録した操作列を再生する方式。動的 DOM（loading・virtualized）で
  脆く、パラメータ注入の型付けができず、誤成功検出の語彙を持てない
- **制限 DSL**（独自のフロー記述言語）: 表現力と実装コストのトレードオフが最悪。
  pagination / 条件分岐のような実ツールで必要な制御を DSL に足すたびに DSL 自体が
  第2のプログラミング言語に肥大化し、その parser/evaluator が新たな攻撃面になる。
  Python の AST ゲートで許可文法を絞る方が、既存の型システム・locator API を再利用でき、
  レビュー可能性も高い
- **flow 同居 assertion callback**（フロー内に検証ロジックを書かせる）: フローと検証を
  同一 LLM が書くと同じ誤解を両方に埋め込む（§共通原因故障）。検証は閉じた語彙 + 別主体
  レビューに分離する

### 結論

**フロー = capability API（型付き操作面）に対してのみ書く / 検証 = 閉じた語彙**。
フローは raw Playwright を触らない。capability 集合と検証語彙の追加は、いずれも検証語彙と
同じ PR 運用（connector 追加と同格）。

---

## 3. capability API v1（閉集合）と AST 許可ノード集合

### capability API v1（`browser_flow_runner.FlowContext` が提供）

フローは `def run(ctx, params)` の単一関数として書く。`ctx` は以下の型付きメソッドのみを持つ:

| capability | 意味 | origin/param の扱い |
|---|---|---|
| `ctx.goto(route_id, **path_params)` | catalog 宣言の named route へ遷移 | origin は常に catalog、path_params は canonicalize して埋め込む。`page.goto(param)` 直結は不可 |
| `ctx.get_by_role(role, name=None, exact=False)` | role + accessible name で locator 取得 | name は値バインディング（文字列補間なし） |
| `ctx.get_by_label(text)` / `ctx.get_by_text(text)` | ラベル / テキストで locator 取得 | 同上 |
| `ctx.fill(locator, value)` | 入力欄に値を入れる | value は params 由来の検証済み値 |
| `ctx.click(locator, *, role, name)` | click。role + accessible name の複合条件を必須引数にする | セレクタ単独 click は不可（§7 破壊的操作抑止） |
| `ctx.wait_stable(predicate)` | stability predicate（§4）が満たされるまで待つ | 素の sleep は提供しない |
| `ctx.read_text(locator)` | locator のテキストを読む（readback 用） | 抽出値は未信頼バイト |
| `ctx.download(trigger_locator, *, role, name)` | click → download を捕捉。runner 生成ランダム名で保存 | サーバー指定 filename は使わない（§retention） |
| `ctx.expect_row_count(locator)` | 行数を数える（row_count_range 用） | — |

capability は v1 の閉集合。追加は PR 運用。フローは制御フロー（`if` / `for`）を書けるが、
反復（pagination 等）は capability プリミティブ側（`ctx.paginate(...)` を将来追加）に
寄せる方針とし、v1 では単純 `for` を許可する（§AST）。

### AST 許可ノード集合（`browser_flow_runner` のロード時ゲート）

フローファイルをロードする前に `ast.parse` して、**許可リスト方式**でノードを検査する。
許可外ノードが1つでもあれば `flow_ast_violation` で拒否（ロード時、ブラウザ非依存）。

**許可するノード**（このリストと `_ALLOWED_AST_NODES` を同期させ、テストで機械検証する）:

- モジュール構造: `Module`、単一の `FunctionDef`（名前 `run`、引数 `ctx, params`）
- 文: `Assign`、`AnnAssign`、`AugAssign`、`Expr`、`Return`、`Pass`、
  `If`、`For`、`While`、`Break`、`Continue`、`With`（`ctx` の context manager 用）
- 式: `Call`、`Attribute`（ただし dunder 名は拒否）、`Name`、`Constant`、
  `Compare`、`BoolOp`、`UnaryOp`、`BinOp`、`Subscript`、`Index`、
  `List`、`Tuple`、`Dict`、`Set`、`keyword`、`Starred`、`Slice`、
  各種比較 / 演算子ノード、`comprehension`（内包表記）
- 引数系: `arguments`、`arg`、`Load`/`Store`/`Del` context

**明示的に拒否するノード**（除外を negative test と対にする）:

- `Import` / `ImportFrom`（モジュール取り込み全面禁止 — capability は引数の `ctx` 経由のみ）
- `FunctionDef` の入れ子 / `Lambda` / `AsyncFunctionDef`（フローは単一 `run` のみ）
- `ClassDef`
- `Global` / `Nonlocal`
- `Attribute` のうち属性名が `__` で始まり `__` で終わる dunder（`__globals__` /
  `__class__` / `__subclasses__` 等の言語機構アクセス）
- `exec` / `eval` / `compile` / `__import__` / `open` / `getattr` / `setattr` への
  `Call`（名前ベースの callee 拒否リスト）

AST ゲートは「悪意コードを完全に防ぐ」ものではない（honest scoping）。dunder 拒否と
import 拒否で**明白な脱出経路と事故**を塞ぎ、レビューを支援する層である。

---

## 4. 検証語彙 v1（閉集合）

決め打ち不可能なのは**組み合わせ**であって語彙ではない。語彙に無い検証はエンジンへの
PR で追加（= connector 追加と同じ運用）。未知語彙は catalog-validate で **fail-closed 拒否**。

### 正しさ（誤成功検出）

| check | 意味 | 役割 |
|---|---|---|
| `filter_readback` | UI 上のフィルタ表示（期間・条件）を読み戻し、params と一致するか照合 | フィルタ未反映の誤成功を検出 |
| `row_count_range` | 抽出行数が期待レンジ内か（`{min, max}`） | 部分取得・pagination 欠落を検出 |
| `selector_exists` | 指定 locator が実在するか（doctor smoke でも使う） | UI ドリフトを検出 |

### 独立 anchor（B1 の必須要件、§5）

セレクタと同一の DOM 解釈に依存しない独立 oracle。以下のいずれかを1つ以上:

| check | 意味 |
|---|---|
| `export_metadata_match` | export ファイル内メタデータ（生成期間・filter）と params の照合 |
| `ui_total_vs_file_rows` | UI に表示された total 件数と export ファイルの行数の照合 |
| `tenant_id_match` | 抽出データ内の tenant / account ID が catalog 宣言の account と一致 |
| `primary_key_unique` | 主キー列に重複がない（部分結合・二重取得の検出） |

### 完全性（改ざん検出）

| check | 意味 |
|---|---|
| `artifact_hash` | bundle→delivery 間の artifact 改ざん検出。**動的データに既知の基準ハッシュは存在しない**ため、セレクタずれの検出は担わない（完全性のみ） |

### identity（画面同一性）

| check | 意味 |
|---|---|
| `screen_fingerprint` | Playwright の accessibility / DOM snapshot ベースの指紋。別 tenant の同型画面を検出。**bespoke なピクセルハッシュは採らない**（描画差分で偽陽性が出る） |

### 安定化語彙（stability predicate — 非決定的 DOM 対策）

「決定的」なのは判定規則であって DOM ではない。loading 中・旧 DOM・virtualized table を
正常と誤認しないよう、以下を `ctx.wait_stable(...)` の predicate として提供する。
**素の sleep は禁止**:

| predicate | 意味 |
|---|---|
| `navigation_settled` | navigation 完了（`networkidle` 相当 + URL 確定） |
| `loading_indicator_gone` | 指定 loading indicator locator が消滅 |
| `readback_stable` | 指定 locator のテキストが N 回連続一致（値の揺れ収束） |
| `row_count_settled` | 行数が一定ウィンドウ内で不変 |

locale / timezone / viewport は context 生成時に固定する（§7）。

### 語彙の役割分担（まとめ）

- `filter_readback` / `row_count_range` + 独立 anchor = **正しさ**（誤成功検出）
- `artifact_hash` = **完全性**（改ざん検出。正しさは担わない）
- `screen_fingerprint` = **identity**（別画面検出）

---

## 5. tier 分類と保証マトリクス

「B1 = 高保証」の単一ラベルは使わない。tier ごとに保証有無を **機械可読マトリクス**で
schema に持たせ、manifest・監査にも未保証事項を出力する。

| tier | 定義 | integrity | identity | filter correctness | completeness | human verification |
|---|---|---|---|---|---|---|
| **B1** | TSV/CSV export あり + 独立 anchor 1つ以上 | ○ (artifact_hash) | ○ (screen_fingerprint) | ○ (filter_readback) | ○ (row_count_range + anchor) | approve |
| **B2** | DOM 抽出（完全性保証なし） | △ | ○ | ○ | ✗（silent truncation 検出のみ、保証なし・人手確認必須） | approve（必須の確認点） |
| **B3** | OCR | — | — | — | — | v1 対象外（tier 定義のみ） |

- **B1 の必須要件**: 独立 anchor（`export_metadata_match` / `ui_total_vs_file_rows` /
  `tenant_id_match` / `primary_key_unique` 等）を検証契約に**最低1つ**含むこと。
  独立 oracle を構成できないツールは B1 を名乗れない（B2 に降格）。この要件は
  catalog-validate で機械強制する（Step 4）
- **専用最小権限アカウントは B1/B2 登録の前提条件**（Open Question ではない）— 不要な
  書込み権限を持たないアカウントで登録する
- **http 還元ゲートを先に必須で通す**（§14）

---

## 6. auth profile と session state 束縛

| profile | 意味 | secret |
|---|---|---|
| `none` | 認証不要 | — |
| `form` | フォームログイン | credentials.json |
| `form+totp` | フォーム + TOTP | credentials.json（TOTP secret） |
| `human-assisted` | 人間ログイン → session state 引き継ぎ | `login` サブコマンドで捕捉 |

- session state は credential と同格の封じ込め（0600・TTL・再認証ポリシー）
- **tool / origin / account に束縛**する: state ファイルに束縛メタデータ
  （`tool_id` / `origin` / `account`）を持たせ、実行時に catalog 宣言と照合する。
  汎用ブラウザ profile の持込み・profile の tool 間共有は**禁止**（束縛不一致は
  `session_binding_mismatch` で拒否）
- 書込みは 0600 atomic（O_NOFOLLOW / umask）。Playwright デフォルト（0644 平文 JSON）で
  書かせない

### login 実行時の allowlist 面（裁定）

SSO / IdP / captcha / CDN を跨ぐ実サイトのログインは method + path 粒度の allowlist では
壊れる（IdP の origin は事前に列挙し切れない）。裁定: **auth profile 側で catalog 明示の
宣言拡張を許す** — `auth.login_origins`（ログイン中のみ有効な追加 origin allowlist）を
schema 概念として持たせる。抽出フェーズの allowlist（`origin_allowlist`）とは分離し、
`login` サブコマンドと `form` login 手続き中のみ `login_origins` を併用する。
v1 のローカル fixture では顕在化しないが、schema には先に持たせる（Step 3）。

---

## 7. 封じ込めモデル（interception の具体仕様）

「宣言外通信ブロック」は `page.route()` の素朴適用では成立しない。以下を実装仕様として固定:

- **第一防御はフローコード規約**: navigation の origin は常に catalog 宣言（named route）
  から構成し、パラメータは検証済み path / 値のみを供給する。`ctx.goto(param)` の
  param→origin 直結は capability API が構造的に禁止。**interception は第二防御線**
- **interception は context スコープ**: `context.route('**/*', ...)`（page スコープでは
  popup / 新規タブを取り逃がす）。全リクエストを catalog allowlist と照合、宣言外は
  abort + 監査記録（`origin_blocked`）
- **allowlist の粒度**: origin 単位でなく **method + path prefix + resource type** まで
  狭める。照合前に URL を正規化（userinfo 拒否・IDN/punycode 正規化・末尾ドット除去・
  port 明示化・encoded separator 拒否）。canonicalize は http connector の
  `_canonicalize_segments` / origin 正規化の流儀を流用
- **同一 origin 内の破壊的操作の抑止**: 状態変更系リクエスト（POST 等）は login / TOTP /
  export job 作成など catalog に明示宣言されたものだけ許可。click 対象は role +
  accessible name の複合条件で確認（`ctx.click` の必須引数）。加えて専用アカウントから
  書込み権限を剥がすのが第一防御
- **service worker は無効化**: context 生成時 `service_workers='block'`
- **WebSocket は deny-by-default**: `route_web_socket` で全 WS 拒否（v1 対象ツールに
  WS 必須のものは入れない）
- **redirect は各ホップを再検証**: リダイレクト先 URL も origin allowlist で照合、宣言外は
  abort（全面拒否は非現実的なため hop 単位の再検証）
- **`data:` / `blob:` への navigation は拒否**（ネットワークリクエストを発生させず
  interception が発火しないため）
- **WebRTC は launch args で無効化**: `--webrtc-ip-handling-policy=disable_non_proxied_udp`
  等。塞ぎ切れない残余は DNS rebinding と同格の既知の限界（§既知の限界）
- **launch profile の隔離**: headless + 実行ごとの ephemeral user-data-dir + remote
  debugging port なし + 実行ごとの fresh context。唯一の例外は `login` サブコマンド
  （headed だが抽出・delivery は構造的に不可能）
- **context 固定**: locale / timezone / viewport を context 生成時に固定（DOM 非決定性の
  抑制）

---

## 8. パラメータ注入の安全規約

「JSON Schema 検証 + エスケープ」だけではセレクタ注入（敵対的に誘発された誤成功）を防げない:

- **セレクタへの文字列補間は禁止**: フローはパラメータを locator の値バインディング
  （`ctx.get_by_role(name=...)` / `.filter(has_text=...)` 相当）でのみ使用する。
  XPath / CSS 文字列への `f-string` 埋め込みは登録レビューで機械的に reject
  （レビューチェックリスト項目 + AST ゲートの補助）
- **params_schema は値ごとに厳格制約を必須化**: enum / pattern / maxLength のいずれかを
  各パラメータに要求（自由文字列を既定で許さない）。meta-schema
  （`browser-extract-params-schema.json`）で強制。selector / JS / 任意 URL / 任意 path を
  型として提供しない
- **URL への埋め込み**: origin は catalog、パラメータは path segment / query 値として
  canonicalize（http connector の encoded-separator 拒否・二重 encoding fail-closed を再利用）

---

## 9. seal-at-prepare 状態機械

### 遷移表

```
prepared(sealed) → approved → delivering → delivered
       │                          │
       │                          └──(不明失敗, hash 照合可)──> delivering（再開）
       │                          └──(不明失敗, hash 照合不可)─> failed（再 prepare が必要）
       ├──(TTL 超過)──────────────────────────────────────────> expired（janitor 回収）
       └──(未承認のまま TTL 超過)────────────────────────────> expired（janitor 回収）
```

| status | 意味 | 次遷移 |
|---|---|---|
| `prepared` | 抽出完了・封印済み（`sealed_artifact_digest` / `sealed_manifest_digest` を持つ） | approve で `approved` へ |
| `approved` | 人間承認済み（pin ハッシュを記録） | execute で `delivering` へ |
| `delivering` | delivery 実行中（CAS で永続化） | 成功で `delivered`、不明失敗で条件付き再開 or `failed` |
| `delivered` | delivery 完了（terminal） | — |
| `failed` | 復旧不能（terminal、再 prepare が必要） | — |
| `expired` | TTL 超過（terminal、janitor 回収対象） | — |

- 遷移は **CAS で永続化**（既存 state.json durable write の流儀）
- **delivery 途中の不明失敗は自動 retry しない**。封印済み成果物のハッシュ照合が
  取れた場合のみ `delivering` を再開できる（取れなければ `failed` → 再 prepare）
- `prepared` は **row_count 付きの第一級監査イベント**（未承認でも抽出事実が残る）

### 承認は single-use・TTL 24h

approve は single-use（consumed = 承認の消費）、TTL 24h。既存 tool-query の
`consume_transition` / `is_expired` / `compute_expires_at` を再利用する（§11）。

---

## 10. approve TTY プロンプトと reason code

### 承認材料（TTY 表示、LLM 代行禁止）

approve は §0 の再導出 + 監査アンカー照合を通した上で、以下を human に提示する:

- どの identity / live session で取得したか
- **read-only は非強制であること**の明示
- **承認は配布のみを制御し抽出は完了済みであること**の明示
- 封印済み成果物のハッシュ（再導出値）とプレビュー（先頭 N 行 + 列→抽出元マッピング）
- 件数 + 独立 anchor の照合結果
- **封印時刻（manifest の `extracted_at`）・経過時間・TTL 残**
- 途中失敗時は再 prepare になること

### プレビュー描画規則（未信頼バイトとして扱う）

抽出データは未信頼バイト。承認プロンプト自体を偽装させない:

- 非印字文字・ESC はエスケープ表示（端末エスケープ注入対策）
- East Asian width を考慮した幅認識クリップ
- 行/列の明示的 truncation マーカー
- 先頭 N 行のデフォルト値（既定 `PREVIEW_ROWS = 10`）

### browser reason code + hint 表（what / why / next）

各 reason は「何が起きたか / なぜ / 次に何をするか」を持つ。**位置情報の方針**:
step index・capability 名・check id は git 管理フロー / catalog 由来の識別子であり
sanitize 不変条件を破らないため添付可。実行時値（URL・セレクタ値・DOM）は**不可**。

| reason code | what | why | next |
|---|---|---|---|
| `selector_not_found` | 期待する locator が見つからない | UI 変更 or フロー誤り | doctor 実行 → フロー修正 PR |
| `ui_drift` | 画面構造が doctor 基準から乖離 | UI 変更 | doctor 実行 → フロー修正 PR |
| `session_expired` | session state が失効 | TTL 超過 or サーバー側失効 | 再認証（`login` or form） |
| `session_binding_mismatch` | session が tool/origin/account と不一致 | 別 profile の持込み | 正しい session で再取得 |
| `origin_blocked` | 宣言外 origin/method/path へのリクエスト | フロー誤り or 攻撃 | フロー修正 PR / allowlist 見直し PR |
| `readback_mismatch` | filter_readback が params と不一致 | フィルタ未反映 | フロー修正 PR / パラメータ確認 |
| `seal_mismatch` | 再導出ハッシュが監査アンカーと不一致 | prepare 後の bundle 改変 | 再 prepare（承認しない） |
| `flow_timeout` | hard wall-clock timeout 超過 | 遅延 or 無限待ち | フロー修正 PR / timeout 見直し |
| `bundle_cap_exceeded` | 未承認 bundle 数が上限超過 | 承認滞留 | 未承認 bundle を approve or 失効させる |
| `flow_pin_mismatch` | フローの SHA-256 が catalog 宣言と不一致 | 未追跡コード | catalog 更新 PR / フロー復元 |
| `flow_ast_violation` | AST ゲート違反（import/exec/dunder 等） | 禁止構文 | フロー修正 PR |
| `internal_error` | 分類不能の catch-all | 予期しない例外 | ログ確認・issue 化 |

reason code ごとにこの表の hint を CLI 出力に配線する（Step 6）。

### login の完了検知と検証

- 完了シグナル: post-login URL 検知 or セレクタ検知 + **TTY Enter 待ちフォールバック** +
  タイムアウト
- 捕捉直後に doctor のログイン疎通チェック相当で state の有効性を検証（未認証のまま捕捉して
  後日 `session_expired` で遅延顕在化させない）
- 成功時は束縛メタデータ（tool / origin / account）と TTL を human に表示する

---

## 11. service 層の再利用境界（シンボル粒度）

モジュール一括の「再利用/再実装」二分では境界が引けない。以下のとおり裁定する:

### そのまま import

- `tool_delivery`（CSV 無害化 / staging-publish）
- `tool_paths`（symlink 拒否パス封じ込め）
- `tool_catalog.load_credential`（wiki_root + ref のみで SQL 非結合。catalog パーサ本体は
  流用しない）
- `lib/domain/tool_query` の純粋述語（`consume_transition` / `approve_transition` /
  `is_expired` / `compute_expires_at` / `sha256_hex` / `parse_plan_id` / `build_plan_id`）

### 先に抽出してから共有（Step 2）

single-use / TTL の enforcement 実体（plan lock 下の state 読取 → matrix 評価 →
`execute_attempted` 監査 → consume → durable state 書込、の fail-closed CAS シーケンス）は
現在 `tool_query_runner.execute` 内にある。これを connector 非依存の承認ライフサイクル
service（`tool_approval.py`）として抽出し、SQL 側・browser 側の両方がこれを使う。
セキュリティ中核の二重実装と divergence を避けるため、再実装ではなく抽出を選ぶ。

- **状態機械はパラメータ化**: tool_approval は status 集合と遷移表を引数に取る
  （SQL デフォルト = `draft/approved/consumed` で不変）。domain には遷移表駆動の汎用
  transition 関数を追加し、既存 `consume_transition` / `approve_transition` はその
  特殊化として温存する
- **state record の codec / status 別不変条件検証も adapter 注入面**に含める。既存
  `state_from_json_dict` は draft/approved/consumed × フィールド不変条件をハードコード
  しており、browser の状態スキーマ（封印ハッシュ・delivery 再開メタ）は別 codec が要る。
  stub adapter contract test に SQL の PlanState 形を焼き込まない
- 共有 core と browser adapter の間は **versioned interface**（bundle schema バージョン +
  状態遷移の意味を固定）とし、SQL / browser 横断の contract test を置く
- browser の遷移（`prepared→approved→delivering→delivered/failed/expired`、hash 照合付き
  delivery 再開を含む）は browser 側遷移表にのみ存在させる

### 監査の一般化（Step 2）

共有 `tool_audit.py` は `ALLOWED_REASONS` / `AUDIT_EVENTS` が閉集合で browser の reason を
通せない。既存 SQL 系の信頼境界（enum 同期テスト含む）に触れないため、**監査は
`browser-audit.jsonl` に分離**し、`AuditLog` は許可 enum レジストリと出力パスを注入可能に
一般化して共有する。**注入面を明示**:

1. **events**: `(event名, plan_dependent)` の組で注入（既存 `PLAN_INDEPENDENT_EVENTS` の
   plan_id 必須/禁止判定も注入面に含める。browser の `login` は plan 非依存イベント）
2. **subcommands**
3. **reasons**（許可 enum レジストリ）
4. **digest フィールド仕様**（`sql_digest` → browser では `artifact_digest` /
   `manifest_digest` / flow ref）
5. **出力パス**

SQL 側デフォルトは不変・enum 同期テスト維持。値を含まないメタデータのみの不変条件は同一。

### browser 専用に新設

catalog パース（browser schema 用）、フロー実行、検証契約エンジン。

### plan namespace の型ガード

bundle 置き場は browser 専用ルート（`outputs/browser-plans/`）に分離し、approve / execute
時に対象 plan の tool type が起動 CLI と一致することを検証する（SQL CLI から browser plan を
consume できる取り違えを遮断）。

---

## 12. 中間成果物の保持ポリシーと janitor

スクショ・trace・一時ダウンロードは CSV 以外にも機密が残る:

- bundle 配下（spool）に閉じ込め、TTL + execute 完了時の削除規則を契約に含める
- `storage_state` は runner が 0600 atomic write（O_NOFOLLOW / umask）で永続化
- **trace は network body capture を無効化**して記録（Authorization / Set-Cookie / token が
  trace.zip に残る）、**HAR はデフォルト off**、auth 操作区間の screenshot は抑止
- 成果物・trace には `max_artifact_bytes`（catalog limits）の byte 上限
- **正常終了時削除だけでは回収できない**（SIGKILL・再起動・disk full）ため、CLI 起動時に
  期限切れ / incomplete bundle を回収する **janitor パス**を持つ。削除失敗は監査して次回再試行

### download の安全規律

- サーバー指定 filename は保存名に使わず runner 生成のランダム名 + atomic rename
  （size / hash 確定後）
- redirect 全 hop の origin 再検証は interception 層が担い、byte 上限・時間上限超過は abort
- partial file は失敗として削除。検証完了前の delivery は構造的に不可能（seal-at-prepare の帰結）

---

## 13. 異常系の扱い

| 異常系 | 扱い |
|---|---|
| 2FA 期限切れ | `session_expired` で fail-closed。再認証（login / form）を hint |
| 権限変更（prepare 後） | seal-at-prepare のため execute 時のブラウザ再実行はない。delivery のみ。prepare 時の権限で完結 |
| 部分取得 | `row_count_range` + 独立 anchor で検出し fail-closed（B1）/ B2 は silent truncation マーク or 拒否 |
| delivery 途中の不明失敗 | 封印ハッシュ照合が取れた場合のみ `delivering` 再開、取れなければ `failed` → 再 prepare |

### 例外の sanitize（runner 境界で全例外）

Playwright の TimeoutError 等は URL（query 内 token 含む）・セレクタ・call log・DOM 断片を
埋め込む。フローコードや capability API 内の非 Playwright 例外（パラメータ値を含む
ValueError 等）も traceback で漏れ得る。**runner 境界を越える全例外**を catch して閉じた
browser reason enum に写像し、生の例外テキストを監査 / stdout / CLI 出力に**一切**通さない
（http connector が `from None` で credential 含み例外を剥がすのと同じ規律）。

### ブラウザライフサイクル契約

context manager で browser / context / page を管理し `finally` で確実に close。フローごとの
hard wall-clock timeout 超過時と SIGINT 時はブラウザプロセスを force-kill してから exit
（130 契約維持）。ゾンビ chromium と user-data-dir ロック残留を許さない。

---

## 14. http 還元ゲート（登録前必須）

browser tool を作る前に必ず通す:

- フロー実行中の network log から export リクエストを捕捉し、`tool_connector_http` での
  replay を試行する
- 再現できたら **http connector として登録し browser tool は作らない**（保証水準が高い方を選ぶ）
- capture 実行の封じ込め: 登録前実行は **draft catalog entry の下で本番と同一の封じ込めを
  適用**する（interception・ephemeral profile・監査すべて同一契約）

---

## 15. 登録時壁打ちワークフロー

1. **http 還元ゲート**（§14、draft catalog entry の封じ込め下）
2. **tier 判定**（§5、独立 anchor を構成できるか）
3. **検証契約の組み立て**（§4 の閉語彙から。B1 は独立 anchor 最低1つ）
4. **別主体レビュー — 独立根拠 + 反証 fixture 必須**:
   共通原因故障（フローと検証を同一 LLM が書くと同じ誤解を両方に埋め込む）を、主体分離
   だけでは防げない（同じ画面・同じ正常 fixture を根拠にした誤解の追認）。登録ゲートの
   条件は「別主体」ではなく「**独立根拠 + 反証 fixture の提示**」。レビュー側が誤成功系
   fixture（セレクタずれ・別 tenant 同型画面・filter 未反映・pagination 欠落・部分 export
   等）を用意し、検証契約が**全て拒否**することを登録合格条件にする
5. **doctor**（ログイン → 遷移 → セレクタ実在確認）
6. **prepare → approve → execute** を一周

fixture は正常系1件で済ませず**誤成功系 corpus**を用意し、各変異の拒否率と正常系の
誤拒否を記録して語彙 v1 の過不足を定量評価する。

### B1 プロトタイプ実測（Step 7）

検証契約 = filter_readback / row_count_range / ui_total_vs_file_rows /
primary_key_unique / tenant_id_match の 5 check（正しさ 3 + 独立 anchor 3、重複含む）で、
誤成功系 corpus 8 変異を `test_browser_extract_corpus.py` で実測した。フロー実行の成果
（ExtractionResult）を fake 注入し、検証契約 enforce（browser 非依存の pure ロジック）を
測っている。

| 誤成功系変異 | 捕捉した check | 拒否 reason |
|---|---|---|
| filter 未反映 | filter_readback | `readback_mismatch` |
| 別 tenant 同型画面（誤セレクタ） | tenant_id_match | `tenant_mismatch` |
| pagination 欠落 | ui_total_vs_file_rows | `ui_total_mismatch` |
| 部分 download | ui_total_vs_file_rows | `ui_total_mismatch` |
| truncation | ui_total_vs_file_rows | `ui_total_mismatch` |
| HTML エラーページ 200 | row_count_range | `row_count_out_of_range` |
| 空結果 | row_count_range | `row_count_out_of_range` |
| 主キー重複（二重取得） | primary_key_unique | `duplicate_primary_key` |

**結果**: 8 変異すべて拒否（拒否率 100%）、正常系の誤拒否 0。

**語彙 v1 の過不足に関する所見**:

- **ui_total_vs_file_rows が完全性系（pagination/部分/truncation）の 3 変異を単独で捕捉**
  している。これは「UI が示す総数」という独立 oracle が効いている証拠。UI に total 表示が
  ない画面ではこの anchor を構成できず B2 降格になる（B1 の前提条件どおり）
- **「値だけが違い、shape・件数・tenant が正常な誤セレクタ」は語彙単独では捕捉できない**
  残余。tenant_id_match は tenant 境界の誤りは捕むが、同一 tenant 内で別の正常データを
  掴む誤セレクタは検出圏外。この class は登録時の別主体レビュー（独立根拠 + 反証 fixture）と
  filter_readback / row_count_range の併用で狭めるしかなく、v1 の既知の限界として記録する
- artifact_hash / screen_fingerprint は本 corpus では未使用（前者は bundle→delivery の
  完全性、後者は catalog 宣言の基準指紋を要する）。実サイト適用時に screen_fingerprint の
  基準取得手順を詰める（次サイクル）

---

## 16. 既知の限界（honest scoping）

guide に明記して受容する残余:

- **read-only 非強制**: 宣言フロー外の操作をしない + 証跡、の honest scoping に留める
  （MariaDB 保証範囲外と同じ流儀）。read-only の機械的**強制**は v1 の Non-Goal
- **DNS rebinding**: allowlist はホスト名照合。長命セッションの rebinding リスクは残る
- **WebRTC 残余**: launch args で塞ぐが、塞ぎ切れない経路は DNS rebinding と同格
- **doctor のログイン副作用**: doctor はデータ非接触を主張**しない** — ログインと遷移自体が
  session 生成・last-login 更新等の副作用を持つ。「**抽出・成果物生成をしない / 明示的
  destructive action をしない**」に狭めて宣言する。doctor 実行中は trace / screenshot /
  DOM 保存を無効化する
- **監査 JSONL の可書性**（§0 の残余）: 攻撃者は監査履歴も書き換えねばならない、まで bar を
  上げるに留まる
- **in-process Python の構造的封じ込め不能**（§2）: hash pin + AST ゲート + PR レビューは
  事故防止とレビュー支援であり、悪意フローへの構造的境界は主張しない
- **egress proxy / network namespace による OS 層の通信封じ込め**: interception 層の限界への
  二重化は将来オプション（本 guide に記録のみ）

---

## 17. bootstrap 手順（Playwright）

browser 系は本体 requirements.txt を汚さない。opt-in で別ファイルを使う:

```
# 依存インストール（別ファイル、flat requirements に extras 機構がないため分離）
uv pip install -r requirements-browser.txt

# ブラウザバイナリの取得（初回のみ）
python -m playwright install chromium
```

playwright は下限 1.48 以上（`route_web_socket` 必須）+ major.minor 上限付きで宣言する。

browser 実 E2E テストは DB smoke と同じ opt-in 環境変数ゲート
（`BROWSER_EXTRACT_SMOKE` 未設定時 skip）。ブラウザ非依存の判定ロジック（allowlist 照合・
URL 正規化・状態機械・保持ポリシー判定・janitor のファイル操作・AST ゲート）は常時実行。

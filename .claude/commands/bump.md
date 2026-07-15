# Plugin Version Bump

プラグインのバージョンを更新し、コミット & プッシュする。

## 引数

`$ARGUMENTS` でバージョンを指定する:

| 引数 | 動作 |
|------|------|
| `major` | メジャーバージョンを +1（例: 0.2.0 → 1.0.0） |
| `minor` | マイナーバージョンを +1（例: 0.2.0 → 0.3.0） |
| `patch` | パッチバージョンを +1（例: 0.2.0 → 0.2.1） |
| `x.y.z` | 指定したバージョンに直接設定 |
| (なし) | `patch` と同じ |

## 手順

1. `.claude-plugin/plugin.json` と `.claude-plugin/marketplace.json` の現在のバージョンを読み取る
2. `$ARGUMENTS` に基づいて新バージョンを計算する
3. 以下のファイルの `"version"` フィールドを新バージョンに更新する:
   - `.claude-plugin/plugin.json`
   - `.claude-plugin/marketplace.json`
   - `.codex-plugin/plugin.json`
4. 変更内容をユーザーに表示して確認を求める:
   ```
   Plugin version bump: {old_version} → {new_version}
   - .claude-plugin/plugin.json
   - .claude-plugin/marketplace.json
   - .codex-plugin/plugin.json
   ```
5. ユーザーが承認したら、コミット & プッシュする:
   - コミットメッセージ: `chore: bump plugin version to {new_version}`
   - `git push`

---
description: "Wiki の品質チェックと修復提案を行う"
---

Skill ツールで `wiki:wiki` を実行する。引数: `lint $ARGUMENTS`

## graph layer 前提

`lint-wiki.py` は `dead_link` / `orphan` を `{wiki_root}/outputs/graph.json` 経由で検出する（`--use-graph` デフォルト ON）。graph が無い場合は **exit 2** で停止する。単独実行時の対処:

1. **推奨**: 事前に `python3 skills/wiki/scripts/graph_gen.py --wiki-root .wiki` を実行してから lint を走らせる
2. **opt-in ショートカット**: `lint $ARGUMENTS --auto-graph` を渡すと lint 側が graph_gen を自動実行してからフォールバックする

`wiki-cycle` 経由で実行した場合は graph_gen ステップが組み込まれているため、ユーザが意識する必要はない。

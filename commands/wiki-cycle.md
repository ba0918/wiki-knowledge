---
description: "Ingest → Compile → Lint を一括実行する"
---

Skill ツールで `wiki:wiki` を実行する。引数: `cycle $ARGUMENTS`

## フロー

cycle は orchestrator として以下のステップを順に実行する:

```
ingest → compile → graph_gen → lint
```

`graph_gen` は compile と lint の間に必ず実行する派生生成ステップ。`scripts/graph_gen.py` を呼び出して `{wiki_root}/outputs/graph.json` を再生成する。これを skip すると lint が graph 欠如で exit 2 となり cycle 全体が失敗する。

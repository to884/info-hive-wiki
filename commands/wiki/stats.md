---
description: wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。
---

`<WIKI_ROOT>` は、ユーザーが明示したパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` とする。次に `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_stats.py" "<WIKI_ROOT>"` を実行し、出力に簡単な解釈を添えてユーザーに示す。

- wiki がスケーリングの閾値に近づいている場合は、そのことを指摘し、移行に何が必要かを説明する。閾値は、インデックスの分割が 150 ページまたはインデックス 300 行、検索スクリプトの日常的な利用が 300 ページ、週1回の lint が 500 ページである。
- ソフト上限（400 行）やハード上限（800 行）を超えているページがあれば、分割を勧める。
- リンク密度が低い（1ページあたり平均 1.5 リンク未満）場合は、相互参照が足りていない可能性がある。独立したページにすべきコンセプトを見つけるために、`--suggest-pages` を付けた lint を提案する。

移行の手順は `skills/llm-wiki/references/scaling-playbook.md` を参照。

**ホスト型 wiki。** 特定した wiki がホスト型の URL である場合（または指示でホスト型の URL が指定されている場合）は、`skills/llm-wiki/references/hosted-mode.md` のホストモードを使う。MCP ツールを使い、MCP が使えないときは `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py"` を使う。数値は `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py" stats` で取得する。シェルが使えない場合は、`wiki_overview` にある件数を使う。あわせて、`vector_lag`、検索モード、未解決のレビューフラグも報告する。インデックスの分割とページの上限は、ローカルの wiki と同じように当てはまる。

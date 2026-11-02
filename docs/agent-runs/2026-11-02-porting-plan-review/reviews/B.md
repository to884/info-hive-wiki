## Summary

All four pages loaded (HTTP 200). No fetch failed. I also fetched `https://code.claude.com/docs/en/plugins/components` because it is the page that holds the Commands section and the subdirectory rule. The plugins-reference and overview pages link to it as "Plugin components," though not to a `#commands` anchor.

Pages read:
- https://code.claude.com/docs/en/plugins-reference
- https://code.claude.com/docs/en/plugins
- https://code.claude.com/docs/en/plugins/components
- https://agentskills.io/specification

## 1. Plugin command invocation and subdirectories

Yes, the prefix is the plugin name. From the plugins-reference page, under `commands`:

> "In the object map, each key becomes the command name after the plugin prefix. For example, `"about"` in plugin `deploy-tools` runs as `/deploy-tools:about`."

From the components page, under "Commands":

> "Save a command at `commands/<file>.md` and it becomes `/<plugin>:<file>`. A subdirectory adds a segment, so `commands/db/migrate.md` is `/my-plugin:db:migrate`."

So a plugin named `llm-wiki` with `commands/wiki/query.md` is invoked as **`/llm-wiki:wiki:query`**. The other three forms (`/wiki:query`, `/llm-wiki:query`, and the reading that matches them) are not what the docs describe.

Caveats:
- The standard-layout table describes `commands/` as "Flat Markdown command files," and only the components page explains nesting.
- The components page says "Commands are the older format, and skills supersede them for new work."

## 2. Same command name in two plugins

None of the pages I read addresses two plugins defining a command with the same name. The only namespacing text is from the plugins-reference page:

> "Claude Code namespaces every component under it, so an agent `reviewer` in plugin `deploy-tools` appears as `deploy-tools:reviewer`."

Because the plugin name is part of every invocation, commands from plugins with different names should not collide. The conflict rules I did find cover other component types:
- Hooks: "When another enabled plugin has the same name, one of the two registers its `hooks/hooks.json` hooks and the other's are left out."
- LSP: "when two enabled servers claim the same extension, the first registered handles those files and the other isn't used for them, whether the servers come from one plugin or two."
- Settings: "Two plugins set the same key: the value from the plugin loaded last applies."

The `loading` page, which the docs link for same-name plugins, was not part of this request and I did not fetch it.

## 3. Does `name` have to match the skill directory?

**Agent Skills spec** (https://agentskills.io/specification), under "`name` field":

> "Must match the parent directory name"

The same list also requires 1–64 characters, only lowercase letters, numbers and hyphens, no leading or trailing hyphen, and no `--`.

**Claude Code docs** do not state this requirement. The components page (under "Skills") says:

> "**Command name**: `/<plugin>:<directory>`, so `skills/review/SKILL.md` in `my-plugin` is `/my-plugin:review`. If you set `name` in the frontmatter, it replaces the last segment and the plugin prefix stays."

The docs treat frontmatter `name` as an override of the directory-derived name, not as something that must equal the directory. The agents section says the same thing in a different form: "`name: audit` in `agents/review/security.md` loads as `my-plugin:review:audit`."

So the spec requires the match, and the Claude Code docs describe `name` as an override without mentioning the requirement. A plugin skill whose `name` differs from its directory would conform to Claude Code's documented behavior but not to the spec. I did not fetch the linked skills page ("how a skill gets its command name"), which may say more.公式文書の取得と、コードの照合が終わりました。結果をまとめます。

## エージェント A のレビュー

### 長所
- **解析キャッシュの世代管理の抜けを最初に挙げた。** `wiki_search.py` の `collect_pages` は sha256 が一致すると保存済みの `meta`・`sections` を使い、frontmatter を再解析しません。P1 の BOM・NFKC・区切りの修正が既存 wiki に効かないという指摘は正しく、私を含む他の全員が見落としていた最重要の抜けの一つです。
- **`--gate` が相対比較だけである点と、日本語の評価基準が「ほぼ 0 → 何でも上回る」になる点を、統計の粒度（1 問で約 3 ポイント）まで含めて示した。** 完了条件を判定可能にする具体案も付いています。
- **frontmatter の `sources:` に書かれる `\uXXXX` がデータ破壊だと見抜いた。** `wiki_evolve_loop.py:451-452,458,659` は既定の `ensure_ascii` で書き、手書きパーサーはエスケープを戻しません。計画はこれを「見た目」に分類しており、重大度の誤りを正しています。
- **`description=__doc__` のため `--help` はすでに日本語で、— を含む点**、**`package.json` が docs 専用のパッケージである点**、**CHANGELOG に `/wiki:migrate` が必ず残り完了条件と衝突する点**、**JSON 出力キーを訳してはいけない点**は、いずれも他の 3 体にない実用的な指摘です。
- 方針 A の代案として「履歴ごと取り込み、upstream を remote に残す」を提示しており、私と同じ結論に独立に至っています。

### 短所
- **リリース数の訂正が誤っている。** A は「2026-07-02 以降 9 リリース」としましたが、CHANGELOG には 2026-07-20 付で 2.0.0〜2.0.7 の 8 本と 3.0.0 があり、合計は 14 本です。私の最初の数（11）も誤りで、2.0.0〜2.0.2 を落としていました。
- **コマンド名前空間の前提を検証していない。** A は計画と同じく `/wiki:*` が維持できると仮定しています。公式文書では `commands/wiki/query.md` は `/<plugin>:wiki:query` になります（後述）。
- **標準入力の文字コードを見落としている。** `wiki_decide.py:641`、`wiki_evolve_agent.py:223`、`wiki_evolve_claude.py:44`、`wiki_evolve_api.py:130` の `sys.stdin` 読み取りは P1 の対象外のままです。
- fastembed の仕様（量子化版か、何トークンで切るか）を未確認のまま推測で書いています。推測の内容自体は正しく、推測と明記している点は誠実です。
- 25 件を 1 枚の表に詰めており、重大度の根拠が読み取りにくい箇所があります。

### 検証した事実
- 解析キャッシュの再利用: `wiki_search.py:233-237`（HEAD）で一致。schema 判定は `:127` のみで一致。
- `--gate` の相対比較: `eval/retrieval/run_eval.py:217-223` で一致。`hybrid_available()`（`:144-152`）が依存欠如時に hybrid を外す点も一致。
- `QUERIES.read_text()` の encoding 未指定: `run_eval.py:199` で一致。`test_decisions.py:40-42,488-490` の `text=True` も一致。
- `wiki_evolve_loop.py:429` の `write_text(encode(record))` の encoding 未指定、`wiki_evolve_agent.py:208` の `text=True`: 一致。
- hosted の漏れ: `wiki_decide.py:216-218`（`client_from_config` の分岐）、`:682`（`--profile`）: 一致。A の行番号は作業ツリー値で 1 行ずれます。
- Agent Skills 仕様の「`name` は親ディレクトリ名と一致」: 仕様書（agentskills.io）で一致。Claude Code の文書は `name` を上書きとして扱い、一致を要求していません。A の「記憶にもとづく」注記は正確でした。
- `package.json` の name が `llm-wiki-plugin-docs`: 一致。
- 評価コーパス 21 ページ・32 問: ページは `SCHEMA.md` を含めて 21 ファイル、本文ページは 20。問数は確かめていません。

## エージェント C のレビュー

### 長所
- **コマンド名前空間の前提の誤りを、公式文書を根拠に 高 で挙げた唯一のレビュー。** 私が取得した公式文書は「`commands/db/migrate.md` は `/my-plugin:db:migrate`」と明記しており、計画の「プラグイン名を変えても `/wiki:*` が残り、移植元と衝突する」という前提は文書上成り立ちません。方針 C と 6 節のリスクの根拠が崩れるため、重大度 高 は妥当です。
- **P2 の基準測定が「0 件」ではなく JSON 解析エラーになる点を発見した。** `wiki_search.py:729-732` は標準出力を空にして正常終了し、`run_eval.py:96,100` は無条件に `json.loads` します。P2 を既存ハーネスで実行できないという指摘は正確です。
- **標準入力の UTF-8 化の抜け**、**`grounded_verdict` の完全一致**（`wiki_evolve_loop.py:77-78` の `quote in source`）、**D2 で空白を全削除すると `now here` と `nowhere` が一致する**という 3 点は、計画の P1・P4 の修正案が目的を達しないことを示す具体的な反例です。
- 「。」で分割するには、現在の正規表現 `(?<=[.!?])\s+` が句読点の後の空白を要求するため、文字クラスに「。」を足すだけでは足りないという細部まで押さえています。

### 短所
- **計画の事実認定の検証が薄い。** 訳の構文木同一性、94 ファイル、コミット数、リリース数、fastembed の量子化・サイズは確認していません。「行番号は作業ツリーの値」と正しく前置きしていますが、計画の「v4.0.1 時点」という誤記を指摘していません。
- **Windows CI の POSIX 依存（symlink、chmod、shebang、`ps`）を挙げていない。** 完了条件「CI が Windows で通る」が達成できない問題は見えていません。
- **設定ファイルと `LLM_WIKI_CONFIG` がホスト型専用で、方針 C の改名対象が存在しない点を挙げていない。**
- 方針 G への「サーバーを提供しないことと接続しないことは別」というコメントは正しいですが、計画はすでに 7 節で「解釈」と明示しており、新しい情報ではありません。
- 低重大度の掃き出し（P0 前提の矛盾など）は #13 に圧縮されており、作者が修正箇所を特定しにくいです。

### 検証した事実
- 公式文書（code.claude.com/docs/en/plugins/components）: 「`commands/<file>.md` は `/<plugin>:<file>` になる。サブディレクトリは区切りを 1 つ足し、`commands/db/migrate.md` は `/my-plugin:db:migrate`」。C の主張と一致。ただし移植元 README は `/wiki:ingest` と書いており、CLI 上の実測は誰も行っていません。
- `run_eval.py:96,100` の無条件 `json.loads`: 一致。
- `grounded_verdict` の完全一致: `wiki_evolve_loop.py:77-78` で一致。
- 標準入力の 4 か所: 一致。
- D2 の引用一致で `confidence=1.0` を返す: `wiki_decide.py:422-423` で一致。`wiki_graph_lint.py:360` が一致した辺を D2 から除く: 一致。
- fastembed の MiniLM 登録情報「512 input tokens truncation」: 手元の uv キャッシュの `pooled_embedding.py` で一致。配布物は `qdrant/…-onnx-Q` の量子化版でした。

## エージェント D のレビュー

### 長所
- **C とほぼ同じ高重大度 5 件（JSON 解析エラー、gate、キャッシュ、標準入力、D2 の空白）を独立に確認し、さらに計画書の内部矛盾を行番号付きで示した。** 計画書の 551-553 行（P5 は P1 後）、683 行（P5 の前提は P0）、597 行（P0 の前提に C・G・J）、530 行（方針 H の「第 1 段階」）はすべて一致しました。
- **グラフの明示参照（`graph.node_id`、`relationships.object`、`source`、`contradicts`、`supersedes`）が NFC 正規化の対象から漏れている**ことを、抽出・lint・query の 3 スクリプトの完全一致箇所まで示しています。計画の方針 D はファイル名とリンクしか見ていないため、有効な補完です。
- 構文木の比較を 29 ファイルで自ら実施し、「docstring を除けば同一」と計画の記述を正確に言い直しています。私の結果と一致します。
- 方針 C と G の矛盾（削除する設定を改名しようとしている）を、`client_from_config` の行まで示して指摘しています。

### 短所
- **コマンド名前空間を 中 に据えている。** 公式文書の記述どおりなら、方針 C の前提（`/wiki:*` 維持、衝突リスク）が崩れるので、C と同じく 高 が適切です。
- **Windows CI の POSIX 依存を挙げていない。** 文字コード以外の理由で Windows ジョブが通らない点が抜けています。
- A が見つけた `--help` の日本語化、`package.json` の性格、CHANGELOG と完了条件の衝突、JSON キーの保護、`fake_backend.py` のトークン化、frontmatter `sources` の `\uXXXX` 破壊を挙げていません。
- 内容と構成が C と大きく重なり、独立した追加価値は上記の 3 点に絞られます。
- fastembed の登録情報は URL 引用のみで、手元での確認はしていません（本文で明記しており誠実です）。

### 検証した事実
- 計画書の行番号 530、551-553、597、683: 一致。
- `wiki_graph_extract.py:195-196`（明示 ID）、`:283-296`（`str(obj)`、`source` をそのまま出力）、`wiki_graph_lint.py:244,275,289-292`（完全一致で検証）、`wiki_graph_query.py:50-72`（SQL の完全一致）: 一致。
- `client_from_config` の hosted 分岐（HEAD 216-218）と `read_remote_profile`（177-192）: 一致。
- 構文木比較: 私の結果（docstring を除いて 29 ファイル SAME）と一致。

## 自分の結論の見直し

結論を改めます。私のレビューは事実認定の照合（fastembed の実物確認、構文木、hosted テストの漏れ、設定ファイルの所在、Windows の POSIX 依存）では堅実でしたが、計画の完了条件を無効にする 3 つの大穴を見落としていました。

- **追加する高重大度の指摘（3 件）**
  1. 解析キャッシュの世代管理がない。P1 の解析器修正は、`.wiki-cache/search-index.json` を持つ既存 wiki に効きません。P1 で schema を上げ、P3 で `VECTOR_INDEX_SCHEMA` と索引 meta に接頭辞・分割方式を含める作業が必要です。
  2. 検索の完了条件が判定できない。`--gate` は方式どうしの相対比較で、hybrid は依存欠如時に静かに外れ、P2 の基準測定は空出力で JSON 解析エラーになります。移植前の絶対値を保存し、許容幅と日本語の合格値を書き、hybrid の省略を失敗にする必要があります。
  3. 方針 C の前提が公式文書と食い違う。`commands/wiki/*.md` は `/<plugin>:wiki:*` になるため、プラグイン名を変えれば `/info-hive:wiki:*` になり、移植元との衝突も起きません。私は plugin-dev スキルの文言と移植元 README から `/wiki:*` を既定の挙動と判断しましたが、公式文書を優先します。P0 で実測し、`commands/` を平らにして `/info-hive:*` にそろえる案を推奨に変えます。Q1 の決定は前提ごと見直しが要ります。
- **追加する中重大度の指摘**: 標準入力の UTF-8 化（P1）、`grounded_verdict` の完全一致（P4 の対象を `wiki_evolve_loop.py:73-79` に広げる）、D2 の空白全削除は英数字間の語境界を失うので許容する差を限定する、「。」分割は空白を要求しない正規表現にする、frontmatter `sources:` の `\uXXXX` はデータ破壊として P1 の最初に直す。
- **訂正**: リリース数は「4 か月で 6」ではなく 14 本（2026-07-20 付の 2.0.0〜2.0.7 と 3.0.0、以降の 5 本）です。私の「11」も誤りでした。結論（更新が集中する）は変わりません。
- **維持する独自の指摘**: Windows の評価テストが symlink・chmod・shebang・`ps` に依存し文字コードを直しても通らないこと、GitHub の Windows ランナーは cp1252 で cp932 の条件を再現できないこと、`wiki_evolve_loop.py` が index・log 行と英語本文のページを書くため「ログ行の書式は自由に変えられる」は過大であること、方針 A は履歴ごと取り込む形にすべきこと、fastembed の MiniLM が量子化版であること。
- **方針への意見の変更**: 方針 C は「`/info-hive:*` に統一、P0 で実測」に変更。方針 H は「D2 の正規化を限定し、評価ループの引用照合も対象に含める」に変更。他は維持します。

## 順位と確信度

1. **エージェント A**。網羅性と独自発見の数で最も優れています。キャッシュ、gate、frontmatter の `\uXXXX` 破壊、`--help`、`package.json`、CHANGELOG と完了条件の衝突、JSON キーの保護は、いずれも検証して正しく、作者がそのまま計画に反映できる形です。欠点は名前空間の未検証とリリース数の誤りですが、全体の価値を覆しません。
2. **エージェント C**。公式文書に基づく名前空間の指摘、JSON 解析エラー、標準入力、`grounded_verdict`、D2 の反例と、計画の修正案が目的を達しない箇所を的確に突いています。計画の事実認定の照合が薄い点で A に劣ります。
3. **エージェント D**。C と同じ高重大度 5 件に加え、グラフの明示参照と計画書の内部矛盾を行番号で示しました。C との重複が大きく、名前空間の重大度を低く見た点で C の下に置きます。
4. **自分**。事実の照合（fastembed の実物、構文木、hosted テスト、設定ファイル、Windows の POSIX 依存）は最も細かく行いましたが、キャッシュ・gate・標準入力・名前空間という計画の成否を左右する 4 点を見落としました。見落としの重さを考えると最下位が妥当です。

確信度: **中**。1 位と 2 位の順序は、独自発見の数と検証の正確さで A が上という判断ですが、名前空間の前提崩れを 高 で捉えた C を 1 位とする見方にも合理性があります。名前空間の挙動は公式文書との照合のみで、CLI 上の実測は 4 体とも行っていません。

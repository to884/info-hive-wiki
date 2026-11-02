# エージェントのメモリとの連携

wiki を初期化したら、AI エージェントが次のセッション以降も wiki の場所を把握できるように、短い定型文（stanza）を追加する。プロジェクトをまたいで使う個人用の wiki なら、ユーザーレベルの固定パスを使ってグローバルなエージェントの指示に書く。プロジェクト専用の wiki なら、相対パスを使ってプロジェクトのメモリファイルに書く。この案内がないと、エージェントは wiki を読まずに学習データから答えてしまうことが多い。

## どのファイルを使うか

メモリファイルの規約はエージェントによって異なる。次の表で適切なファイルを選ぶ。

| エージェント | メモリファイル | 補足 |
|---|---|---|
| Claude Code | `CLAUDE.md` | プロジェクトのルート。Claude は git のルートまでさかのぼってこのファイルを探す。 |
| Codex (OpenAI) | `AGENTS.md` | 標準の `agents.md` 規約。 |
| Cursor | `AGENTS.md`（または `.cursor/rules/*.mdc`） | AGENTS.md のほうが簡単で、ほかのエージェントでも使える。 |
| OpenCode | `AGENTS.md` | `CLAUDE.md` も読むので、どちらでもよい。 |
| Gemini CLI | `GEMINI.md` | Gemini 固有のファイル名。 |
| Pi Agent | `AGENTS.md` | `agents.md` の標準に従う。 |
| OMP (Oh My Pi) | `AGENTS.md` | `agents.md` の標準に従う。 |
| OpenClaw | `AGENTS.md` | `agents.md` の標準に従う。 |

ユーザーが同じプロジェクトで複数のエージェントを使っている場合は、`AGENTS.md` を正本のファイルにし、`CLAUDE.md` をそこへのシンボリックリンクにする（または内容を複製する）のがよい。Claude Code は、シンボリックリンクの `CLAUDE.md` も問題なく読む。

ユーザーがどのエージェントをいちばん使うか分からない場合は、`AGENTS.md` を既定にする。対応している実行環境がもっとも多いためである。

## 正本の定型文

プロジェクトで選んだメモリファイルに、次の定型文を追記する。短く保つこと。メモリファイルはセッションのたびにエージェントのコンテキストに入るので、1行ごとにコストがかかる。

```markdown
## LLM Wiki

This project maintains an LLM-curated wiki at `wiki/` following Andrej Karpathy's "LLM Wiki" pattern (https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Before answering questions that rely on knowledge accumulated in this project, read `wiki/index.md` (or the relevant shard under `wiki/indexes/` if the wiki has been sharded) and use its one-line summaries to find the pages you need. Cite with `[[wikilinks]]`. If the index does not surface good candidates, fall back to `wiki_search.py` from the `llm-wiki` skill for local hybrid retrieval; add `--no-embed` for dependency-free BM25.

To add a new source, follow the `llm-wiki` skill's ingest workflow: decide placement under `wiki/sources/`, `wiki/entities/`, `wiki/concepts/`, or `wiki/synthesis/`; identify touched pages and make surgical `str_replace` updates rather than rewrites; update the index; append a one-line entry to `wiki/log.md`.

Scaling discipline: atomic pages (400-line soft cap, 800-line hard cap), sharded indexes past ~150 pages or 300 index lines, required YAML frontmatter on every page, `[[wikilinks]]` for every cross-reference.

Full conventions live in `wiki/SCHEMA.md`. Treat it as authoritative when it disagrees with this summary.
```

ユーザーが既定以外のディレクトリに wiki を初期化した場合は、パスの記述を合わせて直す（スキーマのファイルと定型文の内容は、常に一致させる）。

## 個人用のグローバルな wiki の定型文

この形は、エージェントのグローバルな指示に書く。wiki が別の場所にある場合は `~/wiki/` を直す。

```markdown
## LLM Wiki

My canonical LLM-curated wiki is at `~/wiki/` and is independent of any project. Before answering from accumulated knowledge, read `~/wiki/index.md` first, then the relevant shard under `~/wiki/indexes/`. Full conventions live in `~/wiki/SCHEMA.md`.

When work in any project produces durable facts, decisions, incidents, or post-mortems, preserve them in `~/wiki/` using the `llm-wiki` skill. Put captured source material under `~/wiki/raw/`. Do not ingest an entire project automatically; add only requested sources and durable findings.
```

## ホスト型 wiki の定型文

wiki が LLM Wiki のサーバー上にある場合は、この形を使う（`hosted-mode.md` を参照）。個人用の wiki ならグローバルな指示に、プロジェクトの wiki ならプロジェクトのメモリファイルに書く。URL は置き換える。トークンをメモリファイルに書いてはならない。

```markdown
## LLM Wiki

My canonical LLM-curated wiki is hosted at `https://<worker>.workers.dev` (MCP server `llm-wiki`, endpoint `/mcp`). Use the `llm-wiki` skill in hosted mode.

Call `wiki_overview` first and follow the `SCHEMA.md` it returns. Use `index.md` first, then `wiki_search`. Cite with `[[wikilinks]]`. Write with `wiki_commit`: `edit` operations for surgical changes, `append` for `log.md` and index lines. After a conflict, read the page again and retry. If MCP is not available, use `python3 scripts/wiki_remote.py` with the token in `LLM_WIKI_TOKEN`.

Preserve durable facts, decisions and post-mortems from any project in this wiki. Do not ingest a full project automatically. A local `pull` mirror, if present, is read-only.
```

エージェントには、MCP サーバーの設定とトークンも必要である。`python3 scripts/wiki_remote.py mcp-config <client>` を実行して結果を示し、ユーザーが承認してから設定を追加する。エージェントごとに別のトークンを使うので、どのコミットをどのエージェントが行ったかが分かる。

ローカルの wiki をホスト型 wiki に移行したら、ローカル用の定型文を置き換える。両方を残してはならない。定型文を変えるのは、インポートの検証に合格し、ユーザーが文面を承認してからにする。

## 初期化時のやり取り

初期化の時点で、ディレクトリ構成ができてスキーマの説明が終わったら、定型文を提案する。黙って追記してはならない。メモリファイルはユーザーのものである。

進め方の例は次のとおり。

1. wiki をプロジェクトをまたいで使うのか、現在のプロジェクト専用にするのかを聞き、次に使っているエージェントを聞く。グローバルな wiki なら、そのエージェントのグローバルな指示を対象にし、ユーザーレベルの固定パスを使う。プロジェクトの wiki なら、上で説明したとおり、プロジェクトの `CLAUDE.md`、`AGENTS.md`、`GEMINI.md` を使う。
2. そのファイルがすでにあるかを確かめる。
   - **ない**場合は、LLM Wiki の定型文だけを内容にしてファイルを作ることを提案する。
   - **ある**場合は、提案する定型文を示し、追記するかを聞く。
3. もっと短い定型文のほうがよいかを聞く。メモリファイルを方針として 50 行未満に保っているユーザーもいる。上の正本の定型文はすでに短いが、3行の代わりの形も提案する。

```markdown
## LLM Wiki

This project has an LLM-curated wiki at `wiki/`. Read `wiki/index.md` before answering research questions. Full conventions in `wiki/SCHEMA.md`. Ingest and query workflows live in the `llm-wiki` skill.
```

4. ユーザーがどれを選んでも、それを書き込み、書き込んだことを伝える。

## wiki の構成が変わったとき

定型文は案内であり、手順書ではない。新しいページの種類、新しいタグ、分野ごとの規約といった wiki の変更のほとんどは、メモリファイルではなく `SCHEMA.md` で行う。メモリファイルを変えるのは、次の場合だけである。

- wiki を標準以外のディレクトリに移した（パスを直す）。
- wiki をホスト型のサーバーに移した（定型文をホスト型 wiki 用のものに置き換える）。
- wiki のインデックスを分割した（エージェントがそこから読み始めるように、`wiki/indexes/` を明記する）。
- wiki を廃止した（定型文を削除する。参照先のない案内を残さない）。

lint やスケーリングの移行作業の途中で、ユーザーの代わりにメモリファイルを編集する場合は、差分を示して同意を得る。エージェントのメモリファイルを黙って書き換えると、ユーザーの信頼をいちばん早く失う。

## 複数のプロジェクト

知識をプロジェクトをまたいで積み上げたいなら、グローバルな wiki を1つ持つのがもっとも簡単である。そのグローバルな指示の定型文には、どの作業ディレクトリからでも同じ wiki を指すように、ユーザーレベルの固定パスを使う。内容を分離したい場合、リポジトリと一緒にコミットしたい場合、別の管理下に置きたい場合は、プロジェクト専用の wiki も引き続き役に立つ。2つの wiki の範囲を黙って混ぜてはならない。もっとも近い明示的な指示に従うか、どの wiki に入れるかをユーザーに聞く。

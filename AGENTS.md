# リポジトリの指示

## リリース

- この指示でいう「公開（publish）」と「パッケージの公開（package publication）」は、Claude Code プラグイン、skills.sh ディレクトリのスキル、GitHub リリースの公開だけを指す。npm への公開は、ユーザーが明示的に求めない限り含めない。
- ユーザーに見えるプラグインやスキルの変更は、どれもマージ前に SemVer のバージョンを決める必要がある。リリースすべき変更を、古いバージョンのまま残してはならない。
- `CONTRIBUTING.md` の「Releasing (maintainers)」セクションを読み、それに従う。LLM Wiki 本体のリリースでは、`package.json`、`.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json` にある2つのバージョンフィールド、`server/package.json`、`server/src/types.ts` の `SERVER_VERSION`、`CHANGELOG.md` をまとめて更新する。スキルには独自のバージョンフィールドがなく、それを含むプラグインのバージョンを引き継ぐ。
- リリースのコミットが `main` に入り、バージョンタグが push され、GitHub リリースが作成され、影響を受けるパッケージがすべて公開され、レジストリとリリースの URL を確認するまでは、リリースの完了を報告してはならない。

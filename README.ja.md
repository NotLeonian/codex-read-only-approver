# codex-read-only-approver
Codex CLI 用の、依存パッケージを持たない保守的な `PermissionRequest` hook です。

Bash コマンド**全体**が、限定された静的な読み取り専用文法に収まる場合だけ自動承認します。書き込み、外部ヘルパー実行、動的なシェル展開、不正な構文、未知のコマンドが含まれる場合は何も出力せず、Codex の通常の人間承認へ戻します。

主に次の運用を想定しています。

```bash
GIT_OPTIONAL_LOCKS=0 \
GIT_NO_LAZY_FETCH=1 \
GIT_PAGER=cat \
codex -s danger-full-access -a untrusted -c approvals_reviewer=user --search
```

これはサンドボックスではなく、Codex の承認ポリシーを置き換えるものでもありません。使用前に [SECURITY.md](SECURITY.md) を確認してください。

## 動作概要
この hook は次を行います。

- パイプラインおよび `&&`、`||`、`;` の各セグメントを個別に解析する
- 全セグメントが読み取り専用と判定された場合だけ許可する
- `/dev/null` への破棄を除き、出力リダイレクトを承認しない
- コマンド置換、プロセス置換、未引用の glob・ブレース展開、heredoc、バックグラウンド実行、グループ化、未知のシェル構文を承認しない
- 既定で実行ファイルの実体が信頼済みインストール先にあるか確認する
- `sed`、`git`、`find`、`fd`、`rg`、`uniq`、`base64`、`yq`、`tar`、圧縮系コマンド、`unzip`、`sysctl`、`date`、`hostname`、`file`、`tree`、`nm`、`objdump` を引数込みで判定する
- 書き込み可能または曖昧なコマンドでは無出力にし、通常の人間承認を維持する
- 自動拒否は行わないため、分類器が理解しない操作でも人間が承認できる

以下の表は、冒頭の推奨起動環境（`GIT_PAGER=cat` と `GIT_NO_LAZY_FETCH=1` を含む）を前提にしています。

| コマンド | 結果 |
|---|---|
| `sed -n '1,80p' file` | 自動承認 |
| `sed 's/old/new/g' file` | 自動承認 |
| `sed -i 's/old/new/g' file` | 人間承認 |
| `sed 'w output.txt' file` | 人間承認 |
| `GIT_OPTIONAL_LOCKS=0 git status --short` | 自動承認 |
| `git status --short`（`GIT_OPTIONAL_LOCKS` 未設定） | 人間承認（Git が index を更新する可能性） |
| `git diff --no-ext-diff --no-textconv --stat \| sed -n '1,20p'` | 自動承認 |
| `git branch -f name HEAD` | 人間承認 |
| `uniq input output` | 人間承認（第2ファイル引数は出力先） |
| `sort input` | 人間承認（内部一時ファイルへ spill する可能性） |
| `cat input \| tee output` | 人間承認 |
| `rg TODO . > matches.txt` | 人間承認 |
| `python3 -c '...'` | 人間承認 |

## 導入
### スクリプトを直接配置する方法
```bash
mkdir -p ~/.codex/hooks
install -m 0755 codex_read_only_approver.py \
  ~/.codex/hooks/codex_read_only_approver.py
```

[`hooks.json.example`](hooks.json.example) の内容を `~/.codex/hooks.json` に統合します。例では `$HOME` 配下のスクリプトを直接実行します。配置場所が異なる場合は変更してください。

次に、Codex 内蔵の既知安全判定で hook を迂回しないよう、同梱の exec-policy ルールを配置します。

```bash
mkdir -p ~/.codex/rules
install -m 0644 codex-read-only-approver.rules.example \
  ~/.codex/rules/codex-read-only-approver.rules
```

このルールは、対応コマンドを自動許可するものではありません。対応コマンドを必ず `PermissionRequest` に送り、hook が `allow` を返さなければ通常の人間承認を表示させます。hook を使わない Codex セッションでもルールは読み込まれるため、その場合は承認要求が増えます。`-a never` や承認を無効化するモードとは併用しないでください。

Codex は人間レビューを明示し、Git の既知の任意書き込みと lazy fetch を抑止する環境で起動します。

```bash
GIT_OPTIONAL_LOCKS=0 \
GIT_NO_LAZY_FETCH=1 \
GIT_PAGER=cat \
codex \
  -s danger-full-access \
  -a untrusted \
  -c approvals_reviewer=user \
  --search
```

Codex の `/hooks` を開き、hook 定義を確認して信頼します。hook のコマンドやファイルを変更し、Codex が再レビューを求めた場合はもう一度確認してください。

### Python パッケージとして導入する方法
```bash
python3 -m pip install .
codex-read-only-approver --version
```

`hooks.json` では、可能ならインストールされたコマンドの絶対パスを指定してください。

## 設定
設定は任意です。次の順序で読み込みます。

1. `CODEX_READ_ONLY_APPROVER_CONFIG` が示すファイル
2. `$XDG_CONFIG_HOME/codex-read-only-approver/config.json`
3. `~/.config/codex-read-only-approver/config.json`

[`config.json.example`](config.json.example) を雛形にしてください。

```json
{
  "verify_executable_paths": true,
  "verify_ambient_environment": true,
  "extra_trusted_executable_roots": [],
  "max_command_bytes": 65536
}
```

通常、`verify_executable_paths` と `verify_ambient_environment` は `true` のままにします。後者は `GIT_EXTERNAL_DIFF`、`RIPGREP_CONFIG_PATH`（`rg --no-config` 使用時を除く）、`TAR_OPTIONS`、圧縮・展開コマンドのオプション環境変数などを検出します。

### Git の追加条件
Git は「読み取り」サブコマンドでも、pager、partial clone の lazy fetch、index refresh、textconv filter などを実行し得ます。そのため、この hook は次を要求します。

- `GIT_PAGER=cat` または `git --no-pager`
- `GIT_NO_LAZY_FETCH=1` または `git --no-lazy-fetch`
- `git status` では、さらに `GIT_OPTIONAL_LOCKS=0` または `git --no-optional-locks`
- `git diff`、通常の `git show`、patch を表示する `git log` / `git stash show` / `git reflog show` では `--no-ext-diff` と `--no-textconv` の両方

起動例の環境変数を使えば、各 Git コマンドに最初の2項目を繰り返す必要はありません。リポジトリ内の blob を直接読む `git show HEAD:path` は diff を生成しないため、この2つの diff helper 無効化オプションを要求しません。バージョンによって意味が変化し得る短縮形 `git branch -l` と `git tag -l` は意図的に人間承認へ回します。明示的な `--list` を使用してください。

実行ファイルのルートを追加するのは、次の両方を満たす場合に限定してください。

- 自動承認したいコマンドに必要である
- そのディレクトリを Codex が独立した人間承認なしに変更できない

`~/.local/bin`、`~/.cargo/bin`、言語マネージャーの shim、プロジェクト内の `node_modules/.bin` など、ユーザーが書き込める場所を追加すると、実行ファイルの同一性確認が弱くなります。

## 診断
Codex を起動せずに判定できます。

```bash
./codex_read_only_approver.py --check "sed -n '1,20p' README.md"
./codex_read_only_approver.py --check "sed -i 's/a/b/' file"
```

想定出力：

```text
ALLOW: sed program contains no write or execute command
ASK: segment 1: sed in-place mode writes files
```

分類器のテスト目的に限り、実行ファイルのパス確認を無効化できます。

```bash
./codex_read_only_approver.py --no-path-check --check \
  "GIT_PAGER=cat GIT_NO_LAZY_FETCH=1 GIT_OPTIONAL_LOCKS=0 git status"
```

PATH や実行ファイルの差し替えリスクを明示的に許容しない限り、本番の hook 定義には `--no-path-check` を入れないでください。

## Codex への推奨指示
[`AGENTS.md.snippet`](AGENTS.md.snippet) は Codex に次を促します。

- 読み取りと変更を別々の tool call に分ける
- shell glob ではなく引用済みパターンを使う
- 読み取り専用 Git 操作に `GIT_OPTIONAL_LOCKS=0` と `GIT_NO_LAZY_FETCH=1` を付ける
- Git pager を `cat` に固定し、diff 系では `--no-ext-diff --no-textconv` を付ける
- 編集には `apply_patch` または独立した明示的な変更コマンドを使う
- 読み取り操作で動的なシェル構文を使わない

これにより、承認判定の精度と監査可能性が上がります。

## テスト
```bash
python3 -m unittest -v
```

読み取り専用の正常系、書き込み可能・動的シェルの異常系、Codex hook の出力契約、不正 JSON、読み取りコマンドへ危険な接尾辞を付けたケースを検証しています。

## 成立に必要な運用条件
意図した挙動には、次の条件がすべて必要です。

- Codex が `approval_policy=untrusted` で動作する
- 承認先が `auto_review` ではなく `user` である
- 同梱の `codex-read-only-approver.rules` が有効で、対応コマンドを `PermissionRequest` に送っている
- 別の exec-policy ルールが変更コマンドを事前許可していない
- 別の `PermissionRequest` hook が変更コマンドに `allow` を返さない
- この hook が有効かつ信頼済みである
- 信頼済み実行ファイルのルートと shell 環境が管理されている
- 読み取りと変更を、一つの複合 shell コマンドに混在させない

Codex は、もともと承認を要求しようとしている操作に対してのみ `PermissionRequest` を呼びます。同梱ルールは、この判定点を既知安全コマンドにも作るためのものです。それでも、別の allow ルール、承認記憶、別 hook、または将来の実行経路が無承認実行を許せば、この hook を迂回し得ます。

## ライセンス
Apache License 2.0 です。[LICENSE](LICENSE) を参照してください。

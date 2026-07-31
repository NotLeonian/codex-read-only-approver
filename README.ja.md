# codex-read-only-approver
Codex CLI 用で、依存パッケージを必要としない保守的な `PermissionRequest` hook です。

Bash コマンド全体が、静的に読み取り専用と判定できるよう意図的に限定した文法に収まる場合だけ自動承認します。
書き込み、レビューされていない外部ヘルパーの実行、動的なシェル展開、不正な構文、未知のコマンドが含まれる場合は何も出力しません。
このため、Codex は通常どおり人間に承認を求めます。

主に次の運用を想定しています。

```bash
GIT_OPTIONAL_LOCKS=0 \
GIT_NO_LAZY_FETCH=1 \
GIT_PAGER=cat \
codex -s danger-full-access -a untrusted -c approvals_reviewer=user --search
```

これはサンドボックスではなく、Codex の承認ポリシーを置き換えるものでもありません。
使用前に [SECURITY.md](SECURITY.md) を確認してください。

## 動作概要
この hook は次を行います。

- パイプラインおよび `&&`、`||`、`;` の各セグメントを個別に解析する
- すべてのセグメントが読み取り専用と判定された場合だけ許可する
- `/dev/null` への破棄を除き、出力リダイレクトを承認しない
- コマンド置換、プロセス置換、引用符で囲まれていない glob やブレースの展開、heredoc、バックグラウンド実行、グループ化、未知のシェル構文を承認しない
- 既定で実行ファイルの実体が信頼済みのインストール先にあるか確認する
- 実行ファイルの確認が有効な場合、パスを含まない名前で実行ファイルを指定すると、`PATH` に相対パスを示す要素または空の要素が含まれていれば解決を拒否する
- 実行ファイルの確認が有効な場合は、認識可能なネイティブ ELF または Mach-O だけを自動承認し、インタープリターで動くラッパースクリプトは人間による承認へ回す
- `sed`、`git`、`find`、`fd`、`rg`、`uniq`、`base64`、`yq`、`tar`、各種圧縮コマンド、`unzip`、`sysctl`、`date`、`hostname`、`file`、`tree`、`nm`、`objdump` を引数も含めて判定する
- 書き込み可能または曖昧なコマンドでは何も出力せず、通常どおり人間による承認を求める
- 自動的な拒否は行わないため、分類器が理解しない操作でも人間が承認できる

以下の表は、冒頭で推奨した起動環境（`GIT_PAGER=cat` と `GIT_NO_LAZY_FETCH=1` を含む）を前提にしています。
自動承認される Git の例には、必須の `-c core.fsmonitor=` も付けています。

| コマンド | 結果 |
|---|---|
| `sed -n '1,80p' file` | 自動承認 |
| `sed 's/old/new/g' file` | 自動承認 |
| `sed -i 's/old/new/g' file` | 人間による承認 |
| `sed 'w output.txt' file` | 人間による承認 |
| `GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short` | 自動承認 |
| `git -c core.fsmonitor= status --short`（`GIT_OPTIONAL_LOCKS` が未設定） | 人間による承認（Git がインデックスを更新する可能性） |
| `git -c core.fsmonitor= diff --no-ext-diff --no-textconv --stat \| sed -n '1,20p'` | 自動承認 |
| `git branch -f name HEAD` | 人間による承認 |
| `uniq input output` | 人間による承認（2 番目の引数は出力先） |
| `sort input` | 人間による承認（内部で一時ファイルに書き出す可能性） |
| `cat input \| tee output` | 人間による承認 |
| `rg TODO . > matches.txt` | 人間による承認 |
| `python3 -c '...'` | 人間による承認 |

## 導入
### スクリプトを直接配置する方法
```bash
mkdir -p ~/.codex/hooks
install -m 0755 codex_read_only_approver.py \
  ~/.codex/hooks/codex_read_only_approver.py
```

[`hooks.json.example`](hooks.json.example) の内容を `~/.codex/hooks.json` に統合します。
例では `$HOME` 配下のスクリプトを直接実行します。
配置場所が異なる場合は変更してください。

次に、Codex がコマンドを既知の安全なものと判定して hook を迂回しないよう、同梱の exec-policy ルールを配置します。

```bash
mkdir -p ~/.codex/rules
install -m 0644 codex-read-only-approver.rules.example \
  ~/.codex/rules/codex-read-only-approver.rules
```

このルールは、対応しているコマンドを自動的に許可するものではありません。
該当するコマンドを必ず `PermissionRequest` に送ります。
hook が `allow` を返さなければ、Codex は通常どおり人間に承認を求めます。
hook を使わない Codex セッションでもルールは読み込まれるため、その場合は承認の要求が増えます。
`-a never` や承認を無効化するモードとは併用しないでください。

Git が必要に応じて行う書き込みと lazy fetch を抑止し、人間を承認者に指定して Codex を起動します。

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

Codex の `/hooks` を開き、hook の定義を確認して信頼します。
hook のコマンドやファイルを変更し、Codex が再度のレビューを求めた場合はもう一度確認してください。

### Python パッケージとして導入する方法
```bash
python3 -m pip install .
codex-read-only-approver --version
```

`hooks.json` では、可能ならインストールされたコマンドの絶対パスを指定してください。

### Windows で使用する場合
Windows のネイティブ環境で動作する Codex agent は PowerShell を使用しますが、この hook が扱うのは Bash/POSIX shell の文法だけです。
そのため、ネイティブ Windows では安全側に倒して自動承認を行わず、診断用の `--check` も `ASK` を返します。
Windows でこの hook を使う場合は、WSL 2 上で Codex を実行してください。

## 設定
設定は任意です。
hook は次の順序で設定を読み込みます。

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

通常、`verify_executable_paths` と `verify_ambient_environment` は `true` のままにします。
実行ファイルの確認では、信頼済みのルート内にあっても、shebang 付きのファイルなど、テキスト形式の実行ファイルを意図的に自動承認しません。
外側のコマンドだけからは、そのインタープリターや推移的に実行されるヘルパーを証明できないためです。
実行ファイルを名前だけで指定する場合、検索に使う `PATH` の各要素は、空でない絶対パスでなければなりません。
この条件を満たさない場合は、実行ファイルを絶対パスで指定するか、`PATH` の設定を修正してください。
`verify_ambient_environment` では、`GIT_EXTERNAL_DIFF`、`RIPGREP_CONFIG_PATH`（`rg --no-config` の使用時を除く）、`TAR_OPTIONS` のほか、圧縮・展開コマンドにオプションを渡す環境変数などを検出します。

### Git の追加条件
Git は読み取り用のサブコマンドでも、pager の起動、partial clone からの lazy fetch、インデックスの更新、FSMonitor として設定されたプログラムや textconv 用のヘルパーの実行などを行う可能性があります。
そのため、この hook は次を要求します。

- `git --version` 以外のすべての Git コマンドで `git -c core.fsmonitor= ...`
- `GIT_PAGER=cat` または `git --no-pager`
- `GIT_NO_LAZY_FETCH=1` または `git --no-lazy-fetch`
- `git status` では、さらに `GIT_OPTIONAL_LOCKS=0` または `git --no-optional-locks`
- `git diff`、通常の `git show`、patch を表示する `git log` / `git stash show` / `git reflog show` では `--no-ext-diff` と `--no-textconv` の両方

FSMonitor に空の値を指定しているのは意図的です。
Git 2.35.1 以前では `core.fsmonitor=false` が hook のパス名として解釈されますが、値を空にすれば、旧版と現行版のどちらでも設定された hook を無効化できます。
冒頭の起動例にある環境変数を設定すれば、pager と lazy fetch に関する指定を各コマンドで繰り返す必要はありません。
ただし、FSMonitor を無効にする `-c core.fsmonitor=` は各コマンドに必要です。
リポジトリ内の blob を直接読む `git -c core.fsmonitor= show HEAD:path` は diff を生成しないため、`--no-ext-diff` と `--no-textconv` は必要ありません。
バージョンによって意味が変化する可能性がある短縮形 `git branch -l` と `git tag -l` は、意図的に人間による承認へ回します。
明示的な `--list` を使用してください。

実行ファイルのルートを追加するのは、次の両方を満たす場合に限定してください。

- 自動承認の対象にしたいコマンドの実行に必要である
- Codex がそのディレクトリを変更するには、別途、人間の承認が必要である

`~/.local/bin`、`~/.cargo/bin`、言語マネージャーの shim、プロジェクト内の `node_modules/.bin` など、ユーザーが書き込める場所を追加すると、実行ファイルの同一性を確認する仕組みが弱くなります。

## 診断
Codex を起動せずに判定できます。

```bash
./codex_read_only_approver.py --check "sed -n '1,20p' README.md"
./codex_read_only_approver.py --check "sed -i 's/a/b/' file"
```

想定される出力：

```text
ALLOW: sed program contains no write or execute command
ASK: segment 1: sed in-place mode writes files
```

分類器をテストする場合に限り、実行ファイルのパスを確認する処理を無効にできます。

```bash
./codex_read_only_approver.py --no-path-check --check \
  "GIT_PAGER=cat GIT_NO_LAZY_FETCH=1 GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status"
```

PATH や実行ファイルの差し替えによるリスクを明示的に許容しない限り、本番の hook の定義には `--no-path-check` を入れないでください。

## Codex に与える指示の例
[`AGENTS.md.snippet`](AGENTS.md.snippet) は Codex に次を促します。

- 読み取り操作と変更操作を別々の tool call に分ける
- shell の glob に頼らず、パターンを引用符で囲む
- Git で読み取り専用の操作を行う場合は `git -c core.fsmonitor= ...`、`GIT_OPTIONAL_LOCKS=0`、`GIT_NO_LAZY_FETCH=1` を使う
- Git の pager を `cat` に固定し、diff を生成するコマンドでは `--no-ext-diff --no-textconv` を付ける
- 編集には `apply_patch` を使うか、変更を行うコマンドを明示的に別途実行する
- 読み取り操作では動的なシェル構文を使わない

これにより、承認を判断する精度が高まり、監査もしやすくなります。

## テスト
```bash
python3 -m unittest -v
```

読み取り専用のコマンドを扱う正常系、書き込み可能なコマンドや動的なシェル構文を扱う異常系、Codex hook の出力仕様、不正な JSON、読み取り用のコマンドに危険な接尾辞を付けたケースを検証しています。

CI では Ubuntu、macOS、Windows 上でテストと Python の品質チェックを実行します。
Windows の runner では、ネイティブ環境で自動承認しないことも検証しますが、これは PowerShell コマンドの分類に対応していることを意味しません。

## 想定どおりの動作に必要な条件
意図した動作には、次の条件がすべて必要です。

- Codex が `approval_policy=untrusted` で動作する
- 承認先が `auto_review` ではなく `user` である
- 同梱の `codex-read-only-approver.rules` が有効で、対応しているコマンドを `PermissionRequest` に送っている
- 別の exec-policy ルールが変更を行うコマンドを事前に許可していない
- 別の `PermissionRequest` hook が変更を行うコマンドに `allow` を返さない
- この hook が有効であり、信頼されている
- 信頼済みの実行ファイルを置くルートと shell の環境が管理されている
- 読み取り操作と変更操作を、1 つの複合 shell コマンドに混在させない

Codex は、承認を要求しようとしている操作に対してのみ `PermissionRequest` を呼び出します。
同梱のルールは、既知の安全なコマンドについても承認を判断する機会を設けます。
それでも、別の allow ルールや記憶された承認、別の hook、将来追加される実行経路によって承認なしでコマンドが実行される場合は、この hook が迂回される可能性があります。

## ライセンス
Apache License 2.0 です。
[LICENSE](LICENSE) を参照してください。

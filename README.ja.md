# codex-read-only-approver

Codex CLI 用で、依存パッケージを必要としない保守的な `PermissionRequest` hook です。

Bash コマンド全体が、静的に読み取り専用と判定できるよう意図的に限定した文法に収まる場合だけ自動承認します。
書き込み、レビューされていない外部ヘルパーの実行、動的なシェル展開、不正な構文、未知のコマンドが含まれる場合は何も出力しません。
この hook 自体は承認画面を強制しません。
応答を返さなかった要求をどう扱うかは、使用中の Codex のポリシーが決めます。

Codex 0.149.0 以降で `approval_policy = "on-request"` を使い、人間を承認者に指定する運用を想定しています。
さらに、Codex が変更内容を示す承認画面を表示するまで、タスクの対象ファイルを書き込めない permission profile を使います。
同梱の skill は読み取りをすぐに行い、Codex 標準の承認画面を利用できなければ変更前に停止します。

この hook はサンドボックスではなく、Codex の承認ポリシーを置き換えるものでもありません。
使用前に [SECURITY.md](SECURITY.md) を確認してください。

## 動作概要

この hook は次を行います。

- パイプラインおよび `&&`、`||`、`;` の各セグメントを個別に解析する
- すべてのセグメントが読み取り専用と判定された場合だけ許可する
- `/dev/null` への破棄を除き、出力リダイレクトを承認しない
- コマンド置換、プロセス置換、引用符で囲まれていない glob やブレースの展開、heredoc、バックグラウンド実行、グループ化、未知のシェル構文を承認しない
- 既定で実行ファイルの実体が信頼済みのインストール先にあるか確認する
- 実行ファイルの確認が有効な場合、パスを含まない名前で実行ファイルを指定すると、`PATH` に相対パスを示す要素または空の要素が含まれていれば解決を拒否する
- 実行ファイルの確認が有効な場合は、認識可能なネイティブ ELF または Mach-O だけを自動承認し、インタープリターで動くラッパースクリプトには判断を返さない
- `sed`、`git`、`find`、`fd`、`rg`、`uniq`、`base64`、`yq`、`tar`、各種圧縮コマンド、`unzip`、`sysctl`、`date`、`hostname`、`file`、`tree`、`nm`、`objdump` を引数も含めて判定する
- 書き込み可能または曖昧なコマンドでは何も出力せず、使用中の Codex のポリシーに判断を委ねる
- 自動的な拒否は行わないため、分類器が理解しない操作でも人間が承認できる

以下の表は、冒頭で推奨した起動環境（`GIT_PAGER=cat` と `GIT_NO_LAZY_FETCH=1` を含む）を前提にしています。
自動承認される Git の例には、必須の `-c core.fsmonitor=` も付けています。

| コマンド                                                                           | 結果                                                                           |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `sed -n '1,80p' file`                                                              | 自動承認                                                                       |
| `sed 's/old/new/g' file`                                                           | 自動承認                                                                       |
| `sed -i 's/old/new/g' file`                                                        | Codex 標準の画面で実行してよいか確認する                                       |
| `sed 'w output.txt' file`                                                          | Codex 標準の画面で実行してよいか確認する                                       |
| `GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short`                       | 自動承認                                                                       |
| `git -c core.fsmonitor= status --short`（`GIT_OPTIONAL_LOCKS` が未設定）           | Codex 標準の画面で実行してよいか確認する（Git がインデックスを更新する可能性） |
| `git -c core.fsmonitor= diff --no-ext-diff --no-textconv --stat \| sed -n '1,20p'` | 自動承認                                                                       |
| `git branch -f name HEAD`                                                          | Codex 標準の画面で実行してよいか確認する                                       |
| `uniq input output`                                                                | Codex 標準の画面で実行してよいか確認する（2 番目の引数は出力先）               |
| `sort input`                                                                       | この hook は自動承認しない。以後は使用中のポリシーが決める                     |
| `cat input \| tee output`                                                          | Codex 標準の画面で実行してよいか確認する                                       |
| `rg TODO . > matches.txt`                                                          | Codex 標準の画面で実行してよいか確認する                                       |
| `python3 -c '...'`                                                                 | この hook は自動承認しない。以後は使用中のポリシーが決める                     |

## 導入

Bash の例では、ホームディレクトリを表す `~` を引用符で囲んでいません。

また、実際の絶対パスに置き換えるプレースホルダーは、二重引用符で囲んでいます。
置き換えた後も二重引用符を残してください。

### スクリプトを直接配置する方法

```bash
mkdir -p ~/.codex/hooks
install -m 0755 codex_read_only_approver.py ~/.codex/hooks/codex_read_only_approver.py
```

[`hooks.json.example`](hooks.json.example) の内容を `~/.codex/hooks.json` に統合します。
例では `$HOME` 配下のスクリプトを直接実行します。
配置場所が異なる場合は変更してください。

次に、Codex がコマンドを既知の安全なものと判定して hook を迂回しないよう、同梱の exec-policy ルールを配置します。

```bash
mkdir -p ~/.codex/rules
install -m 0644 codex-read-only-approver.rules.example ~/.codex/rules/codex-read-only-approver.rules
```

このルールは、対応しているコマンドを自動的に許可するものではありません。
該当するコマンドを必ず `PermissionRequest` に送ります。
hook が `allow` を返さなければ、使用中の Codex のポリシーとクライアントが、その要求を引き続き扱います。
hook を使わない Codex セッションでもルールは読み込まれるため、その場合は承認の要求が増えます。
`-a never` や承認を無効化するモードとは併用しないでください。

この hook とルールが扱うのは、Bash コマンドの実行だけです。
ファイルの変更を承認する要求を発生させたり、変更内容を示したり、ファイルを変更する tool を仲介したりはできません。

### Codex 0.149.0 以降で変更を承認する方法

Codex CLI 0.149.0 の `--ask-for-approval` で指定できるのは `on-request` と `never` であり、以前の `untrusted` は使えません。
現行の App Server は、最初に変更内容を含む `fileChange` item を送り、その後に Codex 標準の承認を要求します。
クライアントは、ユーザーが承認または拒否するまで、その変更内容を表示できます。
判断を受け取ると、Codex は同じ作業を再開するか、その変更を行わずに終了します。
詳しくは公式の [App Server における承認の流れ](https://learn.chatgpt.com/docs/app-server#approvals) を参照してください。

ファイルの変更を承認できる TUI、アプリ、または IDE のクライアントを使ってください。
変更を毎回確認する場合は、セッション全体ではなく一度だけ承認します。
会話に差分を貼って Yes/No を尋ねる方法、`request_permissions`、コマンドの実行承認では代用できません。

Permission profile は、従来の `sandbox_mode` を使わずに workspace を書き込み不可にできます。
この機能は beta であり、macOS の Seatbelt など、各 OS のサンドボックスは引き続き使います。
読み込まれるすべての設定から `sandbox_mode` と `sandbox_workspace_write` を削除し、`--sandbox` も指定しないでください。
従来の設定が残っていると、`default_permissions` より優先されます。
詳しくは公式の [permission profile の説明](https://learn.chatgpt.com/docs/permissions) を参照してください。

次の厳格な例では、一般的な実行環境と使用中の workspace を読み取れますが、書き込みはできません。

```toml
approval_policy = "on-request"
approvals_reviewer = "user"
default_permissions = "review-project-writes"

[permissions.review-project-writes]
description = "Read project files and require native approval for changes."

[permissions.review-project-writes.filesystem]
":minimal" = "read"

[permissions.review-project-writes.filesystem.":workspace_roots"]
"." = "read"
```

この profile を読み込んだうえで、Git が必要に応じて行う書き込みと lazy fetch を抑止して Codex を起動します。

```bash
GIT_OPTIONAL_LOCKS=0 GIT_NO_LAZY_FETCH=1 GIT_PAGER=cat codex -a on-request --search
```

`-s` は従来のサンドボックス設定を選ぶため、追加しないでください。
クライアントがファイルの変更を承認する画面を表示できない場合、skill は会話上の差分や shell コマンドで迂回せず、書き込む前に停止します。

ツールによっては、キャッシュや一時ファイルへ書き込む必要があります。
workspace を書き込み可能にせず、それらを許可するには、`[permissions.review-project-writes.filesystem]` に `":tmpdir" = "write"` と `":slash_tmp" = "write"` を追加します。
この場合は承認なしの書き込みが実際に発生するため、文字どおりすべての書き込みを確認したい場合は追加しないでください。

プロジェクトの実行環境を使うという理由だけで、読み取り用の検証に承認を求めないでください。
使用中の profile がタスクの対象ファイルと外部の状態への書き込みを防いでいる場合、テスト、型チェック、lint、formatter の確認は自動的に実行します。
確認だけを行う mode を選び、bytecode、cache、incremental state は可能な限り無効にします。
使い捨ての cache を作ろうとしたことだけが失敗の原因なら、コマンド全体の承認を求めず、cache を無効にするか、すでに許可された一時ディレクトリへ移してください。
テストが本当に一時ファイルを必要とする場合は、上記の任意設定を使えます。
これは互換性を得るための明示的な例外であり、すべてのファイルシステムへの書き込みを承認する構成ではありません。

Python の tool では、`python -B`、Ruff の `--no-cache`、Mypy の `--cache-dir=/dev/null`（Unix）または `--cache-dir=nul`（Windows）を使うと、書き込みを抑止できます。
Mypy は `--no-incremental` を指定しても cache を書くため、この option だけでは不十分です。

### OrbStack と Docker socket

上記の厳格な profile では、Docker socket を許可していません。
OpenAI は Docker 用の Unix socket を許可する設定を[ローカルの escape hatch](https://learn.chatgpt.com/docs/permissions#unix-sockets) と説明しています。
また、OrbStack は[コンテナと macOS の間で bind mount を双方向に共有](https://docs.orbstack.dev/docker/file-sharing)します。
そのため、OrbStack の Docker socket へ接続できるプロセスは、ローカルのファイルシステムへ直接書き込めなくても、daemon にコンテナの状態を変更させたり、書き込み可能な bind mount を介して Mac のファイルを変更させたりできます。
これらの変更には、ファイルの変更を承認する画面が表示されません。

厳格に運用する場合は socket を許可せず、必要な Docker または OrbStack のコマンドごとに Codex 標準の実行承認を求めます。
現行の permission profile は、Docker API の読み取りだけを行う呼び出しと、変更を行う呼び出しを区別しません。
Docker の読み取りだけを自動的に許可し、変更だけを承認するには、API の操作を判別でき、独立して監査された読み取り専用 proxy などの境界が必要です。
skill と permission profile だけでは、この区別はできません。

すべての変更を承認することよりも OrbStack との互換性を優先する場合は、次の任意設定で、実際に使う socket だけを許可できます。

```toml
[features]
network_proxy = true

[permissions.review-project-writes.network]
enabled = true

[permissions.review-project-writes.network.unix_sockets]
"/absolute/path/to/orbstack/docker.sock" = "allow"
```

使用中の endpoint は `docker context inspect` で確認してください。
OrbStack では別の context 用 socket や symlink を使う場合があるため、`/var/run/docker.sock` と決めつけないでください。
絶対パスで、実際の socket だけを許可します。
この例外を追加すると、すべての変更に Codex 標準の承認を求める保証はできません。

Codex 自体を OrbStack の machine またはコンテナ内で動かす場合も、permission profile は内側のサンドボックスを使います。
外側のコンテナを隔離の境界にして、内側ではすべてのアクセスを許可すれば、サンドボックスを入れ子にした場合の問題は避けられます。
ただし、この構成では通常、ファイルを変更するときに差分を示す承認画面が表示されません。
そのため、現行の仕組みだけでは、内側で制限なく実行することと、ファイルの変更を毎回承認することを同時に保証できません。

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
tar で内容を一覧表示する場合は、`-z`、`-j`、`-J` などを使い、圧縮の方式を明示する必要があります。
これらを指定しない場合、GNU tar は入力が圧縮されているかどうかを自動的に判別し、必要に応じて `PATH` から展開に使うヘルパーを起動できます。
このため、圧縮されていないアーカイブの内容を一覧表示する場合も、Codex 標準の画面で実行してよいか確認します。

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
バージョンによって意味が変化する可能性がある短縮形 `git branch -l` と `git tag -l` は、Codex 標準の画面で実行してよいか確認します。
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
./codex_read_only_approver.py --no-path-check --check "GIT_PAGER=cat GIT_NO_LAZY_FETCH=1 GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status"
```

PATH や実行ファイルの差し替えによるリスクを明示的に許容しない限り、本番の hook の定義には `--no-path-check` を入れないでください。

## Codex に与える指示の例

[`AGENTS.md.snippet`](AGENTS.md.snippet) は Codex に次を促します。

- 読み取り操作と変更操作を別々の tool call に分ける
- shell の glob に頼らず、パターンを引用符で囲む
- Git で読み取り専用の操作を行う場合は `git -c core.fsmonitor= ...`、`GIT_OPTIONAL_LOCKS=0`、`GIT_NO_LAZY_FETCH=1` を使う
- Git の pager を `cat` に固定し、diff を生成するコマンドでは `--no-ext-diff --no-textconv` を付ける
- ファイルを変更する tool を呼び出し、変更内容をすべて示す Codex 標準の承認画面を必須とする
- 一度だけ承認されたら、承認画面を表示したところで応答を終えず、同じタスクで検証まで続ける
- ファイルの変更を承認する画面を利用できなければ、書き込む前に停止する
- タスクの対象ファイルと外部の状態へ書き込めない環境では、テスト、型チェック、lint、formatter の確認を承認なしで実行する
- 読み取り用の検証で cache を作るためだけに承認を求めず、cache を無効にするか、許可済みの一時ディレクトリへ移す
- 読み取り操作では動的なシェル構文を使わない

これにより、承認を判断する精度が高まり、監査もしやすくなります。

## Codex skill の例

[`SKILL.md.example`](SKILL.md.example) は、この hook が静的に読み取り専用と判定できる形式のコマンドを優先するよう Codex に指示する skill の例です。

このリポジトリではファイル名が `SKILL.md.example` であるため、有効な skill として読み込まれません。
Codex が skill として読み込むのは、skill 用ディレクトリ内にある `SKILL.md` です。
この skill を導入しても、変更されるのはコマンドの選び方だけです。
hook の自動承認の範囲や承認ポリシーを変更したり、変更を行うコマンドを読み取り専用として扱ったりするものではありません。
skill は Codex 標準の承認画面を使い、その画面を利用できなければ変更前に停止するよう指示します。
ただし、skill だけでクライアントに承認画面を強制表示することはできません。

### ファイルを直接配置する方法

ユーザーがすべてのリポジトリで利用する場合は、例をユーザー用の skill ディレクトリへコピーし、`SKILL.md` に名前を変更します。

```bash
mkdir -p ~/.agents/skills/codex-read-only-approver
install -m 0644 SKILL.md.example ~/.agents/skills/codex-read-only-approver/SKILL.md
```

特定のリポジトリだけで利用する場合は、`/path/to/repository` をそのリポジトリの絶対パスに置き換え、`.agents/skills` 配下へコピーします。

```bash
mkdir -p "/path/to/repository/.agents/skills/codex-read-only-approver"
install -m 0644 SKILL.md.example "/path/to/repository/.agents/skills/codex-read-only-approver/SKILL.md"
```

これらはファイルシステムを変更するため、この hook による自動承認の対象にはならないことを想定しています。
通常、Codex は skill の変更を自動的に検出します。
skill が表示されない場合は Codex を再起動してください。

### Python パッケージとして導入する方法

Python パッケージには、インストール済みのデータファイルとして `SKILL.md.example` が含まれます。

```bash
python3 -m pip install .
```

別のインストーラーモジュールは追加せず、パッケージに含まれる例をユーザー用の skill ディレクトリへコピーします。

```bash
python3 -c 'from importlib.metadata import distribution; from pathlib import Path; import shutil, sys; dist = distribution("codex-read-only-approver"); source = next(dist.locate_file(item) for item in dist.files or () if item.name == "SKILL.md.example"); target = Path(sys.argv[1]).expanduser(); target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)' ~/.agents/skills/codex-read-only-approver/SKILL.md
```

この導入コマンドは変更を行うため、この hook による自動承認の対象にはならないことを想定しています。

### 使用方法

必要に応じて、導入した skill を `$codex-read-only-approver` と明示して呼び出します。

```text
$codex-read-only-approver を使って、現在のリポジトリの状態を確認してください。
```

Codex がファイルを確認したり、テキストを検索したり、リポジトリの状態をレビューしたりするときは、この skill が自動的に選ばれる場合もあります。

## テスト

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
```

読み取り専用のコマンドを扱う正常系、書き込み可能なコマンドや動的なシェル構文を扱う異常系、Codex hook の出力仕様、不正な JSON、読み取り用のコマンドに危険な接尾辞を付けたケースを検証しています。

CI では Ubuntu、macOS、Windows 上でテストと Python の品質チェックを実行します。
Windows の runner では、ネイティブ環境で自動承認しないことも検証しますが、これは PowerShell コマンドの分類に対応していることを意味しません。

## 想定どおりの動作に必要な条件

意図した動作には、次の条件がすべて必要です。

- Codex が `approval_policy=on-request` で動作する
- 承認先が `auto_review` ではなく `user` である
- 使用中のクライアントがファイルの変更を承認する画面に対応している
- 承認前は、使用中の permission profile でタスクの対象ファイルを書き込めない
- セッション全体や記憶済みの許可ではなく、ファイルの変更ごとに一度だけ承認する
- 同梱の `codex-read-only-approver.rules` が有効で、対応しているコマンドを `PermissionRequest` に送っている
- 別の exec-policy ルールが変更を行うコマンドを事前に許可していない
- 別の `PermissionRequest` hook が変更を行うコマンドに `allow` を返さない
- この hook が有効であり、信頼されている
- 信頼済みの実行ファイルを置くルートと shell の環境が管理されている
- 読み取り操作と変更操作を、1 つの複合 shell コマンドに混在させない

Codex は、コマンドの実行について承認を判断する段階に達した場合だけ、Bash の `PermissionRequest` hook を呼び出します。
同梱のルールは、既知の安全なコマンドについても承認を判断する機会を設けます。
それでも、別の allow ルール、記憶された承認、別の hook、書き込み可能な permission profile、許可された daemon の socket、将来追加される実行経路によって、承認なしで操作が行われる場合は、この hook が迂回される可能性があります。
ファイルの変更を承認する仕組みは別の経路であり、使用中のクライアントと permission profile で有効にする必要があります。

## ライセンス

Apache License 2.0 です。
[LICENSE](LICENSE) を参照してください。

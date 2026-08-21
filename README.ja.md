# codex-read-only-approver

Codex CLI 用で、依存パッケージを必要としない保守的な `PermissionRequest` hook です。

Bash コマンド全体が、静的に読み取り専用と判定できるよう意図的に限定した文法に収まる場合だけ自動承認します。
書き込み、レビューされていない外部ヘルパーの実行、動的なシェル展開、不正な構文、未知のコマンドが含まれる場合は何も出力しません。
この hook 自体は承認画面を強制しません。
応答を返さなかった要求をどう扱うかは、使用中の Codex のポリシーが決めます。

Codex 0.149.0 以降で `approval_policy = "on-request"` を使い、人間を承認者に指定する運用を想定しています。
また、タスクの対象ファイルへ書き込む前に、使用中のクライアントとアクセス制御が、操作に応じた Codex 標準の承認画面を表示できる必要があります。
ファイルを直接編集する場合は、変更内容をすべて示す画面で承認を求めます。
変更を行うこと自体が目的のコマンドでは、正確なコマンドを一度だけ実行するための承認を求めます。
各 OS のサンドボックスを正常に初期化できる環境では、推奨する厳格な permission profile がファイルシステムの境界になります。
同梱の skill は、その profile の有無にかかわらず、読み取りをすぐに行い、操作に応じた Codex 標準の承認画面を利用できなければ変更前に停止します。

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

ファイルの変更とコマンドの実行を承認できる TUI、アプリ、または IDE のクライアントを使ってください。
変更を毎回確認する場合は、セッション全体ではなく一度だけ承認します。
ファイルを直接編集する場合は、会話に差分を貼って Yes/No を尋ねる方法、`request_permissions`、コマンドの実行承認では、変更内容を示す Codex 標準の承認画面を代用できません。

一方、コマンド自体が変更を行う操作では、正確なコマンドを一度だけ実行するための Codex 標準の承認を使ってかまいません。
formatter の write mode、generator、package manager、migration、container を変更するコマンドなどが該当します。
承認したコマンドが行う変更について、ファイルの変更を承認する画面を追加で求める必要はありません。
コマンドは単独の tool call として実行し、承認を記憶する規則は要求せず、承認後も同じタスクを続けて、生成された差分全体を確認します。
ファイルの変更を承認する画面を避けるためだけに、直接表せる編集を shell コマンドへ置き換えないでください。

Permission profile を使うと、従来の `sandbox_mode` を使わずに workspace への書き込みを禁止できます。
この機能は beta であり、macOS の Seatbelt のほか、Linux のコンテナや仮想マシン内でも Linux 用のサンドボックスを引き続き使います。
skill 自体は profile を要求したり変更したりしませんが、読み取り専用の profile が正常に動けば、タスクの対象ファイルを変更するときに承認を必須にする技術的な境界になります。
読み込まれるすべての設定から `sandbox_mode` と `sandbox_workspace_write` を削除し、`--sandbox` も指定しないでください。
従来の設定が残っていると、`default_permissions` より優先されます。
詳しくは公式の [permission profile の説明](https://learn.chatgpt.com/docs/permissions) を参照してください。

各 OS のサンドボックスが動作する環境では、次の厳格な例を使うと、一般的な実行環境と使用中の workspace を読み取れますが、書き込みはできません。

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
クライアントが操作に応じた Codex 標準の承認画面を表示できない場合、skill は会話上の確認や広い権限で迂回せず、書き込む前に停止します。
Codex を、後述する問題が起きる OrbStack 内で動かす場合は、厳格な運用を利用できません。
後述する設定は、読み取りだけを行う場合に限って使ってください。

ツールによっては、キャッシュや一時ファイルへ書き込む必要があります。
workspace を書き込み可能にせず、それらを許可するには、`[permissions.review-project-writes.filesystem]` に `":tmpdir" = "write"` と `":slash_tmp" = "write"` を追加します。
この場合は承認なしの書き込みが実際に発生するため、文字どおりすべての書き込みを確認したい場合は追加しないでください。

プロジェクトの実行環境を使うという理由だけで、検証コマンドを実行するための承認を求めないでください。
ただし、承認なしで実行できるのは、そのコマンドと、そこから起動されるすべての処理が、タスクの対象ファイルや外部の状態を変更できないように技術的に制限されている場合だけです。
check-only、dry-run、bytecode や cache を無効にする option は、tool が通常行う書き込みや付随的な書き込みを減らしますが、この制限を実現する境界の代わりにはなりません。
テストコード、plugin、設定から読み込む hook、compiler script は、これらの option にかかわらず、ファイルや外部の状態を変更できます。

書き込みを禁止する境界が有効な場合は、上記の option を使い、validator が interpreter で動くこと、プロジェクト内にあること、通常は cache を使うことだけを理由に承認を求めないでください。
使い捨ての cache を作ろうとしたことだけが失敗の原因なら、コマンド全体を実行するための承認を求めず、cache を無効にするか、すでに許可された一時ディレクトリへ移してください。
テストが本当に一時ファイルを必要とする場合は、上記の任意設定を使えます。
これは互換性を得るための明示的な例外であり、すべてのファイルシステムへの書き込みを承認する構成ではありません。

書き込みを禁止する境界がなければ、すべての validator を、状態を変更する可能性があるものとして扱い、正確なコマンドについて一度だけ Codex 標準の実行承認を求めます。
その承認画面を表示できない場合は validator を実行せず、実行できなかった検証項目を報告します。
後述する OrbStack の full-access mode ではコマンドの実行承認を確実に表示できないため、check-only や no-cache の option を指定しても、このような validator は利用できません。

Python の tool が通常行う書き込みを減らすには、`python -B`、Ruff の `--no-cache`、Mypy の `--cache-dir=/dev/null`（Unix）または `--cache-dir=nul`（Windows）を使えますが、これらも上記の境界の代わりにはなりません。
Mypy は `--no-incremental` を指定しても cache を書くため、この option だけでは不十分です。

### OrbStack 内で Codex を実行する場合

ファイルシステムを制限する permission profile や従来の sandbox mode を使うと、OrbStack の guest 内で Linux 用のサンドボックスがもう一つ動きます。
問題を確認した OrbStack の Ubuntu 22.04 x86_64 guest では、Codex 0.149.0 が内側のサンドボックスを適用するときに `SeccompInstall(EINVAL)` を返します。
agent がこの skill に従う前だけでなく、通常のコマンドを実行したり patch を検証したりするときにも失敗する可能性があります。

ローカル環境で fresh session と読み取りコマンドを試した結果は、次のとおりです。

| 試した条件                                                                | 確認した結果                                                                                                  |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `review-project-writes`                                                   | AGENTS の instruction を読む途中で fresh session が失敗し、agent は起動しない                                 |
| 同じ規則のまま、コマンドが network へ接続できるようにする                 | `codex sandbox ... sed` は成功するが、fresh session は AGENTS を読む途中で同じように失敗する                  |
| 従来の `sandbox_mode = "danger-full-access"`                              | 対話 TUI が起動し、skill のファイルを読んだ後、`sed` も内側のサンドボックスに起因するエラーを出さずに完了する |
| 同じ従来の mode で、変更を行う `touch` を実行し、承認画面が出るか確かめる | agent が一度だけの承認を求めようとした後も、TUI 内で別の承認画面を表示せずにコマンドを実行する                |

コマンドから network へ接続できるようにしても、実用的な回避策にはなりません。
Codex 0.149.0 は、AGENTS を探すときにファイルシステムの読み取りが制限されていると、内部の[ファイルシステム用サンドボックス](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/core/src/agents_md.rs#L52-L113)を使います。
この helper は、session の profile を引き継がず、network を [`Restricted`](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/exec-server/src/fs_sandbox.rs#L67-L152) に固定して profile を作り直します。
既存ファイルを変更する場合も、[承認を求める前に](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/core/src/tools/handlers/apply_patch.rs#L363-L393)同じ helper で patch の引数を確認します。
そのため、`codex doctor` が session の profile について network を許可していると表示しても、helper は問題の seccomp filter を適用しようとします。

`:read-only` にするとファイルシステムの規則は変わりますが、通常のコマンドや patch で内側のサンドボックスを使わなくなるわけではありません。
反対に、`:danger-full-access` または従来の `sandbox_mode = "danger-full-access"` を使うと、ファイルを変更するときに承認を必須にする境界がなくなります。
Codex 0.149.0 は、この場合に [patch を自動承認](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/core/src/safety.rs#L35-L60)します。
ローカルでコマンドを使って確認したときも、TUI 内では別の承認画面を表示せずに変更を行いました。
`approval_policy = "on-request"` だけでは、確認した構成でどちらの承認画面も復元できません。
`--dangerously-bypass-approvals-and-sandbox` は承認自体も無効にするため、代用できません。

したがって、問題が起きる OrbStack と Codex 0.149.0 では、設定やこの skill だけで、Codex が起動し、読み取りコマンドを自動で実行し、変更を行う前には必ず、操作に応じた Codex 標準の承認を求めるという 3 つの条件を同時に保証できません。
厳格に運用する場合は、macOS 側または制限付きの profile が動く別の環境で Codex を実行し、OrbStack へは個別に承認したコマンドだけで接続してください。
新しい Codex の version で解決したと判断する前に、upstream の修正を特定してください。
その後、fresh session を起動できるか、読み取りコマンドが動くか、ファイルを変更する前に承認画面が出るかをもう一度確認してください。

問題が起きる guest 内で、読み取りだけを行うという制約を受け入れる場合は、permission profile の設定を削除し、起動できることを確認した次の設定を使えます。

```toml
# 読み取りだけを行う。ファイルを変更するときの承認は保証しない。
approval_policy = "on-request"
approvals_reviewer = "user"
sandbox_mode = "danger-full-access"
```

この設定では、skill は、静的に読み取り専用だと判断できるコマンドを引き続き実行できます。
一方、formatter の write mode など、変更を行うコマンドを含め、タスクの対象ファイルや外部の状態を変更する操作はすべて利用できないものとして扱います。
テスト、型チェック、lint、formatter の確認を含むすべての validator は、静的に読み取り専用だと判断できるコマンドには含めず、check-only や no-cache の option を指定しても利用できないものとして扱います。
patch が自動承認されることは上記の source で確認でき、ローカルの TUI ではコマンドの実行承認を表示しませんでした。
そのため、タスク中に変更を試して承認画面が出るか確認しないでください。
ファイルの変更が必要なタスクでは、この設定を使わないでください。

### OrbStack と Docker socket へ接続する場合

上記の厳格な profile では、Docker socket を許可していません。
OpenAI は Docker 用の Unix socket を許可する設定を[ローカルの escape hatch](https://learn.chatgpt.com/docs/permissions#unix-sockets) と説明しています。
また、OrbStack は[コンテナと macOS の間で bind mount を双方向に共有](https://docs.orbstack.dev/docker/file-sharing)します。
そのため、OrbStack の Docker socket へ接続できるプロセスは、ローカルのファイルシステムへ直接書き込めなくても、daemon にコンテナの状態を変更させたり、書き込み可能な bind mount を介して Mac のファイルを変更させたりできます。
これらの変更には、ファイルの変更を承認する画面が表示されません。

厳格に運用する場合は socket を許可せず、必要な Docker または OrbStack のコマンドごとに Codex 標準の実行承認を求めます。
現行の permission profile は、Docker API の読み取りだけを行う呼び出しと、変更を行う呼び出しを区別しません。
Docker の読み取りだけを自動的に許可し、変更だけを承認するには、API の操作を判別でき、独立して監査された読み取り専用 proxy などの境界が必要です。
skill と permission profile だけでは、この区別はできません。

各 OS のサンドボックスと管理された network proxy が正常に動く環境では、すべての変更を承認することよりも OrbStack への接続を優先する場合に限り、次の任意設定で実際に使う socket だけを許可できます。

```toml
[features]
network_proxy = true

[permissions.review-project-writes.network]
enabled = true

[permissions.review-project-writes.network.unix_sockets]
"/absolute/path/to/orbstack/docker.sock" = "allow"
```

使用中の endpoint は `docker context inspect` で確認してください。
OrbStack では別の context 用 socket や symlink を使う場合があるため、実際の socket path を確認し、`/var/run/docker.sock` を前提にしないでください。
絶対パスで、実際の socket だけを許可します。
この例外を追加すると、すべての変更に Codex 標準の承認を求める保証はできません。

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
- ファイルを直接編集する場合は、変更内容をすべて示す Codex 標準の承認画面を必須とする
- formatter など、コマンド自体が変更を行う場合は、ファイルの変更を承認する画面を追加で求めず、そのコマンドを一度だけ実行するための Codex 標準の承認を求める
- 一度だけ承認されたら、承認画面を表示したところで応答を終えず、同じタスクで検証まで続ける
- 操作に応じた Codex 標準の承認画面を利用できなければ、書き込む前に停止する
- validator と、そこから起動されるコードが、タスクの対象ファイルや外部の状態を変更できないように技術的に制限されている場合だけ、検証を承認なしで実行する
- check-only や no-cache の option は通常の書き込みを減らすために使い、書き込みを禁止する境界の代わりにはしない
- 書き込みを禁止する境界がなければ一度だけコマンドの実行承認を求め、その画面を表示できなければ実行できなかった検証項目を報告する
- 読み取り操作では動的なシェル構文を使わない

これにより、承認を判断する精度が高まり、監査もしやすくなります。

## Codex skill の例

[`SKILL.md.example`](SKILL.md.example) は、この hook が静的に読み取り専用と判定できる形式のコマンドを優先するよう Codex に指示する skill の例です。

このリポジトリではファイル名が `SKILL.md.example` であるため、有効な skill として読み込まれません。
Codex が skill として読み込むのは、skill 用ディレクトリ内にある `SKILL.md` です。
この skill を導入しても、変更されるのはコマンドの選び方だけです。
hook の自動承認の範囲や承認ポリシーを変更したり、変更を行うコマンドを読み取り専用として扱ったりするものではありません。
skill は、直接編集する場合はファイルの変更を承認する画面を使い、変更を行うコマンドでは実行承認を使うよう指示します。
操作に応じた画面を利用できなければ、変更前に停止します。
ただし、skill だけでクライアントにどちらの画面も強制表示することはできません。

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

これらはファイルシステムを変更するため、一度だけの Codex 標準の実行承認を使います。
実行結果について、ファイルの変更を承認する画面を追加で求める必要はありません。
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

この導入コマンドは変更を行うため、hook で自動承認せず、一度だけの Codex 標準の実行承認を使います。

### 使用方法

必要に応じて、導入した skill を `$codex-read-only-approver` と明示して呼び出します。

```text
$codex-read-only-approver を使って、現在のリポジトリの状態を確認してください。
```

Codex がファイルを確認したり、テキストを検索したり、リポジトリの状態をレビューしたりするときは、この skill が自動的に選ばれる場合もあります。

## テスト

```bash
python -B -m unittest -v
```

読み取り専用のコマンドを扱う正常系、書き込み可能なコマンドや動的なシェル構文を扱う異常系、Codex hook の出力仕様、不正な JSON、読み取り用のコマンドに危険な接尾辞を付けたケースを検証しています。

CI では Ubuntu、macOS、Windows 上でテストと Python の品質チェックを実行します。
Windows の runner では、ネイティブ環境で自動承認しないことも検証しますが、これは PowerShell コマンドの分類に対応していることを意味しません。

## 想定どおりの動作に必要な条件

意図した動作には、次の条件がすべて必要です。

- Codex が `approval_policy=on-request` で動作する
- 承認先が `auto_review` ではなく `user` である
- 使用中のクライアントがファイルの変更を承認する画面とコマンドの実行承認に対応している
- 使用中のアクセス制御を正常に初期化でき、承認前はタスクの対象ファイルを書き込めない
- 外部の状態を変更するすべての操作に承認を必須とする場合は、コマンドから network へ直接、無制限に接続できず、daemon socket にも接続できない
- 直接編集する場合は変更内容をすべて示す画面を使い、変更を行うコマンドでは正確なコマンドを示す実行承認を使う
- セッション全体や記憶済みの許可ではなく、毎回一度だけ承認する
- 同梱の `codex-read-only-approver.rules` が有効で、対応しているコマンドを `PermissionRequest` に送っている
- 別の exec-policy ルールが変更を行うコマンドを事前に許可していない
- 別の `PermissionRequest` hook が変更を行うコマンドに `allow` を返さない
- この hook が有効であり、信頼されている
- 信頼済みの実行ファイルを置くルートと shell の環境が管理されている
- 読み取り操作と変更操作を、1 つの複合 shell コマンドに混在させない

Codex は、コマンドの実行について承認を判断する段階に達した場合だけ、Bash の `PermissionRequest` hook を呼び出します。
同梱のルールは、既知の安全なコマンドについても承認を判断する機会を設けます。
それでも、別の allow ルール、記憶された承認、別の hook、書き込み可能な permission profile、コマンドから直接接続できる network、許可された daemon の socket、将来追加される実行経路によって、承認なしで操作が行われる場合は、この hook が迂回される可能性があります。
ファイルの変更を承認する経路はコマンドの実行経路と分かれているため、使用中のクライアントとアクセス制御が、操作に応じた承認を必須にする必要があります。

## ライセンス

Apache License 2.0 です。
[LICENSE](LICENSE) を参照してください。

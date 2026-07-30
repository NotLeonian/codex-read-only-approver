# codex-read-only-approver
A conservative, dependency-free `PermissionRequest` hook for Codex CLI.

It auto-approves a Bash command only when the entire command string fits a deliberately small, statically read-only grammar. Anything that writes, executes an unreviewed helper, uses dynamic shell expansion, is malformed, or is unknown produces no hook decision and therefore falls through to Codex's normal human approval prompt.

This project is designed for the following operating model:

```bash
GIT_OPTIONAL_LOCKS=0 \
GIT_NO_LAZY_FETCH=1 \
GIT_PAGER=cat \
codex -s danger-full-access -a untrusted -c approvals_reviewer=user --search
```

It is not a sandbox and does not replace Codex approval policy. Read [SECURITY.md](SECURITY.md) before relying on it.

## What it does
The hook:

- parses complete pipelines and `&&` / `||` / `;` chains;
- requires every segment to be classified as read-only;
- rejects output redirection except output discarded to `/dev/null`;
- rejects command substitution, process substitution, unquoted glob/brace expansion, heredocs, backgrounding, grouping, and unknown shell syntax;
- verifies executable paths against trusted installation roots by default;
- handles argument-sensitive commands including `sed`, `git`, `find`, `fd`, `rg`, `uniq`, `base64`, `yq`, `tar`, compressors, `unzip`, `sysctl`, `date`, `hostname`, `file`, `tree`, `nm`, and `objdump`;
- remains silent for a write-capable or ambiguous command, preserving the normal human approval flow;
- never returns an automatic denial. The human can still approve an operation that the classifier does not understand.

The table below assumes the recommended launch environment, including `GIT_PAGER=cat` and `GIT_NO_LAZY_FETCH=1`.

| Command | Result |
|---|---|
| `sed -n '1,80p' file` | automatic allow |
| `sed 's/old/new/g' file` | automatic allow |
| `sed -i 's/old/new/g' file` | human approval |
| `sed 'w output.txt' file` | human approval |
| `GIT_OPTIONAL_LOCKS=0 git status --short` | automatic allow |
| `git status --short` with `GIT_OPTIONAL_LOCKS` unset | human approval (Git may refresh the index) |
| `git diff --no-ext-diff --no-textconv --stat \| sed -n '1,20p'` | automatic allow |
| `git branch -f name HEAD` | human approval |
| `uniq input output` | human approval (second operand is an output file) |
| `sort input` | human approval (may spill to temporary files) |
| `cat input \| tee output` | human approval |
| `rg TODO . > matches.txt` | human approval |
| `python3 -c '...'` | human approval |

## Installation
### Direct script installation
```bash
mkdir -p ~/.codex/hooks
install -m 0755 codex_read_only_approver.py \
  ~/.codex/hooks/codex_read_only_approver.py
```

Merge the contents of [`hooks.json.example`](hooks.json.example) into `~/.codex/hooks.json`. The example invokes the script through `$HOME`; adjust the command if your hook environment uses a different location.

Install the accompanying exec-policy rules so commands on Codex's own known-safe list do not bypass this hook:

```bash
mkdir -p ~/.codex/rules
install -m 0644 codex-read-only-approver.rules.example \
  ~/.codex/rules/codex-read-only-approver.rules
```

The rules do not auto-allow these commands. They route supported command families through `PermissionRequest`; if the hook does not return `allow`, Codex shows its normal human approval prompt. Codex also loads this file in sessions where the hook is absent, which increases prompts. Do not combine it with `-a never` or another mode that forbids approval prompts.

Start Codex with a human reviewer and Git's known optional writes/lazy fetch disabled:

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

Open `/hooks` in Codex, inspect the exact hook definition, and trust it. Re-review it after changing the hook command or files when Codex reports that review is required.

### Optional package installation
The repository is also installable as a small Python package:

```bash
python3 -m pip install .
codex-read-only-approver --version
```

Use an absolute path to the installed console script in `hooks.json` when possible.

### Windows
Native Windows Codex agents use PowerShell, but this hook recognizes only Bash/POSIX shell grammar. It therefore fails closed on native Windows, does not auto-approve commands, and reports `ASK` in diagnostic `--check` mode. To use this hook on Windows, run Codex inside WSL 2.

## Configuration
Configuration is optional. The hook reads, in order:

1. the file named by `CODEX_READ_ONLY_APPROVER_CONFIG`; or
2. `$XDG_CONFIG_HOME/codex-read-only-approver/config.json`; or
3. `~/.config/codex-read-only-approver/config.json`.

Start from [`config.json.example`](config.json.example).

```json
{
  "verify_executable_paths": true,
  "verify_ambient_environment": true,
  "extra_trusted_executable_roots": [],
  "max_command_bytes": 65536
}
```

`verify_executable_paths` and `verify_ambient_environment` should normally remain `true`. Ambient checks reject option-injecting variables such as `GIT_EXTERNAL_DIFF`, `RIPGREP_CONFIG_PATH` (unless `rg --no-config` is used), `TAR_OPTIONS`, and compressor/archive option variables.

### Additional Git conditions
Even nominally read-only Git commands may launch a pager, lazy-fetch partial-clone objects, refresh the index, or execute a configured textconv filter. The hook therefore requires:

- `GIT_PAGER=cat` or `git --no-pager`;
- `GIT_NO_LAZY_FETCH=1` or `git --no-lazy-fetch`;
- for `git status`, also `GIT_OPTIONAL_LOCKS=0` or `git --no-optional-locks`; and
- for `git diff`, ordinary `git show`, and patch-producing `git log` / `git stash show` / `git reflog show`, both `--no-ext-diff` and `--no-textconv`.

The recommended launch environment supplies the first two conditions once for the entire Codex process. A direct blob read such as `git show HEAD:path` does not generate a diff and therefore does not require these two diff-helper flags. Version-sensitive shorthand such as `git branch -l` and `git tag -l` is deliberately sent to human review; use explicit `--list` forms.

Add an executable root only when:

- it is required for commands you intend to auto-approve; and
- Codex cannot modify that directory without an independently reviewed write operation.

Adding user-writable locations such as `~/.local/bin`, `~/.cargo/bin`, language-manager shims, or a project `node_modules/.bin` weakens the executable identity check.

## Diagnostics
Classify a command without starting Codex:

```bash
./codex_read_only_approver.py --check "sed -n '1,20p' README.md"
./codex_read_only_approver.py --check "sed -i 's/a/b/' file"
```

Expected output:

```text
ALLOW: sed program contains no write or execute command
ASK: segment 1: sed in-place mode writes files
```

For classifier tests only, executable path verification can be disabled:

```bash
./codex_read_only_approver.py --no-path-check --check \
  "GIT_PAGER=cat GIT_NO_LAZY_FETCH=1 GIT_OPTIONAL_LOCKS=0 git status"
```

Do not put `--no-path-check` in the production hook definition unless you explicitly accept PATH and executable-substitution risk.

## Recommended Codex guidance
[`AGENTS.md.snippet`](AGENTS.md.snippet) encourages Codex to:

- separate inspection and mutation into different tool calls;
- quote patterns rather than rely on shell glob expansion;
- use `GIT_OPTIONAL_LOCKS=0` and `GIT_NO_LAZY_FETCH=1` for read-only Git inspection;
- force the Git pager to `cat` and add `--no-ext-diff --no-textconv` to diff-producing commands;
- use `apply_patch` or a separate explicit command for edits;
- avoid dynamic shell constructs in inspection commands.

This improves both approval precision and auditability.

## Tests
```bash
python3 -m unittest -v
```

The test suite covers positive read-only cases, write-capable and dynamic-shell cases, the Codex hook output contract, malformed input, and mutation suffixes appended to otherwise safe commands.

CI runs the tests and Python quality checks on Ubuntu, macOS, and Windows. The Windows runner also verifies native fail-closed behavior; it does not imply support for classifying PowerShell commands.

## Required deployment assumptions
For the intended behavior, all of the following must remain true:

- Codex uses `approval_policy=untrusted`;
- approvals are routed to `user`, not `auto_review`;
- the included `codex-read-only-approver.rules` file is active and routes supported commands through `PermissionRequest`;
- mutating commands are not pre-allowed by another exec-policy rule;
- no other matching `PermissionRequest` hook returns `allow` for those commands;
- the hook is active and trusted;
- trusted executable roots and the shell environment are controlled;
- ambiguous commands remain separate from read-only commands rather than being hidden in one compound shell fragment.

Codex only invokes `PermissionRequest` when it is already about to request approval. The included rules create that decision point for known-safe command families. A separate allow rule, remembered approval, another hook, or a future execution path can still bypass this hook if it runs a command without requesting approval.

## License
Apache License 2.0. See [LICENSE](LICENSE).

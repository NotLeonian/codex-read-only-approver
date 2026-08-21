# codex-read-only-approver

A conservative, dependency-free `PermissionRequest` hook for Codex CLI.

It auto-approves a Bash command only when the entire command string fits a deliberately small, statically read-only grammar.
Anything that writes, executes an unreviewed helper, uses dynamic shell expansion, is malformed, or is unknown produces no hook decision.
The hook does not itself force a prompt; the active Codex policy decides what happens to an unresolved request.

This project is designed for Codex 0.149.0 and later with `approval_policy = "on-request"`, a human reviewer, and a client and permission boundary that can present the applicable native approval before a task file is written.
Direct edits use native file-change approval over the complete proposed change; intentionally write-producing commands use one-time native command approval over the exact command.
On hosts where the platform sandbox initializes successfully, the recommended strict permission profile provides a hard filesystem boundary.
The companion skill applies independently of that profile: it runs read-only inspections immediately and fails closed before a mutation if the applicable native approval UI is unavailable.

The hook is not a sandbox and does not replace Codex approval policy.
Read [SECURITY.md](SECURITY.md) before relying on it.

## What it does

The hook:

- parses complete pipelines and `&&` / `||` / `;` chains;
- requires every segment to be classified as read-only;
- rejects output redirection except output discarded to `/dev/null`;
- rejects command substitution, process substitution, unquoted glob/brace expansion, heredocs, backgrounding, grouping, and unknown shell syntax;
- verifies executable paths against trusted installation roots by default;
- rejects relative or empty `PATH` components before resolving bare executables when path verification is enabled;
- requires a recognized native ELF or Mach-O executable when path verification is enabled, leaving interpreted wrapper scripts unresolved;
- handles argument-sensitive commands including `sed`, `git`, `find`, `fd`, `rg`, `uniq`, `base64`, `yq`, `tar`, compressors, `unzip`, `sysctl`, `date`, `hostname`, `file`, `tree`, `nm`, and `objdump`;
- remains silent for a write-capable or ambiguous command, leaving the pending request to the active Codex policy;
- never returns an automatic denial.
  The human can still approve an operation that the classifier does not understand.

The table below assumes the recommended launch environment, including `GIT_PAGER=cat` and `GIT_NO_LAZY_FETCH=1`.
Every automatically allowed Git example also carries the required `-c core.fsmonitor=` override.

| Command                                                                            | Result                                                     |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `sed -n '1,80p' file`                                                              | automatic allow                                            |
| `sed 's/old/new/g' file`                                                           | automatic allow                                            |
| `sed -i 's/old/new/g' file`                                                        | native command approval                                    |
| `sed 'w output.txt' file`                                                          | native command approval                                    |
| `GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short`                       | automatic allow                                            |
| `git -c core.fsmonitor= status --short` with `GIT_OPTIONAL_LOCKS` unset            | native command approval (Git may refresh the index)        |
| `git -c core.fsmonitor= diff --no-ext-diff --no-textconv --stat \| sed -n '1,20p'` | automatic allow                                            |
| `git branch -f name HEAD`                                                          | native command approval                                    |
| `uniq input output`                                                                | native command approval (second operand is an output file) |
| `sort input`                                                                       | not auto-approved by this hook; active policy decides      |
| `cat input \| tee output`                                                          | native command approval                                    |
| `rg TODO . > matches.txt`                                                          | native command approval                                    |
| `python3 -c '...'`                                                                 | not auto-approved by this hook; active policy decides      |

## Installation

In Bash examples, `~` represents the home directory and is left unquoted.

In addition, replaceable absolute-path placeholders are enclosed in double quotes.
Keep the double quotes after substituting the actual path.

### Direct script installation

```bash
mkdir -p ~/.codex/hooks
install -m 0755 codex_read_only_approver.py ~/.codex/hooks/codex_read_only_approver.py
```

Merge the contents of [`hooks.json.example`](hooks.json.example) into `~/.codex/hooks.json`.
The example invokes the script through `$HOME`; adjust the command if your hook environment uses a different location.

Install the accompanying exec-policy rules so commands on Codex's own known-safe list do not bypass this hook:

```bash
mkdir -p ~/.codex/rules
install -m 0644 codex-read-only-approver.rules.example ~/.codex/rules/codex-read-only-approver.rules
```

The rules do not auto-allow these commands.
They route supported command families through `PermissionRequest`; if the hook does not return `allow`, the request remains pending for the active Codex policy and client.
Codex also loads this file in sessions where the hook is absent, which increases prompts.
Do not combine it with `-a never` or another mode that forbids approval prompts.

The hook and rules cover Bash command execution only.
They cannot create `item/fileChange/requestApproval`, display a proposed file diff, or mediate a file-change tool.

### Codex 0.149.0 and native change approval

Codex CLI 0.149.0 accepts `on-request` and `never` for `--ask-for-approval`; the former `untrusted` value is no longer available.
The current App Server first sends a `fileChange` item containing the proposed changes, then sends the native approval request.
The client can display those changes while it waits for an accept or decline decision, after which the server resumes or declines the same work.
See the official [App Server approval flow](https://learn.chatgpt.com/docs/app-server#approvals).

Use an interactive TUI, app, or IDE client that supports native file-change and command-execution requests.
Choose one-time approval rather than `acceptForSession` when every change must be reviewed.
For a direct file edit, a diff pasted into chat, `request_permissions`, and command-execution approval are not substitutes for the native file-change UI.

A command whose intended operation produces changes may instead use one-time native command-execution approval for the exact command.
This includes formatters in write mode, generators, package managers, migrations, and mutating container commands.
Do not require an additional file-change approval for changes produced by that approved command.
Keep the command in a separate tool call without a persistent approval rule, continue the same task after approval, and inspect the complete resulting diff.
Do not wrap a directly representable edit in a shell command merely to avoid the file-change UI.

Permission profiles replace the legacy `sandbox_mode` settings and can keep the workspace non-writable without setting `sandbox_mode = "read-only"`.
They are beta and still use the platform sandbox, including Seatbelt on macOS and the Linux sandbox inside Linux containers or virtual machines.
The skill does not require or change a profile, but a working read-only profile supplies the technical boundary that forces task-file changes through native approval.
Remove `sandbox_mode` and `sandbox_workspace_write` from every loaded config layer, and do not pass `--sandbox`, because legacy settings take precedence over `default_permissions`.
See the official [permission profile documentation](https://learn.chatgpt.com/docs/permissions).

On a host where the platform sandbox works, this strict example allows common runtime reads and reads within the active workspace, but no writes:

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

With that profile loaded, start Codex with Git's known optional writes and lazy fetch disabled:

```bash
GIT_OPTIONAL_LOCKS=0 GIT_NO_LAZY_FETCH=1 GIT_PAGER=cat codex -a on-request --search
```

Do not add `-s`; it selects the legacy sandbox system.
If a client cannot present the applicable native approval, the skill must stop before writing rather than use a conversational prompt or broad permission as a workaround.
If Codex runs inside an affected OrbStack guest, the strict workflow is unavailable; use the degraded configuration below only for inspection-only work.

Some toolchains need to write caches or temporary files.
To allow those writes without making the workspace writable, add `":tmpdir" = "write"` and `":slash_tmp" = "write"` to `[permissions.review-project-writes.filesystem]`.
Those are real unapproved writes, so omit them when the requirement literally covers every filesystem write.

Validation should not prompt merely because it uses a project environment, but the no-write property must be enforced rather than inferred from flags.
Run tests, type checkers, linters, and formatter checks automatically only when an active boundary technically prevents the command and every process or code path it starts from modifying task files or external state.
Check-only, dry-run, no-bytecode, no-cache, and no-incremental options reduce expected or incidental writes, but they are not a no-write boundary; test code, plugins, configuration hooks, and compiler scripts may still mutate state.

When an enforceable no-write boundary is active, prefer those options and do not request approval merely because a validator is interpreted, project-local, or normally uses a cache.
If a check fails only because it tried to create disposable cache data, disable that cache or redirect it to an already allowed temporary directory instead of escalating the whole command.
A test that genuinely needs temporary files can use the optional temp grants above; this is a deliberate compatibility exception, not approval of every filesystem write.

Without an enforceable no-write boundary, treat every validator as potentially mutating and request one-time native command approval for the exact command.
If that approval path is unavailable, do not run the validator and report the omitted validation.
In the affected OrbStack full-access mode below, command approval is not reliable, so such validators are unavailable even with check-only or no-cache options.

For Python tools, options that reduce expected writes include `python -B`, Ruff `--no-cache`, and Mypy `--cache-dir=/dev/null` on Unix or `--cache-dir=nul` on Windows; they do not replace the boundary described above.
Mypy still writes cache data with `--no-incremental`, so that flag alone is insufficient.

### Running Codex inside OrbStack

Restrictive permission profiles and legacy sandbox modes start another Linux sandbox inside the OrbStack guest.
In the affected OrbStack Ubuntu 22.04 x86_64 guest, Codex 0.149.0 reports `SeccompInstall(EINVAL)` while applying that inner sandbox.
The failure can occur before the agent follows this skill or while an ordinary command or patch is being checked.

The local end-to-end probes produced these results:

| Tested condition                                           | Observed result                                                                                                           |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `review-project-writes`                                    | A fresh session fails while loading AGENTS instructions, before the agent starts.                                         |
| The same filesystem rules with command network enabled     | Direct `codex sandbox ... sed` succeeds, but a fresh session still fails at AGENTS loading.                               |
| Legacy `sandbox_mode = "danger-full-access"`               | The interactive TUI starts, reads the skill file, and runs `sed` with no inner sandbox error.                             |
| The same legacy mode with a mutating `touch` command probe | The command runs without a separate approval prompt inside the TUI after the agent attempts to request one-time approval. |

Enabling command network access is not a usable workaround.
Codex 0.149.0 routes restricted filesystem reads for AGENTS discovery through an internal [filesystem sandbox](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/core/src/agents_md.rs#L52-L113).
That helper rebuilds the profile with network access hard-coded to [`Restricted`](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/exec-server/src/fs_sandbox.rs#L67-L152), regardless of the session profile.
Existing-file patch verification uses the same helper [before approval](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/core/src/tools/handlers/apply_patch.rs#L363-L393).
The helper therefore tries to install the failing seccomp filter even when `codex doctor` reports that the session profile has network enabled.

`:read-only` changes the filesystem policy but does not remove the inner sandbox from ordinary command and patch paths.
Conversely, `:danger-full-access` or legacy `sandbox_mode = "danger-full-access"` removes the boundary that would force a file-change request.
Codex 0.149.0 then [auto-approves patches](https://github.com/openai/codex/blob/rust-v0.149.0/codex-rs/core/src/safety.rs#L35-L60), and the local command probe also ran without a separate approval prompt inside the TUI.
`approval_policy = "on-request"` alone does not restore either prompt in this tested configuration.
`--dangerously-bypass-approvals-and-sandbox` also disables approvals and is never a substitute.

Consequently, the affected OrbStack environment and Codex 0.149.0 cannot use configuration or this skill alone to guarantee all three properties: successful startup, automatic read-only commands, and the applicable native approval for every mutation.
For the strict workflow, run Codex on the macOS host or another runtime where the restrictive profile works, and access OrbStack only through separately approved commands.
Do not assume that a later release fixes the incompatibility; repeat an end-to-end test only after identifying an upstream fix.

If you intentionally accept an inspection-only degraded mode inside the affected guest, remove permission-profile settings and use the tested startup configuration:

```toml
# Inspection only: this does not guarantee approval for file changes.
approval_policy = "on-request"
approvals_reviewer = "user"
sandbox_mode = "danger-full-access"
```

Under that configuration, this skill may continue statically read-only inspections, but it must treat every task-file and external-state mutation as unavailable, including write-mode formatters and other mutating commands.
All validators, including tests, type checkers, linters, and formatter checks, are outside the statically read-only inspection category and are unavailable even with check-only or no-cache options.
The source confirms patch auto-approval, and the tested TUI did not show command approval, so do not probe for approval by attempting a mutation during a task.
Do not use this degraded mode for a task that requires file changes.

### Accessing OrbStack and Docker sockets

The strict profile intentionally does not allow a Docker socket.
OpenAI documents Unix socket proxying for Docker as a [local escape hatch](https://learn.chatgpt.com/docs/permissions#unix-sockets), and OrbStack supports [two-way bind mounts between containers and macOS](https://docs.orbstack.dev/docker/file-sharing).
It follows that a process with access to the OrbStack Docker socket can ask the daemon to change container state or write to a Mac path through a writable bind mount even when the local process has read-only filesystem access.
Such changes do not pass through native file-change approval.

For the strict workflow, keep the socket blocked and request native command approval for each required Docker or OrbStack command.
Current permission profiles do not distinguish read-only Docker API calls from mutating calls.
Automatically allowing Docker reads while approving only mutations requires an independently audited, API-aware read-only proxy or equivalent boundary; the skill and permission profile alone cannot provide that distinction.

On a host where the platform sandbox and managed network proxy work, this optional exception enables the exact active socket when OrbStack access is more important than approving every possible mutation:

```toml
[features]
network_proxy = true

[permissions.review-project-writes.network]
enabled = true

[permissions.review-project-writes.network.unix_sockets]
"/absolute/path/to/orbstack/docker.sock" = "allow"
```

Find the active endpoint with `docker context inspect`; do not assume `/var/run/docker.sock`, because OrbStack may expose a different context socket or symlink.
Allow only the exact absolute socket path.
This exception is not compatible with a guarantee that every mutation receives native approval.

Open `/hooks` in Codex, inspect the exact hook definition, and trust it.
Re-review it after changing the hook command or files when Codex reports that review is required.

### Optional package installation

The repository is also installable as a small Python package:

```bash
python3 -m pip install .
codex-read-only-approver --version
```

Use an absolute path to the installed console script in `hooks.json` when possible.

### Windows

Native Windows Codex agents use PowerShell, but this hook recognizes only Bash/POSIX shell grammar.
It therefore fails closed on native Windows, does not auto-approve commands, and reports `ASK` in diagnostic `--check` mode.
To use this hook on Windows, run Codex inside WSL 2.

## Configuration

Configuration is optional.
The hook reads, in order:

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

`verify_executable_paths` and `verify_ambient_environment` should normally remain `true`.
Path verification deliberately does not auto-approve shebang or other text executables, even inside a trusted root, because their interpreter and transitive helper execution cannot be proven from the outer command.
Each `PATH` component used to resolve a bare executable must be non-empty and absolute; use an absolute executable path or clean `PATH` when that condition is not met.
Ambient checks reject option-injecting variables such as `GIT_EXTERNAL_DIFF`, `RIPGREP_CONFIG_PATH` (unless `rg --no-config` is used), `TAR_OPTIONS`, and compressor/archive option variables.
Tar listings require an explicit supported compression option such as `-z`, `-j`, or `-J`.
Without one, GNU tar can detect a compressed input and dispatch a decompressor through `PATH`, so even a nominally uncompressed listing remains unresolved for native command approval.

### Additional Git conditions

Even nominally read-only Git commands may launch a pager, lazy-fetch partial-clone objects, refresh the index, or execute configured FSMonitor and textconv helpers.
The hook therefore requires:

- `git -c core.fsmonitor= ...` for every Git command other than `git --version`;
- `GIT_PAGER=cat` or `git --no-pager`;
- `GIT_NO_LAZY_FETCH=1` or `git --no-lazy-fetch`;
- for `git status`, also `GIT_OPTIONAL_LOCKS=0` or `git --no-optional-locks`; and
- for `git diff`, ordinary `git show`, and patch-producing `git log` / `git stash show` / `git reflog show`, both `--no-ext-diff` and `--no-textconv`.

The empty FSMonitor value is deliberate.
Unlike `core.fsmonitor=false`, which Git 2.35.1 and earlier interpret as a hook pathname, an empty value disables the configured hook across old and current Git versions.
The recommended launch environment supplies the pager and lazy-fetch conditions once for the entire Codex process, while each command must carry the FSMonitor override.
A direct blob read such as `git -c core.fsmonitor= show HEAD:path` does not generate a diff and therefore does not require the two diff-helper flags.
Version-sensitive shorthand such as `git branch -l` and `git tag -l` is deliberately left for native command approval; use explicit `--list` forms.

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
./codex_read_only_approver.py --no-path-check --check "GIT_PAGER=cat GIT_NO_LAZY_FETCH=1 GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status"
```

Do not put `--no-path-check` in the production hook definition unless you explicitly accept PATH and executable-substitution risk.

## Recommended Codex guidance

[`AGENTS.md.snippet`](AGENTS.md.snippet) encourages Codex to:

- separate inspection and mutation into different tool calls;
- quote patterns rather than rely on shell glob expansion;
- use `git -c core.fsmonitor= ...`, `GIT_OPTIONAL_LOCKS=0`, and `GIT_NO_LAZY_FETCH=1` for read-only Git inspection;
- force the Git pager to `cat` and add `--no-ext-diff --no-textconv` to diff-producing commands;
- use the patch/file-change tool and require native approval over the complete proposed change for direct edits;
- use one-time native command approval, without an additional file-change approval, for formatters and other commands whose intended operation produces changes;
- continue the same task after one-time approval rather than ending merely to show the approval UI;
- stop before writing if the applicable native approval is unavailable;
- run validators automatically only when an enforceable boundary prevents them and the code they start from modifying task files or external state;
- use check-only and no-cache options to reduce expected writes, without treating those options as a no-write boundary;
- require one-time native command approval when that boundary is absent, or report the validator as unavailable when approval cannot be presented;
- avoid dynamic shell constructs in inspection commands.

This improves both approval precision and auditability.

## Optional Codex skill example

[`SKILL.md.example`](SKILL.md.example) is an example Codex skill that instructs Codex to prefer command forms this hook can classify as statically read-only.

The file is inactive in this repository because Codex loads a skill only from a file named `SKILL.md` inside a skill directory.
Installing it changes command-selection guidance only.
It does not widen the hook's automatic allowlist, change the approval policy, or make a mutating command read-only.
It directs Codex to use file-change approval for direct edits, command approval for write-producing commands, and to fail closed when the current client cannot provide the applicable UI.
A skill cannot technically force the client to display either UI.

### Direct file installation

For user-wide use, copy the example to the user skill directory and rename it to `SKILL.md`:

```bash
mkdir -p ~/.agents/skills/codex-read-only-approver
install -m 0644 SKILL.md.example ~/.agents/skills/codex-read-only-approver/SKILL.md
```

For use only in another repository, replace `/path/to/repository` with that repository's absolute path and copy the example below its `.agents/skills` directory:

```bash
mkdir -p "/path/to/repository/.agents/skills/codex-read-only-approver"
install -m 0644 SKILL.md.example "/path/to/repository/.agents/skills/codex-read-only-approver/SKILL.md"
```

These installation commands modify the filesystem and should use one-time native command approval; their results do not also require file-change approval.
Codex normally detects skill changes automatically; restart Codex if the skill does not appear.

### Optional package installation

The Python package includes `SKILL.md.example` in its installed data files.

```bash
python3 -m pip install .
```

After installation, copy the packaged example to the user skill directory without adding a separate installer module:

```bash
python3 -c 'from importlib.metadata import distribution; from pathlib import Path; import shutil, sys; dist = distribution("codex-read-only-approver"); source = next(dist.locate_file(item) for item in dist.files or () if item.name == "SKILL.md.example"); target = Path(sys.argv[1]).expanduser(); target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)' ~/.agents/skills/codex-read-only-approver/SKILL.md
```

The installation command is intentionally explicit and should use one-time native command approval rather than automatic hook approval.

### Usage

Invoke the installed skill explicitly with `$codex-read-only-approver` when needed.

```text
Use $codex-read-only-approver to inspect the current repository state.
```

Codex may also select it automatically when it inspects files, searches text, or reviews repository state.

## Tests

```bash
python -B -m unittest -v
```

The test suite covers positive read-only cases, write-capable and dynamic-shell cases, the Codex hook output contract, malformed input, and mutation suffixes appended to otherwise safe commands.

CI runs the tests and Python quality checks on Ubuntu, macOS, and Windows.
The Windows runner also verifies native fail-closed behavior; it does not imply support for classifying PowerShell commands.

## Required deployment assumptions

For the intended behavior, all of the following must remain true:

- Codex uses `approval_policy=on-request`;
- approvals are routed to `user`, not `auto_review`;
- the active client supports native file-change approval and native command-execution approval;
- the active permission boundary initializes successfully and keeps task files non-writable before approval;
- commands have neither direct unrestricted network access nor a daemon socket when every external-state mutation must be forced through approval;
- direct edits use file-change approval over the complete proposed change, while intentionally write-producing commands use approval over the exact command;
- every approval is one-time rather than `acceptForSession` or a remembered decision;
- the included `codex-read-only-approver.rules` file is active and routes supported commands through `PermissionRequest`;
- mutating commands are not pre-allowed by another exec-policy rule;
- no other matching `PermissionRequest` hook returns `allow` for those commands;
- the hook is active and trusted;
- trusted executable roots and the shell environment are controlled;
- ambiguous commands remain separate from read-only commands rather than being hidden in one compound shell fragment.

Codex only invokes the Bash `PermissionRequest` hook for command execution that reaches that approval point.
The included rules create that decision point for known-safe command families.
A separate allow rule, remembered approval, another hook, a writable permission profile, unrestricted command network access, an allowed daemon socket, or a future execution path can still bypass this hook if it runs an operation without requesting approval.
The native file-change path is separate from command execution; the client and active permission boundary must enforce both approval paths where they apply.

## License

Apache License 2.0.
See [LICENSE](LICENSE).

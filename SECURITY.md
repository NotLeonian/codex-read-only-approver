# Security model

## Purpose

`codex-read-only-approver` reduces approval fatigue by automatically approving a narrow set of commands whose **visible shell syntax and documented options** are classified as read-only.
It fails closed relative to auto-approval: an unknown, malformed, dynamic, write-capable, or helper-executing command emits no decision.
The hook does not itself force Codex to request human approval; the active policy and client decide how an unresolved request proceeds.

## What the hook enforces

For commands that reach Codex `PermissionRequest`, the hook attempts to ensure that an automatic allow contains none of the following explicit mechanisms:

- file output redirection, except discarding output to `/dev/null`;
- append or in-place editing flags;
- command or process substitution;
- shell grouping, backgrounding, heredocs, and unsupported syntax;
- unquoted glob or brace expansion that could inject option-looking filenames;
- known command options that execute helpers, editors, pagers, plugins, or subprocesses;
- unneutralized repository-configured Git FSMonitor hooks;
- known write, delete, configuration mutation, archive extraction, or system-setting operations;
- executables outside configured trusted installation roots; and
- interpreted or unrecognized executable formats, whose transitive interpreter and helper execution cannot be verified.

Pipelines and command chains are approved only when every parsed segment is approved.

## What the hook cannot guarantee

This is not a syscall-level read-only boundary.
A command string classifier cannot prove that an arbitrary executable performs no writes internally.

Examples include:

- merely reading a file can update access-time metadata depending on the filesystem and mount options;
- programs may create caches, temporary files, telemetry, history, lock files, or database state internally;
- commands that may legitimately spill to temporary files, such as `sort`, are deliberately not auto-approved;
- Git may invoke configured attribute, signature, or other repository-controlled helpers that are outside this classifier's complete knowledge;
- partial-clone Git reads may lazy-fetch and store missing objects unless lazy fetch is disabled;
- a nominal read can access a special device or filesystem with side effects;
- shell startup code, aliases, functions, or a changed PATH can alter which implementation runs;
- an executable can be replaced between classification and execution;
- a trusted binary, repository, plugin, pager, diff driver, locale database, or configuration file can be malicious;
- reading secrets is still a read and may expose them in command output;
- Codex may not call `PermissionRequest` for operations already allowed by its own policy or another rule;
- another matching hook may return `allow` independently;
- a write-capable permission profile may let a file-change tool write without native approval;
- a full-access fallback used to avoid a nested sandbox may allow local changes without native approval;
- an allowed Docker or other daemon socket may change external state or host files outside the local filesystem boundary;
- future command versions may add new side-effecting options not yet recognized by this classifier.

For a hard no-write guarantee before approval, use a permission profile or other mandatory access-control boundary that initializes successfully and technically denies writes to task files.
Direct edits require native file-change approval over the complete proposed change.
An intentionally write-producing command, such as a formatter in write mode, may instead use one-time native command approval for the exact command; it does not also require file-change approval for its results.
Do not wrap a directly representable edit in a shell command merely to select the command-approval path.
The companion skill must stop before mutation when the current client cannot present the applicable native approval.
Neither the hook nor the skill can technically force either UI while the active environment already permits the write.

## Deployment requirements

Use all of the following:

```text
approval_policy = on-request
approvals_reviewer = user
```

Codex 0.149.0 no longer accepts `untrusted`.
On a supported host, use a permission profile that keeps the active workspace non-writable, and use an interactive client that implements native file-change and command-execution approval.
Permission profiles replace, rather than compose with, `sandbox_mode` and `sandbox_workspace_write`; remove the legacy settings from every loaded configuration layer.
See the official [permission profile documentation](https://learn.chatgpt.com/docs/permissions) and [App Server approval flow](https://learn.chatgpt.com/docs/app-server#approvals).

A strict least-privilege starting point is:

```toml
default_permissions = "review-project-writes"

[permissions.review-project-writes.filesystem]
":minimal" = "read"

[permissions.review-project-writes.filesystem.":workspace_roots"]
"." = "read"
```

Granting write access to `:tmpdir` or `:slash_tmp` can improve tool compatibility, but those writes occur without approval.
Allowing an OrbStack or Docker Unix socket is a broader exception: the daemon can change external state and, with a writable bind mount, host files.
OpenAI documents Unix socket proxying as a local escape hatch, and OrbStack documents two-way bind mounts with macOS.
Do not allow that socket in a workflow that must approve every possible mutation.

In the tested OrbStack Ubuntu 22.04 x86_64 guest, a restrictive profile makes Codex 0.149.0 fail with `SeccompInstall(EINVAL)` while its inner Linux sandbox is applied.
Enabling network in the session profile does not fix fresh-session startup because the internal filesystem helper rebuilds its profile with restricted network access.
The same helper participates in existing-file patch verification before approval.

`:danger-full-access` and legacy `sandbox_mode = "danger-full-access"` avoid that inner sandbox, but they remove the filesystem boundary and can allow file changes without native approval.
In the local interactive probe, Codex 0.149.0 also ran a mutating `touch` command without showing a separate approval prompt inside the TUI after the agent attempted to request one-time command approval.
They are an inspection-only degraded mode, not an equivalent fallback for this project's strict workflow.
For the strict guarantee, run Codex on the macOS host or another runtime where the restrictive profile initializes, and retest only after identifying an upstream fix.
See [README.md](README.md#running-codex-inside-orbstack) for the measured results and source paths.

Tests, type checkers, linters, and formatter checks can run without approval when their command forms are constrained not to mutate task files or external state.
This rule does not depend on a read-only profile being active.
Use check-only modes and disable bytecode, caches, and incremental state where possible.
Do not grant a validator broader permissions solely so it can create disposable cache data; either disable the cache or place it in an explicitly allowed temporary directory.

Install the supplied `codex-read-only-approver.rules.example` as an active Codex rules file.
It routes supported command families through `PermissionRequest`, including commands that Codex might otherwise classify as known-safe.
Without this mediation rule, the hook cannot inspect an operation that Codex runs without asking.

Do not combine the intended policy with `auto_review`, `-a never`, `--ignore-rules`, `acceptForSession`, remembered approvals, or another mode that bypasses or forbids the native approval path.
Review all exec-policy rules and all other matching hooks.
A remembered or explicit allow rule can cause a command to bypass this hook's approval point.

Keep executable verification enabled.
Only add trusted executable roots that cannot be modified by the agent without a separately reviewed write operation.

Use a clean non-interactive shell where possible.
Avoid aliases and shell functions that shadow allowed command names.

The classifier understands POSIX Bash syntax only.
The native Windows Codex agent runs commands in PowerShell, whose quoting, escaping, operators, and redirections have different semantics.
The hook therefore fails closed on native Windows and emits no approval decision, and the diagnostic `--check` mode returns `ASK`.
Use Codex in WSL2 when running this hook on a Windows machine.

Launch Codex with conservative Git environment defaults where practical:

```bash
GIT_OPTIONAL_LOCKS=0 \
GIT_NO_LAZY_FETCH=1 \
GIT_PAGER=cat \
codex -a on-request --search
```

The hook requires configured FSMonitor execution, the pager, and lazy fetch to be explicitly disabled for Git commands, and optional locks to be disabled before auto-approving `git status`.
Diff-producing commands also require both `--no-ext-diff` and `--no-textconv`.
It accepts the same environment variables as explicit per-command prefixes.
Every Git command other than `git --version` must use `git -c core.fsmonitor= ...`.
The empty value works across Git versions; `core.fsmonitor=false` is not accepted because Git 2.35.1 and earlier interpret `false` as a hook pathname.
These settings prevent configured FSMonitor execution and reduce pager execution, on-demand object fetches, optional index writes, and configured external-diff and text-conversion execution, but they do not turn Git or repository configuration into a formally verified read-only system.

## Fail-closed behavior

- malformed hook JSON: no decision;
- invalid configuration: no decision;
- parser exception: no decision;
- unknown command or option: no decision;
- executable path verification failure: no decision.

No decision means only that this hook did not auto-approve the request.
The active Codex policy and client determine whether to prompt, decline, or proceed.

## Reporting a vulnerability

Do not include secrets or destructive proof-of-concept payloads in a public issue.
Report a minimal command that is incorrectly classified as `ALLOW`, the operating system, command version, and expected side effect through the repository's private security advisory channel.

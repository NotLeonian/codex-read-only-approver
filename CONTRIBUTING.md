# Contributing

Changes to an automatic allowlist are security-sensitive.

## Required for classifier changes

Every new automatic-allow path must include:

1. at least one positive test for the intended read-only form;
2. negative tests for every documented write, execute, helper, output-file, and in-place option of that command;
3. a compound-command test showing that a dangerous suffix or pipeline is not upgraded to `ALLOW`;
4. a source note in the pull request identifying the command documentation reviewed;
5. confirmation that the executable-path and shell-expansion assumptions remain valid.

Unknown syntax must remain `ASK`.
Do not broaden a rule merely to suppress a prompt.

## Third-party code

Do not copy code, tests, regular expressions, or documentation from an unlicensed source.
For licensed code, document the exact source, revision, license, required notices, and compatibility before incorporation.

## Test commands

On Unix-like systems, including WSL 2, run:

```bash
python -B -m ruff format --check codex_read_only_approver.py test_codex_read_only_approver.py
python -B -m ruff check --no-cache codex_read_only_approver.py test_codex_read_only_approver.py
python -B -m mypy --cache-dir=/dev/null codex_read_only_approver.py test_codex_read_only_approver.py
python -B -m pyright codex_read_only_approver.py test_codex_read_only_approver.py
python -B -m unittest -v
python -m py_compile codex_read_only_approver.py test_codex_read_only_approver.py
```

On native Windows, use the same commands but replace the Mypy command with:

```powershell
python -B -m mypy --cache-dir=nul codex_read_only_approver.py test_codex_read_only_approver.py
```

The first five commands, with the Windows-specific Mypy variant when applicable, avoid repository cache and bytecode writes, although tests may still create temporary fixtures.
These options reduce expected writes but do not sandbox test code, plugins, configuration hooks, or compiler scripts; when Codex runs these commands, it may proceed without approval only behind an enforceable no-write boundary, or it must use one-time native command approval for the exact command.
The final `py_compile` command writes bytecode and therefore needs a separate approval or an explicitly allowed cache destination.

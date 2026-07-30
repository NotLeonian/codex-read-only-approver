# Contributing
Changes to an automatic allowlist are security-sensitive.

## Required for classifier changes
Every new automatic-allow path must include:

1. at least one positive test for the intended read-only form;
2. negative tests for every documented write, execute, helper, output-file, and in-place option of that command;
3. a compound-command test showing that a dangerous suffix or pipeline is not upgraded to `ALLOW`;
4. a source note in the pull request identifying the command documentation reviewed;
5. confirmation that the executable-path and shell-expansion assumptions remain valid.

Unknown syntax must remain `ASK`. Do not broaden a rule merely to suppress a prompt.

## Third-party code
Do not copy code, tests, regular expressions, or documentation from an unlicensed source. For licensed code, document the exact source, revision, license, required notices, and compatibility before incorporation.

## Test command
```bash
python -m ruff format --check codex_read_only_approver.py test_codex_read_only_approver.py
python -m ruff check codex_read_only_approver.py test_codex_read_only_approver.py
python -m mypy codex_read_only_approver.py test_codex_read_only_approver.py
python -m pyright codex_read_only_approver.py test_codex_read_only_approver.py
python -m unittest -v
python -m py_compile codex_read_only_approver.py test_codex_read_only_approver.py
```

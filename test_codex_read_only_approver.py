# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import codex_read_only_approver as hook

CONFIG = hook.Config(
    verify_executable_paths=False,
    trusted_executable_roots=(),
    max_command_bytes=65_536,
)

SAFE_GIT_ENV = {
    "GIT_PAGER": "cat",
    "GIT_NO_LAZY_FETCH": "1",
}

# Keep tests independent of shell/runtime injection variables in the developer
# or CI environment while retaining variables needed to spawn Python on Windows.
TEST_ENV = {
    name: os.environ[name]
    for name in (
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    if name in os.environ
}
TEST_ENV.update(SAFE_GIT_ENV)
TEST_ENV["CODEX_READ_ONLY_APPROVER_CONFIG"] = str(
    Path(__file__).with_name(".nonexistent-test-config.json")
)


ALLOW_CASES = {
    "basic cat": "cat README.md",
    "directory listing": "ls -la",
    "grep": "grep -n TODO src/app.py",
    "ripgrep": "rg --hidden 'TODO|FIXME' .",
    "fd": "fd -t f py src",
    "find": "find . -type f -name '*.py'",
    "find safe exec": r"find . -type f -exec cat {} \;",
    "sed range": "sed -n '1,120p' README.md",
    "sed substitution": "sed -E 's/foo/bar/g' README.md",
    "sed delete output": "sed '/^#/d' README.md",
    "sed multiple expressions": "sed -n -e '1,5p' -e '$p' README.md",
    "sed combined expression": "sed -ne '1,5p' README.md",
    "sed read another file": "sed '1r header.txt' body.txt",
    "pipeline": "git diff --no-ext-diff --no-textconv -- README.md | sed -n '1,80p' | head -40",
    "chain": "pwd && GIT_OPTIONAL_LOCKS=0 git status --short && rg TODO .",
    "multiline read-only": "pwd\nGIT_OPTIONAL_LOCKS=0 git status --short\nrg TODO .",
    "input redirect": "wc -l < README.md",
    "stderr discard": "grep x missing 2>/dev/null",
    "fd duplicate": "GIT_OPTIONAL_LOCKS=0 git status 2>&1",
    "safe env": "LC_ALL=C NO_COLOR=1 rg TODO .",
    "git status global flag": "git --no-optional-locks status --short",
    "git status optional locks": "GIT_OPTIONAL_LOCKS=0 git status --short",
    "git diff": "git --no-pager diff --no-ext-diff --no-textconv --stat",
    "git log": "git log --oneline -20",
    "git show": "git -C . show HEAD:README.md",
    "git branch show": "git branch --show-current",
    "git branch list": "git branch --list 'feature/*'",
    "git for-each-ref": "git for-each-ref --format='%(refname:short)' refs/heads/",
    "git tag list": "git tag --list 'v*'",
    "git config get": "git config --get user.name",
    "git config modern get": "git config get user.name",
    "git config modern list": "git config list",
    "git config shorthand get": "git config user.name",
    "git remote list": "git remote -v",
    "git remote url": "git remote get-url origin",
    "git stash list": "git stash list",
    "git stash show": "git stash show -p --no-ext-diff --no-textconv stash@{0}",
    "git worktree list": "git worktree list --porcelain",
    "git reflog": "git reflog show --date=iso",
    "git notes": "git notes list",
    "git submodule": "git submodule status",
    "uniq stdout": "uniq -c names.txt",
    "printf stdout": "printf '%s\n' hello",
    "diff stdout": "diff -u before.txt after.txt",
    "diff3 stdout": "diff3 mine.txt base.txt theirs.txt",
    "base64 stdout": "base64 input.bin",
    "base64 decode stdout": "base64 -d input.b64",
    "jq": "jq -r '.items[]?.name' data.json",
    "yq stdout": "yq '.services' compose.yaml",
    "yq pretty stdout": "yq -P '.services' compose.yaml",
    "tar list": "tar -tf archive.tar",
    "tar long list": "tar --list --file archive.tar",
    "tar list alphabetic archive": "tar -tf archive",
    "tar compressed list": "tar -ztf archive.tar.gz",
    "gzip stdout": "gzip -dc archive.gz",
    "gunzip test": "gunzip -t archive.gz",
    "zcat": "zcat archive.gz | head",
    "unzip list": "unzip -l archive.zip",
    "unzip stdout": "unzip -p archive.zip README.md | head",
    "sysctl read": "sysctl kern.ostype",
    "sysctl all": "sysctl --all",
    "date format": "date +%Y-%m-%d",
    "date UTC": "date --utc +%Y-%m-%d",
    "date BSD no-set parse": "date -j 01010000 +%Y-%m-%d",
    "hostname query": "hostname -s",
    "file inspect": "file README.md",
    "file brief": "file --brief README.md",
    "nm inspect": "nm binary",
    "objdump inspect": "objdump -h binary",
    "tree listing": "tree -L 2 .",
    "timeout wrapper": "timeout 5 GIT_OPTIONAL_LOCKS=0 git status --short",
    "command wrapper": "command -- git --no-optional-locks status --short",
    "command lookup": "command -v git",
    "env wrapper": "env LC_ALL=C GIT_OPTIONAL_LOCKS=0 git status --short",
    "comment": "GIT_OPTIONAL_LOCKS=0 git status # read only",
    "literal dollar in sed": "sed -n '$p' README.md",
}


ASK_CASES = {
    "empty": "",
    "unknown": "my-custom-tool inspect",
    "relative executable": "./git status",
    "write redirect": "git status > status.txt",
    "network device input": "cat </dev/tcp/example.com/80",
    "append redirect": "cat a >> b",
    "tee write": "cat a | tee b",
    "background": "git status &",
    "subshell": "(git status)",
    "command substitution": "echo $(touch owned)",
    "backticks": "echo `touch owned`",
    "double quote expansion": 'echo "$HOME"',
    "process substitution": "diff <(cat a) <(cat b)",
    "heredoc": "cat <<'EOF'\nhello\nEOF",
    "unsafe env": "GIT_CONFIG_COUNT=1 git status",
    "standalone env assignment": "LC_ALL=C",
    "sed inplace": "sed -i 's/a/b/' file",
    "sed inplace backup": "sed -i.bak 's/a/b/' file",
    "sed long inplace": "sed --in-place=.bak 's/a/b/' file",
    "sed trailing inplace": "sed -e 'p' file -i",
    "sed unquoted glob": "sed -n 'p' *",
    "sed brace option injection": "sed -n 'p' {-i,file}",
    "sed write command": "sed 'w output.txt' input.txt",
    "sed W command": "sed 'W output.txt' input.txt",
    "sed substitution write": "sed 's/a/b/w output.txt' input.txt",
    "sed execute": "sed 'e touch owned' input.txt",
    "sed substitution execute": "sed 's/a/b/e' input.txt",
    "sed script file": "sed -f transform.sed input.txt",
    "sed control flow": "sed '/x/{p}' input.txt",
    "rg pre": "rg --pre 'python transform.py' pattern .",
    "rg zip": "rg -z pattern archive.zip",
    "rg combined zip": "rg -zH pattern archive.zip",
    "fd exec": "fd -x rm {}",
    "fd combined exec": "fd -HIx rm {}",
    "find delete": "find . -delete",
    "find fprint": "find . -fprint output.txt",
    "find unsafe exec": r"find . -exec rm {} \;",
    "find shell exec": r"find . -exec sh -c 'touch owned' \;",
    "git status may write index": "git status --short",
    "git add": "git add README.md",
    "git commit": "git commit -m test",
    "git push": "git push origin main",
    "git fetch": "git fetch origin",
    "git pull": "git pull --ff-only",
    "git checkout": "git checkout main",
    "git switch": "git switch -c feature",
    "git restore": "git restore README.md",
    "git reset": "git reset --hard HEAD~1",
    "git clean": "git clean -fd",
    "git branch create": "git branch new-branch",
    "git branch delete": "git branch -D old",
    "git branch force move": "git branch -f name HEAD",
    "git branch upstream": "git branch -u origin/main name",
    "git branch legacy short l": "git branch -l",
    "git branch signature format": "git branch --format='%(signature:grade)' --list",
    "git tag create": "git tag v1.0.0",
    "git tag annotated": "git tag -a v1 -m release",
    "git tag clustered annotate": "git tag -av1 -m release",
    "git tag legacy short l": "git tag -l",
    "git tag signature format": "git tag --format='%(signature:grade)' --list",
    "git for-each-ref signature format": (
        "git for-each-ref --format='%(signature:grade)' refs/heads/"
    ),
    "git config set": "git config user.name Alice",
    "git config modern set": "git config set user.name Alice",
    "git config unset": "git config --unset user.name",
    "git config get with edit": "git config get --edit user.name",
    "git remote add": "git remote add origin https://example.com/repo.git",
    "git stash push": "git stash push -m temp",
    "git worktree add": "git worktree add ../other branch",
    "git notes add": "git notes add -m note",
    "git submodule update": "git submodule update --init",
    "git submodule summary": "git submodule summary",
    "git dangerous global c": "git -c core.pager='touch owned' log",
    "git output": "git diff --output=diff.txt",
    "git ext diff": "git diff --ext-diff",
    "git diff external diff not disabled": "git diff --no-textconv --stat",
    "git diff textconv not disabled": "git diff --no-ext-diff --stat",
    "git log patch external diff not disabled": "git log -p --no-textconv -1",
    "git log patch textconv not disabled": "git log -p --no-ext-diff -1",
    "git reflog patch external diff not disabled": "git reflog show -p --no-textconv -1",
    "git reflog patch textconv not disabled": "git reflog show -p --no-ext-diff -1",
    "git pager helper": "git grep --open-files-in-pager=vim pattern",
    "git pager helper short": "git grep -Ovim pattern",
    "printf variable assignment": "printf -v EXPORTED_VAR '%s' changed",
    "printf n conversion": "printf '%n' PATH",
    "printf positional n conversion": "printf '%1$n' PATH",
    "diff pager": "diff --paginate before.txt after.txt",
    "diff3 external program": "diff3 --diff-program=evil mine base theirs",
    "sort may spill temporary files": "sort -u names.txt",
    "sort output": "sort -o sorted.txt input.txt",
    "sort helper": "sort --compress-program=evil input.txt",
    "sort temp dir": "sort -T /tmp input.txt",
    "sort joined temp dir": "sort -uT/tmp input.txt",
    "uniq output operand": "uniq input.txt output.txt",
    "uniq output operand after options": "uniq -c input.txt output.txt",
    "base64 output": "base64 -o output.txt input.bin",
    "base64 clustered output": "base64 -Dooutput.txt input.bin",
    "yq inplace": "yq -i '.x = 1' file.yaml",
    "yq clustered inplace": "yq -iP '.x = 1' file.yaml",
    "yq split output": "yq --split-exp '.name' data.yaml",
    "yq attached split output": "yq -s=.name data.yaml",
    "yq abbreviated inplace": "yq --in-p '.x = 1' file.yaml",
    "tar extract": "tar -xf archive.tar",
    "tar create": "tar -cf archive.tar src",
    "tar helper": "tar -tf archive.tar --checkpoint-action=exec='touch owned'",
    "tar abbreviated helper": "tar -tf archive.tar --checkpoint-act=exec='touch owned'",
    "tar joined compressor helper": "tar -tf archive.tar -Ievil",
    "tar volume file": "tar -tf archive.tar --volno-file=state",
    "tar files from": "tar -tf archive.tar --files-from names.txt",
    "tar unknown long option": "tar -tf archive.tar --totally-unknown",
    "tar remote archive": "tar -tf host:/srv/archive.tar",
    "gzip replace": "gzip file.txt",
    "gzip named output": "gzip -cooutput.gz file.txt",
    "zcat named output": "zcat -ooutput.txt archive.gz",
    "gunzip extract": "gunzip archive.gz",
    "unzip extract default": "unzip archive.zip",
    "sysctl write": "sysctl -w kern.maxfiles=10000",
    "sysctl load": "sysctl -p /etc/sysctl.conf",
    "sysctl abbreviated load": "sysctl --loa /etc/sysctl.conf",
    "sysctl system": "sysctl --system",
    "date GNU set": "date -s tomorrow",
    "date GNU attached set": "date -stomorrow",
    "date GNU abbreviated set": "date --se=tomorrow",
    "date BSD set": "date 01010000",
    "hostname set": "hostname newname",
    "hostname file set": "hostname -F host.txt",
    "file compile": "file -C -m magic",
    "file clustered compile": "file -kC -m magic",
    "file abbreviated compile": "file --comp -m magic",
    "file decompress helper": "file -z archive.gz",
    "file abbreviated uncompress": "file --uncomp archive.gz",
    "nm plugin": "nm --plugin evil.so binary",
    "nm abbreviated plugin": "nm --plug evil.so binary",
    "objdump plugin": "objdump --plugin=evil.so binary",
    "tree output": "tree -o listing.txt .",
    "timeout unsafe": "timeout 5 rm file",
    "env unsafe": "env LD_PRELOAD=evil.so git status",
    "shell": "sh -c 'git status'",
    "python": "python3 -c 'print(1)'",
    "node": "node -e 'console.log(1)'",
    "package manager": "uv pip list",
    "apply patch-like write": "printf x > file",
}


class ClassificationTests(unittest.TestCase):
    def test_allow_cases(self) -> None:
        failures = []
        with mock.patch.dict(os.environ, TEST_ENV, clear=True):
            for name, command in ALLOW_CASES.items():
                with self.subTest(name=name, command=command):
                    result = hook.classify(command, CONFIG)
                    if result.verdict is not hook.Verdict.ALLOW:
                        failures.append((name, command, result.reason))
        self.assertEqual([], failures)

    def test_ask_cases(self) -> None:
        failures = []
        with mock.patch.dict(os.environ, TEST_ENV, clear=True):
            for name, command in ASK_CASES.items():
                with self.subTest(name=name, command=command):
                    result = hook.classify(command, CONFIG)
                    if result.verdict is not hook.Verdict.ASK:
                        failures.append((name, command, result.reason))
        self.assertEqual([], failures)

    def test_oversized_command_fails_closed(self) -> None:
        small_config = hook.Config(False, (), 16)
        self.assertEqual(
            hook.Verdict.ASK, hook.classify("cat " + "x" * 100, small_config).verdict
        )

    def test_untrusted_absolute_executable_fails_closed(self) -> None:
        strict = hook.Config(True, (Path("/usr/bin"),), 65_536)
        result = hook.classify("/tmp/git status", strict)
        self.assertEqual(hook.Verdict.ASK, result.verdict)

    def test_dangerous_ambient_environment_fails_closed(self) -> None:
        cases = [
            ({"LD_PRELOAD": "/tmp/evil.so"}, "cat README.md"),
            ({"BASH_ENV": "/tmp/evil.sh"}, "cat README.md"),
            ({"BASH_FUNC_cat%%": "() { touch owned; }"}, "cat README.md"),
            ({"PYTHONPATH": "/tmp/evil"}, "yq '.x' file.yaml"),
            ({"PERL5OPT": "-MEvil"}, "shasum file"),
            ({"GIT_EXTERNAL_DIFF": "touch owned"}, "git diff"),
            ({"RIPGREP_CONFIG_PATH": "/tmp/rg.conf"}, "rg pattern ."),
            (
                {"TAR_OPTIONS": "--checkpoint-action=exec=touch owned"},
                "tar -tf archive.tar",
            ),
            ({"UNZIPOPT": "-o"}, "unzip -l archive.zip"),
            ({"DEBUGINFOD_URLS": "https://debuginfod.example"}, "objdump -h binary"),
        ]
        for environment, command in cases:
            with self.subTest(environment=environment, command=command):
                with mock.patch.dict(
                    os.environ, {**TEST_ENV, **environment}, clear=True
                ):
                    result = hook.classify(command, CONFIG)
                self.assertEqual(hook.Verdict.ASK, result.verdict)

    def test_safe_inline_environment_overrides_unsafe_ambient_value(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                **TEST_ENV,
                "GIT_PAGER": "evil-pager",
                "PAGER": "less",
                "GIT_OPTIONAL_LOCKS": "1",
                "GIT_NO_LAZY_FETCH": "0",
            },
            clear=True,
        ):
            result = hook.classify(
                "GIT_PAGER=cat GIT_OPTIONAL_LOCKS=0 "
                "GIT_NO_LAZY_FETCH=1 git status --short",
                CONFIG,
            )
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

    def test_git_requires_explicit_pager_and_lazy_fetch_neutralization(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = hook.classify("git log --oneline -1", CONFIG)
        self.assertEqual(hook.Verdict.ASK, result.verdict)
        self.assertIn("pager", result.reason.lower())

        with mock.patch.dict(os.environ, {"GIT_PAGER": "cat"}, clear=True):
            result = hook.classify("git log --oneline -1", CONFIG)
        self.assertEqual(hook.Verdict.ASK, result.verdict)
        self.assertIn("lazy fetch", result.reason.lower())

    def test_tar_ambient_remote_defaults_fail_closed(self) -> None:
        with mock.patch.dict(
            os.environ, {**TEST_ENV, "TAPE": "host:/dev/nst0"}, clear=True
        ):
            result = hook.classify("tar -t", CONFIG)
        self.assertEqual(hook.Verdict.ASK, result.verdict)

        with mock.patch.dict(
            os.environ, {**TEST_ENV, "TAR_RSH": "evil-helper"}, clear=True
        ):
            result = hook.classify("tar -tf archive.tar", CONFIG)
        self.assertEqual(hook.Verdict.ASK, result.verdict)

    def test_rg_no_config_neutralizes_ambient_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {**TEST_ENV, "RIPGREP_CONFIG_PATH": "/tmp/rg.conf"},
            clear=True,
        ):
            result = hook.classify("rg --no-config pattern .", CONFIG)
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

    def test_write_and_dynamic_suffixes_never_upgrade_safe_commands(self) -> None:
        bases = [
            "cat README.md",
            "ls -la",
            "sed -n '1,5p' README.md",
            "GIT_OPTIONAL_LOCKS=0 git status --short",
            "rg TODO .",
            "find . -type f",
        ]
        suffixes = [
            " > output.txt",
            " >> output.txt",
            " | tee output.txt",
            " ; touch output.txt",
            " && rm output.txt",
            " $(touch output.txt)",
            " `touch output.txt`",
        ]
        failures = []
        with mock.patch.dict(os.environ, TEST_ENV, clear=True):
            for base in bases:
                for suffix in suffixes:
                    command = base + suffix
                    result = hook.classify(command, CONFIG)
                    if result.verdict is not hook.Verdict.ASK:
                        failures.append((command, result.reason))
        self.assertEqual([], failures)


class RuleFileTests(unittest.TestCase):
    def test_rules_route_every_supported_command_family(self) -> None:
        expected = set(hook.SIMPLE_READ_ONLY_COMMANDS)
        expected.update(hook.GREP_COMMANDS)
        expected.update(hook.ZCAT_COMMANDS)
        expected.update(
            {
                "cd",
                "command",
                "env",
                "timeout",
                "date",
                "hostname",
                "file",
                "tree",
                "nm",
                "objdump",
                "printf",
                "diff",
                "diff3",
                "rg",
                "fd",
                "fdfind",
                "find",
                "base64",
                "jq",
                "yq",
                "sed",
                "git",
                "tar",
                "gzip",
                "gunzip",
                "bzip2",
                "bunzip2",
                "xz",
                "unxz",
                "lzma",
                "unlzma",
                "zipinfo",
                "unzip",
                "uniq",
                "sysctl",
            }
        )
        rules_path = Path(__file__).with_name("codex-read-only-approver.rules.example")
        pattern = re.compile(
            r'^prefix_rule\(pattern=\[("(?:[^"\\]|\\.)*")\], '
            r'decision="prompt", justification="[^"]+"\)$'
        )
        actual: set[str] = set()
        for line in rules_path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            match = pattern.fullmatch(line)
            self.assertIsNotNone(match, f"unexpected rule syntax: {line}")
            assert match is not None
            actual.add(json.loads(match.group(1)))
        self.assertEqual(expected, actual)


class HookProtocolTests(unittest.TestCase):
    def run_hook(self, command: str) -> subprocess.CompletedProcess[str]:
        script = Path(__file__).with_name("codex_read_only_approver.py")
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        # Disable path checking only in the isolated protocol test.
        with self.subTest(command=command):
            return subprocess.run(
                [sys.executable, str(script), "--no-path-check"],
                input=payload,
                text=True,
                capture_output=True,
                check=False,
                env=TEST_ENV,
            )

    @unittest.skipIf(os.name == "nt", "native Windows hooks fail closed")
    def test_allow_shape_matches_codex_permission_request_contract(self) -> None:
        result = self.run_hook("sed -n '1,5p' README.md")
        self.assertEqual(0, result.returncode)
        output = json.loads(result.stdout)
        self.assertEqual(
            "PermissionRequest", output["hookSpecificOutput"]["hookEventName"]
        )
        self.assertEqual("allow", output["hookSpecificOutput"]["decision"]["behavior"])

    def test_native_windows_hook_fails_closed_before_parsing(self) -> None:
        with (
            mock.patch.object(hook.os, "name", "nt"),
            mock.patch.object(hook.json, "load") as load,
            mock.patch.object(hook, "classify") as classify,
        ):
            self.assertEqual(0, hook._hook_main(CONFIG))
        load.assert_not_called()
        classify.assert_not_called()

    def test_native_windows_diagnostic_fails_closed(self) -> None:
        with (
            mock.patch.object(hook.os, "name", "nt"),
            mock.patch("builtins.print") as print_output,
        ):
            self.assertEqual(
                1,
                hook._cli_main(["--no-path-check", "--check", r"echo safe \> victim"]),
            )
        print_output.assert_called_once_with(
            "ASK: native Windows PowerShell is not supported; run Codex in WSL2"
        )

    @unittest.skipUnless(os.name == "nt", "requires native Windows")
    def test_native_windows_hook_is_always_silent(self) -> None:
        for command in (
            "cat README.md",
            r"echo safe \; Remove-Item victim",
            r"echo safe \> victim",
        ):
            with self.subTest(command=command):
                result = self.run_hook(command)
                self.assertEqual(0, result.returncode)
                self.assertEqual("", result.stdout)

    @unittest.skipUnless(os.name == "nt", "requires native Windows")
    def test_native_windows_diagnostic_subprocess_fails_closed(self) -> None:
        script = Path(__file__).with_name("codex_read_only_approver.py")
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--no-path-check",
                "--check",
                r"echo safe \> victim",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=TEST_ENV,
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(
            "ASK: native Windows PowerShell is not supported; run Codex in WSL2\n",
            result.stdout,
        )

    def test_ask_is_silent(self) -> None:
        result = self.run_hook("sed -i 's/a/b/' file")
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

    def test_other_hook_event_is_silent(self) -> None:
        script = Path(__file__).with_name("codex_read_only_approver.py")
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "cat README.md"},
            }
        )
        result = subprocess.run(
            [sys.executable, str(script), "--no-path-check"],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            env=TEST_ENV,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

    def test_non_bash_is_silent(self) -> None:
        script = Path(__file__).with_name("codex_read_only_approver.py")
        payload = json.dumps(
            {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch"}}
        )
        result = subprocess.run(
            [sys.executable, str(script), "--no-path-check"],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            env=TEST_ENV,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

    def test_malformed_json_is_silent(self) -> None:
        script = Path(__file__).with_name("codex_read_only_approver.py")
        result = subprocess.run(
            [sys.executable, str(script), "--no-path-check"],
            input="{broken",
            text=True,
            capture_output=True,
            check=False,
            env=TEST_ENV,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

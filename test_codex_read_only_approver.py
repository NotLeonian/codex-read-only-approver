# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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
    "pipeline": "git -c core.fsmonitor= diff --no-ext-diff --no-textconv -- README.md | sed -n '1,80p' | head -40",
    "chain": "pwd && GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short && rg TODO .",
    "multiline read-only": "pwd\nGIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short\nrg TODO .",
    "input redirect": "wc -l < README.md",
    "stderr discard": "grep x missing 2>/dev/null",
    "fd duplicate": "GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status 2>&1",
    "safe env": "LC_ALL=C NO_COLOR=1 rg TODO .",
    "git status global flag": "git -c core.fsmonitor= --no-optional-locks status --short",
    "git status optional locks": "GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short",
    "git diff": "git --no-pager -c core.fsmonitor= diff --no-ext-diff --no-textconv --stat",
    "git log": "git -c core.fsmonitor= log --oneline -20",
    "git show": "git -C . -c core.fsmonitor= show HEAD:README.md",
    "git branch show": "git -c core.fsmonitor= branch --show-current",
    "git branch list": "git -c core.fsmonitor= branch --list 'feature/*'",
    "git branch parsed list after format value": (
        "git -c core.fsmonitor= branch --format --list --list evil"
    ),
    "git branch bare color before list": (
        "git -c core.fsmonitor= branch --color --list 'feature/*'"
    ),
    "git branch attached column before list": (
        "git -c core.fsmonitor= branch --column=row --list 'feature/*'"
    ),
    "git for-each-ref": "git -c core.fsmonitor= for-each-ref --format='%(refname:short)' refs/heads/",
    "git tag list": "git -c core.fsmonitor= tag --list 'v*'",
    "git tag parsed list after format value": (
        "git -c core.fsmonitor= tag --format --list --list evil"
    ),
    "git tag bare color before list": (
        "git -c core.fsmonitor= tag --color --list 'v*'"
    ),
    "git tag attached column before list": (
        "git -c core.fsmonitor= tag --column=row --list 'v*'"
    ),
    "git config get": "git -c core.fsmonitor= config --get user.name",
    "git config modern get": "git -c core.fsmonitor= config get user.name",
    "git config modern list": "git -c core.fsmonitor= config list",
    "git config shorthand get": "git -c core.fsmonitor= config user.name",
    "git remote list": "git -c core.fsmonitor= remote -v",
    "git remote url": "git -c core.fsmonitor= remote get-url origin",
    "git stash list": "git -c core.fsmonitor= stash list",
    "git stash show": "git -c core.fsmonitor= stash show -p --no-ext-diff --no-textconv stash@{0}",
    "git worktree list": "git -c core.fsmonitor= worktree list --porcelain",
    "git reflog": "git -c core.fsmonitor= reflog show --date=iso",
    "git notes": "git -c core.fsmonitor= notes list",
    "git submodule": "git -c core.fsmonitor= submodule status",
    "uniq stdout": "uniq -c names.txt",
    "uniq dash-named input": "uniq -- -c",
    "printf stdout": "printf '%s\n' hello",
    "diff stdout": "diff -u before.txt after.txt",
    "base64 stdout": "base64 input.bin",
    "base64 decode stdout": "base64 -d input.b64",
    "jq": "jq -r '.items[]?.name' data.json",
    "yq stdout": "yq '.services' compose.yaml",
    "yq pretty stdout": "yq -P '.services' compose.yaml",
    "tar list": "tar -tf archive.tar",
    "tar long list": "tar --list --file archive.tar",
    "tar list alphabetic archive": "tar -tf archive",
    "tar compressed list": "tar -ztf archive.tar.gz",
    "tar traditional list": "tar tf archive.tar",
    "tar traditional ordered local values": "tar tCf directory archive.tar",
    "tar force local before remote archive": "tar --force-local -tf host:archive",
    "tar force local after remote archive": "tar -tf host:archive --force-local",
    "tar dash short archive value before force local": "tar -tf -- --force-local",
    "tar dash long archive value before force local": (
        "tar --list --file -- --force-local"
    ),
    "tar dash directory value before forced local remote": (
        "tar -C -- -tf host:archive --force-local"
    ),
    "gzip stdout": "gzip -dc archive.gz",
    "gzip split stdout": "gzip -v -c archive.gz",
    "gunzip test": "gunzip -t archive.gz",
    "bzip2 final test mode": "bzip2 -z -t archive.bz2",
    "xz final test mode": "xz -d -t archive.xz",
    "zcat": "zcat archive.gz | head",
    "unzip list": "unzip -l archive.zip",
    "unzip quiet list": "unzip -ql archive.zip",
    "unzip stdout": "unzip -p archive.zip README.md | head",
    "unzip stdout with names": "unzip -c archive.zip README.md | head",
    "unzip archive comment": "unzip -z archive.zip",
    "unzip list with dash-named member": "unzip -l archive.zip -- -l",
    "sysctl read": "sysctl kern.ostype",
    "sysctl all": "sysctl --all",
    "date format": "date +%Y-%m-%d",
    "date UTC": "date --utc +%Y-%m-%d",
    "date BSD no-set parse": "date -j 01010000 +%Y-%m-%d",
    "date BSD clustered no-set parse": "date -ju 30 +%M",
    "date reference operand": "date -r 30 +%s",
    "date BSD formatted no-set parse": "date -j -f %b Jul +%b",
    "hostname query": "hostname -s",
    "file inspect": "file README.md",
    "file brief": "file --brief README.md",
    "nm inspect": "nm binary",
    "objdump inspect": "objdump -h binary",
    "tree listing": "tree -L 2 .",
    "timeout wrapper": "timeout 5 GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short",
    "command wrapper": "command -- git -c core.fsmonitor= --no-optional-locks status --short",
    "command lookup": "command -v git",
    "env wrapper": "env LC_ALL=C GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short",
    "env option terminator": "env -- cat /dev/null",
    "comment": "GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status # read only",
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
    "git status may write index": "git -c core.fsmonitor= status --short",
    "git add": "git -c core.fsmonitor= add README.md",
    "git commit": "git -c core.fsmonitor= commit -m test",
    "git push": "git -c core.fsmonitor= push origin main",
    "git fetch": "git -c core.fsmonitor= fetch origin",
    "git pull": "git -c core.fsmonitor= pull --ff-only",
    "git checkout": "git -c core.fsmonitor= checkout main",
    "git switch": "git -c core.fsmonitor= switch -c feature",
    "git restore": "git -c core.fsmonitor= restore README.md",
    "git reset": "git -c core.fsmonitor= reset --hard HEAD~1",
    "git clean": "git -c core.fsmonitor= clean -fd",
    "git branch create": "git -c core.fsmonitor= branch new-branch",
    "git branch delete": "git -c core.fsmonitor= branch -D old",
    "git branch force move": "git -c core.fsmonitor= branch -f name HEAD",
    "git branch upstream": "git -c core.fsmonitor= branch -u origin/main name",
    "git branch legacy short l": "git -c core.fsmonitor= branch -l",
    "git branch list as format value": (
        "git -c core.fsmonitor= --no-pager --no-lazy-fetch branch --format --list evil"
    ),
    "git branch bare color before disguised list": (
        "git -c core.fsmonitor= branch --color --format --list evil"
    ),
    "git branch bare column before disguised list": (
        "git -c core.fsmonitor= branch --column --format --list evil"
    ),
    "git branch bare color before branch name": (
        "git -c core.fsmonitor= branch --color evil"
    ),
    "git branch bare column before branch name": (
        "git -c core.fsmonitor= branch --column evil"
    ),
    "git branch attached color value resembling list": (
        "git -c core.fsmonitor= branch --color=--list evil"
    ),
    "git branch attached column value resembling list": (
        "git -c core.fsmonitor= branch --column=--list evil"
    ),
    "git branch signature format": (
        "git -c core.fsmonitor= branch --format='%(signature:grade)' --list"
    ),
    "git tag create": "git -c core.fsmonitor= tag v1.0.0",
    "git tag annotated": "git -c core.fsmonitor= tag -a v1 -m release",
    "git tag clustered annotate": "git -c core.fsmonitor= tag -av1 -m release",
    "git tag legacy short l": "git -c core.fsmonitor= tag -l",
    "git tag list as format value": (
        "git -c core.fsmonitor= --no-pager --no-lazy-fetch tag --format --list evil"
    ),
    "git tag bare color before disguised list": (
        "git -c core.fsmonitor= tag --color --format --list evil"
    ),
    "git tag bare column before disguised list": (
        "git -c core.fsmonitor= tag --column --format --list evil"
    ),
    "git tag attached color value resembling list": (
        "git -c core.fsmonitor= tag --color=--list evil"
    ),
    "git tag attached column value resembling list": (
        "git -c core.fsmonitor= tag --column=--list evil"
    ),
    "git tag signature format": (
        "git -c core.fsmonitor= tag --format='%(signature:grade)' --list"
    ),
    "git for-each-ref signature format": (
        "git -c core.fsmonitor= for-each-ref --format='%(signature:grade)' refs/heads/"
    ),
    "git config set": "git -c core.fsmonitor= config user.name Alice",
    "git config modern set": "git -c core.fsmonitor= config set user.name Alice",
    "git config unset": "git -c core.fsmonitor= config --unset user.name",
    "git config get with edit": ("git -c core.fsmonitor= config get --edit user.name"),
    "git remote add": (
        "git -c core.fsmonitor= remote add origin https://example.com/repo.git"
    ),
    "git stash push": "git -c core.fsmonitor= stash push -m temp",
    "git worktree add": "git -c core.fsmonitor= worktree add ../other branch",
    "git notes add": "git -c core.fsmonitor= notes add -m note",
    "git submodule update": "git -c core.fsmonitor= submodule update --init",
    "git submodule summary": "git -c core.fsmonitor= submodule summary",
    "git dangerous global c": "git -c core.pager='touch owned' log",
    "git fsmonitor false is version-dependent": (
        "git -c core.fsmonitor=false log --oneline -1"
    ),
    "git fsmonitor helper path": "git -c core.fsmonitor=/bin/false log --oneline -1",
    "git later dangerous config override": (
        "git -c core.fsmonitor= -c core.pager='touch owned' log"
    ),
    "git version trailing help": "git --version --help",
    "git subcommand help viewer": "git -c core.fsmonitor= log --help",
    "git output": "git -c core.fsmonitor= diff --output=diff.txt",
    "git ext diff": "git -c core.fsmonitor= diff --ext-diff",
    "git diff external diff not disabled": (
        "git -c core.fsmonitor= diff --no-textconv --stat"
    ),
    "git diff textconv not disabled": (
        "git -c core.fsmonitor= diff --no-ext-diff --stat"
    ),
    "git log patch external diff not disabled": (
        "git -c core.fsmonitor= log -p --no-textconv -1"
    ),
    "git log patch textconv not disabled": (
        "git -c core.fsmonitor= log -p --no-ext-diff -1"
    ),
    "git reflog patch external diff not disabled": (
        "git -c core.fsmonitor= reflog show -p --no-textconv -1"
    ),
    "git reflog patch textconv not disabled": (
        "git -c core.fsmonitor= reflog show -p --no-ext-diff -1"
    ),
    "git pager helper": (
        "git -c core.fsmonitor= grep --open-files-in-pager=vim pattern"
    ),
    "git pager helper short": "git -c core.fsmonitor= grep -Ovim pattern",
    "printf variable assignment": "printf -v EXPORTED_VAR '%s' changed",
    "printf n conversion": "printf '%n' PATH",
    "printf positional n conversion": "printf '%1$n' PATH",
    "diff pager": "diff --paginate before.txt after.txt",
    "diff3 default helper": "diff3 mine.txt base.txt theirs.txt",
    "diff3 external program": "diff3 --diff-program=evil mine base theirs",
    "sort may spill temporary files": "sort -u names.txt",
    "sort output": "sort -o sorted.txt input.txt",
    "sort helper": "sort --compress-program=evil input.txt",
    "sort temp dir": "sort -T /tmp input.txt",
    "sort joined temp dir": "sort -uT/tmp input.txt",
    "uniq output operand": "uniq input.txt output.txt",
    "uniq output operand after options": "uniq -c input.txt output.txt",
    "uniq flag-looking output operand": "uniq input.txt -c",
    "uniq long-flag-looking output operand": "uniq input.txt --count",
    "uniq option terminator as output operand": "uniq input.txt --",
    "uniq value-option-looking output operand": "uniq input.txt -f1",
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
    "tar post-terminator force local operand": (
        "tar -tf host:archive -- --force-local"
    ),
    "tar long post-terminator force local operand": (
        "tar --list --file host:archive -- --force-local"
    ),
    "tar terminator-looking long option value": (
        "tar --list --exclude -- --file host:archive"
    ),
    "tar force local-looking long option value": (
        "tar -tf host:archive --exclude --force-local"
    ),
    "tar terminator-looking short option value": ("tar -t -C -- -f host:archive"),
    "tar force local-looking short option value": (
        "tar -C --force-local -tf host:archive"
    ),
    "tar traditional ordered option values": "tar tCf directory host:archive",
    "tar traditional force local-looking value": ("tar tfC host:archive --force-local"),
    "tar traditional numeric option": "tar 0 --list -f archive.tar",
    "tar traditional helper option": "tar I0 evil --list -f archive.tar",
    "tar checkpoint before compressor helper": (
        "tar -tf archive.tar --checkpoint -Ievil"
    ),
    "tar occurrence before compressor helper": (
        "tar -tf archive.tar --occurrence -Ievil"
    ),
    "tar checkpoint before remote archive": (
        "tar --list --checkpoint --file host:archive"
    ),
    "tar occurrence before remote archive": (
        "tar --list --occurrence --file host:archive"
    ),
    "gzip replace": "gzip file.txt",
    "gzip named output": "gzip -cooutput.gz file.txt",
    "gzip option terminator before stdout-looking file": "gzip -- -c victim",
    "bzip2 option terminator before stdout-looking file": "bzip2 -- -c victim",
    "xz option terminator before list-looking file": "xz -- -l victim",
    "gzip suffix value resembling stdout option": "gzip -S -c victim",
    "gzip attached suffix containing safe letter": "gzip -S.c victim",
    "xz attached format containing safe letter": "xz -Flzma victim",
    "gzip safe-looking option after operand": "gzip victim -c",
    "xz test mode overridden by decompression": "xz -t -d archive.xz",
    "xz trailing mode override after operand": "xz -t archive.xz -d",
    "bzip2 test mode overridden by compression": "bzip2 -t -z archive.bz2",
    "bzip2 clustered test mode overridden": "bzip2 -tz archive.bz2",
    "bzip2 long test mode overridden": ("bzip2 --test --compress archive.bz2"),
    "xz list mode overridden by decompression": "xz -l -d archive.xz",
    "zcat named output": "zcat -ooutput.txt archive.gz",
    "gunzip extract": "gunzip archive.gz",
    "unzip extract default": "unzip archive.zip",
    "unzip member resembling list option": "unzip archive.zip -l",
    "unzip terminated member resembling list option": "unzip archive.zip -- -l",
    "unzip ambiguous leading double dash": "unzip -- -l archive.zip",
    "unzip double dash cancels list mode": "unzip -l -- -l archive.zip",
    "unzip lone dash permits later mode negation": "unzip -l - --l archive.zip",
    "sysctl write": "sysctl -w kern.maxfiles=10000",
    "sysctl load": "sysctl -p /etc/sysctl.conf",
    "sysctl abbreviated load": "sysctl --loa /etc/sysctl.conf",
    "sysctl system": "sysctl --system",
    "date GNU set": "date -s tomorrow",
    "date GNU attached set": "date -stomorrow",
    "date GNU abbreviated set": "date --se=tomorrow",
    "date BSD set": "date 01010000",
    "date BSD two-digit set": "date 30",
    "date trailing no-set option": "date 30 -j",
    "date terminated BSD set": "date -- 30",
    "date BSD formatted set": "date -f %b Jul +%b",
    "date clustered GNU set": "date -us tomorrow",
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
    "env repeated option terminator": "env -- -- cat /dev/null",
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

    @unittest.skipIf(os.name == "nt", "POSIX PATH semantics are not supported")
    def test_relative_path_component_fails_closed_after_cd(self) -> None:
        cat_name = hook.shutil.which("cat")
        if cat_name is None:
            self.skipTest("cat executable is unavailable")
        cat_command = Path(cat_name).absolute()
        cat_executable = cat_command.resolve()
        strict = hook.Config(True, (cat_executable.parent,), 65_536)
        path_values = (
            f".{os.pathsep}{cat_command.parent}",
            f"{os.pathsep}{cat_command.parent}",
            f"relative/bin{os.pathsep}{cat_command.parent}",
        )
        for path_value in path_values:
            with self.subTest(path=path_value):
                with mock.patch.dict(
                    os.environ, {**TEST_ENV, "PATH": path_value}, clear=True
                ):
                    result = hook.classify("cd /tmp && cat victim", strict)
                self.assertEqual(hook.Verdict.ASK, result.verdict)
                self.assertIn("PATH", result.reason)

        with mock.patch.dict(
            os.environ, {**TEST_ENV, "PATH": str(cat_command.parent)}, clear=True
        ):
            result = hook.classify("cd /tmp && cat victim", strict)
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

        with mock.patch.dict(
            os.environ,
            {**TEST_ENV, "PATH": f".{os.pathsep}{cat_command.parent}"},
            clear=True,
        ):
            result = hook.classify(f"cd /tmp && {cat_command} victim", strict)
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

    @unittest.skipIf(os.name == "nt", "POSIX PATH semantics are not supported")
    def test_relative_path_component_applies_to_external_wrappers(self) -> None:
        env_name = hook.shutil.which("env")
        pwd_name = hook.shutil.which("pwd")
        if env_name is None or pwd_name is None:
            self.skipTest("env or pwd executable is unavailable")
        env_command = Path(env_name).absolute()
        pwd_command = Path(pwd_name).absolute()
        trusted_roots = (env_command.resolve().parent, pwd_command.resolve().parent)
        strict = hook.Config(True, trusted_roots, 65_536)
        absolute_path = os.pathsep.join(
            dict.fromkeys((str(env_command.parent), str(pwd_command.parent)))
        )
        with mock.patch.dict(
            os.environ,
            {**TEST_ENV, "PATH": f".{os.pathsep}{absolute_path}"},
            clear=True,
        ):
            result = hook.classify(f"{env_command} pwd", strict)
        self.assertEqual(hook.Verdict.ASK, result.verdict)
        self.assertIn("PATH", result.reason)

        with mock.patch.dict(
            os.environ, {**TEST_ENV, "PATH": absolute_path}, clear=True
        ):
            result = hook.classify(f"{env_command} pwd", strict)
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

    @unittest.skipIf(os.name == "nt", "POSIX executable scripts are not supported")
    def test_trusted_non_native_executable_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trusted_root = Path(directory).resolve()
            strict = hook.Config(True, (trusted_root,), 65_536)
            wrappers = {
                "egrep": "#!/bin/sh\n",
                "fgrep": "#!/usr/bin/env sh\n",
                "zcat": "#!/usr/bin/env -Ssh\n",
                "bzcat": "#!/usr/bin/env -S\\_sh\n",
                "gunzip": "#!/usr/bin/env -S -C /tmp sh\n",
                "xzcat": "#!/usr/bin/env -C /tmp sh\n",
                "lzcat": "#!/bin/busybox sh\n",
                "grep": "",
                "cat": "exit 42\n\0",
                "shasum": "#!/usr/bin/perl\n",
                "yq": "#!/usr/bin/env python3\n",
            }
            for name, shebang in wrappers.items():
                with self.subTest(name=name, shebang=shebang):
                    wrapper = trusted_root / name
                    wrapper.write_text(
                        shebang + 'exec grep "$@"\n',
                        encoding="utf-8",
                    )
                    wrapper.chmod(0o755)
                    with mock.patch.dict(os.environ, TEST_ENV, clear=True):
                        result = hook.classify(f"{wrapper} pattern file", strict)
                    self.assertEqual(hook.Verdict.ASK, result.verdict)
                    self.assertIn("not a recognized native binary", result.reason)

    @unittest.skipIf(os.name == "nt", "native Windows hooks fail closed")
    def test_trusted_native_executable_is_still_allowed(self) -> None:
        executable_name = hook.shutil.which("date")
        if executable_name is None:
            self.skipTest("date executable is unavailable")
        executable = Path(executable_name).resolve()
        strict = hook.Config(True, (executable.parent,), 65_536)
        with mock.patch.dict(os.environ, TEST_ENV, clear=True):
            result = hook.classify(f"{executable} +%Y", strict)
        self.assertEqual(hook.Verdict.ALLOW, result.verdict, result.reason)

    def test_dangerous_ambient_environment_fails_closed(self) -> None:
        cases = [
            ({"LD_PRELOAD": "/tmp/evil.so"}, "cat README.md"),
            ({"LD_PROFILE": "libc.so.6"}, "cat /dev/null"),
            (
                {
                    "LD_PROFILE": "libc.so.6",
                    "LD_PROFILE_OUTPUT": "/tmp/profile",
                },
                "cat /dev/null",
            ),
            (
                {"LD_DEBUG": "libs", "LD_DEBUG_OUTPUT": "/tmp/loader-debug"},
                "cat /dev/null",
            ),
            (
                {"LD_DEBUG": "libs", "LD_DEBUG_OUTPUT": ""},
                "cat /dev/null",
            ),
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

    def test_inactive_dynamic_loader_output_variables_are_allowed(self) -> None:
        environments = (
            {"LD_PROFILE_OUTPUT": "/tmp/profile"},
            {"LD_DEBUG": "libs"},
            {"LD_DEBUG_OUTPUT": "/tmp/loader-debug"},
            {"LD_DEBUG_OUTPUT": ""},
        )
        for environment in environments:
            with self.subTest(environment=environment):
                with mock.patch.dict(
                    os.environ, {**TEST_ENV, **environment}, clear=True
                ):
                    result = hook.classify("cat /dev/null", CONFIG)
                self.assertEqual(hook.Verdict.ALLOW, result.verdict, result.reason)

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
                "GIT_NO_LAZY_FETCH=1 git -c core.fsmonitor= status --short",
                CONFIG,
            )
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

    def test_git_requires_explicit_fsmonitor_neutralization(self) -> None:
        commands = (
            "GIT_OPTIONAL_LOCKS=0 git status --short",
            "git diff --no-ext-diff --no-textconv --stat",
            "git ls-files",
            "git grep pattern",
            "git blame README.md",
            "git submodule status",
            "git config --get user.name",
        )
        with mock.patch.dict(os.environ, TEST_ENV, clear=True):
            for command in commands:
                with self.subTest(command=command):
                    result = hook.classify(command, CONFIG)
                    self.assertEqual(hook.Verdict.ASK, result.verdict)
                    self.assertIn("FSMonitor", result.reason)

    def test_git_version_does_not_require_fsmonitor_neutralization(self) -> None:
        with mock.patch.dict(os.environ, TEST_ENV, clear=True):
            result = hook.classify("git --version", CONFIG)
        self.assertEqual(hook.Verdict.ALLOW, result.verdict)

    def test_git_requires_explicit_pager_and_lazy_fetch_neutralization(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = hook.classify("git -c core.fsmonitor= log --oneline -1", CONFIG)
        self.assertEqual(hook.Verdict.ASK, result.verdict)
        self.assertIn("pager", result.reason.lower())

        with mock.patch.dict(os.environ, {"GIT_PAGER": "cat"}, clear=True):
            result = hook.classify("git -c core.fsmonitor= log --oneline -1", CONFIG)
        self.assertEqual(hook.Verdict.ASK, result.verdict)
        self.assertIn("lazy fetch", result.reason.lower())

    def test_git_only_accepts_exact_git_pager_sentinels(self) -> None:
        command = "git -c core.fsmonitor= log --oneline -1"
        base_environment = {"GIT_NO_LAZY_FETCH": "1"}

        for value in ("", "cat"):
            with self.subTest(GIT_PAGER=value):
                with mock.patch.dict(
                    os.environ,
                    {**base_environment, "GIT_PAGER": value},
                    clear=True,
                ):
                    result = hook.classify(command, CONFIG)
                self.assertEqual(hook.Verdict.ALLOW, result.verdict, result.reason)

        unsafe_environments = (
            {"GIT_PAGER": " cat "},
            {"GIT_PAGER": "\tcat"},
            {"GIT_PAGER": "cat\n"},
            {"GIT_PAGER": "/bin/cat"},
            {"GIT_PAGER": "/usr/bin/cat"},
            {"PAGER": ""},
            {"PAGER": "cat"},
            {"PAGER": " cat "},
        )
        for environment in unsafe_environments:
            with self.subTest(environment=environment):
                with mock.patch.dict(
                    os.environ,
                    {**base_environment, **environment},
                    clear=True,
                ):
                    result = hook.classify(command, CONFIG)
                self.assertEqual(hook.Verdict.ASK, result.verdict)
                self.assertIn("pager", result.reason.lower())

    def test_git_pager_sentinel_check_is_independent_of_ambient_check(self) -> None:
        config = hook.Config(False, (), 65_536, verify_ambient_environment=False)
        with mock.patch.dict(
            os.environ,
            {"GIT_NO_LAZY_FETCH": "1", "GIT_PAGER": " cat "},
            clear=True,
        ):
            result = hook.classify("git -c core.fsmonitor= log --oneline -1", config)
        self.assertEqual(hook.Verdict.ASK, result.verdict)
        self.assertIn("pager", result.reason.lower())

    def test_git_counts_only_parsed_global_safety_flags(self) -> None:
        cases = (
            (
                "git -C --no-pager --no-lazy-fetch -c core.fsmonitor= log --oneline -1",
                "pager",
            ),
            (
                "git --no-pager -C --no-lazy-fetch -c core.fsmonitor= log --oneline -1",
                "lazy fetch",
            ),
            (
                (
                    "git --no-pager --no-lazy-fetch -C --no-optional-locks "
                    "-c core.fsmonitor= status --short"
                ),
                "status may refresh",
            ),
        )
        allowed = (
            "git --no-pager --no-lazy-fetch -c core.fsmonitor= log --oneline -1",
            (
                "git --no-pager --no-lazy-fetch --no-optional-locks "
                "-c core.fsmonitor= status --short"
            ),
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            for command, reason_fragment in cases:
                with self.subTest(command=command):
                    result = hook.classify(command, CONFIG)
                    self.assertEqual(hook.Verdict.ASK, result.verdict)
                    self.assertIn(reason_fragment, result.reason.lower())
            for command in allowed:
                with self.subTest(command=command):
                    result = hook.classify(command, CONFIG)
                    self.assertEqual(hook.Verdict.ALLOW, result.verdict, result.reason)

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

    def test_only_leading_rg_no_config_neutralizes_ambient_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {**TEST_ENV, "RIPGREP_CONFIG_PATH": "/tmp/rg.conf"},
            clear=True,
        ):
            result = hook.classify("rg --no-config pattern .", CONFIG)
            self.assertEqual(hook.Verdict.ALLOW, result.verdict)

            unsafe_placements = (
                "rg pattern -- --no-config",
                "rg -e --no-config .",
                "rg --glob --no-config pattern .",
                "rg pattern --no-config .",
            )
            for command in unsafe_placements:
                with self.subTest(command=command):
                    result = hook.classify(command, CONFIG)
                    self.assertEqual(hook.Verdict.ASK, result.verdict)

    def test_write_and_dynamic_suffixes_never_upgrade_safe_commands(self) -> None:
        bases = [
            "cat README.md",
            "ls -la",
            "sed -n '1,5p' README.md",
            "GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor= status --short",
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

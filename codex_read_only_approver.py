#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Conservative Codex PermissionRequest hook for read-only Bash commands.

The hook prints an ``allow`` decision only when the complete command can be
classified as read-only by a deliberately small, fail-closed grammar. For a
write-capable, executable, malformed, or unknown command it prints nothing, so
Codex continues with its normal human approval flow.

This is a command-string policy, not a syscall sandbox. See SECURITY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__version__ = "0.1.0"


class Verdict(str, Enum):
    ALLOW = "allow"
    ASK = "ask"


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class Token:
    kind: str  # "word" or "op"
    value: str


@dataclass(frozen=True)
class Config:
    verify_executable_paths: bool = True
    trusted_executable_roots: tuple[Path, ...] = ()
    max_command_bytes: int = 65_536
    verify_ambient_environment: bool = True


class PolicyError(ValueError):
    """Raised when a shell fragment is outside the supported safe grammar."""


DEFAULT_TRUSTED_EXECUTABLE_ROOTS = (
    Path("/bin"),
    Path("/sbin"),
    Path("/usr/bin"),
    Path("/usr/sbin"),
    Path("/usr/local/bin"),
    Path("/usr/local/sbin"),
    Path("/usr/local/Cellar"),
    Path("/opt/homebrew"),
    Path("/opt/local/bin"),
    Path("/home/linuxbrew/.linuxbrew"),
    Path("/nix/store"),
)

SHELL_BUILTINS = {
    ":",
    "[",
    "cd",
    "command",
    "echo",
    "false",
    "printf",
    "pwd",
    "test",
    "true",
}

SAFE_ENV_VALUES: dict[str, re.Pattern[str] | set[str]] = {
    "LC_ALL": re.compile(r"[A-Za-z0-9_.@-]+"),
    "LANG": re.compile(r"[A-Za-z0-9_.@-]+"),
    "LANGUAGE": re.compile(r"[A-Za-z0-9_:.@-]+"),
    "TZ": re.compile(r"[A-Za-z0-9_+./:-]+"),
    "NO_COLOR": re.compile(r"[A-Za-z0-9_.-]*"),
    "CLICOLOR": {"0", "1"},
    "CLICOLOR_FORCE": {"0", "1"},
    "TERM": re.compile(r"[A-Za-z0-9_.+-]+"),
    "COLUMNS": re.compile(r"[0-9]+"),
    "LINES": re.compile(r"[0-9]+"),
    "PAGER": {"cat", ""},
    "GIT_PAGER": {"cat", ""},
    "GIT_OPTIONAL_LOCKS": {"0"},
    "GIT_NO_LAZY_FETCH": {"1"},
}

# Commands whose documented command-line interface has no direct file-write or
# arbitrary-command execution facility. Commands with write-capable options are
# handled by dedicated classifiers below instead.
SIMPLE_READ_ONLY_COMMANDS = {
    ":",
    "basename",
    "cal",
    "cat",
    "cksum",
    "column",
    "comm",
    "cmp",
    "cut",
    "df",
    "diff",
    "diff3",
    "dirname",
    "du",
    "echo",
    "expr",
    "false",
    "fold",
    "fmt",
    "free",
    "head",
    "hexdump",
    "hostid",
    "id",
    "lsof",
    "ls",
    "md5",
    "md5sum",
    "nl",
    "od",
    "otool",
    "paste",
    "pgrep",
    "pidof",
    "pr",
    "printenv",
    "printf",
    "ps",
    "pwd",
    "readelf",
    "readlink",
    "realpath",
    "rev",
    "seq",
    "sha1sum",
    "sha224sum",
    "sha256sum",
    "sha384sum",
    "sha512sum",
    "shasum",
    "size",
    "sleep",
    "stat",
    "strings",
    "tail",
    "test",
    "tr",
    "true",
    "uname",
    "unexpand",
    "uptime",
    "vm_stat",
    "wc",
    "whereis",
    "which",
    "whoami",
}

GREP_COMMANDS = {"grep", "egrep", "fgrep"}
ZCAT_COMMANDS = {"zcat", "bzcat", "xzcat", "lzcat"}

CONTROL_OPERATORS = {"&&", "||", ";", "|", "|&"}
FORBIDDEN_GROUP_OPERATORS = {"&", "(", ")", "{", "}"}
REDIRECTION_OPERATORS = {
    "<",
    "0<",
    "1<",
    "2<",
    "<<",
    "<<<",
    "<>",
    ">",
    "1>",
    "2>",
    ">>",
    "1>>",
    "2>>",
    ">|",
    "1>|",
    "2>|",
    "&>",
    "&>>",
    "<&",
    "0<&",
    "1<&",
    "2<&",
    ">&",
    "0>&",
    "1>&",
    "2>&",
}

_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
_NATIVE_WINDOWS_UNSUPPORTED_REASON = (
    "native Windows PowerShell is not supported; run Codex in WSL2"
)


def allow(reason: str) -> Result:
    return Result(Verdict.ALLOW, reason)


def ask(reason: str) -> Result:
    return Result(Verdict.ASK, reason)


def _config_path() -> Path:
    override = os.environ.get("CODEX_READ_ONLY_APPROVER_CONFIG")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "codex-read-only-approver" / "config.json"


def load_config() -> Config:
    roots = list(DEFAULT_TRUSTED_EXECUTABLE_ROOTS)
    verify = True
    verify_environment = True
    max_bytes = 65_536
    path = _config_path()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PolicyError(f"config must be a JSON object: {path}")
        if "verify_executable_paths" in raw:
            if not isinstance(raw["verify_executable_paths"], bool):
                raise PolicyError("verify_executable_paths must be boolean")
            verify = raw["verify_executable_paths"]
        if "verify_ambient_environment" in raw:
            if not isinstance(raw["verify_ambient_environment"], bool):
                raise PolicyError("verify_ambient_environment must be boolean")
            verify_environment = raw["verify_ambient_environment"]
        extra_roots = raw.get("extra_trusted_executable_roots", [])
        if not isinstance(extra_roots, list) or not all(
            isinstance(item, str) for item in extra_roots
        ):
            raise PolicyError("extra_trusted_executable_roots must be a string array")
        roots.extend(Path(item).expanduser() for item in extra_roots)
        if "max_command_bytes" in raw:
            value = raw["max_command_bytes"]
            if not isinstance(value, int) or not 1024 <= value <= 1_048_576:
                raise PolicyError(
                    "max_command_bytes must be an integer from 1024 to 1048576"
                )
            max_bytes = value

    normalized: list[Path] = []
    for root in roots:
        try:
            normalized.append(Path(os.path.realpath(root)))
        except OSError:
            normalized.append(root.absolute())
    return Config(verify, tuple(normalized), max_bytes, verify_environment)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_executable_path(command_word: str) -> Path | None:
    """Resolve an executable without deciding whether its location is trusted."""

    name = os.path.basename(command_word)
    if name in SHELL_BUILTINS and "/" not in command_word:
        return None
    if "/" in command_word:
        candidate = Path(command_word).expanduser()
        if not candidate.is_absolute():
            return None
    else:
        found = shutil.which(command_word)
        if not found:
            return None
        candidate = Path(found)

    try:
        real = Path(os.path.realpath(candidate))
    except OSError:
        return None
    if not real.is_file() or not os.access(real, os.X_OK):
        return None
    return real


def _is_native_executable(path: Path) -> bool | None:
    """Return whether *path* has a native executable format for this OS."""

    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return None
    if sys.platform == "darwin":
        return magic in {
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
            b"\xca\xfe\xba\xbf",
            b"\xbf\xba\xfe\xca",
        }
    if os.name == "posix":
        return magic == b"\x7fELF"
    return False


def _shebang_interpreter(path: Path) -> str | None:
    """Return the interpreter basename selected by an executable shebang."""

    try:
        with path.open("rb") as handle:
            first_line = handle.readline(1024)
    except OSError:
        return None
    if not first_line.startswith(b"#!"):
        return None
    try:
        words = shlex.split(first_line[2:].decode("utf-8", errors="replace").strip())
    except ValueError:
        return None
    if not words:
        return None

    interpreter = os.path.basename(words[0])
    if interpreter != "env":
        return interpreter

    args = words[1:]
    i = 0
    while i < len(args):
        token = args[i]
        if token == "-S":
            i += 1
            break
        if token in {"-u", "--unset"}:
            i += 2
            continue
        if token.startswith("-") or _ASSIGNMENT_RE.fullmatch(token):
            i += 1
            continue
        break
    if i >= len(args):
        return None
    return os.path.basename(args[i])


def _trusted_executable(command_word: str, config: Config) -> tuple[bool, str]:
    name = os.path.basename(command_word)
    if name in SHELL_BUILTINS and "/" not in command_word:
        return True, "shell builtin"
    if "/" in command_word and not Path(command_word).expanduser().is_absolute():
        return False, "relative executable paths are not trusted"
    if not config.verify_executable_paths:
        return True, "path verification disabled"

    real = _resolve_executable_path(command_word)
    if real is None:
        return False, f"executable not found or not executable: {command_word}"
    if any(_is_under(real, root) for root in config.trusted_executable_roots):
        native = _is_native_executable(real)
        if native is None:
            return False, f"executable could not be inspected safely: {real}"
        if not native:
            return (
                False,
                (
                    "executable is not a recognized native binary; interpreted "
                    f"wrappers may launch unverifiable transitive helpers: {real}"
                ),
            )
        return True, str(real)
    return False, f"executable is outside trusted roots: {real}"


def _normalize_shebang_runtime(name: str) -> str | None:
    name = os.path.basename(name)
    if re.fullmatch(r"python(?:3)?(?:\.\d+)*", name):
        return "python"
    if re.fullmatch(r"perl(?:\d+(?:\.\d+)*)?", name):
        return "perl"
    if re.fullmatch(r"ruby(?:\d+(?:\.\d+)*)?", name):
        return "ruby"
    if name in {"node", "nodejs"} or re.fullmatch(r"node\d+(?:\.\d+)*", name):
        return "node"
    return None


def _shebang_runtime(path: Path) -> str | None:
    """Return a supported interpreter family for an executable script."""

    interpreter = _shebang_interpreter(path)
    if interpreter is None:
        return None
    return _normalize_shebang_runtime(interpreter)


def _command_runtime(command_word: str) -> str | None:
    """Identify interpreter-level environment variables that can affect a command."""

    command = os.path.basename(command_word)
    # shasum is specified and distributed as a Perl program on the supported
    # Unix platforms, including the system copy shipped by macOS.
    if command == "shasum":
        return "perl"
    path = _resolve_executable_path(command_word)
    if path is None:
        return None
    return _shebang_runtime(path)


def lex_shell(command: str) -> list[Token]:
    """Tokenize a conservative subset of POSIX shell syntax.

    Dynamic expansion, command substitution, heredocs, shell grouping and
    unsupported operators are rejected later or during lexing. Quotes are
    removed, preserving their literal contents.
    """

    tokens: list[Token] = []
    buf: list[str] = []
    word_started = False
    i = 0
    n = len(command)

    def flush_word() -> None:
        nonlocal buf, word_started
        if word_started:
            tokens.append(Token("word", "".join(buf)))
            buf = []
            word_started = False

    def emit_semicolon() -> None:
        if tokens and not (
            tokens[-1].kind == "op" and tokens[-1].value in CONTROL_OPERATORS
        ):
            tokens.append(Token("op", ";"))

    while i < n:
        ch = command[i]

        if ch in " \t\r":
            flush_word()
            i += 1
            continue
        if ch == "\n":
            flush_word()
            emit_semicolon()
            i += 1
            continue

        if ch == "#" and not word_started:
            # Shell comment at a token boundary.
            while i < n and command[i] != "\n":
                i += 1
            continue

        if ch == "'":
            word_started = True
            i += 1
            while i < n and command[i] != "'":
                buf.append(command[i])
                i += 1
            if i >= n:
                raise PolicyError("unterminated single quote")
            i += 1
            continue

        if ch == '"':
            word_started = True
            i += 1
            while i < n:
                inner = command[i]
                if inner == '"':
                    i += 1
                    break
                if inner in {"$", "`"}:
                    raise PolicyError(
                        "dynamic expansion inside double quotes is not supported"
                    )
                if inner == "\\":
                    if i + 1 >= n:
                        raise PolicyError("trailing backslash in double quote")
                    nxt = command[i + 1]
                    if nxt == "\n":
                        i += 2
                        continue
                    if nxt in {'"', "\\", "$", "`"}:
                        buf.append(nxt)
                        i += 2
                        continue
                    buf.append("\\")
                    buf.append(nxt)
                    i += 2
                    continue
                buf.append(inner)
                i += 1
            else:
                raise PolicyError("unterminated double quote")
            continue

        if ch == "\\":
            word_started = True
            if i + 1 >= n:
                raise PolicyError("trailing backslash")
            nxt = command[i + 1]
            if nxt == "\n":
                i += 2
                continue
            buf.append(nxt)
            i += 2
            continue

        if ch in {"$", "`"}:
            raise PolicyError(
                "dynamic expansion or command substitution is not supported"
            )
        if ch in {"*", "?", "["}:
            raise PolicyError(
                "unquoted glob expansion is not auto-approved; quote the pattern"
            )
        if ch == "{":
            close = command.find("}", i + 1)
            if close != -1:
                body = command[i + 1 : close]
                if "," in body or ".." in body:
                    raise PolicyError("unquoted brace expansion is not auto-approved")

        # Longest-match shell operators. Braces are treated as grouping syntax
        # only when they appear at a token boundary; embedded braces remain data.
        op = None
        for candidate in (
            "&>>",
            "<<<",
            "&&",
            "||",
            "|&",
            "&>",
            "<<",
            ">>",
            "<>",
            ">|",
            "<&",
            ">&",
            "|",
            "&",
            ";",
            "<",
            ">",
            "(",
            ")",
        ):
            if command.startswith(candidate, i):
                op = candidate
                break
        if op is not None:
            # Process substitution is dynamic execution.
            if op in {"<", ">"} and i + 1 < n and command[i + 1] == "(":
                raise PolicyError("process substitution is not supported")
            # A numeric file descriptor is part of a redirection only when
            # immediately adjacent, e.g. 2>file rather than `2 > file`.
            if op[0] in "<>" and word_started and buf and "".join(buf).isdigit():
                fd = "".join(buf)
                buf = []
                word_started = False
                tokens.append(Token("op", fd + op))
            else:
                flush_word()
                tokens.append(Token("op", op))
            i += len(op)
            continue

        word_started = True
        buf.append(ch)
        i += 1

    flush_word()
    # Ignore a trailing command separator, which is syntactically harmless.
    while tokens and tokens[-1] == Token("op", ";"):
        tokens.pop()
    return tokens


def _split_simple_commands(tokens: Sequence[Token]) -> list[list[Token]]:
    if not tokens:
        raise PolicyError("empty command")
    commands: list[list[Token]] = []
    current: list[Token] = []
    for token in tokens:
        if token.kind == "op" and token.value in CONTROL_OPERATORS:
            if not current:
                raise PolicyError(f"empty command near operator {token.value}")
            commands.append(current)
            current = []
            continue
        if token.kind == "op" and token.value in FORBIDDEN_GROUP_OPERATORS:
            raise PolicyError(f"unsupported shell operator: {token.value}")
        current.append(token)
    if not current:
        raise PolicyError("command ends with a control operator")
    commands.append(current)
    return commands


def _strip_redirections(tokens: Sequence[Token]) -> tuple[list[str], list[str]]:
    argv: list[str] = []
    notes: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "word":
            argv.append(token.value)
            i += 1
            continue
        op = token.value
        if op not in REDIRECTION_OPERATORS:
            raise PolicyError(f"unsupported operator inside command: {op}")
        if i + 1 >= len(tokens) or tokens[i + 1].kind != "word":
            raise PolicyError(f"redirection {op} is missing a target")
        target = tokens[i + 1].value

        if op in {"<", "0<", "1<", "2<"}:
            if target.startswith(("/dev/tcp/", "/dev/udp/")):
                raise PolicyError(
                    f"shell network device redirection is not auto-approved: {op}{target}"
                )
            notes.append(f"input redirection {op}{target}")
        elif op in {"<&", "0<&", "1<&", "2<&"}:
            if not (target.isdigit() or target == "-"):
                raise PolicyError(
                    f"input fd redirection to a path is not allowed: {op}{target}"
                )
            notes.append(f"fd duplication {op}{target}")
        elif op in {">&", "0>&", "1>&", "2>&"}:
            if not (target in {"1", "2", "-"}):
                raise PolicyError(f"output redirection is not read-only: {op}{target}")
            notes.append(f"fd duplication {op}{target}")
        elif op in {
            ">",
            "1>",
            "2>",
            ">>",
            "1>>",
            "2>>",
            ">|",
            "1>|",
            "2>|",
            "&>",
            "&>>",
        }:
            if target != "/dev/null":
                raise PolicyError(f"output redirection may write a file: {op}{target}")
            notes.append(f"discard output via {op}/dev/null")
        else:
            raise PolicyError(f"heredoc or read/write redirection is not allowed: {op}")
        i += 2

    if not argv:
        raise PolicyError("redirection without a command")
    return argv, notes


def _safe_env_assignment(token: str) -> bool:
    match = _ASSIGNMENT_RE.fullmatch(token)
    if not match:
        return False
    name, value = match.groups()
    rule = SAFE_ENV_VALUES.get(name)
    if rule is None:
        return False
    if isinstance(rule, set):
        return value in rule
    return rule.fullmatch(value) is not None


def _strip_safe_env(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    rest = list(argv)
    assignments: list[str] = []
    while rest and _ASSIGNMENT_RE.fullmatch(rest[0]):
        assignment = rest.pop(0)
        if not _safe_env_assignment(assignment):
            raise PolicyError(
                f"environment assignment can alter command behavior: {assignment}"
            )
        assignments.append(assignment)
    if not rest:
        raise PolicyError("standalone environment assignment is not auto-approved")
    return rest, assignments


def _has_option(argv: Sequence[str], *names: str) -> bool:
    for arg in argv:
        for name in names:
            if arg == name or (name.startswith("--") and arg.startswith(name + "=")):
                return True
    return False


def _long_option_name(arg: str) -> str:
    return arg.split("=", 1)[0]


def _is_long_option_or_abbreviation(arg: str, option: str) -> bool:
    """Return whether *arg* could select *option* via GNU-style abbreviation.

    Many command-line parsers accept a unique prefix such as ``--comp`` for
    ``--compile``. Security checks therefore cannot compare only the full
    spelling. Ambiguous prefixes are rejected too; false positives fail closed.
    """

    name = _long_option_name(arg)
    return name == option or (
        name.startswith("--") and len(name) > 2 and option.startswith(name)
    )


def _has_long_option_or_abbreviation(argv: Sequence[str], *options: str) -> str | None:
    for arg in argv:
        if not arg.startswith("--"):
            continue
        for option in options:
            if _is_long_option_or_abbreviation(arg, option):
                return arg
    return None


def _short_cluster_contains(arg: str, letters: str) -> bool:
    return (
        arg.startswith("-")
        and not arg.startswith("--")
        and len(arg) > 1
        and any(letter in arg[1:] for letter in letters)
    )


def _printf_format_uses_n_conversion(format_string: str) -> bool:
    """Return whether a printf format contains a ``%n`` assignment conversion.

    Bash's printf builtin writes the number of emitted bytes into a shell
    variable named by the corresponding argument.  It is therefore a state
    mutation even though no file redirection is present.  The parser below is
    intentionally conservative and understands the common flag/width/precision
    portion of printf conversions while ignoring a literal ``%%``.
    """

    conversion = re.compile(
        r"%(?!%)(?:[0-9]+\$)?[-+ #0']*(?:\*|[0-9]+)?"
        r"(?:\.(?:\*|[0-9]+))?(?:hh|ll|[hlLzjt])?n"
    )
    return conversion.search(format_string) is not None


def _classify_printf(argv: Sequence[str]) -> Result:
    # Bash printf -v assigns a shell variable. If that variable is exported, it
    # can alter a later command in the same chain, so it is not treated as a
    # pure stdout operation.
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg == "-v" or arg.startswith("-v"):
            return ask("printf -v mutates shell state")
        if arg.startswith("-"):
            return ask(f"unsupported printf option: {arg}")
        break
    if i >= len(args):
        return ask("printf is missing a format string")
    if _printf_format_uses_n_conversion(args[i]):
        return ask("printf %n mutates shell variables")
    return allow("printf to standard output")


def _classify_diff(argv: Sequence[str]) -> Result:
    command = os.path.basename(argv[0])
    if command == "diff":
        dangerous = _has_long_option_or_abbreviation(argv[1:], "--paginate")
        if dangerous or any(
            arg == "-l" or _short_cluster_contains(arg, "l") for arg in argv[1:]
        ):
            return ask("diff pagination executes the external pr helper")
        return allow("diff comparison to standard output")
    dangerous = _has_long_option_or_abbreviation(argv[1:], "--diff-program")
    if dangerous:
        return ask(f"diff3 option executes an external program: {dangerous}")
    # GNU diff3 always launches a build-time-selected diff program through
    # PATH. The helper name can itself be transformed at build time, so it
    # cannot be inferred reliably from argv or validated here.
    return ask("diff3 executes a default external diff helper")


def _classify_timeout(argv: Sequence[str], config: Config) -> Result:
    i = 1
    value_options = {"-k", "--kill-after", "-s", "--signal"}
    flag_options = {"--foreground", "--preserve-status", "--verbose"}
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg in value_options:
            if i + 1 >= len(argv):
                return ask(f"timeout option requires a value: {arg}")
            i += 2
            continue
        if any(
            arg.startswith(name + "=")
            for name in value_options
            if name.startswith("--")
        ):
            i += 1
            continue
        if arg in flag_options:
            i += 1
            continue
        if arg.startswith("-"):
            return ask(f"unsupported timeout option: {arg}")
        break
    if i >= len(argv):
        return ask("timeout is missing a duration")
    i += 1  # duration
    if i >= len(argv):
        return ask("timeout is missing the wrapped command")
    inner = _classify_argv(argv[i:], config)
    if inner.verdict is Verdict.ALLOW:
        return allow(f"timeout wrapper around read-only command: {inner.reason}")
    return inner


def _classify_command_builtin(argv: Sequence[str], config: Config) -> Result:
    if len(argv) >= 2 and argv[1] in {"-v", "-V"}:
        return allow("command lookup")
    i = 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    if i >= len(argv):
        return ask("command wrapper is missing an inner command")
    return _classify_argv(argv[i:], config)


def _classify_env(argv: Sequence[str], config: Config) -> Result:
    if len(argv) == 1:
        return allow("environment listing")
    i = 1
    while i < len(argv) and argv[i] == "--":
        i += 1
    assignments: list[str] = []
    while i < len(argv) and _ASSIGNMENT_RE.fullmatch(argv[i]):
        if not _safe_env_assignment(argv[i]):
            return ask(f"env assignment can alter command behavior: {argv[i]}")
        assignments.append(argv[i])
        i += 1
    if i == len(argv):
        return allow("environment listing with safe assignments")
    if argv[i].startswith("-"):
        return ask(f"unsupported env option: {argv[i]}")
    # Preserve safe assignments so the inner classifier can reason about their
    # effective values (for example GIT_OPTIONAL_LOCKS=0).
    return _classify_argv([*assignments, *argv[i:]], config)


def _classify_cd(argv: Sequence[str]) -> Result:
    args = list(argv[1:])
    allowed_options = {"-L", "-P", "-e", "-@"}
    while args and args[0] in allowed_options:
        args.pop(0)
    if args and args[0] == "--":
        args.pop(0)
    if len(args) <= 1:
        return allow("directory change only")
    return ask("cd accepts at most one path")


def _classify_rg(argv: Sequence[str]) -> Result:
    dangerous_exact = {"--pre", "--hostname-bin", "--search-zip", "-z"}
    dangerous_prefixes = ("--pre=", "--hostname-bin=")
    for arg in argv[1:]:
        if arg in dangerous_exact or arg.startswith(dangerous_prefixes):
            return ask(f"rg option can execute helper programs: {arg}")
        if re.fullmatch(r"-[A-Za-z]+", arg) and "z" in arg[1:]:
            return ask(f"rg zip-search option can execute helper programs: {arg}")
    return allow("ripgrep search")


def _classify_fd(argv: Sequence[str]) -> Result:
    dangerous = {"-x", "--exec", "-X", "--exec-batch"}
    for arg in argv[1:]:
        if arg in dangerous or arg.startswith(("--exec=", "--exec-batch=")):
            return ask(f"fd option can execute commands: {arg}")
        if (
            arg.startswith("-")
            and not arg.startswith("--")
            and any(ch in arg[1:] for ch in "xX")
        ):
            return ask(f"fd short-option cluster can execute commands: {arg}")
    return allow("fd file search")


def _classify_uniq(argv: Sequence[str]) -> Result:
    """Allow uniq only when no output-file operand can be present."""

    value_options = {
        "-f",
        "--skip-fields",
        "-s",
        "--skip-chars",
        "-w",
        "--check-chars",
    }
    safe_flags = {
        "-c",
        "--count",
        "-d",
        "--repeated",
        "-D",
        "--all-repeated",
        "-i",
        "--ignore-case",
        "-u",
        "--unique",
        "-z",
        "--zero-terminated",
        "--help",
        "--version",
    }
    positionals: list[str] = []
    i = 1
    end_options = False
    while i < len(argv):
        arg = argv[i]
        if not end_options and arg == "--":
            end_options = True
            i += 1
            continue
        if not end_options and arg in value_options:
            if i + 1 >= len(argv):
                return ask(f"uniq option requires a value: {arg}")
            i += 2
            continue
        if not end_options and any(
            arg.startswith(name + "=")
            for name in value_options
            if name.startswith("--")
        ):
            i += 1
            continue
        if not end_options and (
            arg in safe_flags
            or arg.startswith("--all-repeated=")
            or re.fullmatch(r"-[fsw][0-9]+", arg)
        ):
            i += 1
            continue
        if not end_options and arg.startswith("-"):
            # Combined boolean short flags such as -ci are harmless; value-taking
            # options in a cluster are deliberately not inferred.
            if re.fullmatch(r"-[cdiuDz]+", arg):
                i += 1
                continue
            return ask(f"unsupported uniq option: {arg}")
        positionals.append(arg)
        i += 1
    if len(positionals) > 1:
        return ask("uniq's second file operand is an output file")
    return allow("uniq comparison to standard output")


def _classify_base64(argv: Sequence[str]) -> Result:
    for arg in argv[1:]:
        if (
            arg in {"-o", "--output"}
            or arg.startswith("--output=")
            or _short_cluster_contains(arg, "o")
        ):
            return ask("base64 output option writes a file")
    return allow("base64 transform to standard output")


def _classify_yq(argv: Sequence[str]) -> Result:
    dangerous_long = _has_long_option_or_abbreviation(
        argv[1:],
        "--inplace",
        "--in-place",
        "--split-exp",
        "--split-exp-file",
    )
    if dangerous_long:
        if "split" in dangerous_long:
            return ask(f"yq split-output mode may write files: {dangerous_long}")
        return ask(f"yq in-place mode writes files: {dangerous_long}")
    for arg in argv[1:]:
        if arg == "-i" or _short_cluster_contains(arg, "i"):
            return ask("yq in-place mode writes files")
        if arg == "-s" or _short_cluster_contains(arg, "s"):
            return ask("yq split-output mode may write files")
    return allow("yq query to standard output")


def _read_delimited(text: str, start: int, delimiter: str) -> int | None:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == delimiter:
            return i + 1
        i += 1
    return None


def _skip_sed_address(script: str, start: int) -> int | None:
    i = start
    while i < len(script) and script[i] in " \t":
        i += 1
    if i >= len(script):
        return None
    if script[i].isdigit():
        while i < len(script) and script[i].isdigit():
            i += 1
        return i
    if script[i] == "$":
        return i + 1
    if script[i] == "/":
        return _read_delimited(script, i + 1, "/")
    if script[i] == "\\" and i + 1 < len(script):
        delimiter = script[i + 1]
        return _read_delimited(script, i + 2, delimiter)
    return None


def _safe_sed_script(script: str) -> tuple[bool, str]:
    """Accept a useful, explicit subset of sed that cannot write or execute.

    The accepted subset includes common addresses, printing/deleting commands,
    substitutions without ``e``/``w`` flags, transliteration, and read-only
    ``r``/``R`` file inclusion. Unknown sed grammar falls through to approval.
    """

    i = 0
    n = len(script)
    simple_commands = set("pPdDqQnNhHgGx=lFvz")
    while True:
        while i < n and script[i] in " \t;\n":
            i += 1
        if i >= n:
            return True, "read-only sed program"
        if script[i] == "#":
            newline = script.find("\n", i)
            if newline == -1:
                return True, "read-only sed program"
            i = newline + 1
            continue

        first_end = _skip_sed_address(script, i)
        if first_end is not None:
            i = first_end
            while i < n and script[i] in " \t":
                i += 1
            if i < n and script[i] == ",":
                second_end = _skip_sed_address(script, i + 1)
                if second_end is None:
                    return False, "invalid or unsupported sed address range"
                i = second_end
            while i < n and script[i] in " \t":
                i += 1
        if i < n and script[i] == "!":
            i += 1
            while i < n and script[i] in " \t":
                i += 1
        if i >= n:
            return False, "sed address has no command"

        cmd = script[i]
        i += 1
        if cmd in {"w", "W"}:
            return False, "sed w/W writes a file"
        if cmd == "e":
            return False, "sed e executes a shell command"
        if cmd in {"{", "}", ":", "b", "t", "T"}:
            return (
                False,
                f"sed control-flow command is outside the supported subset: {cmd}",
            )

        if cmd == "s":
            if i >= n or script[i] in "\n\\":
                return False, "invalid sed substitution delimiter"
            delimiter = script[i]
            i += 1
            pattern_end = _read_delimited(script, i, delimiter)
            if pattern_end is None:
                return False, "unterminated sed substitution pattern"
            i = pattern_end
            replacement_end = _read_delimited(script, i, delimiter)
            if replacement_end is None:
                return False, "unterminated sed substitution replacement"
            i = replacement_end
            flags_start = i
            while i < n and script[i] not in ";\n":
                i += 1
            flags = script[flags_start:i].strip()
            if not re.fullmatch(r"[0-9gpiImM]*", flags):
                return False, "sed substitution uses a write, execute, or unknown flag"
            continue

        if cmd == "y":
            if i >= n or script[i] in "\n\\":
                return False, "invalid sed transliteration delimiter"
            delimiter = script[i]
            i += 1
            left_end = _read_delimited(script, i, delimiter)
            if left_end is None:
                return False, "unterminated sed transliteration source"
            i = left_end
            right_end = _read_delimited(script, i, delimiter)
            if right_end is None:
                return False, "unterminated sed transliteration destination"
            i = right_end
            while i < n and script[i] in " \t":
                i += 1
            if i < n and script[i] not in ";\n":
                return False, "unexpected text after sed transliteration"
            continue

        if cmd in simple_commands:
            # q/Q/l may take a numeric extension on some implementations.
            while i < n and script[i] in " \t":
                i += 1
            while i < n and script[i].isdigit():
                i += 1
            while i < n and script[i] in " \t":
                i += 1
            if i < n and script[i] not in ";\n":
                return False, f"unexpected argument to sed command {cmd}"
            continue

        if cmd in {"r", "R"}:
            # Reads another file and emits it; it does not write. Require a
            # static non-empty path and consume to the command separator.
            while i < n and script[i] in " \t":
                i += 1
            path_start = i
            while i < n and script[i] not in ";\n":
                i += 1
            if not script[path_start:i].strip():
                return False, f"sed {cmd} is missing a path"
            continue

        if cmd in {"a", "i", "c"}:
            # Appending/inserting/changing pattern-space output is read-only.
            # Keep this to a one-line literal to avoid complex continuation
            # grammar and accidental command-boundary confusion.
            while i < n and script[i] in " \t":
                i += 1
            while i < n and script[i] != "\n":
                i += 1
            continue

        return False, f"unsupported sed command: {cmd}"


def _classify_sed(argv: Sequence[str]) -> Result:
    scripts: list[str] = []
    operands: list[str] = []
    i = 1
    end_options = False
    allowed_flags = {
        "-n",
        "--quiet",
        "--silent",
        "-E",
        "-r",
        "--regexp-extended",
        "-u",
        "--unbuffered",
        "-z",
        "--null-data",
        "--posix",
        "-s",
        "--separate",
        "-b",
        "--binary",
        "--debug",
    }
    read_only_short_flags = set("nEruszb")
    while i < len(argv):
        arg = argv[i]
        if not end_options and arg == "--":
            end_options = True
            i += 1
            continue
        if not end_options and (
            arg in {"-i", "-I", "--in-place"}
            or arg.startswith("--in-place=")
            or (arg.startswith("-") and not arg.startswith("--") and "i" in arg[1:])
        ):
            return ask("sed in-place mode writes files")
        if not end_options and (arg in {"-f", "--file"} or arg.startswith("--file=")):
            return ask("sed script files are not inspected by this hook")
        if not end_options and arg in {"-e", "--expression"}:
            if i + 1 >= len(argv):
                return ask(f"sed option requires a script: {arg}")
            scripts.append(argv[i + 1])
            i += 2
            continue
        if not end_options and arg.startswith("--expression="):
            scripts.append(arg.split("=", 1)[1])
            i += 1
            continue
        if not end_options and arg.startswith("-e") and len(arg) > 2:
            scripts.append(arg[2:])
            i += 1
            continue
        if not end_options and arg in allowed_flags:
            i += 1
            continue
        if not end_options and arg.startswith("-") and not arg.startswith("--"):
            chars = arg[1:]
            if "e" in chars:
                e_index = chars.index("e")
                if any(ch not in read_only_short_flags for ch in chars[:e_index]):
                    return ask(f"unsupported sed option cluster: {arg}")
                inline_script = chars[e_index + 1 :]
                if inline_script:
                    scripts.append(inline_script)
                    i += 1
                    continue
                if i + 1 >= len(argv):
                    return ask(f"sed option cluster requires a script: {arg}")
                scripts.append(argv[i + 1])
                i += 2
                continue
            if chars and set(chars) <= read_only_short_flags:
                i += 1
                continue
            return ask(f"unsupported sed option: {arg}")
        if not end_options and arg.startswith("-"):
            return ask(f"unsupported sed option: {arg}")
        operands.append(arg)
        i += 1

    if not scripts:
        if not operands:
            return ask("sed is missing an inline script")
        scripts.append(operands.pop(0))
    for script in scripts:
        safe, reason = _safe_sed_script(script)
        if not safe:
            return ask(reason)
    return allow("sed program contains no write or execute command")


def _classify_find(argv: Sequence[str], config: Config) -> Result:
    i = 1
    write_primaries = {"-delete", "-fls", "-fprint", "-fprint0", "-fprintf"}
    exec_primaries = {"-exec", "-execdir", "-ok", "-okdir"}
    while i < len(argv):
        arg = argv[i]
        if arg in write_primaries:
            return ask(f"find primary can modify or write files: {arg}")
        if arg in exec_primaries:
            end = i + 1
            while end < len(argv) and argv[end] not in {";", "+"}:
                end += 1
            if end >= len(argv) or end == i + 1:
                return ask(f"malformed find {arg} action")
            inner = _classify_argv(argv[i + 1 : end], config)
            if inner.verdict is Verdict.ASK:
                return ask(f"find {arg} wraps a non-read-only command: {inner.reason}")
            i = end + 1
            continue
        i += 1
    return allow("find expression has no write or unsafe exec primary")


def _git_reject_common_options(args: Sequence[str]) -> str | None:
    exact = {
        "--ext-diff",
        "--textconv",
        "--open-files-in-pager",
        "--filters",
        "--show-signature",
        "-O",
    }
    prefixes = ("--output=", "--open-files-in-pager=", "-O")
    dangerous_long = _has_long_option_or_abbreviation(
        args,
        "--output",
        "--ext-diff",
        "--textconv",
        "--open-files-in-pager",
        "--filters",
        "--show-signature",
    )
    if dangerous_long:
        return dangerous_long
    for arg in args:
        if arg in exact or arg == "--output":
            return arg
        if arg.startswith(prefixes):
            return arg
        # Pretty-format signature placeholders can invoke GPG verification.
        if "%G" in arg:
            return arg
    return None


def _consume_git_global_options(argv: Sequence[str]) -> tuple[int | None, str | None]:
    i = 1
    value_options = {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
    safe_flags = {
        "--no-pager",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--bare",
    }
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return (i + 1 if i + 1 < len(argv) else None), None
        if arg in value_options:
            if i + 1 >= len(argv):
                return None, f"git global option requires a value: {arg}"
            i += 2
            continue
        if any(
            arg.startswith(name + "=")
            for name in value_options
            if name.startswith("--")
        ):
            i += 1
            continue
        if arg in safe_flags:
            i += 1
            continue
        if arg == "--version":
            return -1, None
        if arg in {
            "-c",
            "--config-env",
            "--exec-path",
            "-p",
            "--paginate",
        } or arg.startswith(("-c=", "--config-env=", "--exec-path=")):
            return None, f"git global option can alter execution: {arg}"
        if arg.startswith("-"):
            return None, f"unsupported git global option: {arg}"
        return i, None
    return None, "git is missing a subcommand"


def _git_format_invokes_signature_helper(args: Sequence[str]) -> bool:
    """Detect ref-format signature atoms that can invoke signature helpers."""

    i = 0
    while i < len(args):
        arg = args[i]
        value: str | None = None
        if arg == "--format":
            if i + 1 < len(args):
                value = args[i + 1]
            i += 2
        elif arg.startswith("--format="):
            value = arg.split("=", 1)[1]
            i += 1
        else:
            i += 1
        if value is not None and "%(signature" in value.lower():
            return True
    return False


def _git_branch_read_only(args: Sequence[str]) -> Result:
    if _git_format_invokes_signature_helper(args):
        return ask("git branch format may invoke signature verification helpers")
    mutating = {
        "-d",
        "-D",
        "-m",
        "-M",
        "-c",
        "-C",
        "--delete",
        "--move",
        "--copy",
        "--edit-description",
        "--set-upstream-to",
        "--unset-upstream",
        "--track",
        "--no-track",
        "--create-reflog",
        "-u",
        "-f",
        "--force",
    }
    if any(arg in mutating or arg.startswith("--set-upstream-to=") for arg in args):
        return ask("git branch mutation")
    if any(
        arg.startswith("-")
        and not arg.startswith("--")
        and any(ch in arg[1:] for ch in "dDmMcCuf")
        for arg in args
    ):
        return ask("git branch short option may mutate branches")
    list_mode = any(arg == "--list" for arg in args)
    value_options = {
        "--format",
        "--sort",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--color",
        "--column",
    }
    flags = {
        "--list",
        "--show-current",
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "--no-color",
        "--ignore-case",
        "-i",
        "--omit-empty",
        "--abbrev",
        "--no-abbrev",
        "-q",
        "--quiet",
        "--no-column",
    }
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_options:
            if i + 1 >= len(args):
                return ask(f"git branch option requires a value: {arg}")
            i += 2
            continue
        if any(arg.startswith(name + "=") for name in value_options):
            i += 1
            continue
        if arg in flags or arg.startswith("--abbrev="):
            i += 1
            continue
        if arg.startswith("-"):
            return ask(f"unsupported git branch option: {arg}")
        positionals.append(arg)
        i += 1
    if positionals and not list_mode:
        return ask("git branch positional argument may create a branch")
    return allow("git branch listing")


def _git_tag_read_only(args: Sequence[str]) -> Result:
    if _git_format_invokes_signature_helper(args):
        return ask("git tag format may invoke signature verification helpers")
    mutating = {
        "-a",
        "--annotate",
        "-s",
        "--sign",
        "-u",
        "--local-user",
        "-f",
        "--force",
        "-d",
        "--delete",
        "--create-reflog",
        "-m",
        "-F",
        "--file",
        "--cleanup",
        "-v",
        "--verify",
    }
    for arg in args:
        if arg in mutating or arg.startswith(
            ("--local-user=", "--file=", "--cleanup=")
        ):
            return ask("git tag mutation or external verification")
        if (
            arg.startswith("-")
            and not arg.startswith("--")
            and any(ch in arg[1:] for ch in "asufdmFv")
        ):
            return ask("git tag short option may mutate or invoke verification")

    if not args:
        return allow("git tag listing")
    if "--list" not in args:
        return ask("git tag requires explicit --list for auto-approval")

    value_options = {
        "--sort",
        "--format",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--color",
        "--column",
    }
    flags = {
        "--list",
        "--no-column",
        "--ignore-case",
        "-i",
        "--omit-empty",
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_options:
            if i + 1 >= len(args):
                return ask(f"git tag option requires a value: {arg}")
            i += 2
            continue
        if any(arg.startswith(name + "=") for name in value_options):
            i += 1
            continue
        if arg in flags or re.fullmatch(r"-n[0-9]*", arg):
            i += 1
            continue
        if arg.startswith("-"):
            return ask(f"unsupported git tag option: {arg}")
        # Positional operands are list patterns because --list is explicit.
        i += 1
    return allow("git tag listing")


def _git_config_read_only(args: Sequence[str]) -> Result:
    for arg in args:
        if arg in {"set", "unset", "rename-section", "remove-section"}:
            return ask(f"git config {arg} mutates configuration")
    mutating = {
        "--add",
        "--replace-all",
        "--unset",
        "--unset-all",
        "--rename-section",
        "--remove-section",
        "--edit",
        "-e",
    }
    if any(arg in mutating for arg in args):
        return ask("git config mutation")
    if args and args[0] in {"get", "list"}:
        return allow(f"git config {args[0]} read")
    read_actions = {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
    }
    value_options = {"--file", "-f", "--blob", "--type", "--default"}
    flags = {
        "--global",
        "--system",
        "--local",
        "--worktree",
        "--includes",
        "--no-includes",
        "--show-origin",
        "--show-scope",
        "--name-only",
        "--fixed-value",
        "--null",
        "-z",
        "--bool",
        "--int",
        "--bool-or-int",
        "--bool-or-str",
        "--path",
        "--expiry-date",
        "--color",
    }
    positionals: list[str] = []
    saw_read_action = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in read_actions:
            saw_read_action = True
            i += 1
            continue
        if arg in value_options:
            if i + 1 >= len(args):
                return ask(f"git config option requires a value: {arg}")
            i += 2
            continue
        if any(
            arg.startswith(name + "=")
            for name in value_options
            if name.startswith("--")
        ):
            i += 1
            continue
        if arg in flags:
            i += 1
            continue
        if arg.startswith("-"):
            return ask(f"unsupported git config option: {arg}")
        positionals.append(arg)
        i += 1
    if saw_read_action:
        return allow("git config read")
    if len(positionals) <= 1:
        return allow("git config value lookup")
    return ask("git config key plus value writes configuration")


def _git_patch_output_requested(args: Sequence[str]) -> bool:
    patch_flags = {
        "-p",
        "-u",
        "--patch",
        "--stat",
        "--numstat",
        "--shortstat",
        "--dirstat",
        "--summary",
        "--name-only",
        "--name-status",
        "--raw",
        "--cc",
        "--combined-all-paths",
    }
    for arg in args:
        if arg in patch_flags or any(
            arg.startswith(name + "=") for name in patch_flags if name.startswith("--")
        ):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "p" in arg[1:]:
            return True
    return False


def _git_show_is_blob_read(args: Sequence[str]) -> bool:
    positionals = [arg for arg in args if not arg.startswith("-")]
    return bool(positionals) and all(":" in arg for arg in positionals)


def _classify_git(
    argv: Sequence[str], inline_environment: dict[str, str] | None = None
) -> Result:
    index, error = _consume_git_global_options(argv)
    if error:
        return ask(error)
    if index == -1:
        return allow("git version query")
    if index is None or index >= len(argv):
        return ask("git is missing a subcommand")
    subcommand = argv[index]
    args = list(argv[index + 1 :])
    effective_environment = inline_environment or {}
    global_args = list(argv[1:index])

    def effective_env(name: str) -> str:
        return effective_environment.get(name, os.environ.get(name, ""))

    optional_locks_disabled = (
        effective_env("GIT_OPTIONAL_LOCKS") == "0"
        or "--no-optional-locks" in global_args
    )
    lazy_fetch_disabled = (
        effective_env("GIT_NO_LAZY_FETCH") == "1" or "--no-lazy-fetch" in global_args
    )
    if "GIT_PAGER" in effective_environment or "GIT_PAGER" in os.environ:
        pager_value = effective_env("GIT_PAGER")
        pager_is_explicit = True
    elif "PAGER" in effective_environment or "PAGER" in os.environ:
        pager_value = effective_env("PAGER")
        pager_is_explicit = True
    else:
        pager_value = ""
        pager_is_explicit = False
    pager_disabled = "--no-pager" in global_args or (
        pager_is_explicit and _safe_pager_value(pager_value)
    )
    if not pager_disabled:
        return ask(
            "Git pager execution is not explicitly disabled; use GIT_PAGER=cat "
            "or git --no-pager"
        )
    if not lazy_fetch_disabled:
        return ask(
            "Git lazy fetch is not explicitly disabled; use GIT_NO_LAZY_FETCH=1 "
            "or git --no-lazy-fetch"
        )

    common_bad = _git_reject_common_options(args)
    if common_bad:
        return ask(f"git option can write or execute helpers: {common_bad}")

    generic_read_only = {
        "blame",
        "cat-file",
        "cherry",
        "count-objects",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
        "whatchanged",
    }
    if subcommand in generic_read_only:
        if subcommand == "status" and not optional_locks_disabled:
            return ask(
                "git status may refresh and write the index; use "
                "GIT_OPTIONAL_LOCKS=0 git status or git --no-optional-locks status"
            )
        needs_textconv_guard = (
            subcommand == "diff"
            or (subcommand == "show" and not _git_show_is_blob_read(args))
            or (
                subcommand in {"log", "whatchanged"}
                and _git_patch_output_requested(args)
            )
        )
        if needs_textconv_guard and "--no-ext-diff" not in args:
            return ask(
                f"git {subcommand} may execute configured external diff helpers; "
                "add --no-ext-diff"
            )
        if needs_textconv_guard and "--no-textconv" not in args:
            return ask(
                f"git {subcommand} may execute configured textconv filters; "
                "add --no-textconv"
            )
        return allow(f"git {subcommand} read")
    if subcommand == "for-each-ref":
        if _git_format_invokes_signature_helper(args):
            return ask(
                "git for-each-ref format may invoke signature verification helpers"
            )
        return allow("git for-each-ref read")
    if subcommand == "branch":
        return _git_branch_read_only(args)
    if subcommand == "tag":
        return _git_tag_read_only(args)
    if subcommand == "config":
        return _git_config_read_only(args)
    if subcommand == "remote":
        if not args or args == ["-v"] or args == ["--verbose"]:
            return allow("git remote listing")
        if (
            args
            and args[0] == "get-url"
            and not any(
                arg.startswith("-") and arg not in {"--all", "--push"}
                for arg in args[1:]
            )
        ):
            return allow("git remote URL lookup")
        if (
            args
            and args[0] == "show"
            and any(arg in {"-n", "--no-query"} for arg in args[1:])
        ):
            return allow("git remote local-only show")
        return ask("git remote operation is not a proven local read")
    if subcommand == "stash":
        if args and args[0] in {"list", "show"}:
            if args[0] == "show" and _git_patch_output_requested(args[1:]):
                if "--no-ext-diff" not in args[1:]:
                    return ask(
                        "git stash show may execute external diff helpers; add --no-ext-diff"
                    )
                if "--no-textconv" not in args[1:]:
                    return ask(
                        "git stash show may execute textconv filters; add --no-textconv"
                    )
            return allow(f"git stash {args[0]} read")
        return ask("git stash operation may modify the stash")
    if subcommand == "worktree":
        if args and args[0] == "list":
            return allow("git worktree listing")
        return ask("git worktree operation may modify worktrees")
    if subcommand == "reflog":
        if not args or args[0] == "show":
            show_args = args[1:] if args and args[0] == "show" else args
            if _git_patch_output_requested(show_args):
                if "--no-ext-diff" not in show_args:
                    return ask(
                        "git reflog show may execute external diff helpers; add --no-ext-diff"
                    )
                if "--no-textconv" not in show_args:
                    return ask(
                        "git reflog show may execute textconv filters; add --no-textconv"
                    )
            return allow("git reflog read")
        return ask("git reflog operation may modify reflogs")
    if subcommand == "notes":
        if args and args[0] in {"list", "show", "get-ref"}:
            return allow(f"git notes {args[0]} read")
        return ask("git notes operation may modify notes")
    if subcommand == "submodule":
        if args and args[0] == "status":
            return allow("git submodule status read")
        return ask("git submodule operation is not a proven helper-free read")
    return ask(f"git subcommand is not in the read-only allowlist: {subcommand}")


def _parse_tar_short_bundle(arg: str) -> tuple[bool, set[str], str | None]:
    """Parse the deliberately small tar short-option subset used for listing."""

    bundle = arg.removeprefix("-")
    if not bundle:
        return False, set(), "empty tar option bundle"
    safe_letters = set("tvzjJZfC")
    write_letters = set("cxruAd")
    compression: set[str] = set()
    found_list = False
    i = 0
    while i < len(bundle):
        letter = bundle[i]
        if letter in write_letters:
            return False, compression, f"tar mode can modify files or archives: {arg}"
        if letter not in safe_letters:
            return (
                False,
                compression,
                f"unsupported tar short option in list mode: -{letter}",
            )
        if letter == "t":
            found_list = True
        elif letter in {"z", "j", "J", "Z"}:
            compression.add(letter)
        elif letter in {"f", "C"} and i + 1 < len(bundle):
            # Attached remainder is the option value, not more option letters.
            break
        i += 1
    return found_list, compression, None


def _tar_archive_paths(args: Sequence[str]) -> tuple[list[str], bool, str | None]:
    """Extract explicit tar archive names from supported short/long syntax."""

    archives: list[str] = []
    force_local = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--force-local":
            force_local = True
            i += 1
            continue
        if arg == "--file":
            if i + 1 >= len(args):
                return archives, force_local, "tar --file requires an archive name"
            archives.append(args[i + 1])
            i += 2
            continue
        if arg.startswith("--file="):
            value = arg.split("=", 1)[1]
            if not value:
                return archives, force_local, "tar --file has an empty archive name"
            archives.append(value)
            i += 1
            continue

        is_old_style_bundle = (
            i == 0 and not arg.startswith("-") and bool(re.fullmatch(r"[A-Za-z]+", arg))
        )
        if arg.startswith("-") or is_old_style_bundle:
            bundle = arg.removeprefix("-")
            if "f" in bundle:
                index = bundle.index("f")
                attached = bundle[index + 1 :]
                if attached:
                    archives.append(attached)
                    i += 1
                    continue
                if i + 1 >= len(args):
                    return archives, force_local, "tar -f requires an archive name"
                archives.append(args[i + 1])
                i += 2
                continue
        i += 1
    return archives, force_local, None


def _verify_tar_compression_helpers(
    compression: set[str], config: Config
) -> Result | None:
    helper_names = {
        "z": ("gzip", ("GZIP",)),
        "j": ("bzip2", ("BZIP2", "BZIP")),
        "J": ("xz", ("XZ_OPT",)),
        "L": ("lzma", ("XZ_OPT",)),
        "Z": ("uncompress", ()),
    }
    for mode in sorted(compression):
        helper, option_envs = helper_names[mode]
        trusted, detail = _trusted_executable(helper, config)
        if not trusted:
            return ask(f"tar compression helper is not trusted: {helper}: {detail}")
        if config.verify_ambient_environment:
            for name in option_envs:
                if os.environ.get(name):
                    return ask(
                        f"ambient {name} can inject options into tar helper {helper}"
                    )
    return None


def _classify_tar(argv: Sequence[str], config: Config) -> Result:
    found_list = False
    compression: set[str] = set()
    args = list(argv[1:])

    dangerous_long_options = (
        "--create",
        "--extract",
        "--get",
        "--append",
        "--update",
        "--concatenate",
        "--delete",
        "--to-command",
        "--checkpoint-action",
        "--use-compress-program",
        "--index-file",
        "--volno-file",
        "--listed-incremental",
        "--incremental",
        "--rmt-command",
        "--rsh-command",
        "--info-script",
        "--new-volume-script",
        "--remove-files",
        "--atime-preserve",
        "--files-from",
    )
    safe_long_flags = {
        "--list",
        "--verbose",
        "--ignore-zeros",
        "--read-full-records",
        "--anchored",
        "--no-anchored",
        "--ignore-case",
        "--no-ignore-case",
        "--wildcards",
        "--no-wildcards",
        "--wildcards-match-slash",
        "--no-wildcards-match-slash",
        "--recursion",
        "--no-recursion",
        "--one-file-system",
        "--numeric-owner",
        "--full-time",
        "--utc",
        "--show-transformed-names",
        "--show-stored-names",
        "--no-unquote",
        "--block-number",
        "--same-order",
        "--preserve-order",
        "--force-local",
        "--help",
        "--version",
    }
    safe_long_values = {
        "--file",
        "--directory",
        "--occurrence",
        "--starting-file",
        "--exclude",
        "--exclude-from",
        "--strip-components",
        "--transform",
        "--quoting-style",
        "--quote-chars",
        "--no-quote-chars",
        "--warning",
        "--checkpoint",
        "--format",
    }
    compression_long = {
        "--gzip": "z",
        "--gunzip": "z",
        "--ungzip": "z",
        "--bzip2": "j",
        "--xz": "J",
        "--lzma": "L",
        "--compress": "Z",
        "--uncompress": "Z",
    }

    exact_safe_long = safe_long_flags | safe_long_values | set(compression_long)
    abbreviation_candidates = [
        arg for arg in args if _long_option_name(arg) not in exact_safe_long
    ]
    dangerous = _has_long_option_or_abbreviation(
        abbreviation_candidates, *dangerous_long_options
    )
    if dangerous:
        return ask(f"tar option can write, delete, or execute: {dangerous}")

    if args and not args[0].startswith("-") and re.fullmatch(r"[A-Za-z]+", args[0]):
        is_list, modes, error = _parse_tar_short_bundle(args[0])
        if error:
            return ask(error)
        found_list = found_list or is_list
        compression.update(modes)

    i = 0
    while i < len(args):
        arg = args[i]
        if i == 0 and not arg.startswith("-") and re.fullmatch(r"[A-Za-z]+", arg):
            i += 1
            continue
        if arg == "--":
            i += 1
            break
        if arg.startswith("--"):
            name = _long_option_name(arg)
            if name == "--list":
                found_list = True
            if name in compression_long:
                compression.add(compression_long[name])
                i += 1
                continue
            if name in safe_long_flags:
                if "=" in arg:
                    return ask(f"tar flag does not accept an inline value: {arg}")
                i += 1
                continue
            if name in safe_long_values:
                if "=" in arg:
                    if not arg.split("=", 1)[1]:
                        return ask(f"tar option has an empty value: {arg}")
                    i += 1
                    continue
                if i + 1 >= len(args):
                    return ask(f"tar option requires a value: {arg}")
                i += 2
                continue
            return ask(f"unsupported tar long option in list mode: {arg}")
        if arg.startswith("-"):
            is_list, modes, error = _parse_tar_short_bundle(arg)
            if error:
                return ask(error)
            found_list = found_list or is_list
            compression.update(modes)
        i += 1

    if not found_list:
        return ask("tar is allowed only in list mode")
    archives, force_local, archive_error = _tar_archive_paths(args)
    if archive_error:
        return ask(archive_error)
    if not force_local:
        for archive in archives:
            if archive != "-" and ":" in archive:
                return ask(
                    "tar archive names containing ':' may invoke a remote-shell helper; "
                    "use --force-local for a local filename"
                )
    helper_issue = _verify_tar_compression_helpers(compression, config)
    if helper_issue is not None:
        return helper_issue
    return allow("tar archive listing")


def _compressor_has_safe_mode(command: str, args: Sequence[str]) -> bool:
    """Recognize a safe mode only in an unambiguous leading option prefix."""

    if command in ZCAT_COMMANDS:
        return True

    if command in {"gzip", "gunzip"}:
        safe_action_short = set("tl")
        unsafe_action_short = set("d")
        prefix_short = set("afhkLnNqrvV123456789")
        safe_action_long = {"--test", "--list"}
        unsafe_action_long = {"--decompress", "--uncompress"}
    elif command in {"bzip2", "bunzip2"}:
        safe_action_short = set("t")
        unsafe_action_short = set("dz")
        prefix_short = set("fhkLqsvV123456789")
        safe_action_long = {"--test"}
        unsafe_action_long = {"--compress", "--decompress"}
    else:
        safe_action_short = set("tl")
        unsafe_action_short = set("dz")
        prefix_short = set("efhHkqvV0123456789")
        safe_action_long = {"--test", "--list"}
        unsafe_action_long = {"--compress", "--decompress"}

    stdout_mode = False
    safe_action = False
    for index, arg in enumerate(args):
        # Operands and option termination end the only region whose parsing is
        # invariant under POSIXLY_CORRECT and the supported implementations.
        if arg == "--":
            break
        if arg == "-" or not arg.startswith("-"):
            # GNU option permutation can interpret a later "-d" or "-z" as a
            # mode override, while POSIXLY_CORRECT treats it as a filename.
            # Refuse that environment-dependent ordering.
            trailing_options = iter(args[index + 1 :])
            for trailing in trailing_options:
                if trailing == "--":
                    break
                if trailing.startswith("-") and trailing != "-":
                    return False
            break
        if arg.startswith("--"):
            # Unknown and argument-taking long options are deliberately not
            # skipped: a following "-c", "-t", or "-l" may be their value.
            if arg in {"--stdout", "--to-stdout"}:
                stdout_mode = True
            elif arg in safe_action_long:
                safe_action = True
            elif arg in unsafe_action_long:
                safe_action = False
            else:
                return False
            continue
        for letter in arg[1:]:
            if letter == "c":
                stdout_mode = True
            elif letter in safe_action_short:
                safe_action = True
            elif letter in unsafe_action_short:
                safe_action = False
            elif letter not in prefix_short:
                # For example, gzip -S -c and xz -Flzma: the remainder or
                # following word is an option value, not another mode option.
                return False
    return stdout_mode or safe_action


def _classify_compressor(argv: Sequence[str]) -> Result:
    command = os.path.basename(argv[0])
    args = argv[1:]
    output_option = _has_long_option_or_abbreviation(args, "--output")
    if output_option:
        return ask(f"{command} output option may write a named file: {output_option}")
    for arg in args:
        if arg == "-o" or _short_cluster_contains(arg, "o"):
            return ask(f"{command} output option may write a named file: {arg}")

    if _compressor_has_safe_mode(command, args):
        return allow(f"{command} output/test/list mode")
    return ask(f"{command} may replace or create files without stdout/test mode")


def _classify_unzip(argv: Sequence[str]) -> Result:
    safe_letters = set("clptvzZ")
    saw_safe_mode = False
    before_archive = True
    for arg in argv[1:]:
        if not before_archive:
            continue
        # Info-ZIP uses "--" as an option-negation toggle in positions where
        # conventional parsers use it as a terminator. It can cancel an earlier
        # list/stdout mode, so its meaning is not safe to infer.
        if arg == "--":
            return ask("unzip -- has ambiguous mode-negation semantics")
        if arg == "-":
            return ask("unzip lone - has ambiguous option semantics")
        if not arg.startswith("-"):
            before_archive = False
            continue
        if arg.startswith("--"):
            return ask(f"unsupported unzip option: {arg}")
        letters = set(arg[1:])
        if letters & safe_letters:
            saw_safe_mode = True
        if letters - safe_letters - set("q"):
            return ask(f"unsupported unzip option cluster: {arg}")
    return (
        allow("unzip listing/test/stdout mode")
        if saw_safe_mode
        else ask("unzip defaults to extraction")
    )


def _classify_sysctl(argv: Sequence[str]) -> Result:
    args = argv[1:]
    dangerous = _has_long_option_or_abbreviation(args, "--write", "--load", "--system")
    if dangerous:
        return ask(f"sysctl option can change kernel settings: {dangerous}")
    safe_short = {
        "-a",
        "-A",
        "-b",
        "-d",
        "-e",
        "-i",
        "-n",
        "-N",
        "-o",
        "-q",
        "-r",
        "-x",
    }
    safe_long_flags = {
        "--all",
        "--binary",
        "--deprecated",
        "--description",
        "--ignore",
        "--names",
        "--quiet",
        "--values",
        "--help",
        "--version",
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if "=" in arg and not arg.startswith("--pattern="):
            return ask("sysctl assignment can change kernel settings")
        if arg in {"-w", "-p", "-f"} or arg.startswith(("-w", "-p", "-f")):
            return ask(f"sysctl option can change kernel settings: {arg}")
        if arg in safe_short or arg in safe_long_flags:
            i += 1
            continue
        if arg == "--pattern":
            if i + 1 >= len(args):
                return ask("sysctl --pattern requires a value")
            i += 2
            continue
        if arg.startswith("--pattern="):
            i += 1
            continue
        if arg.startswith("-"):
            return ask(f"unsupported sysctl option: {arg}")
        i += 1
    return allow("sysctl read")


def _classify_date(argv: Sequence[str]) -> Result:
    args = list(argv[1:])
    no_set = False
    i = 0
    long_value_options = {"--date", "--file", "--reference", "--rfc-3339"}
    long_flags = {
        "--debug",
        "--help",
        "--resolution",
        "--rfc-email",
        "--universal",
        "--utc",
        "--version",
    }
    short_value_options = set("dfrvz")
    short_flags = set("jnRu")

    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg == "-" or not arg.startswith("-"):
            break
        if arg.startswith("--"):
            if _is_long_option_or_abbreviation(arg, "--set"):
                return ask(f"date option changes the system clock: {arg}")
            name = _long_option_name(arg)
            if name in long_value_options:
                if "=" not in arg:
                    if i + 1 >= len(args):
                        return ask(f"date option requires a value: {arg}")
                    i += 1
                elif not arg.split("=", 1)[1]:
                    return ask(f"date option has an empty value: {arg}")
            elif name == "--iso-8601":
                pass
            elif arg not in long_flags:
                return ask(f"unsupported date option: {arg}")
            i += 1
            continue

        bundle = arg[1:]
        position = 0
        while position < len(bundle):
            letter = bundle[position]
            if letter == "s":
                return ask("date -s changes the system clock")
            if letter == "j":
                no_set = True
                position += 1
                continue
            if letter in short_flags:
                position += 1
                continue
            if letter == "I":
                # BSD/GNU -I has an optional attached output precision.
                break
            if letter in short_value_options:
                if position + 1 == len(bundle):
                    if i + 1 >= len(args):
                        return ask(f"date option requires a value: -{letter}")
                    i += 1
                # An attached remainder or the next argv word is the value.
                break
            return ask(f"unsupported date option: -{letter}")
        i += 1

    # On BSD/macOS every non-format operand can be a clock-setting operand,
    # including the two-digit minutes form and arbitrary input accepted by -f.
    # Only an -j parsed as a real option (not an option value or trailing word)
    # proves that those operands will be parsed without setting the clock.
    if not no_set and any(not arg.startswith("+") for arg in args[i:]):
        return ask("date operand may set the system clock on BSD/macOS")
    return allow("date query or formatting")


def _classify_hostname(argv: Sequence[str]) -> Result:
    query_flags = {
        "-a",
        "-A",
        "-d",
        "-f",
        "-i",
        "-I",
        "-s",
        "-y",
        "--fqdn",
        "--long",
        "--short",
    }
    positionals = [arg for arg in argv[1:] if not arg.startswith("-")]
    if positionals:
        return ask("hostname positional argument may change the host name")
    if any(
        arg in {"-b", "-F", "--boot", "--file"} or arg.startswith("--file=")
        for arg in argv[1:]
    ):
        return ask("hostname option may change the host name")
    if any(arg.startswith("-") and arg not in query_flags for arg in argv[1:]):
        return ask("unsupported hostname option")
    return allow("hostname query")


def _classify_file(argv: Sequence[str]) -> Result:
    dangerous = _has_long_option_or_abbreviation(
        argv[1:], "--compile", "--uncompress", "--uncompress-noreport"
    )
    if dangerous:
        if _is_long_option_or_abbreviation(dangerous, "--compile"):
            return ask(f"file --compile writes a magic database: {dangerous}")
        return ask(f"file decompression may execute external helpers: {dangerous}")
    for arg in argv[1:]:
        if arg == "-C" or _short_cluster_contains(arg, "C"):
            return ask("file -C writes a compiled magic database")
        if arg in {"-z", "-Z", "-S"} or _short_cluster_contains(arg, "zZS"):
            return ask(
                "file decompression/sandbox options may execute external helpers"
            )
    return allow("file type inspection")


def _classify_tree(argv: Sequence[str]) -> Result:
    dangerous = _has_long_option_or_abbreviation(argv[1:], "--output")
    if dangerous:
        return ask(f"tree output option writes a file: {dangerous}")
    for arg in argv[1:]:
        if arg == "-o" or arg.startswith("-o"):
            return ask("tree output option writes a file")
    return allow("tree directory listing")


def _classify_binutils(argv: Sequence[str]) -> Result:
    dangerous = _has_long_option_or_abbreviation(argv[1:], "--plugin")
    if dangerous:
        return ask(
            f"{os.path.basename(argv[0])} plugin option loads executable code: {dangerous}"
        )
    return allow(f"{os.path.basename(argv[0])} binary inspection")


def _safe_pager_value(value: str) -> bool:
    if not value:
        return True
    try:
        parts = value.split()
    except ValueError:
        return False
    return parts in [["cat"], ["/bin/cat"], ["/usr/bin/cat"]]


def _ambient_environment_issue(
    command_word: str,
    command: str,
    argv: Sequence[str],
    config: Config,
    inline_environment: dict[str, str] | None = None,
) -> str | None:
    if not config.verify_ambient_environment:
        return None

    overrides = inline_environment or {}

    def env_value(name: str) -> str:
        if name in overrides:
            return overrides[name]
        return os.environ.get(name, "")

    # These variables can arrange for code to run before or inside an otherwise
    # trusted executable. They invalidate command-name and executable-path checks.
    global_injection_variables = (
        "LD_PRELOAD",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
    )
    for name in global_injection_variables:
        if env_value(name):
            return f"ambient {name} can inject code or replace executable dependencies"
    for name, ambient_value in os.environ.items():
        value = overrides.get(name, ambient_value)
        if name.startswith("BASH_FUNC_") and value:
            return f"ambient {name} imports a Bash function that can shadow commands"

    runtime_variables = {
        "python": (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONINSPECT",
            "PYTHONSTARTUP",
            "PYTHONWARNINGS",
        ),
        "perl": ("PERL5OPT", "PERL5LIB"),
        "ruby": ("RUBYOPT", "RUBYLIB"),
        "node": ("NODE_OPTIONS", "NODE_PATH"),
    }
    runtime = _command_runtime(command_word)
    if runtime is not None:
        for name in runtime_variables[runtime]:
            if env_value(name):
                return (
                    f"ambient {name} can inject code or replace dependencies "
                    f"for {runtime} scripts"
                )
    elif command == "yq":
        # The name `yq` is used by both compiled implementations and Python
        # entry-point scripts. If the executable cannot be inspected, fail
        # closed when Python's import path has been customized.
        for name in runtime_variables["python"]:
            if env_value(name):
                return f"ambient {name} may affect a script-based yq; executable runtime is unknown"

    env = os.environ
    if command == "git":
        direct = {
            "GIT_EXTERNAL_DIFF",
            "GIT_EXEC_PATH",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
        }
        for name in direct:
            if env_value(name):
                return f"ambient {name} can alter Git execution, configuration, or write targets"
        for name, ambient_value in env.items():
            value = overrides.get(name, ambient_value)
            if value and name.startswith(
                ("GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
            ):
                return f"ambient {name} can alter Git behavior or write trace output"
        if "GIT_PAGER" in overrides or "GIT_PAGER" in os.environ:
            pager_name = "GIT_PAGER"
            pager_value = env_value("GIT_PAGER")
        else:
            pager_name = "PAGER"
            pager_value = env_value("PAGER")
        if pager_value and not _safe_pager_value(pager_value):
            return f"ambient {pager_name} may execute a pager: {pager_value!r}"
        for name in ("LESSOPEN", "LESSCLOSE"):
            if env_value(name):
                return f"ambient {name} may execute a pager preprocessor"
        optional_locks = env_value("GIT_OPTIONAL_LOCKS")
        if optional_locks not in {"", "0"}:
            return "ambient GIT_OPTIONAL_LOCKS is not 0"

    if command in GREP_COMMANDS and env_value("GREP_OPTIONS"):
        return "ambient GREP_OPTIONS can inject command options"
    if (
        command == "rg"
        and env_value("RIPGREP_CONFIG_PATH")
        and "--no-config" not in argv[1:]
    ):
        return "ambient RIPGREP_CONFIG_PATH can inject helper-executing options; use --no-config"
    if command == "tar":
        if env_value("TAR_OPTIONS"):
            return "ambient TAR_OPTIONS can inject write or helper-executing options"
        for name in ("TAPE", "TAR_RSH", "RSH"):
            if env_value(name):
                return (
                    f"ambient {name} can select a remote archive or remote-shell helper"
                )
    if command in {"gzip", "gunzip", "zcat"} and env_value("GZIP"):
        return "ambient GZIP can inject compressor options"
    if command in {"bzip2", "bunzip2", "bzcat"} and (
        env_value("BZIP2") or env_value("BZIP")
    ):
        return "ambient BZIP2/BZIP can inject compressor options"
    if command in {"xz", "unxz", "lzma", "unlzma", "xzcat", "lzcat"} and env_value(
        "XZ_OPT"
    ):
        return "ambient XZ_OPT can inject compressor options"
    if command in {"unzip", "zipinfo"}:
        for name in ("UNZIP", "UNZIPOPT", "ZIPINFO", "ZIPINFOOPT"):
            if env_value(name):
                return f"ambient {name} can inject archive options"
    if command in {"nm", "objdump", "readelf", "strings"}:
        for name in ("DEBUGINFOD_URLS", "DEBUGINFOD_CACHE_PATH"):
            if env_value(name):
                return f"ambient {name} can trigger debuginfod network/cache activity"
    return None


def _classify_argv(argv: Sequence[str], config: Config) -> Result:
    if not argv:
        return ask("empty command")
    try:
        argv, assignments = _strip_safe_env(argv)
    except PolicyError as exc:
        return ask(str(exc))
    inline_environment = {
        match.group(1): match.group(2)
        for assignment in assignments
        if (match := _ASSIGNMENT_RE.fullmatch(assignment)) is not None
    }

    command_word = argv[0]
    command = os.path.basename(command_word)
    trusted, detail = _trusted_executable(command_word, config)
    if not trusted:
        return ask(detail)
    environment_issue = _ambient_environment_issue(
        command_word, command, argv, config, inline_environment
    )
    if environment_issue:
        return ask(environment_issue)

    if command == "cd":
        result = _classify_cd(argv)
    elif command == "command":
        result = _classify_command_builtin(argv, config)
    elif command == "env":
        result = _classify_env(argv, config)
    elif command == "timeout":
        result = _classify_timeout(argv, config)
    elif command == "date":
        result = _classify_date(argv)
    elif command == "hostname":
        result = _classify_hostname(argv)
    elif command == "file":
        result = _classify_file(argv)
    elif command == "tree":
        result = _classify_tree(argv)
    elif command in {"nm", "objdump"}:
        result = _classify_binutils(argv)
    elif command == "printf":
        result = _classify_printf(argv)
    elif command in {"diff", "diff3"}:
        result = _classify_diff(argv)
    elif command in SIMPLE_READ_ONLY_COMMANDS:
        result = allow(f"{command} has no direct write-capable interface")
    elif command in GREP_COMMANDS:
        result = allow(f"{command} search")
    elif command == "rg":
        result = _classify_rg(argv)
    elif command in {"fd", "fdfind"}:
        result = _classify_fd(argv)
    elif command == "find":
        result = _classify_find(argv, config)
    elif command == "uniq":
        result = _classify_uniq(argv)
    elif command == "base64":
        result = _classify_base64(argv)
    elif command == "jq":
        result = allow("jq query to standard output")
    elif command == "yq":
        result = _classify_yq(argv)
    elif command == "sed":
        result = _classify_sed(argv)
    elif command == "git":
        result = _classify_git(argv, inline_environment)
    elif command == "tar":
        result = _classify_tar(argv, config)
    elif (
        command
        in {
            "gzip",
            "gunzip",
            "bzip2",
            "bunzip2",
            "xz",
            "unxz",
            "lzma",
            "unlzma",
        }
        or command in ZCAT_COMMANDS
    ):
        result = _classify_compressor(argv)
    elif command == "zipinfo":
        result = allow(f"{command} read to standard output")
    elif command == "unzip":
        result = _classify_unzip(argv)
    elif command == "sysctl":
        result = _classify_sysctl(argv)
    else:
        result = ask(f"command is not in the read-only allowlist: {command}")

    if result.verdict is Verdict.ALLOW and assignments:
        return allow(f"safe env prefix; {result.reason}")
    return result


def classify(command: str, config: Config | None = None) -> Result:
    config = config or load_config()
    if not isinstance(command, str) or not command.strip():
        return ask("empty command")
    if len(command.encode("utf-8")) > config.max_command_bytes:
        return ask("command exceeds configured size limit")
    try:
        tokens = lex_shell(command)
        simple_commands = _split_simple_commands(tokens)
        reasons: list[str] = []
        for index, segment in enumerate(simple_commands, start=1):
            argv, redirection_notes = _strip_redirections(segment)
            result = _classify_argv(argv, config)
            if result.verdict is Verdict.ASK:
                return ask(f"segment {index}: {result.reason}")
            reason = result.reason
            if redirection_notes:
                reason += "; " + ", ".join(redirection_notes)
            reasons.append(reason)
        return allow("; ".join(reasons))
    except (PolicyError, ValueError, OSError) as exc:
        return ask(str(exc))


def _emit_allow() -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def _hook_main(config: Config) -> int:
    if os.name == "nt":
        # Native Windows Codex agents execute PowerShell, while this classifier
        # models POSIX shell syntax. Parsing PowerShell as POSIX could turn
        # escaped control operators into apparently harmless literal arguments,
        # so the hook must remain silent and defer to normal approval.
        return 0

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event_name = payload.get("hook_event_name")
        if event_name not in {None, "PermissionRequest"}:
            return 0
        if payload.get("tool_name") != "Bash":
            return 0
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        command = tool_input.get("command")
        if not isinstance(command, str):
            return 0
        result = classify(command, config)
        if result.verdict is Verdict.ALLOW:
            _emit_allow()
    except Exception:  # noqa: BLE001 -- the security boundary must fail closed.
        # Permission hooks must fail closed: no output means normal human review.
        return 0
    return 0


def _cli_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--check",
        metavar="COMMAND",
        help="classify one command without reading a hook payload",
    )
    parser.add_argument(
        "--no-path-check",
        action="store_true",
        help="disable executable-root verification for this diagnostic invocation",
    )
    args = parser.parse_args(argv)

    if os.name == "nt" and args.check is not None:
        print(f"ASK: {_NATIVE_WINDOWS_UNSUPPORTED_REASON}")
        return 1

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 -- invalid config must fail closed.
        if args.check is not None:
            print(f"ASK: invalid configuration: {exc}")
            return 1
        return 0
    if args.no_path_check:
        config = Config(
            False,
            config.trusted_executable_roots,
            config.max_command_bytes,
            config.verify_ambient_environment,
        )

    if args.check is not None:
        result = classify(args.check, config)
        print(f"{result.verdict.value.upper()}: {result.reason}")
        return 0 if result.verdict is Verdict.ALLOW else 1
    return _hook_main(config)


def main() -> int:
    """Console-script entry point."""
    return _cli_main()


if __name__ == "__main__":
    raise SystemExit(main())

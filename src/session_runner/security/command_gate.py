"""Three-tier read-only gate for agent ``bash`` calls.

See ``.dev/serve_project/plans/skill-dynamic-injection.md`` for the design.

- ``SAFE``: read-only whitelisted executable, no shell composition, every path
  argument inside the allowed prefixes. Auto-approved.
- ``BLOCKED``: interpreter, writer or privilege tool. Rejected outright.
- ``RISKY``: everything else (unknown executables, composition, writes).
  Handed to the approver.

The four SAFE conditions (executable / argument form / no composition /
path prefix) are intentionally conservative: a command only auto-approves when
all four hold, so a miss downgrades to ``RISKY`` instead of leaking through.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Executables that only read when their arguments also check out.
SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "basename",
        "cat",
        "cut",
        "date",
        "dirname",
        "echo",
        "file",
        "grep",
        "head",
        "ls",
        "md5sum",
        "od",
        "pwd",
        "realpath",
        "readlink",
        "sha256sum",
        "sort",
        "stat",
        "tail",
        "tr",
        "uniq",
        "wc",
        "whoami",
    }
)

#: Executables that are never allowed, whatever their arguments look like.
DENIED_COMMANDS: frozenset[str] = frozenset(
    {
        "apt",
        "apt-get",
        "bash",
        "chmod",
        "chown",
        "cp",
        "dd",
        "docker",
        "kill",
        "kubectl",
        "mkdir",
        "mkfs",
        "mv",
        "nc",
        "node",
        "npm",
        "perl",
        "pip",
        "pip3",
        "pkill",
        "python",
        "python3",
        "rm",
        "rmdir",
        "rsync",
        "scp",
        "sh",
        "shred",
        "ssh",
        "su",
        "sudo",
        "tee",
        "touch",
        "truncate",
        "xargs",
        "zsh",
    }
)

#: Default (deployment) read-only whitelist. ``curl``/``wget`` are deliberately
#: NOT denied: a tenant preset may declare safe prefixes for download-style
#: CLIs, and everything else stays RISKY (``default`` mode asks a human,
#: ``silent`` mode asks the LLM judge).

#: Flags that turn an otherwise read-only command into a writer.
UNSAFE_FLAGS: dict[str, tuple[str, ...]] = {
    "sort": ("-o", "--output"),
}

#: Directories a SAFE command may be invoked from by absolute path.
TRUSTED_EXECUTABLE_DIRS: tuple[str, ...] = ("/bin", "/usr/bin")

#: Characters that compose, redirect or substitute. Never auto-approved.
COMPOSITION_CHARS: frozenset[str] = frozenset("|&;<>`$\n\r")


class CommandTier(StrEnum):
    SAFE = "safe"
    RISKY = "risky"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CommandVerdict:
    tier: CommandTier
    reason: str


@dataclass(frozen=True)
class CommandPolicy:
    """Deployment-tunable lists. Tightening is the only supported direction."""

    safe: frozenset[str] = SAFE_COMMANDS
    denied: frozenset[str] = DENIED_COMMANDS

    def tightened(
        self,
        *,
        disable_safe: Iterable[str] = (),
        deny_more: Iterable[str] = (),
    ) -> CommandPolicy:
        safe = self.safe - {item.strip().lower() for item in disable_safe if item.strip()}
        denied = self.denied | {item.strip().lower() for item in deny_more if item.strip()}
        return CommandPolicy(safe=frozenset(safe), denied=frozenset(denied))


DEFAULT_POLICY = CommandPolicy()


def classify_command(
    command: str,
    *,
    allowed_prefixes: Sequence[str],
    cwd: str | Path,
    policy: CommandPolicy = DEFAULT_POLICY,
    declared_safe_prefixes: Sequence[str] = (),
) -> CommandVerdict:
    """Classify one bash command into SAFE / RISKY / BLOCKED.

    ``declared_safe_prefixes`` are sub-command prefixes a tenant preset marked
    as safe (e.g. ``mytool list``). They are the *second* source of SAFE
    verdicts; the deployment lists still win: a denied executable stays denied
    and composition characters never auto-approve.
    """

    text = (command or "").strip()
    if not text:
        return CommandVerdict(CommandTier.BLOCKED, "empty command")

    if any(char in COMPOSITION_CHARS for char in text):
        return CommandVerdict(
            CommandTier.RISKY,
            "shell composition (pipe / redirect / substitution) is never auto-approved",
        )

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        return CommandVerdict(CommandTier.RISKY, f"unparseable command: {exc}")
    if not tokens:
        return CommandVerdict(CommandTier.BLOCKED, "empty command")

    raw_executable = tokens[0]
    executable = Path(raw_executable).name

    if executable in policy.denied:
        return CommandVerdict(CommandTier.BLOCKED, f"{executable} is on the denied list")

    declared = _match_declared_prefix(tokens, declared_safe_prefixes)
    if declared is not None:
        return CommandVerdict(
            CommandTier.SAFE, f"declared safe prefix (tenant preset): {declared}"
        )

    if ("/" in raw_executable or "\\" in raw_executable) and not _is_trusted_executable_path(
        raw_executable
    ):
        return CommandVerdict(
            CommandTier.RISKY, f"executable path is not trusted: {raw_executable}"
        )

    if executable not in policy.safe:
        return CommandVerdict(
            CommandTier.RISKY, f"{executable} is not on the read-only whitelist"
        )

    for token in tokens[1:]:
        if _is_unsafe_flag(executable, token):
            return CommandVerdict(
                CommandTier.RISKY, f"{executable} flag {token} can write"
            )

    prefixes = [Path(prefix).resolve() for prefix in allowed_prefixes]
    base = Path(cwd).resolve()
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        candidate = Path(token)
        resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        if not any(resolved == prefix or resolved.is_relative_to(prefix) for prefix in prefixes):
            return CommandVerdict(
                CommandTier.RISKY, f"path outside allowed prefixes: {token}"
            )

    return CommandVerdict(
        CommandTier.SAFE, "read-only whitelisted command inside allowed prefixes"
    )


def _is_trusted_executable_path(raw_executable: str) -> bool:
    normalized = raw_executable.replace("\\", "/")
    return any(
        normalized.startswith(f"{directory}/") for directory in TRUSTED_EXECUTABLE_DIRS
    )


def _match_declared_prefix(
    tokens: Sequence[str], declared_safe_prefixes: Sequence[str]
) -> str | None:
    """Return the declared prefix a command matches, if any."""

    for declared in declared_safe_prefixes:
        parts = (declared or "").split()
        if not parts:
            continue
        if len(tokens) >= len(parts) and list(tokens[: len(parts)]) == parts:
            return declared
    return None


def _is_unsafe_flag(executable: str, token: str) -> bool:
    for flag in UNSAFE_FLAGS.get(executable, ()):
        if token == flag or token.startswith(f"{flag}="):
            return True
    return False

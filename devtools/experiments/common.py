"""Shared helpers for the AgentSupport optimization experiments.

Every experiment script writes phase evidence into
``docs/optimization-experiments/evidence/<experiment>/<phase>.md`` so the
same script can be re-run before and after a code change to prove the fix.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the src tree importable regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EVIDENCE_ROOT = REPO_ROOT / "docs" / "optimization-experiments" / "evidence"

# Local experiment database; the schema is created/dropped by each script.
EXPERIMENT_DATABASE_URL = os.getenv(
    "AGENTSUPPORT_EXP_DATABASE_URL",
    "postgresql+psycopg://agent:agent@localhost:5432/agentsupport_exp",
)

# Isolate from the developer .env file for every experiment process.
os.environ.setdefault("AGENTSUPPORT_PERSISTENCE_MODE", "memory")
os.environ.setdefault("AGENTSUPPORT_EXECUTION_MODE", "inline")
os.environ.setdefault("AGENTSUPPORT_REDIS_URL", "")
os.environ.setdefault("AGENTSUPPORT_RUNTIME_DRIVER", "memory")
os.environ.setdefault("AGENTSUPPORT_AUTO_CREATE_SCHEMA", "false")


def git_head() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT,
                text=True,
            )
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def record_evidence(
    experiment: str,
    phase: str,
    markdown: str,
    *,
    command: str = "",
) -> Path:
    """Persist one phase's evidence and return the written file path."""

    target_dir = EVIDENCE_ROOT / experiment
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{phase}.md"
    header = [
        f"# Experiment `{experiment}` — phase `{phase}`",
        "",
        f"- timestamp: `{datetime.now(UTC).isoformat(timespec='seconds')}`",
        f"- git head: `{git_head()}`",
        f"- python: `{sys.version.split()[0]}`",
        f"- database: `{EXPERIMENT_DATABASE_URL}`",
    ]
    if command:
        header.append(f"- command: `{command}`")
    body = "\n".join(header) + "\n\n---\n\n" + markdown.rstrip() + "\n"
    target.write_text(body, encoding="utf-8")
    print(f"[evidence] wrote {target}")
    return target


def table(rows: list[list[object]], headers: list[str]) -> str:
    """Render a compact markdown table."""

    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)

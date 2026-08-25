import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[2]
TRAE_SOURCE = PROJECT_ROOT / ".tools" / "trae-agent-src"
sys.path.insert(0, str(TRAE_SOURCE))

from trae_agent.agent.agent import Agent  # noqa: E402
from trae_agent.utils.config import Config  # noqa: E402


MARKER = "ALPHA-4821"


def load_config() -> Config:
    return Config.create(config_file=str(ROOT / "trae-config.yaml"))


def message_texts(agent: Agent) -> list[str]:
    return [message.content or "" for message in agent.agent.initial_messages]


async def verify_same_process_lifecycle() -> dict[str, object]:
    agent = Agent("trae_agent", load_config())
    core_object_id = id(agent.agent)

    agent.agent.new_task(
        f"Remember marker {MARKER}",
        {"project_path": str(ROOT / "workspace"), "issue": f"Remember marker {MARKER}"},
    )
    first_messages = message_texts(agent)

    agent.agent.new_task(
        "What marker was supplied previously?",
        {"project_path": str(ROOT / "workspace"), "issue": "What marker was supplied previously?"},
    )
    second_messages = message_texts(agent)

    assert id(agent.agent) == core_object_id
    assert any(MARKER in text for text in first_messages)
    assert all(MARKER not in text for text in second_messages)
    assert len(first_messages) == 2
    assert len(second_messages) == 2

    lifecycle_counts = {"mcp_init": 0, "execute": 0, "mcp_cleanup": 0}
    agent.agent.allow_mcp_servers = ["synthetic"]

    async def initialise_mcp(_self):
        lifecycle_counts["mcp_init"] += 1

    async def execute_task(_self):
        lifecycle_counts["execute"] += 1
        return SimpleNamespace(success=True, final_result="ok")

    async def cleanup_mcp(_self):
        lifecycle_counts["mcp_cleanup"] += 1

    agent.agent.initialise_mcp = MethodType(initialise_mcp, agent.agent)
    agent.agent.execute_task = MethodType(execute_task, agent.agent)
    agent.agent.cleanup_mcp_clients = MethodType(cleanup_mcp, agent.agent)

    for issue in ("task one", "task two"):
        await agent.run(issue, {"project_path": str(ROOT / "workspace"), "issue": issue})

    assert lifecycle_counts == {"mcp_init": 2, "execute": 2, "mcp_cleanup": 2}

    return {
        "same_core_object_reused": True,
        "first_task_message_count": len(first_messages),
        "second_task_message_count": len(second_messages),
        "prior_marker_retained_by_second_task": False,
        "lifecycle_counts_for_two_tasks": lifecycle_counts,
    }


def measure_startup() -> dict[str, object]:
    creation_ms = []
    for _ in range(20):
        started = time.perf_counter()
        Agent("trae_agent", load_config())
        creation_ms.append((time.perf_counter() - started) * 1000)

    cli = PROJECT_ROOT / ".tools" / "bin" / "trae-cli.exe"
    process_ms = []
    for _ in range(10):
        started = time.perf_counter()
        completed = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
        assert "trae-cli" in completed.stdout
        process_ms.append((time.perf_counter() - started) * 1000)

    return {
        "agent_object_creation_ms": {
            "median": round(statistics.median(creation_ms), 3),
            "p95": round(sorted(creation_ms)[18], 3),
        },
        "cli_process_start_ms": {
            "median": round(statistics.median(process_ms), 3),
            "p95": round(sorted(process_ms)[8], 3),
        },
    }


async def main() -> None:
    result = {
        "trae_version": subprocess.check_output(
            [str(PROJECT_ROOT / ".tools" / "bin" / "trae-cli.exe"), "--version"],
            text=True,
            encoding="utf-8",
        ).strip(),
        "lifecycle": await verify_same_process_lifecycle(),
        "startup": measure_startup(),
    }
    output = ROOT / "results" / "trae-lifecycle.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

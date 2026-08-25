"""Experiment 16: runner selection ignores the advertised load.

``select_ready_runner`` returns the first READY registration from the registry,
so a Runner that reports a high load is still preferred over an idle one. This
experiment registers three runners, sets loads 5 / 2 / 0 via heartbeats and
records which runner is selected over 60 calls.
"""

from __future__ import annotations

import argparse
from collections import Counter

from common import record_evidence, table

from agent_runner_contracts.registration import RunnerRegistrationRequest
from agentsupport.application.runner_registry import InMemoryRunnerRegistry
from agentsupport.application.service import AgentSupportService


def main(phase: str) -> None:
    registry = InMemoryRunnerRegistry()
    runners: dict[str, object] = {}
    for name, endpoint in (("A", "http://runner-a:8080"), ("B", "http://runner-b:8080"), ("C", "http://runner-c:8080")):
        entry = registry.register(
            RunnerRegistrationRequest(
                provider="trae",
                endpoint=endpoint,
                capabilities=["run"],
            ),
            token_hash=f"hash-{name}",
        )
        runners[name] = entry.runner_id
    names = {str(runner_id): name for name, runner_id in runners.items()}

    loads = {"A": 5, "B": 2, "C": 0}
    for name, runner_id in runners.items():
        registry.heartbeat(
            runner_id,
            "READY",
            loads[name],
            capabilities=["run"],
        )

    # Exercise the service-level selection path.
    service = AgentSupportService.__new__(AgentSupportService)
    service.runner_registry = registry
    selections = Counter()
    for _ in range(60):
        selected = service.select_ready_runner({"run"})
        selections[names[str(selected.runner_id)]] += 1

    rows = [
        ["Runner A advertised load", 5],
        ["Runner B advertised load", 2],
        ["Runner C advertised load", 0],
        ["selection count -> A", selections.get("A", 0)],
        ["selection count -> B", selections.get("B", 0)],
        ["selection count -> C", selections.get("C", 0)],
    ]
    markdown = "\n".join(
        [
            "## Runner selection vs advertised load",
            table(rows, ["observation", "value"]),
            "",
            "Before the fix the first-registered Runner is always picked even",
            "with load 5. After the fix the lowest-load Runner is preferred.",
        ]
    )
    record_evidence(
        "exp16_runner_load_balancing",
        phase,
        markdown,
        command=f"python devtools/experiments/exp16_runner_load_balancing.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)

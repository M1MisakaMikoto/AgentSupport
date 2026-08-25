"""Experiment 11: Kubernetes runner Pods ship without resource limits.

The Docker development driver bounds CPU/memory/pids, but the Kubernetes
runtime's Pod manifest has no ``resources`` block, so a runaway agent can
exhaust node capacity in production. This experiment renders the Pod manifest
and checks the container resources before and after the fix.
"""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from common import record_evidence, table

from agentsupport.adapters.runtime.kubernetes import KubernetesRuntimeDriver


def main(phase: str) -> None:
    driver = KubernetesRuntimeDriver(
        api_server="https://kubernetes.test",
        namespace="agents",
        image="agentsupport-runner:test",
    )
    manifest = driver._pod_manifest(
        "runner-test-abc123-7",
        uuid4(),
        uuid4(),
        7,
    )
    container = manifest["spec"]["containers"][0]
    resources = container.get("resources")

    rows = [
        ["container name", container["name"]],
        ["resources block present", str(resources is not None)],
        ["resources.requests", json.dumps((resources or {}).get("requests", {}))],
        ["resources.limits", json.dumps((resources or {}).get("limits", {}))],
        ["readOnlyRootFilesystem", container.get("securityContext", {}).get(
            "readOnlyRootFilesystem", False
        )],
    ]
    markdown = "\n".join(
        [
            "## Kubernetes runner Pod resources",
            table(rows, ["observation", "value"]),
            "",
            "### rendered container spec (resources section)",
            "```json",
            json.dumps(
                {
                    "name": container["name"],
                    "resources": resources,
                    "securityContext": container.get("securityContext"),
                },
                indent=2,
                default=str,
            ),
            "```",
        ]
    )
    record_evidence(
        "exp11_k8s_pod_resources",
        phase,
        markdown,
        command=f"python devtools/experiments/exp11_k8s_pod_resources.py --phase {phase}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["before", "after"])
    args = parser.parse_args()
    main(args.phase)

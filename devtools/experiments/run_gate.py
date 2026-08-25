"""TDD gate: re-run every experiment in its fixed ("after") state.

Each experiment script asserts its own key invariants when run with
``--phase after``, so this runner is the regression gate: if any fix regresses,
the corresponding experiment exits non-zero and the gate fails.

Usage (from the repository root, with PostgreSQL/Redis available):

    python devtools/experiments/run_gate.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# exp8 has no script (it is the CI coverage gap, validated by ci.yml itself).
EXPERIMENTS = [
    "exp1_nplus1",
    "exp2_event_load",
    "exp3_publish_tasks",
    "exp4_runner_orphan",
    "exp5_checkpoint_growth",
    "exp6_outbox_metrics",
    "exp7_indexes",
    "exp9_event_loop_blocking",
    "exp10_request_size_limits",
    "exp11_k8s_pod_resources",
    "exp12_rest_route_blocking",
    "exp13_sse_keepalive",
    "exp14_list_pagination",
    "exp15_redis_pubsub_churn",
    "exp16_runner_load_balancing",
    "exp17_httpx_client_reuse",
    "exp18_event_store_lost_wakeup",
    "exp19_sse_last_event_id",
    "exp20_metrics_temporal_truth",
    "exp21_list_default_limit",
    "exp22_event_partitioning",
]


def main() -> int:
    failed: list[str] = []
    for name in EXPERIMENTS:
        script = REPO_ROOT / "devtools" / "experiments" / f"{name}.py"
        print(f"=== gate: {name} ===", flush=True)
        result = subprocess.run(
            [sys.executable, str(script), "--phase", "after"],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            failed.append(name)
    if failed:
        print(f"GATE FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"ALL {len(EXPERIMENTS)} EXPERIMENT GATES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

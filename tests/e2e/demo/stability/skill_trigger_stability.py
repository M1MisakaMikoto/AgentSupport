"""Skill trigger stability runner — user-level, Playwright-driven.

Drives the demo UI the way a person does: pick the skill chip, type the task,
send it. Repeats the same flow N times and reports how often the agent actually
read the skill (trigger rate) plus how often small talk wrongly pulled it in
(false-trigger rate).

The "did it trigger" decision is read back from the control-plane event stream
(a ``bash`` call whose command reads that skill's ``SKILL.md``), not from the
agent's own prose.

Usage (demo stack running, Chromium/Edge available)::

    .venv/Scripts/python.exe tests/e2e/demo/stability/skill_trigger_stability.py \
        --count 3 --archive

Report goes to ``docs/testing/stability-reports/<timestamp>/``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, sync_playwright

# tests/e2e/demo/stability/<file> -> project root is four levels up
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "docs" / "testing" / "stability-reports"
ASSET_DIR = Path(__file__).resolve().parent / "assets"

SKILL_ID = "quarterly-margin-report"
SKILL_MD = ASSET_DIR / SKILL_ID / "SKILL.md"
FINGERPRINT = "MARGIN-V2"

DEMO_BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8900")
CONTROL_PLANE_URL = os.environ.get("AGENTSUPPORT_URL", "http://127.0.0.1:8000")
TENANT = os.environ.get("DEMO_TENANT", "t-demo")
CHANNEL = os.environ.get("PLAYWRIGHT_CHANNEL", "msedge")
ROUND_TIMEOUT = int(os.environ.get("DEMO_ROUND_TIMEOUT", "600"))
POLL_INTERVAL = 3.0

MATCHING_TASK = (
    "把本季度销售毛利算出来，按公司统一口径给我一份报告。"
    "数据：收入 1200 万，成本 800 万。"
)
CONTROL_TASK = "你好，简单介绍一下你自己就行。"


def _headers() -> dict[str, str]:
    return {"X-Tenant-Id": TENANT}


def ensure_skill(client: httpx.Client) -> None:
    """Upload the fixture skill once (idempotent overwrite)."""

    with SKILL_MD.open("rb") as handle:
        response = client.post(
            "/skills",
            data={"skill_id": SKILL_ID},
            files={"file": ("SKILL.md", handle, "text/markdown")},
        )
    if response.status_code not in {200, 201}:
        raise SystemExit(f"skill upload failed: {response.status_code} {response.text[:200]}")


def conversation_events(client: httpx.Client, conversation_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/conversations/{conversation_id}/events", params={"limit": 1000})
    if response.status_code != 200:
        return []
    return response.json()


def measure(events: list[dict[str, Any]], message: str) -> dict[str, Any]:
    reads = [
        (event.get("payload") or {}).get("arguments") or {}
        for event in events
        if event.get("type") == "tool.call"
        and (event.get("payload") or {}).get("name") == "bash"
        and SKILL_ID in str(((event.get("payload") or {}).get("arguments") or {}).get("command"))
        and "SKILL.md"
        in str(((event.get("payload") or {}).get("arguments") or {}).get("command"))
    ]
    gates = [
        event.get("payload") or {}
        for event in events
        if event.get("type") == "command.gate"
    ]
    # Adoption means the run followed the skill: the fingerprint has to show up in
    # what the agent produced — its final message or the artifact it wrote. A run
    # that reads and then deliberately does not follow is a legitimate outcome, so
    # this stays separate from ``triggered``.
    adopted_via = ""
    if FINGERPRINT in (message or ""):
        adopted_via = "message"
    else:
        for event in events:
            if event.get("type") != "tool.call":
                continue
            payload = event.get("payload") or {}
            if FINGERPRINT in json.dumps(payload.get("arguments") or {}, ensure_ascii=False):
                adopted_via = f"tool:{payload.get('name')}"
                break
    return {
        "triggered": bool(reads),
        "adopted": bool(adopted_via),
        "adopted_via": adopted_via,
        "failure": next(
            (
                str((event.get("payload") or {}).get("message") or "")
                for event in events
                if event.get("type") == "run.failed"
            ),
            "",
        ),
        "read_command": str(reads[0].get("command")) if reads else "",
        "read_reason": str(reads[0].get("reason")) if reads else "",
        "safe_auto_approved": sum(1 for gate in gates if gate.get("source") == "gate"),
        "human_approved": sum(1 for gate in gates if gate.get("source") == "human"),
    }


def demo_state(page: Page) -> dict[str, Any]:
    response = page.request.get(f"{DEMO_BASE_URL}/api/state")
    if not response.ok:
        raise RuntimeError(f"GET /api/state -> {response.status}")
    return response.json()


def wait_for_round(page: Page, previous_ids: set[str]) -> dict[str, Any]:
    """Wait for a round that was not in ``previous_ids`` to finish.

    Reloading the page starts a fresh session, so the round counter resets; the
    conversation id is the stable identity to look for.
    """

    deadline = time.time() + ROUND_TIMEOUT
    latest: dict[str, Any] | None = None
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            latest = demo_state(page)
            rounds = latest.get("rounds") or []
            if (
                rounds
                and rounds[-1].get("conversation_id") not in previous_ids
                and rounds[-1].get("state") in {"completed", "failed"}
            ):
                return rounds[-1]
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(
        "round did not complete in time: "
        + json.dumps(latest, ensure_ascii=False, default=str)[:400]
    )


def select_skill(page: Page) -> None:
    chip = page.locator(".skill-chip", has_text=SKILL_ID).first
    chip.wait_for(timeout=15000)
    classes = chip.get_attribute("class") or ""
    if "on" not in classes.split():
        chip.click()
        page.wait_for_function(
            """() => {
                const chip = [...document.querySelectorAll('.skill-chip')]
                    .find(c => c.textContent.includes('%s'));
                return !!chip && chip.classList.contains('on');
            }"""
            % SKILL_ID,
            timeout=10000,
        )


def run_once(page: Page, task: str) -> dict[str, Any]:
    page.goto(DEMO_BASE_URL)
    page.wait_for_selector("#skillChips .skill-chip", timeout=15000)
    select_skill(page)
    auto = page.locator("#autoApprove")
    if auto.count() and not auto.first.is_checked():
        auto.first.check()

    previous_ids = {
        str(item.get("conversation_id"))
        for item in (demo_state(page).get("rounds") or [])
    }
    started = time.time()
    page.locator("#input").fill(task)
    page.locator("#btnSend").click()
    summary = wait_for_round(page, previous_ids)
    elapsed = time.time() - started
    return {"summary": summary, "elapsed": elapsed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3, help="iterations per task kind")
    parser.add_argument("--archive", action="store_true", help="keep the report on disk")
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir = Path(args.report_root) / stamp
    report_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    with httpx.Client(base_url=CONTROL_PLANE_URL, headers=_headers(), timeout=60.0) as client:
        ensure_skill(client)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=CHANNEL)
            page = browser.new_page()
            try:
                for kind, task in (("matching", MATCHING_TASK), ("control", CONTROL_TASK)):
                    for index in range(1, args.count + 1):
                        outcome = run_once(page, task)
                        summary = outcome["summary"]
                        events = conversation_events(client, summary["conversation_id"])
                        verdict = measure(events, summary.get("message") or "")
                        record = {
                            "kind": kind,
                            "iteration": index,
                            "elapsed": round(outcome["elapsed"], 1),
                            "state": summary.get("state"),
                            "conversation_id": summary.get("conversation_id"),
                            "tool_calls": summary.get("tool_calls"),
                            **verdict,
                        }
                        records.append(record)
                        print(
                            f"[{kind} #{index}] triggered={record['triggered']} "
                            f"adopted={record['adopted']} {record['elapsed']}s",
                            flush=True,
                        )
            finally:
                browser.close()

        client.delete(f"/skills/{SKILL_ID}")

    matching = [r for r in records if r["kind"] == "matching"]
    control = [r for r in records if r["kind"] == "control"]
    triggered = sum(1 for r in matching if r["triggered"])
    adopted = sum(1 for r in matching if r["adopted"])
    false_triggers = sum(1 for r in control if r["triggered"])
    completed = [r for r in matching if r["state"] == "completed"]
    failures = [r for r in records if r["state"] != "completed"]
    latencies = [r["elapsed"] for r in matching] or [0.0]

    report = {
        "started_at": stamp,
        "skill_id": SKILL_ID,
        "demo_base_url": DEMO_BASE_URL,
        "iterations_per_kind": args.count,
        "trigger_rate": triggered / len(matching) if matching else 0.0,
        "trigger_rate_on_completed": (
            sum(1 for r in completed if r["triggered"]) / len(completed)
            if completed
            else 0.0
        ),
        "adopt_rate": adopted / len(matching) if matching else 0.0,
        "false_trigger_rate": false_triggers / len(control) if control else 0.0,
        "runs_completed": len(completed),
        "runs_failed": len(failures),
        "failures": [
            {"kind": r["kind"], "iteration": r["iteration"], "reason": r["failure"]}
            for r in failures
        ],
        "latency_seconds": {
            "min": min(latencies),
            "median": statistics.median(latencies),
            "max": max(latencies),
        },
        "records": records,
    }
    (report_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Skill 触发稳定性",
        "",
        f"- 技能：`{SKILL_ID}`；每类任务重复 {args.count} 次",
        f"- 触发率：{triggered}/{len(matching)} = {report['trigger_rate']:.0%}",
        f"- 触发率（仅完成轮）：{report['trigger_rate_on_completed']:.0%}"
        f"（完成 {len(completed)}/{len(matching)}）",
        f"- 采用率（报告含 `{FINGERPRINT}`）：{adopted}/{len(matching)} = {report['adopt_rate']:.0%}",
        f"- 误触发率（寒暄任务）：{false_triggers}/{len(control)} = {report['false_trigger_rate']:.0%}",
        f"- 时延：min {latencies and min(latencies)}s / median "
        f"{statistics.median(latencies)}s / max {max(latencies)}s",
        "",
        "| # | 类型 | 触发 | 采用（来源） | 时延(s) | 读取命令 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, record in enumerate(records, start=1):
        lines.append(
            f"| {index} | {record['kind']} | {record['triggered']} | "
            f"{record['adopted']} ({record['adopted_via'] or '-'}) | {record['elapsed']} | "
            f"`{record['read_command'][:80]}` |"
        )
    if failures:
        lines += ["", "## 失败轮", ""]
        for record in failures:
            lines.append(
                f"- {record['kind']} #{record['iteration']}：{record['failure'] or '(无原因)'}"
            )
    lines += ["", f"- 完成/失败：{len(completed)}/{len(failures)}", ""]
    (report_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nreport: {report_dir}")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

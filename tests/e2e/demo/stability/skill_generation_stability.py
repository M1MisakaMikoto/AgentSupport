"""Skill generation stability runner.

Repeats the fixed multi-round dialogue + "总结成 Skill" flow through the demo
stack API (no browser) and reports success rate, retry distribution, per-round
and generation latency, failure causes and output isolation.

Usage (demo stack must be running, see ``demo/start-demo.ps1``)::

    .venv/Scripts/python.exe tests/e2e/demo/stability/skill_generation_stability.py \
        --count 3 --archive

Report is written to ``docs/testing/stability-reports/<timestamp>/``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "docs" / "testing" / "stability-reports"
WORKSPACE_DATA = PROJECT_ROOT / "workspace-data"
RUNNER_TRAJECTORIES = Path(r"D:\workspace\.agentsupport\trajectories")

ROUND_TIMEOUT = 900
SUMMARY_TIMEOUT = 1500
POLL_INTERVAL = 3.0

ROUND_TASKS = [
    (
        "你是后台运营专员。在 workspace 用 excel_edit_tool 创建 tenant-usage-weekly.xlsx："
        "Sheet 名 W2026-35，表头为 租户/会话数/输入Token/输出Token/成功会话，写入 3 行数据"
        "（t-core: 120, 840000, 360000, 114；t-demo: 45, 210000, 98000, 44；t-plat: 78, 430000, 176000, 75）。"
        "创建完成后用 read 回读核对表头与数据，task_done 并回复文件路径。"
        "禁止 bash；文件操作只能使用文档工具。"
    ),
    (
        "你是后台审核员。用 word_edit_tool 创建 skill-review.docx：标题『Skill 草稿审核清单』，"
        "2 条记录段落（首条『记录：n-plus-one-query-remediation | 状态：待审核 | 提交人：alice』；"
        "次条『记录：slash-command-protocol | 状态：已通过 | 提交人：bob』）。"
        "然后把首条状态改为『已通过』，追加一段『审核意见：审核通过，已安排入库。』，最后 read 回读核对。"
        "task_done 并回复关键改动。禁止 bash；文件操作只能使用文档工具。"
    ),
    (
        "你是后台质量负责人。用 pdf_tool 创建 eval-run-summary.pdf：标题『评估运行摘要』，"
        "3 个要点（数据集：eval-dataset-orders；用例数：40；通过率：95.0%），"
        "创建后用 read 回读核对中文内容可提取。task_done 并回复文件路径。"
        "禁止 bash；文件操作只能使用文档工具。"
    ),
    (
        "你是导师。请把前面三轮文档操作经验整理成 3 条可复用规则"
        "（工具选择、回读核对、边界注意），直接回复即可，task_done 结束。禁止 bash。"
    ),
]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct / 100
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _wait_state(
    client: httpx.Client,
    base_url: str,
    predicate,
    *,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            resp = client.get(f"{base_url}/api/state")
            if resp.status_code == 200:
                last = resp.json()
                if predicate(last):
                    return last
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"timed out waiting for demo state: {json.dumps(last, ensure_ascii=False)[:500]}")


def _archive_materials(run_dir: Path, started_at: float, ended_at: float) -> None:
    """Copy the latest generation events and runner trajectories into the run dir."""

    traj_dir = run_dir / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    if RUNNER_TRAJECTORIES.is_dir():
        for item in sorted(RUNNER_TRAJECTORIES.glob("*.json")):
            if started_at - 10 <= item.stat().st_mtime <= ended_at + 10:
                shutil.copy2(item, traj_dir / item.name)

    if WORKSPACE_DATA.is_dir():
        candidates = sorted(
            WORKSPACE_DATA.glob("**/.agentsupport/skill-generation-*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
        )
        if candidates:
            shutil.copy2(candidates[-1], run_dir / "events.jsonl")


def run_once(
    client: httpx.Client,
    base_url: str,
    *,
    run_number: int,
    run_dir: Path,
    archive: bool,
) -> dict[str, Any]:
    started_at = time.time()
    record: dict[str, Any] = {
        "run": run_number,
        "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
        "rounds": [],
        "summary": None,
        "status": "failed",
        "error": None,
    }

    resp = client.post(f"{base_url}/api/start", json={"auto_approve": True, "skills": []})
    if resp.status_code != 200:
        record["error"] = f"start failed: {resp.status_code} {resp.text[:200]}"
        return record

    for index, task in enumerate(ROUND_TASKS):
        round_start = time.time()
        try:
            before = len(client.get(f"{base_url}/api/state").json()["rounds"])
            resp = client.post(f"{base_url}/api/round", json={"task": task})
            if resp.status_code != 200:
                raise RuntimeError(f"round {index + 1} api error: {resp.text[:200]}")
            state = _wait_state(
                client,
                base_url,
                lambda current, n=before: len(current["rounds"]) > n
                and current["rounds"][-1].get("state") == "completed",
                timeout=ROUND_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - record and stop this run
            record["error"] = f"round {index + 1} failed: {exc}"
            record["rounds"].append(
                {
                    "index": index + 1,
                    "state": "failed",
                    "seconds": round(time.time() - round_start, 1),
                    "error": str(exc),
                }
            )
            return record
        round_summary = state["rounds"][-1]
        record["rounds"].append(
            {
                "index": index + 1,
                "state": round_summary.get("state"),
                "seconds": round(time.time() - round_start, 1),
                "tool_calls": round_summary.get("tool_calls") or [],
                "usage": round_summary.get("usage"),
            }
        )

    generation_start = time.time()
    try:
        resp = client.post(f"{base_url}/api/summarize")
        if resp.status_code != 200:
            raise RuntimeError(f"summarize api error: {resp.text[:200]}")
        state = _wait_state(
            client,
            base_url,
            lambda current: bool(current.get("summary"))
            and current["summary"].get("status") in ("completed", "failed"),
            timeout=SUMMARY_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - record and stop this run
        record["error"] = f"generation failed: {exc}"
        return record

    summary = state.get("summary") or {}
    record["summary"] = {
        "status": summary.get("status"),
        "attempt": summary.get("attempt"),
        "skill_id": summary.get("skill_id"),
        "file_path": summary.get("file_path"),
        "error": summary.get("error"),
        "seconds": round(time.time() - generation_start, 1),
        "content_head": (summary.get("content") or "")[:120],
    }
    record["status"] = "completed" if summary.get("status") == "completed" else "failed"
    record["error"] = record["error"] or summary.get("error")

    if archive:
        _archive_materials(run_dir, started_at, time.time())
    return record


def _summarize(records: list[dict[str, Any]], report_dir: Path) -> dict[str, Any]:
    total = len(records)
    succeeded = [r for r in records if r["status"] == "completed"]
    attempts = [r.get("summary") or {} for r in records]
    attempts_hit = [s.get("attempt") for s in attempts if s.get("status") == "completed"]
    round_seconds = [
        r["seconds"]
        for record in records
        for r in record["rounds"]
        if isinstance(r.get("seconds"), (int, float))
    ]
    gen_seconds = [
        s.get("seconds")
        for s in attempts
        if isinstance(s.get("seconds"), (int, float))
    ]
    skill_ids = [
        s.get("skill_id")
        for s in attempts
        if s.get("status") == "completed" and s.get("skill_id")
    ]
    frontmatter_ok = sum(
        1
        for r in succeeded
        if (r.get("summary") or {}).get("content_head", "").lstrip().startswith("---")
    )

    def _stats(values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        return {
            "min": round(min(values), 1),
            "median": round(statistics.median(values), 1),
            "p95": round(_percentile(values, 95), 1),
            "max": round(max(values), 1),
            "n": len(values),
        }

    summary = {
        "total": total,
        "succeeded": len(succeeded),
        "failed": total - len(succeeded),
        "success_rate": round(len(succeeded) / total * 100, 1) if total else 0.0,
        "attempt_hits": {
            "once": attempts_hit.count(1),
            "retry": sum(1 for a in attempts_hit if a and a > 1),
            "unknown": sum(1 for a in attempts_hit if not a),
        },
        "round_seconds": _stats(round_seconds),
        "generation_seconds": _stats(gen_seconds),
        "unique_skill_ids": len(set(skill_ids)),
        "frontmatter_ok": frontmatter_ok,
        "failures": [
            {"run": r["run"], "error": r["error"]} for r in records if r["status"] == "failed"
        ],
    }
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _write_markdown(report_dir: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    lines = [
        "# Skill 生成稳定性测试报告",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 任务组：固定 4 轮后台文档操作（excel/word/pdf + 总结）",
        f"- 运行次数：{summary['total']}",
        "",
        "## 汇总",
        "",
        f"- 成功率：{summary['success_rate']}%（{summary['succeeded']}/{summary['total']}）",
        f"- 首试成功：{summary['attempt_hits']['once']} / 重试后成功：{summary['attempt_hits']['retry']}",
        f"- 唯一 skill_id：{summary['unique_skill_ids']} / frontmatter 合法：{summary['frontmatter_ok']}",
        "",
        "## 耗时（秒）",
        "",
        "| 阶段 | min | median | p95 | max | n |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in ("round_seconds", "generation_seconds"):
        stats = summary.get(name) or {}
        lines.append(
            f"| {name} | {stats.get('min', '-')} | {stats.get('median', '-')} | "
            f"{stats.get('p95', '-')} | {stats.get('max', '-')} | {stats.get('n', '-')} |"
        )
    lines += [
        "",
        "## 失败明细",
        "",
    ]
    failures = summary.get("failures") or []
    if failures:
        for item in failures:
            lines.append(f"- run {item['run']}: {item['error']}")
    else:
        lines.append("无")
    lines += [
        "",
        "## 每次运行明细（摘要）",
        "",
        "| run | 状态 | attempt | skill_id | 轮数 | 总耗时(秒) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        summary_info = record.get("summary") or {}
        total_seconds = round(
            sum(r.get("seconds") or 0 for r in record["rounds"]) + (summary_info.get("seconds") or 0),
            1,
        )
        lines.append(
            f"| {record['run']} | {record['status']} | {summary_info.get('attempt', '-')} | "
            f"{summary_info.get('skill_id', '-')} | {len(record['rounds'])} | {total_seconds} |"
        )
    (report_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill generation stability runner")
    parser.add_argument("--count", type=int, default=3, help="number of full runs")
    parser.add_argument("--base-url", default="http://127.0.0.1:8900")
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--archive", action="store_true", help="archive events/trajectories per run")
    args = parser.parse_args()

    report_dir = args.report_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=60) as client:
        for run_number in range(1, args.count + 1):
            run_dir = report_dir / f"run-{run_number}"
            run_dir.mkdir(exist_ok=True)
            print(f"[stability] run {run_number}/{args.count} starting ...", flush=True)
            record = run_once(
                client,
                args.base_url,
                run_number=run_number,
                run_dir=run_dir,
                archive=args.archive,
            )
            records.append(record)
            print(
                f"[stability] run {run_number} -> {record['status']} "
                f"(rounds={len(record['rounds'])}, "
                f"summary={record.get('summary', {}).get('status')})",
                flush=True,
            )

    summary = _summarize(records, report_dir)
    _write_markdown(report_dir, summary, records)
    (report_dir / "runs.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[stability] report written to {report_dir}")


if __name__ == "__main__":
    main()

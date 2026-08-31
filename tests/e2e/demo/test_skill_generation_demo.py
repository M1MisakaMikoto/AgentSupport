"""Playwright E2E: multi-round dialogue -> generate skill.

Covers ``docs/testing/demo-skill-generation-case.md``: four backend-document
rounds driven through the demo UI, then "总结成 Skill" publishes a SKILL.md
which is asserted both on the page and on disk.

Requires the demo stack (``demo/start-demo.ps1``) and Chromium
(``python -m playwright install chromium``).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest

DEMO_BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8900")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ROUND_TIMEOUT = int(os.environ.get("DEMO_ROUND_TIMEOUT", "900"))
SUMMARY_TIMEOUT = int(os.environ.get("DEMO_SUMMARY_TIMEOUT", "1500"))
RUN_ACTIVATION = os.environ.get("RUN_ACTIVATION") == "1"

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

EXPECTED_ROUND_TOOLS = [
    {"excel_edit_tool"},
    {"word_edit_tool"},
    {"pdf_tool"},
    set(),
]


def _state(page) -> dict:
    resp = page.request.get(f"{DEMO_BASE_URL}/api/state")
    assert resp.ok, f"GET /api/state -> {resp.status}: {resp.text[:200]}"
    return resp.json()


def _wait_state(page, predicate, *, timeout: int, interval: float = 3.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            last = _state(page)
            if predicate(last):
                return last
        time.sleep(interval)
    pytest.fail(
        "timed out waiting for demo state: "
        + json.dumps(last, ensure_ascii=False, default=str)[:800]
    )


@pytest.fixture(scope="module")
def demo_page(playwright):
    """Launch a system browser via Playwright channel (no bundled download).

    Defaults to the pre-installed Microsoft Edge; override with
    ``PLAYWRIGHT_CHANNEL=chromium`` when the bundled Chromium is available.
    """

    channel = os.environ.get("PLAYWRIGHT_CHANNEL", "msedge")
    browser = playwright.chromium.launch(channel=channel)
    page = browser.new_page()
    yield page
    browser.close()


def test_multi_round_dialogue_generates_skill(demo_page):
    page = demo_page
    page.goto(DEMO_BASE_URL)
    page.wait_for_selector("#connText", timeout=15000)
    page.wait_for_function(
        "document.querySelector('#connText').textContent.includes('已连接')"
        " || document.querySelector('#connText').textContent.includes('会话')",
        timeout=15000,
    )

    for index, task in enumerate(ROUND_TASKS):
        before = len(_state(page)["rounds"])
        page.locator("#input").fill(task)
        page.locator("#btnSend").click()

        state = _wait_state(
            page,
            lambda current, n=before: len(current["rounds"]) > n
            and current["rounds"][-1].get("state") == "completed",
            timeout=ROUND_TIMEOUT,
        )
        round_summary = state["rounds"][-1]
        calls = set(round_summary.get("tool_calls") or [])
        expected = EXPECTED_ROUND_TOOLS[index]
        assert expected <= calls, f"round {index + 1} tool calls: {sorted(calls)}"
        page.wait_for_selector(f"text=第 {index + 1} 轮完成", timeout=15000)

    page.locator("#btnSum").click()
    state = _wait_state(
        page,
        lambda current: bool(current.get("summary"))
        and current["summary"].get("status") == "completed",
        timeout=SUMMARY_TIMEOUT,
    )
    summary = state["summary"]
    skill_id = summary.get("skill_id")
    file_path = summary.get("file_path")
    content = summary.get("content") or ""
    assert skill_id, "no skill_id in summary"
    assert file_path, "no file_path in summary"
    assert content.strip(), "empty SKILL.md content"
    assert "name:" in content and "description:" in content, "frontmatter missing"

    skill_file = PROJECT_ROOT / file_path
    assert skill_file.is_file(), f"published skill file missing: {skill_file}"

    page.wait_for_selector(".summary-panel", timeout=30000)
    panel_text = page.locator(".summary-panel").inner_text()
    assert "总结完成，已入库" in panel_text
    assert skill_id in panel_text

    if RUN_ACTIVATION:
        resp = page.request.post(f"{DEMO_BASE_URL}/api/activate", json={"skill_id": skill_id})
        assert resp.ok, resp.text[:300]
        state = _wait_state(
            page,
            lambda current: current.get("activation") is not None,
            timeout=2400,
        )
        activation = state["activation"] or []
        assert activation, "no activation results"
        assert all(item.get("state") == "completed" for item in activation), activation

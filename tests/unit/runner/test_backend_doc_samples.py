"""Back-office business document samples: seed + tool execution + assertions.

Each sample mirrors ``demo/samples/README.md``: seed the document, execute the
agent's key tool steps against a copy, then read the result back and assert the
business expectation. Office-COM conversion cases are skipped unless
``AGENTSUPPORT_RUN_COM_TESTS=1`` (interactive session).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from demo.samples.seed_backend_samples import (
    seed_audit_events,
    seed_eval_run,
    seed_monthly_ops_report,
    seed_skill_review,
    seed_tenant_usage_weekly,
)
from session_runner.tools.document_tools import (
    DocumentConvertTool,
    ExcelEditTool,
    PdfTool,
    WordEditTool,
)


async def test_sample1_tenant_usage_weekly_total_row(tmp_path):
    """样例一：用量周报补合计行，四列求和与成功率正确。"""

    path = tmp_path / "tenant-usage-weekly.xlsx"
    seed_tenant_usage_weekly(path)
    tool = ExcelEditTool()

    total = {"sessions": 336, "input": 2020000, "output": 850000, "success": 323}
    result = await tool.execute(
        {
            "command": "append_rows",
            "path": str(path),
            "rows": [["合计", total["sessions"], total["input"], total["output"], total["success"], 96.1]],
        }
    )
    assert result.error_code == 0

    result = await tool.execute({"command": "read", "path": str(path), "sheet_name": "W2026-35"})
    payload = json.loads(result.output)
    rows = payload["rows"]
    assert rows[0] == ["租户", "会话数", "输入Token", "输出Token", "成功会话", "成功率%"]
    assert rows[-1][0] == "合计"
    assert rows[-1][1] == total["sessions"]
    assert rows[-1][2] == total["input"]
    assert rows[-1][3] == total["output"]
    assert rows[-1][4] == total["success"]
    assert rows[-1][5] == pytest.approx(total["success"] / total["sessions"] * 100, abs=0.1)


async def test_sample2_skill_review_update_and_opinion(tmp_path):
    """样例二：技能审核清单改状态并追加审核意见。"""

    path = tmp_path / "skill-review.docx"
    seed_skill_review(path)
    tool = WordEditTool()

    result = await tool.execute(
        {
            "command": "replace",
            "path": str(path),
            "old_text": "记录：n-plus-one-query-remediation | 描述：批量查询修复 N+1 | 状态：待审核 | 提交人：alice",
            "new_text": "记录：n-plus-one-query-remediation | 描述：批量查询修复 N+1 | 状态：已通过 | 提交人：alice",
        }
    )
    assert result.error_code == 0
    result = await tool.execute(
        {
            "command": "append",
            "path": str(path),
            "paragraphs": ["审核意见：审核通过，已安排入库。"],
        }
    )
    assert result.error_code == 0

    result = await tool.execute({"command": "read", "path": str(path)})
    assert "状态：已通过" in result.output
    assert "审核通过，已安排入库。" in result.output
    assert "状态：待审核 | 提交人：carol" in result.output


async def test_sample3_eval_runs_merge_and_watermark(tmp_path):
    """样例三：评估摘要合并并加水印。"""

    first = tmp_path / "eval-run-a.pdf"
    second = tmp_path / "eval-run-b.pdf"
    merged = tmp_path / "eval-runs-merged.pdf"
    watermarked = tmp_path / "eval-runs-draft.pdf"
    seed_eval_run(first, "评估运行摘要 A", "eval-dataset-orders", 40, 38, ["case-07", "case-19"])
    seed_eval_run(second, "评估运行摘要 B", "eval-dataset-skills", 24, 24, [])
    tool = PdfTool()

    result = await tool.execute(
        {
            "command": "merge",
            "path": str(first),
            "paths": [str(first), str(second)],
            "output_path": str(merged),
        }
    )
    assert result.error_code == 0
    result = await tool.execute(
        {
            "command": "watermark",
            "path": str(merged),
            "text": "DRAFT",
            "output_path": str(watermarked),
        }
    )
    assert result.error_code == 0

    result = await tool.execute({"command": "read", "path": str(watermarked)})
    assert "评估运行摘要 A" in result.output
    assert "评估运行摘要 B" in result.output
    assert "DRAFT" in result.output


async def test_sample4_audit_events_seed_and_readback(tmp_path):
    """样例四：审计对账表种子与读回（PDF 转换在交互会话验证）。"""

    path = tmp_path / "audit-events.xlsx"
    seed_audit_events(path)
    tool = ExcelEditTool()

    result = await tool.execute({"command": "read", "path": str(path), "sheet_name": "audit"})
    payload = json.loads(result.output)
    rows = payload["rows"]
    assert len(rows) == 9
    assert rows[0] == ["时间", "操作人", "操作", "资源", "结果"]
    assert rows[-1][4] == "denied"


async def test_sample5_monthly_ops_report_append_and_readback(tmp_path):
    """样例五：月度运营报告追加结论（docx→pdf 转换在交互会话验证）。"""

    path = tmp_path / "monthly-ops-report.docx"
    seed_monthly_ops_report(path)
    tool = WordEditTool()

    result = await tool.execute(
        {
            "command": "append",
            "path": str(path),
            "paragraphs": ["结论：本月各项指标达标，下月重点提升技能审核时效。"],
        }
    )
    assert result.error_code == 0
    result = await tool.execute({"command": "read", "path": str(path)})
    assert "结论：本月各项指标达标，下月重点提升技能审核时效。" in result.output
    assert "会话量：本月累计 336 个会话" in result.output


@pytest.mark.skipif(
    os.environ.get("AGENTSUPPORT_RUN_COM_TESTS") != "1",
    reason="Office COM conversion is validated during the demo run",
)
async def test_sample5_convert_monthly_report_to_pdf(tmp_path):
    """样例五扩展：docx 转 PDF（依赖交互式 Office 会话）。"""

    source = tmp_path / "monthly-ops-report.docx"
    target = tmp_path / "monthly-ops-report.pdf"
    seed_monthly_ops_report(source)

    tool = DocumentConvertTool()
    result = await tool.execute(
        {"command": "docx_to_pdf", "input_path": str(source), "output_path": str(target)}
    )
    assert result.error_code == 0
    assert target.is_file()

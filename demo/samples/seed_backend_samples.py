"""Generate backend-business document sample files.

Outputs the seed documents referenced by ``demo/samples/README.md``. The same
seed functions are reused by the automated sample tests so the demo and the
test suite stay in sync.

Usage::

    .venv/Scripts/python.exe demo/samples/seed_backend_samples.py [--out DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


def seed_tenant_usage_weekly(path: Path) -> None:
    """Weekly tenant usage report: sessions, tokens and success rate."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "W2026-35"
    sheet.append(["租户", "会话数", "输入Token", "输出Token", "成功会话", "成功率%"])
    for row in (
        ("t-core", 120, 840000, 360000, 114, 95.0),
        ("t-demo", 45, 210000, 98000, 44, 97.8),
        ("t-plat", 78, 430000, 176000, 75, 96.2),
        ("t-ops", 32, 150000, 64000, 30, 93.8),
        ("t-sec", 61, 390000, 152000, 60, 98.4),
    ):
        sheet.append(row)
    workbook.save(path)


def seed_skill_review(path: Path) -> None:
    """Skill draft review checklist: name, description, status, submitter."""

    document = Document()
    document.add_paragraph("Skill 草稿审核清单", style="Title")
    document.add_paragraph(
        "记录：n-plus-one-query-remediation | 描述：批量查询修复 N+1 | 状态：待审核 | 提交人：alice"
    )
    document.add_paragraph(
        "记录：custom-command-trigger-reply | 描述：自定义命令触发回复 | 状态：已通过 | 提交人：bob"
    )
    document.add_paragraph(
        "记录：slash-command-protocol | 描述：斜杠命令协议 | 状态：待审核 | 提交人：carol"
    )
    document.save(path)


def seed_eval_run(path: Path, title: str, dataset: str, cases: int, passed: int, failed: list[str]) -> None:
    """Eval run summary PDF: dataset, cases, pass rate and failed cases."""

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    styles["Title"].fontName = "STSong-Light"
    styles["BodyText"].fontName = "STSong-Light"
    story: list[Paragraph | Spacer] = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
    lines = [
        f"数据集：{dataset}",
        f"用例数：{cases}",
        f"通过率：{passed / cases * 100:.1f}%",
        "失败用例：" + ("、".join(failed) if failed else "无"),
    ]
    for line in lines:
        story.append(Paragraph(line, styles["BodyText"]))
        story.append(Spacer(1, 6))
    SimpleDocTemplate(str(path), pagesize=A4).build(story)


def seed_audit_events(path: Path) -> None:
    """Back-office audit event log: time, operator, action, resource, result."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "audit"
    sheet.append(["时间", "操作人", "操作", "资源", "结果"])
    for row in (
        ("2026-08-30 09:12", "alice", "create_skill_draft", "skills/t-demo/n-plus-one", "ok"),
        ("2026-08-30 09:31", "bob", "review_skill_draft", "skills/t-demo/n-plus-one", "approved"),
        ("2026-08-30 10:04", "carol", "create_session", "sessions/7f3a", "ok"),
        ("2026-08-30 10:22", "alice", "run_eval", "evals/eval-run-a", "ok"),
        ("2026-08-30 11:05", "bob", "create_workspace", "workspaces/demo-x", "ok"),
        ("2026-08-30 11:47", "carol", "upload_skill", "skills/t-demo/slash-command", "ok"),
        ("2026-08-30 14:02", "alice", "approve_skill_draft", "skills/t-demo/custom-command", "approved"),
        ("2026-08-30 14:33", "bob", "delete_session", "sessions/91bc", "denied"),
    ):
        sheet.append(row)
    workbook.save(path)


def seed_monthly_ops_report(path: Path) -> None:
    """Monthly back-office operations report."""

    document = Document()
    document.add_paragraph("2026 年 8 月后台运营报告", style="Title")
    for text in (
        "会话量：本月累计 336 个会话，环比增长 12%。",
        "技能上线：新增 3 个 skill，累计 15 个；审核通过率 80%。",
        "评估通过率：本月 4 次评估运行，平均通过率 96.2%。",
        "下月计划：提升技能审核时效，推进审计对账自动化。",
    ):
        document.add_paragraph(text)
    document.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate backend-business document samples")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "seed")
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    seed_tenant_usage_weekly(out / "tenant-usage-weekly.xlsx")
    seed_skill_review(out / "skill-review.docx")
    seed_eval_run(
        out / "eval-run-a.pdf",
        "评估运行摘要 A",
        "eval-dataset-orders",
        40,
        38,
        ["case-07", "case-19"],
    )
    seed_eval_run(
        out / "eval-run-b.pdf",
        "评估运行摘要 B",
        "eval-dataset-skills",
        24,
        24,
        [],
    )
    seed_audit_events(out / "audit-events.xlsx")
    seed_monthly_ops_report(out / "monthly-ops-report.docx")
    print(f"Seeds written to {out}")


if __name__ == "__main__":
    main()

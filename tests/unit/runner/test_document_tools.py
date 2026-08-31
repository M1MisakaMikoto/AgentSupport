"""Document tool unit tests: Word/Excel/PDF roundtrips and error paths."""

from __future__ import annotations

import json
import os

import pytest

from session_runner.tools.document_tools import (
    DocumentConvertTool,
    ExcelEditTool,
    PdfTool,
    ToolError,
    WordEditTool,
)


async def test_word_create_read_append_replace(tmp_path):
    tool = WordEditTool()
    path = tmp_path / "report.docx"

    result = await tool.execute(
        {
            "command": "create",
            "path": str(path),
            "title": "季度销售报告",
            "paragraphs": ["第一段", "第二段"],
        }
    )
    assert result.error_code == 0
    assert path.is_file()

    with pytest.raises(ToolError):
        await tool.execute({"command": "create", "path": str(path)})

    result = await tool.execute({"command": "read", "path": str(path)})
    assert "季度销售报告" in result.output
    assert "第一段" in result.output

    result = await tool.execute(
        {"command": "append", "path": str(path), "paragraphs": ["第三段"]}
    )
    assert result.error_code == 0

    result = await tool.execute(
        {
            "command": "replace",
            "path": str(path),
            "old_text": "第一段",
            "new_text": "第一段（改）",
        }
    )
    assert result.error_code == 0

    result = await tool.execute({"command": "read", "path": str(path)})
    assert "第一段（改）" in result.output
    assert "第三段" in result.output

    with pytest.raises(ToolError):
        await tool.execute(
            {"command": "replace", "path": str(path), "old_text": "不存在", "new_text": "x"}
        )

    with pytest.raises(ToolError):
        await tool.execute({"command": "read", "path": str(tmp_path / "a.txt")})


async def test_excel_create_read_set_update_append(tmp_path):
    tool = ExcelEditTool()
    path = tmp_path / "data.xlsx"
    rows = [["Product", "Qty", "Price"], ["A", 3, 100]]

    result = await tool.execute(
        {"command": "create", "path": str(path), "sheet_name": "Sales", "rows": rows}
    )
    assert result.error_code == 0
    assert path.is_file()

    with pytest.raises(ToolError):
        await tool.execute({"command": "create", "path": str(path), "rows": rows})

    result = await tool.execute(
        {"command": "read", "path": str(path), "sheet_name": "Sales"}
    )
    payload = json.loads(result.output)
    assert payload["sheet"] == "Sales"
    assert payload["rows"] == rows

    result = await tool.execute(
        {"command": "set_cell", "path": str(path), "cell": "C1", "value": "Amount"}
    )
    assert result.error_code == 0
    result = await tool.execute(
        {"command": "update", "path": str(path), "cells": {"B2": 5}}
    )
    assert result.error_code == 0
    result = await tool.execute(
        {"command": "append_rows", "path": str(path), "rows": [["B", 2, 200]]}
    )
    assert result.error_code == 0

    result = await tool.execute({"command": "read", "path": str(path)})
    payload = json.loads(result.output)
    assert payload["rows"][0][2] == "Amount"
    assert payload["rows"][1][1] == 5
    assert payload["rows"][-1] == ["B", 2, 200]

    with pytest.raises(ToolError):
        await tool.execute({"command": "read", "path": str(tmp_path / "a.csv")})


async def test_pdf_create_read_merge_split_watermark(tmp_path):
    tool = PdfTool()
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    merged = tmp_path / "merged.pdf"
    watermarked = tmp_path / "wm.pdf"
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = await tool.execute(
        {"command": "create", "path": str(first), "title": "摘要", "paragraphs": ["alpha", "beta"]}
    )
    assert result.error_code == 0
    assert first.is_file()
    result = await tool.execute(
        {"command": "create", "path": str(second), "paragraphs": ["gamma"]}
    )
    assert result.error_code == 0

    result = await tool.execute({"command": "read", "path": str(first)})
    assert "alpha" in result.output
    assert "beta" in result.output

    result = await tool.execute(
        {
            "command": "merge",
            "path": str(first),
            "paths": [str(first), str(second)],
            "output_path": str(merged),
        }
    )
    assert result.error_code == 0
    assert merged.is_file()
    result = await tool.execute({"command": "read", "path": str(merged)})
    assert "alpha" in result.output and "gamma" in result.output

    result = await tool.execute(
        {"command": "split", "path": str(merged), "pages": [1, 2], "output_path": str(out_dir)}
    )
    assert result.error_code == 0
    assert (out_dir / "page-0001.pdf").is_file()
    assert (out_dir / "page-0002.pdf").is_file()

    result = await tool.execute(
        {
            "command": "watermark",
            "path": str(merged),
            "text": "DRAFT",
            "output_path": str(watermarked),
        }
    )
    assert result.error_code == 0
    assert watermarked.is_file()
    result = await tool.execute({"command": "read", "path": str(watermarked)})
    assert "DRAFT" in result.output

    with pytest.raises(ToolError):
        await tool.execute({"command": "read", "path": str(tmp_path / "a.docx")})


async def test_convert_error_paths(tmp_path):
    tool = DocumentConvertTool()

    with pytest.raises(ToolError):
        await tool.execute(
            {
                "command": "docx_to_pdf",
                "input_path": str(tmp_path / "missing.docx"),
                "output_path": str(tmp_path / "out.pdf"),
            }
        )
    with pytest.raises(ToolError):
        await tool.execute(
            {
                "command": "xlsx_to_pdf",
                "input_path": str(tmp_path / "data.xlsx"),
                "output_path": str(tmp_path / "out.txt"),
            }
        )
    with pytest.raises(ToolError):
        await tool.execute(
            {
                "command": "pdf_to_pdf",
                "input_path": str(tmp_path / "data.xlsx"),
                "output_path": str(tmp_path / "out.pdf"),
            }
        )


@pytest.mark.skipif(
    os.environ.get("AGENTSUPPORT_RUN_COM_TESTS") != "1",
    reason="Office COM conversion is validated during the demo run",
)
async def test_convert_docx_to_pdf_via_office(tmp_path):
    from docx import Document

    source = tmp_path / "report.docx"
    target = tmp_path / "report.pdf"
    document = Document()
    document.add_paragraph("COM conversion test")
    document.save(str(source))

    tool = DocumentConvertTool()
    result = await tool.execute(
        {"command": "docx_to_pdf", "input_path": str(source), "output_path": str(target)}
    )
    assert result.error_code == 0
    assert target.is_file()

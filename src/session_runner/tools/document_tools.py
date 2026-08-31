"""Word/Excel/PDF document tools for the Trae agent.

Tools run in-process inside the session runner and reuse the standard
``trae_agent.tools.base.Tool`` contract, so they flow through the same
approval / sandbox / LLM-schema pipeline as the built-in edit tools.

Libraries (python-docx, openpyxl, PyMuPDF, reportlab) and the Office COM
bridge (pywin32) are imported lazily so importing this module stays cheap.
"""

from __future__ import annotations

import contextlib
import importlib
import json
from pathlib import Path
from typing import Any

try:
    from trae_agent.tools.base import (
        Tool,
        ToolCallArguments,
        ToolError,
        ToolExecResult,
        ToolParameter,
    )
except ImportError:
    from session_runner.adapters.trae import _ensure_vendored_trae_path

    _ensure_vendored_trae_path()
    from trae_agent.tools.base import (
        Tool,
        ToolCallArguments,
        ToolError,
        ToolExecResult,
        ToolParameter,
    )

MAX_OUTPUT_CHARS = 20000


def _maybe_truncate(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "\n<... output clipped ...>"
    return text


def _resolve_path(value: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except OSError as exc:
        raise ToolError(f"invalid path {value!r}: {exc}") from exc


def _coerce_scalar(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _as_paragraphs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ToolError("'paragraphs' must be a list of strings")


class WordEditTool(Tool):
    """Create, read, append to and replace text in Word .docx documents."""

    def __init__(self, model_provider: str | None = None) -> None:
        super().__init__(model_provider)

    def get_name(self) -> str:
        return "word_edit_tool"

    def get_description(self) -> str:
        return """Tool for creating, reading and editing Word .docx documents
* Commands: create, read, append, replace
* `create` writes a new .docx (title + paragraphs); fails if the file already exists
* `read` extracts paragraph text and table contents with line numbers
* `append` adds paragraphs at the end of an existing .docx
* `replace` replaces every paragraph whose text equals `old_text` with `new_text`
* Paths are Windows absolute paths (e.g. D:\\workspace\\report.docx) inside the workspace
"""

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The operation to perform.",
                required=True,
                enum=["create", "read", "append", "replace"],
            ),
            ToolParameter(
                name="path",
                type="string",
                description="Absolute path to the .docx file, e.g. D:\\workspace\\report.docx.",
                required=True,
            ),
            ToolParameter(
                name="title",
                type="string",
                description="Optional document title used by the `create` command.",
                required=False,
            ),
            ToolParameter(
                name="paragraphs",
                type="array",
                description="List of paragraph strings; required for `create` and `append`.",
                required=False,
                items={"type": "string"},
            ),
            ToolParameter(
                name="old_text",
                type="string",
                description="Exact paragraph text to find; required for `replace`.",
                required=False,
            ),
            ToolParameter(
                name="new_text",
                type="string",
                description="Replacement text; required for `replace`.",
                required=False,
            ),
        ]

    async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
        from docx import Document

        command = arguments.get("command")
        if command not in self.get_parameters()[0].enum:
            raise ToolError(f"unsupported command: {command!r}")
        path = _resolve_path(arguments.get("path", ""))
        if path.suffix.lower() != ".docx":
            raise ToolError("word_edit_tool only supports .docx files")

        if command == "create":
            if path.exists():
                raise ToolError(f"file already exists, use read/append/replace: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            doc = Document()
            title = arguments.get("title")
            if isinstance(title, str) and title.strip():
                doc.add_paragraph(title.strip(), style="Title")
            for paragraph in _as_paragraphs(arguments.get("paragraphs")):
                doc.add_paragraph(paragraph)
            doc.save(str(path))
            return ToolExecResult(output=f"Created Word document: {path}")

        if not path.is_file():
            raise ToolError(f"file does not exist: {path}")
        doc = Document(str(path))

        if command == "read":
            lines = [f"Document: {path}", "Paragraphs:"]
            for index, paragraph in enumerate(doc.paragraphs, start=1):
                if paragraph.text:
                    lines.append(f"{index}: {paragraph.text}")
            if doc.tables:
                lines.append(f"Tables ({len(doc.tables)}):")
                for table_index, table in enumerate(doc.tables, start=1):
                    lines.append(f"Table {table_index}:")
                    for row in table.rows:
                        cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
                        lines.append(" | ".join(cells))
            return ToolExecResult(output=_maybe_truncate("\n".join(lines)))

        if command == "append":
            paragraphs = _as_paragraphs(arguments.get("paragraphs"))
            if not paragraphs:
                raise ToolError("`append` requires non-empty `paragraphs`")
            for paragraph in paragraphs:
                doc.add_paragraph(paragraph)
            doc.save(str(path))
            return ToolExecResult(
                output=f"Appended {len(paragraphs)} paragraph(s) to {path}"
            )

        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise ToolError("`replace` requires non-empty `old_text`")
        if not isinstance(new_text, str):
            raise ToolError("`replace` requires `new_text`")
        replaced = 0
        for paragraph in doc.paragraphs:
            if paragraph.text == old_text:
                for run in paragraph.runs:
                    run.text = ""
                paragraph.add_run(new_text)
                replaced += 1
        if replaced == 0:
            raise ToolError(f"no paragraph matches old_text: {old_text!r}")
        doc.save(str(path))
        return ToolExecResult(output=f"Replaced {replaced} paragraph(s) in {path}")


class ExcelEditTool(Tool):
    """Create, read and edit Excel .xlsx workbooks."""

    def __init__(self, model_provider: str | None = None) -> None:
        super().__init__(model_provider)

    def get_name(self) -> str:
        return "excel_edit_tool"

    def get_description(self) -> str:
        return """Tool for creating, reading and editing Excel .xlsx workbooks
* Commands: create, read, set_cell, update, append_rows
* `create` writes a new .xlsx from a list of rows; fails if the file already exists
* `read` dumps cell values for a sheet (optionally a range like A1:C5) as JSON
* `set_cell` updates one cell (e.g. B2); `update` sets many cells via {cell: value}
* `append_rows` appends rows below the last used row
* Paths are Windows absolute paths (e.g. D:\\workspace\\data.xlsx) inside the workspace
"""

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The operation to perform.",
                required=True,
                enum=["create", "read", "set_cell", "update", "append_rows"],
            ),
            ToolParameter(
                name="path",
                type="string",
                description="Absolute path to the .xlsx file, e.g. D:\\workspace\\data.xlsx.",
                required=True,
            ),
            ToolParameter(
                name="sheet_name",
                type="string",
                description="Sheet to operate on; defaults to the active sheet.",
                required=False,
            ),
            ToolParameter(
                name="rows",
                type="array",
                description="List of rows (each row is a list of values); required for `create` and `append_rows`.",
                required=False,
                items={"type": "array"},
            ),
            ToolParameter(
                name="cell",
                type="string",
                description="Cell reference like B2; required for `set_cell`.",
                required=False,
            ),
            ToolParameter(
                name="value",
                type="string",
                description="Value to write for `set_cell`; numbers/booleans are auto-coerced.",
                required=False,
            ),
            ToolParameter(
                name="cells",
                type="object",
                description="Mapping {cell: value} for the `update` command.",
                required=False,
            ),
            ToolParameter(
                name="range",
                type="string",
                description="Optional range like A1:C5 for `read`; defaults to the used range.",
                required=False,
            ),
        ]

    async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
        from openpyxl import Workbook, load_workbook

        command = arguments.get("command")
        if command not in self.get_parameters()[0].enum:
            raise ToolError(f"unsupported command: {command!r}")
        path = _resolve_path(arguments.get("path", ""))
        if path.suffix.lower() != ".xlsx":
            raise ToolError("excel_edit_tool only supports .xlsx files")
        sheet_name = arguments.get("sheet_name")
        if sheet_name is not None and not isinstance(sheet_name, str):
            raise ToolError("`sheet_name` must be a string")

        if command == "create":
            if path.exists():
                raise ToolError(f"file already exists, use read/set_cell/update/append_rows: {path}")
            rows = arguments.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ToolError("`create` requires non-empty `rows`")
            path.parent.mkdir(parents=True, exist_ok=True)
            workbook = Workbook()
            sheet = workbook.active
            if sheet_name:
                sheet.title = sheet_name[:31]
            for row in rows:
                if not isinstance(row, list):
                    raise ToolError("each row in `rows` must be a list")
                sheet.append([_coerce_scalar(value) for value in row])
            workbook.save(str(path))
            return ToolExecResult(output=f"Created Excel workbook: {path}")

        if not path.is_file():
            raise ToolError(f"file does not exist: {path}")
        workbook = load_workbook(str(path))
        sheet = workbook[sheet_name] if sheet_name else workbook.active

        if command == "read":
            cell_range = arguments.get("range")
            if cell_range is not None and not isinstance(cell_range, str):
                raise ToolError("`range` must be a string")
            data: list[list[Any]] = []
            if cell_range:
                for row in sheet[cell_range]:
                    data.append([cell.value for cell in row])
            else:
                for row in sheet.iter_rows():
                    data.append([cell.value for cell in row])
            output = {
                "file": str(path),
                "sheet": sheet.title,
                "range": cell_range or "used",
                "rows": data,
            }
            return ToolExecResult(
                output=_maybe_truncate(json.dumps(output, ensure_ascii=False, default=str, indent=2))
            )

        if command == "set_cell":
            cell = arguments.get("cell")
            if not isinstance(cell, str) or not cell:
                raise ToolError("`set_cell` requires `cell` (e.g. B2)")
            sheet[cell] = _coerce_scalar(arguments.get("value"))
            workbook.save(str(path))
            return ToolExecResult(output=f"Set {sheet.title}!{cell} in {path}")

        if command == "update":
            cells = arguments.get("cells")
            if not isinstance(cells, dict) or not cells:
                raise ToolError("`update` requires non-empty `cells` mapping")
            for cell, value in cells.items():
                sheet[cell] = _coerce_scalar(value)
            workbook.save(str(path))
            return ToolExecResult(
                output=f"Updated {len(cells)} cell(s) in {path}"
            )

        rows = arguments.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ToolError("`append_rows` requires non-empty `rows`")
        for row in rows:
            if not isinstance(row, list):
                raise ToolError("each row in `rows` must be a list")
            sheet.append([_coerce_scalar(value) for value in row])
        workbook.save(str(path))
        return ToolExecResult(output=f"Appended {len(rows)} row(s) to {path}")


class PdfTool(Tool):
    """Create, read, merge, split and stamp PDF documents."""

    def __init__(self, model_provider: str | None = None) -> None:
        super().__init__(model_provider)

    def get_name(self) -> str:
        return "pdf_tool"

    def get_description(self) -> str:
        return """Tool for creating, reading and manipulating PDF documents
* Commands: create, read, merge, split, watermark
* `create` generates a new PDF (title + paragraphs) with reportlab
* `read` extracts text page by page with PyMuPDF
* `merge` combines the PDFs listed in `paths` into `output_path`
* `split` writes the 1-based page numbers in `pages` as page-N.pdf under `output_path` (a directory)
* `watermark` stamps `text` at the top of every page
* PDFs cannot be edited in place; changes produce a new file via merge/split/watermark
* Paths are Windows absolute paths (e.g. D:\\workspace\\summary.pdf) inside the workspace
"""

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The operation to perform.",
                required=True,
                enum=["create", "read", "merge", "split", "watermark"],
            ),
            ToolParameter(
                name="path",
                type="string",
                description="Input PDF path (create/read/merge/watermark) or source for split.",
                required=True,
            ),
            ToolParameter(
                name="output_path",
                type="string",
                description="Output PDF path, or an existing directory for `split`.",
                required=False,
            ),
            ToolParameter(
                name="title",
                type="string",
                description="Optional document title used by the `create` command.",
                required=False,
            ),
            ToolParameter(
                name="paragraphs",
                type="array",
                description="List of paragraph strings; required for `create`.",
                required=False,
                items={"type": "string"},
            ),
            ToolParameter(
                name="paths",
                type="array",
                description="List of input PDF paths; required for `merge`.",
                required=False,
                items={"type": "string"},
            ),
            ToolParameter(
                name="pages",
                type="array",
                description="1-based page numbers; required for `split`.",
                required=False,
                items={"type": "integer"},
            ),
            ToolParameter(
                name="text",
                type="string",
                description="Stamp text; required for `watermark`.",
                required=False,
            ),
        ]

    async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
        command = arguments.get("command")
        if command not in self.get_parameters()[0].enum:
            raise ToolError(f"unsupported command: {command!r}")
        path = _resolve_path(arguments.get("path", ""))
        if path.suffix.lower() != ".pdf":
            raise ToolError("pdf_tool only supports .pdf files")

        if command == "create":
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

            output_path = _resolve_path(arguments.get("output_path") or arguments.get("path", ""))
            if output_path.suffix.lower() != ".pdf":
                raise ToolError("`output_path` must end with .pdf")
            if output_path.exists():
                raise ToolError(f"file already exists: {output_path}")
            paragraphs = _as_paragraphs(arguments.get("paragraphs"))
            if not paragraphs:
                raise ToolError("`create` requires non-empty `paragraphs`")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            styles = getSampleStyleSheet()
            styles["Title"].fontName = "STSong-Light"
            styles["BodyText"].fontName = "STSong-Light"
            story: list[Any] = []
            title = arguments.get("title")
            if isinstance(title, str) and title.strip():
                story.append(Paragraph(title.strip(), styles["Title"]))
                story.append(Spacer(1, 12))
            for paragraph in paragraphs:
                story.append(Paragraph(paragraph, styles["BodyText"]))
                story.append(Spacer(1, 6))
            SimpleDocTemplate(str(output_path), pagesize=A4).build(story)
            return ToolExecResult(output=f"Created PDF: {output_path}")

        if not path.is_file():
            raise ToolError(f"file does not exist: {path}")

        if command == "read":
            import pymupdf as fitz

            with fitz.open(str(path)) as document:
                lines = [f"Document: {path}", f"Pages: {document.page_count}"]
                for page_index, page in enumerate(document, start=1):
                    lines.append(f"--- page {page_index} ---")
                    text = page.get_text().strip()
                    lines.append(text or "(no extractable text)")
                return ToolExecResult(output=_maybe_truncate("\n".join(lines)))

        if command == "merge":
            import pymupdf as fitz

            paths = arguments.get("paths")
            output_path = arguments.get("output_path")
            if not isinstance(paths, list) or not paths:
                raise ToolError("`merge` requires non-empty `paths`")
            if not isinstance(output_path, str) or not output_path:
                raise ToolError("`merge` requires `output_path`")
            merged = fitz.open()
            try:
                for item in paths:
                    source = _resolve_path(item)
                    if not source.is_file():
                        raise ToolError(f"input PDF does not exist: {source}")
                    with fitz.open(str(source)) as incoming:
                        merged.insert_pdf(incoming)
                target = _resolve_path(output_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                merged.save(str(target))
                return ToolExecResult(
                    output=f"Merged {len(paths)} PDF(s) into {target}"
                )
            finally:
                merged.close()

        if command == "split":
            import pymupdf as fitz

            pages = arguments.get("pages")
            output_path = arguments.get("output_path")
            if not isinstance(pages, list) or not pages:
                raise ToolError("`split` requires non-empty `pages`")
            if not isinstance(output_path, str) or not output_path:
                raise ToolError("`split` requires `output_path` (an existing directory)")
            output_dir = _resolve_path(output_path)
            if not output_dir.is_dir():
                raise ToolError(f"split output directory does not exist: {output_dir}")
            with fitz.open(str(path)) as document:
                written: list[str] = []
                for page_number in pages:
                    if not isinstance(page_number, int) or isinstance(page_number, bool):
                        raise ToolError("each `pages` entry must be an integer")
                    if page_number < 1 or page_number > document.page_count:
                        raise ToolError(
                            f"page {page_number} out of range (1..{document.page_count})"
                        )
                    target = output_dir / f"page-{page_number:04d}.pdf"
                    single = fitz.open()
                    try:
                        single.insert_pdf(document, from_page=page_number - 1, to_page=page_number - 1)
                        single.save(str(target))
                    finally:
                        single.close()
                    written.append(str(target))
                return ToolExecResult(output="Split pages:\n" + "\n".join(written))

        import pymupdf as fitz

        text = arguments.get("text")
        if not isinstance(text, str) or not text:
            raise ToolError("`watermark` requires non-empty `text`")
        output_path = arguments.get("output_path")
        if not isinstance(output_path, str) or not output_path:
            raise ToolError("`watermark` requires `output_path`")
        target = _resolve_path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(str(path)) as document:
            for page in document:
                point = fitz.Point(72, page.rect.height - 72)
                page.insert_text(point, text, fontsize=28, color=(0.6, 0.6, 0.6))
            document.save(str(target))
        return ToolExecResult(output=f"Watermarked {path} -> {target}")


class DocumentConvertTool(Tool):
    """Convert Office documents to PDF using the local Office installation."""

    def __init__(self, model_provider: str | None = None) -> None:
        super().__init__(model_provider)

    def get_name(self) -> str:
        return "document_convert_tool"

    def get_description(self) -> str:
        return """Tool for converting Office documents to PDF via the local Microsoft Office installation
* Commands: docx_to_pdf, xlsx_to_pdf
* Requires Word/Excel to be installed on the host; fails with a clear error otherwise
* Output path must end with .pdf and must differ from the input path
* Paths are Windows absolute paths (e.g. D:\\workspace\\report.docx -> D:\\workspace\\report.pdf)
"""

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The conversion to perform.",
                required=True,
                enum=["docx_to_pdf", "xlsx_to_pdf"],
            ),
            ToolParameter(
                name="input_path",
                type="string",
                description="Absolute path to the source .docx or .xlsx file.",
                required=True,
            ),
            ToolParameter(
                name="output_path",
                type="string",
                description="Absolute path to the generated .pdf file.",
                required=True,
            ),
        ]

    async def execute(self, arguments: ToolCallArguments) -> ToolExecResult:
        command = arguments.get("command")
        if command not in self.get_parameters()[0].enum:
            raise ToolError(f"unsupported command: {command!r}")
        input_path = _resolve_path(arguments.get("input_path", ""))
        output_path = _resolve_path(arguments.get("output_path", ""))
        if not input_path.is_file():
            raise ToolError(f"input file does not exist: {input_path}")
        if output_path.suffix.lower() != ".pdf":
            raise ToolError("`output_path` must end with .pdf")
        if output_path == input_path:
            raise ToolError("`output_path` must differ from `input_path`")

        if importlib.util.find_spec("win32com.client") is None:
            raise ToolError("pywin32 is not installed; Office COM conversion is unavailable")

        if command == "docx_to_pdf":
            if input_path.suffix.lower() != ".docx":
                raise ToolError("docx_to_pdf requires a .docx input")
            _convert_office(
                "Word.Application",
                input_path,
                output_path,
                open_and_save=lambda app, src, dst: (
                    _office_do(
                        app,
                        "Documents.Open",
                        [str(src), False, True],
                        doc_close=("Close", [False]),
                        save=(src, dst, "SaveAs2", 17),
                    )
                ),
            )
        else:
            if input_path.suffix.lower() != ".xlsx":
                raise ToolError("xlsx_to_pdf requires a .xlsx input")
            _convert_office(
                "Excel.Application",
                input_path,
                output_path,
                open_and_save=lambda app, src, dst: (
                    _office_do(
                        app,
                        "Workbooks.Open",
                        [str(src), False, True],
                        doc_close=("Close", [False]),
                        save=(src, dst, "ExportAsFixedFormat", 0),
                    )
                ),
            )
        return ToolExecResult(output=f"Converted {input_path} -> {output_path}")


def _convert_office(prog_id: str, input_path: Path, output_path: Path, open_and_save) -> None:
    import pythoncom
    import win32com.client

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()
    app = None
    try:
        try:
            app = win32com.client.DispatchEx(prog_id)
        except Exception as exc:
            raise ToolError(
                f"{prog_id} is not available on this host; install Microsoft Office: {exc}"
            ) from exc
        try:
            app.Visible = False
            app.DisplayAlerts = False
            open_and_save(app, input_path, output_path)
        finally:
            with contextlib.suppress(Exception):
                app.Quit()
    finally:
        pythoncom.CoUninitialize()


def _office_do(app, open_method: str, open_args: list, *, doc_close, save) -> None:
    """Open an Office document, save it in another format, then close it."""
    document = getattr(app, open_method)(*open_args)
    try:
        _, dst, save_method, format_code = save
        getattr(document, save_method)(str(dst), format_code)
    finally:
        with contextlib.suppress(Exception):
            getattr(document, doc_close[0])(*doc_close[1])


def register_document_tools() -> None:
    """Register document tools into the Trae tools registry (idempotent)."""
    from trae_agent.tools import tools_registry

    tools_registry.setdefault("word_edit_tool", WordEditTool)
    tools_registry.setdefault("excel_edit_tool", ExcelEditTool)
    tools_registry.setdefault("pdf_tool", PdfTool)
    tools_registry.setdefault("document_convert_tool", DocumentConvertTool)

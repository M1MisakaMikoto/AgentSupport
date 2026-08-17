"""Load the hand-written API reference into the OpenAPI description.

The canonical reference lives at ``docs/api/agentsupport-api.md`` in the
repository root.  It is embedded into the FastAPI ``description`` so that
``/docs`` (Swagger UI), ``/redoc`` and ``/openapi.json`` all carry the full
reference instead of only the auto-generated operation schemas.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_REFERENCE_RELATIVE = Path("docs") / "api" / "agentsupport-api.md"

_FALLBACK_DESCRIPTION = (
    "AgentSupport API 参考：部署包未附带 docs/api/agentsupport-api.md，"
    "完整内容请参见仓库文档。"
)

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _repo_root() -> Path:
    # This file lives at src/agentsupport/serving/http/api_docs.py, so the
    # repository root is four parents up.  In the container the package is
    # copied to /app/src, which still resolves to /app.
    return Path(__file__).resolve().parents[4]


def _resolve_reference_path() -> Path | None:
    override = os.environ.get("AGENTSUPPORT_API_DOC_PATH")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    candidate = _repo_root() / _REFERENCE_RELATIVE
    return candidate if candidate.is_file() else None


def _strip_relative_links(markdown: str) -> str:
    """Keep absolute URLs, turn relative links and anchors into plain text."""

    def replace(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if url.startswith(("http://", "https://")):
            return match.group(0)
        return label

    return _MARKDOWN_LINK_RE.sub(replace, markdown)


@lru_cache(maxsize=1)
def api_reference_description() -> str:
    path = _resolve_reference_path()
    if path is None:
        return _FALLBACK_DESCRIPTION
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_DESCRIPTION
    return _strip_relative_links(markdown)

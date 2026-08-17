"""Contract: the FastAPI /docs surface embeds the hand-written API reference."""

import pytest

from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService
from agentsupport.serving.http.api_docs import api_reference_description


@pytest.fixture
def service(tmp_path):
    return AgentSupportService(Settings(workspace_root=tmp_path))


def test_openapi_description_embeds_api_reference(service):
    app = create_app(service)
    description = app.openapi()["info"]["description"]

    assert "AgentSupport API 参考" in description
    assert "## 1. 访问入口" in description
    # relative links and in-page anchors are de-linked so the Swagger UI
    # does not render broken links
    assert "](../" not in description
    assert "[4.1](" not in description


def test_missing_reference_file_falls_back_gracefully(service, monkeypatch):
    monkeypatch.setattr(
        "agentsupport.serving.http.api_docs._resolve_reference_path",
        lambda: None,
    )
    api_reference_description.cache_clear()
    try:
        app = create_app(service)
        description = app.openapi()["info"]["description"]
    finally:
        api_reference_description.cache_clear()

    assert "AgentSupport API 参考" in description
    assert "未附带" in description

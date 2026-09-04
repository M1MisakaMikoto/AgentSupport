"""Eval service wiring: engine reuse and settings-driven timeout."""

from __future__ import annotations

from pathlib import Path

from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.container import build_agentsupport_dependencies, build_eval_service
from agentsupport.bootstrap.settings import Settings


def _config(tmp_path: Path, **overrides) -> Settings:
    values = {
        "persistence_mode": "postgres",
        "execution_mode": "temporal",
        "database_url": f"sqlite:///{tmp_path / 'db.sqlite'}",
        "auto_create_schema": True,
        "workspace_root": tmp_path / "workspace",
        "skills_root": tmp_path / "skills",
    }
    values.update(overrides)
    return Settings(**values)


def test_build_eval_service_reuses_agentsupport_engine(tmp_path):
    config = _config(tmp_path)
    deps = build_agentsupport_dependencies(config)
    service = AgentSupportService(config, **deps)

    eval_service = build_eval_service(config, agentsupport=service)

    assert eval_service.agentsupport is service
    assert eval_service.store.engine is service.repository.engine


def test_eval_case_timeout_comes_from_settings(tmp_path):
    config = _config(tmp_path, eval_case_timeout_seconds=123)
    deps = build_agentsupport_dependencies(config)
    service = AgentSupportService(config, **deps)

    eval_service = build_eval_service(config, agentsupport=service)

    assert eval_service.case_timeout_seconds == 123

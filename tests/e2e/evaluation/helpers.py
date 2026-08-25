"""Test-only wiring for the evaluation layer.

The in-process deterministic Session Runner is a test tool: production eval
runs real agents through the configured execution backend (Temporal), so the
production builder never references it. Delete this helper along with the
deterministic runner when it is retired.
"""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport

from agentsupport.adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
from agentsupport.adapters.runner import TraeCoreRunnerRuntime
from agentsupport.adapters.workspace import LocalWorkspaceStorageDriver
from agentsupport.application.service import AgentSupportService
from agentsupport.bootstrap.container import build_agentsupport_dependencies
from agentsupport.bootstrap.settings import Settings
from agentsupport.evaluation import EvalService
from agentsupport.evaluation.store import InMemoryEvalStore
from session_runner.server import create_runner_app


def build_test_eval_service(config: Settings, **eval_kwargs: Any) -> EvalService:
    """Build an eval service whose cases run on the deterministic runner."""

    eval_kwargs.setdefault("case_timeout_seconds", config.eval_case_timeout_seconds)
    eval_kwargs.setdefault("auto_interaction", config.eval_auto_interaction)
    eval_kwargs.setdefault("auto_input", config.eval_auto_input)
    eval_kwargs.setdefault("auto_answer_limit", config.eval_auto_answer_limit)
    deps = build_agentsupport_dependencies(config)
    service = AgentSupportService(config, **deps)
    service.core_runtime = TraeCoreRunnerRuntime(
        config.core_runner_url or "http://runner",
        timeout_seconds=config.core_runner_timeout_seconds,
        transport=ASGITransport(app=create_runner_app(runner_mode="deterministic")),
    )
    if config.persistence_mode == "postgres":
        repository = getattr(service, "repository", None)
        engine = getattr(repository, "engine", None) if repository is not None else None
        store = SqlAlchemyEvalStore(
            config.database_url,
            create_schema=config.auto_create_schema,
            engine=engine,
        )
    else:
        store = InMemoryEvalStore()
    return EvalService(
        workspace_driver=LocalWorkspaceStorageDriver(config.workspace_root),
        agentsupport=service,
        store=store,
        **eval_kwargs,
    )

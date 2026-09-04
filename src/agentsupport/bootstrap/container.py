from __future__ import annotations

from typing import Any

from ..adapters.notification import InMemoryEventStore, create_event_notifier
from ..adapters.persistence.sqlalchemy import PostgresRepository
from ..adapters.registry import SqlAlchemyRunnerRegistry
from ..adapters.runner import TraeCoreRunnerRuntime
from ..adapters.skills import LocalSkillProvider
from ..adapters.workspace import LocalWorkspaceStorageDriver
from ..application.ports import CoreRuntime, RunnerRegistry
from ..application.runner_registry import InMemoryRunnerRegistry
from ..application.service import AgentSupportService
from ..execution.temporal import TemporalRunCoordinator
from .settings import Settings, settings


def build_repository(config: Settings) -> PostgresRepository | None:
    if config.persistence_mode != "postgres":
        return None
    return PostgresRepository(config.database_url, create_schema=config.auto_create_schema)


def build_core_runtime(config: Settings) -> CoreRuntime | None:
    if not config.core_runner_url:
        return None
    return TraeCoreRunnerRuntime(
        config.core_runner_url or "http://runner",
        timeout_seconds=config.core_runner_timeout_seconds,
    )


def build_runner_registry(config: Settings, repository: PostgresRepository | None) -> RunnerRegistry:
    if repository is not None:
        return SqlAlchemyRunnerRegistry(repository)
    return InMemoryRunnerRegistry()


def build_workspace_provider(config: Settings) -> Any:
    return LocalWorkspaceStorageDriver(config.workspace_root)


def build_skill_provider(config: Settings) -> LocalSkillProvider:
    return LocalSkillProvider(config.skills_root)


def build_temporal_coordinator(config: Settings) -> TemporalRunCoordinator | None:
    if config.execution_mode != "temporal":
        return None
    return TemporalRunCoordinator(
        host=config.temporal_host,
        namespace=config.temporal_namespace,
        task_queue=config.temporal_task_queue,
        workflow_timeout_seconds=config.temporal_workflow_timeout_seconds,
    )


def build_agentsupport_dependencies(config: Settings) -> dict[str, Any]:
    repository = build_repository(config)
    return {
        "events_store": InMemoryEventStore(),
        "event_notifier": create_event_notifier(config.redis_url),
        "workspace_provider": build_workspace_provider(config),
        "skill_provider": build_skill_provider(config),
        "core_runtime": build_core_runtime(config),
        "repository": repository,
        "runner_registry": build_runner_registry(config, repository),
        "temporal": build_temporal_coordinator(config),
    }


def build_agentsupport_service(config: Settings = settings) -> AgentSupportService:
    return AgentSupportService(config, **build_agentsupport_dependencies(config))


def build_eval_service(
    config: Settings = settings,
    *,
    agentsupport: AgentSupportService | None = None,
):
    """Wiring for the evaluation layer.

    The eval service drives the platform execution path (Session/Conversation),
    so real agents run through the configured execution backend (Temporal in
    production, inline for local/dev). Tests inject their own in-process
    deterministic runner instead of relying on this builder.
    """

    from ..adapters.persistence.sqlalchemy.eval_store import SqlAlchemyEvalStore
    from ..application.service import AgentSupportService
    from ..evaluation import EvalService
    from ..evaluation.store import InMemoryEvalStore

    service = agentsupport or AgentSupportService(
        config, **build_agentsupport_dependencies(config)
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
        case_timeout_seconds=config.eval_case_timeout_seconds,
        auto_interaction=config.eval_auto_interaction,
        auto_input=config.eval_auto_input,
        auto_answer_limit=config.eval_auto_answer_limit,
    )

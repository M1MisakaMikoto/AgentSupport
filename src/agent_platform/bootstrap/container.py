from __future__ import annotations

from typing import Any

from ..adapters.notification import InMemoryEventStore, create_event_notifier
from ..adapters.persistence.sqlalchemy import PostgresRepository
from ..adapters.runner import TraeCoreRunnerRuntime
from ..adapters.runtime import (
    DockerCliRuntimeDriver,
    DockerRuntimeDriver,
    KubernetesRuntimeDriver,
    default_session_container_env,
)
from ..adapters.skills import LocalSkillProvider
from ..adapters.workspace import KubernetesWorkspaceProvider, LocalWorkspaceProvider
from ..application.platform_service import PlatformService
from ..application.ports import CoreRuntime, RuntimeDriver
from .settings import Settings, settings


def build_repository(config: Settings) -> PostgresRepository | None:
    if config.persistence_mode != "postgres":
        return None
    return PostgresRepository(config.database_url, create_schema=config.auto_create_schema)


def build_runtime_driver(config: Settings) -> RuntimeDriver:
    if config.runtime_driver == "kubernetes":
        return KubernetesRuntimeDriver(
            api_server=config.kubernetes_api_server,
            namespace=config.kubernetes_namespace,
            image=config.runner_image,
            pvc_size=config.kubernetes_pvc_size,
            storage_class=config.kubernetes_storage_class,
            runner_secret_name=config.kubernetes_runner_secret_name,
            startup_timeout_seconds=config.runtime_start_timeout_seconds,
            container_env=default_session_container_env(),
        )
    if config.runtime_driver == "docker_cli":
        return DockerCliRuntimeDriver(
            context=config.runtime_context,
            stop_grace_seconds=config.runtime_stop_grace_seconds,
            startup_timeout_seconds=config.runtime_start_timeout_seconds,
            container_env=default_session_container_env(),
        )
    return DockerRuntimeDriver()


def build_core_runtime(config: Settings) -> CoreRuntime | None:
    if not (config.core_runner_url or config.runtime_driver == "docker_cli"):
        return None
    return TraeCoreRunnerRuntime(
        config.core_runner_url or "http://runner",
        timeout_seconds=config.core_runner_timeout_seconds,
    )


def build_workspace_provider(config: Settings) -> Any:
    if config.runtime_driver == "kubernetes":
        return KubernetesWorkspaceProvider()
    return LocalWorkspaceProvider(config.workspace_root)


def build_skill_provider(config: Settings) -> LocalSkillProvider:
    return LocalSkillProvider(config.skills_root)


def build_platform_dependencies(config: Settings) -> dict[str, Any]:
    return {
        "events_store": InMemoryEventStore(),
        "event_notifier": create_event_notifier(config.redis_url),
        "workspace_provider": build_workspace_provider(config),
        "skill_provider": build_skill_provider(config),
        "runtime_driver": build_runtime_driver(config),
        "core_runtime": build_core_runtime(config),
        "repository": build_repository(config),
    }


def build_platform_service(config: Settings = settings) -> PlatformService:
    return PlatformService(config, **build_platform_dependencies(config))

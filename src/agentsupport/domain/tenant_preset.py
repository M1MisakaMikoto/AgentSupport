"""Tenant presets: per-tenant CLI app bundles used to build runner images.

A tenant preset is the *same entity* as the tenant seen from the configuration
side: it is stored globally (no per-tenant partitioning, no authentication) and
looked up by ``tenant_id``. A preset declares which CLI applications a tenant
may use, which sub-command prefixes are safe to auto-approve, which environment
variable names the CLI expects, and which skills accompany it.

Missing parts of a tenant preset fall back part-by-part to the deployment
default preset; a session without ``tenant_id`` uses the default runner image.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .execution import utc_now

#: Reserved tenant that represents "no tenant": the deployment default preset.
DEFAULT_TENANT_ID = "default"

#: One CLI package may be at most this large (decoded bytes).
MAX_CLI_PACKAGE_BYTES = 64 * 1024 * 1024

_CLI_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_cli_id(value: str) -> bool:
    return bool(_CLI_ID.fullmatch(value or ""))


def normalize_safe_prefix(value: str) -> str:
    """Collapse a declared safe prefix into its canonical single-line form."""

    return " ".join((value or "").split())


def safe_prefix_tokens(value: str) -> list[str]:
    normalized = normalize_safe_prefix(value)
    return normalized.split(" ") if normalized else []


class CliDaemon(BaseModel):
    """A long-running sub-command of a CLI the image starts with every runner."""

    command: str
    port: int | None = None

    def normalized(self) -> CliDaemon:
        return CliDaemon(
            command=normalize_safe_prefix(self.command),
            port=self.port,
        )


class CliApp(BaseModel):
    """One CLI application a tenant preset installs into the runner image."""

    cli_id: str
    entry: str
    description: str = ""
    #: Sub-command prefixes that may run without approval (e.g. ``mytool list``).
    safe_prefixes: list[str] = Field(default_factory=list)
    #: Environment variable *names* the CLI reads; values live in runner env.
    env: list[str] = Field(default_factory=list)
    #: Uploaded package file name (the build copies it into the image).
    package: str | None = None
    #: Optional long-running daemon the runner container starts on boot.
    daemon: CliDaemon | None = None

    def normalized(self) -> CliApp:
        return CliApp(
            cli_id=self.cli_id,
            entry=normalize_safe_prefix(self.entry),
            description=self.description,
            safe_prefixes=[
                item
                for item in dict.fromkeys(
                    normalize_safe_prefix(prefix) for prefix in self.safe_prefixes
                )
                if item
            ],
            env=[name for name in dict.fromkeys(self.env) if name],
            package=self.package,
            daemon=self.daemon.normalized() if self.daemon else None,
        )


class TenantPreset(BaseModel):
    """The configuration half of a tenant: what it may run and with which skills."""

    tenant_id: str
    name: str = ""
    description: str = ""
    cli_apps: list[CliApp] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def normalized(self) -> TenantPreset:
        return TenantPreset(
            tenant_id=self.tenant_id,
            name=self.name,
            description=self.description,
            cli_apps=[app.normalized() for app in self.cli_apps],
            skills=[item for item in dict.fromkeys(self.skills) if item],
            env=[name for name in dict.fromkeys(self.env) if name],
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class BuildStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"


TERMINAL_BUILD_STATUSES = frozenset({BuildStatus.READY, BuildStatus.FAILED})


class PresetBuild(BaseModel):
    """One asynchronous image build triggered by a preset upload."""

    build_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    status: BuildStatus = BuildStatus.PENDING
    content_hash: str = ""
    image_tag: str | None = None
    error: str | None = None
    log_tail: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


def image_tag_for(tenant_id: str, content_hash: str) -> str:
    """Deterministic image tag: same preset content always builds the same tag."""

    slug = re.sub(r"[^A-Za-z0-9._-]", "-", tenant_id).strip("-") or DEFAULT_TENANT_ID
    return f"agentsupport-runner:{slug}-{content_hash[:16]}"


def preset_content_hash(
    preset: TenantPreset, package_digests: dict[str, str] | None = None
) -> str:
    """Content hash over the normalised preset plus uploaded package digests."""

    normalized = preset.normalized()
    payload = {
        "tenant_id": normalized.tenant_id,
        "name": normalized.name,
        "description": normalized.description,
        "cli_apps": [
            {
                "cli_id": app.cli_id,
                "entry": app.entry,
                "description": app.description,
                "safe_prefixes": app.safe_prefixes,
                "env": app.env,
                "package": app.package,
                "daemon": (
                    {"command": app.daemon.command, "port": app.daemon.port}
                    if app.daemon
                    else None
                ),
                "digest": (package_digests or {}).get(app.cli_id, ""),
            }
            for app in normalized.cli_apps
        ],
        "skills": normalized.skills,
        "env": normalized.env,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve_preset_fields(
    preset: TenantPreset | None, default: TenantPreset | None
) -> dict[str, Any]:
    """Part-wise fallback: missing parts of a tenant preset use the default.

    "Missing" is per field: a preset without CLI apps still gets the default
    CLI set, and likewise for skills and env names. ``used_default`` records
    which fields actually fell back, for the audit trail.
    """

    source = preset or default
    used_default = {
        "cli_apps": preset is None or not preset.cli_apps,
        "skills": preset is None or not preset.skills,
        "env": preset is None or not preset.env,
    }
    cli_apps = (preset.cli_apps if preset and preset.cli_apps else (default.cli_apps if default else []))
    skills = (preset.skills if preset and preset.skills else (default.skills if default else []))
    env = (preset.env if preset and preset.env else (default.env if default else []))
    return {
        "tenant_id": preset.tenant_id if preset else DEFAULT_TENANT_ID,
        "source": "preset" if preset else "default",
        "cli_apps": [app.normalized() for app in cli_apps],
        "skills": list(skills),
        "env": list(env),
        "used_default": used_default,
        "name": source.name if source else "",
    }


def cli_policy_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """The per-run policy the runner needs to gate bash calls."""

    cli_apps = fields.get("cli_apps") or []
    allowed_safe_prefixes: list[str] = []
    for app in cli_apps:
        for prefix in getattr(app, "safe_prefixes", []) or []:
            normalized = normalize_safe_prefix(prefix)
            if normalized and normalized not in allowed_safe_prefixes:
                allowed_safe_prefixes.append(normalized)
    return {
        "tenant_id": fields.get("tenant_id", DEFAULT_TENANT_ID),
        "cli_apps": [
            {
                "cli_id": app.cli_id,
                "entry": app.entry,
                "safe_prefixes": list(app.safe_prefixes),
            }
            for app in cli_apps
        ],
        "allowed_safe_prefixes": allowed_safe_prefixes,
    }


__all__ = [
    "DEFAULT_TENANT_ID",
    "MAX_CLI_PACKAGE_BYTES",
    "TERMINAL_BUILD_STATUSES",
    "BuildStatus",
    "CliApp",
    "CliDaemon",
    "PresetBuild",
    "TenantPreset",
    "cli_policy_from_fields",
    "image_tag_for",
    "normalize_safe_prefix",
    "preset_content_hash",
    "resolve_preset_fields",
    "safe_prefix_tokens",
    "valid_cli_id",
]

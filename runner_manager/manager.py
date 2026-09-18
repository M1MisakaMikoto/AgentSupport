"""Build tenant images and run session runners on demand.

Two responsibilities, both driven from the host (the only place with Docker):

* **builds** — claim a PENDING preset build from the control plane, materialise
  the uploaded CLI packages into a staging directory, ``docker build`` a tenant
  image and report the outcome back;
* **runners** — launch one runner container per tenant on demand, publish its
  port, wait until it is ready and reclaim it once the tenant has been idle.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .settings import ManagerSettings

logger = logging.getLogger(__name__)

LOG_TAIL_CHARS = 4000

#: The canonical runner command, identical to the compose runner service. The
#: tenant image sets it explicitly so the daemon entrypoint never depends on
#: whatever CMD the base image happens to carry.
RUNNER_CMD = (
    'CMD ["uvicorn", "session_runner.main:app", "--host", "0.0.0.0", "--port", "8080"]'
)


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def log_tail(self) -> str:
        combined = (self.stderr or self.stdout or "").strip()
        return combined[-LOG_TAIL_CHARS:]


class SubprocessCommandRunner:
    """Runs a command with asyncio and captures its output."""

    async def __call__(self, argv: list[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-") or "default"


def render_dockerfile(
    base_image: str, cli_apps: list[dict[str, Any]], env: list[str]
) -> str:
    """The tenant image: the platform base plus the tenant's CLI packages."""

    lines = [
        f"FROM {base_image}",
        "USER root",
        "COPY packages /opt/cli",
        # Archives are extracted into the CLI's own directory so that
        # ``<cli_id>/bin`` and ``<cli_id>/`` end up on PATH as declared.
        "RUN set -eu; \\",
        "    for dir in /opt/cli/*/; do \\",
        '      for archive in "$dir"*.tar.gz "$dir"*.tgz "$dir"*.tar; do \\',
        '        if [ -f "$archive" ]; then tar -xf "$archive" -C "$dir" && rm -f "$archive"; fi; \\',
        "      done; \\",
        "    done",
        "RUN chmod -R a+rX /opt/cli",
    ]
    path_entries = ["/opt/cli/bin"]
    daemon_lines: list[str] = []
    for app in cli_apps:
        cli_id = _slug(str(app.get("cli_id") or ""))
        path_entries.extend([f"/opt/cli/{cli_id}", f"/opt/cli/{cli_id}/bin"])
        daemon = app.get("daemon") or None
        if isinstance(daemon, dict) and daemon.get("command"):
            daemon_lines.append(
                f"cd /opt/cli/{cli_id} && nohup {daemon['command']} "
                f">> /tmp/{cli_id}-daemon.log 2>&1 &"
            )
    for app in cli_apps:
        entry = Path(str(app.get("entry") or "")).name
        if entry:
            # Uploaded archives lose their exec bit (e.g. when packaged on
            # Windows), so make the declared entry command executable.
            lines.append(
                f'RUN find /opt/cli -type f -name "{entry}" -exec chmod a+x {{}} +'
            )
    lines.append("ENV PATH=\"" + ":".join(path_entries) + ":$PATH\"")
    if env:
        lines.append("# Environment variable names this preset expects: " + ", ".join(env))
    if daemon_lines:
        # One entrypoint per image: every runner container starts the declared
        # long-running sub-commands before handing over to the runner itself.
        script = ["#!/bin/sh", "set -u"] + daemon_lines + ['exec "$@"']
        quoted = " ".join("'" + line.replace("'", "'\\''") + "'" for line in script)
        lines.append(f"RUN printf '%s\\n' {quoted} > /opt/cli/entrypoint.sh")
        lines.append("RUN chmod +x /opt/cli/entrypoint.sh")
        lines.append('ENTRYPOINT ["/opt/cli/entrypoint.sh"]')
        lines.append(RUNNER_CMD)
    lines.append("USER agent")
    return "\n".join(lines) + "\n"


class RunnerManager:
    """Deterministic core of the host-side manager (no FastAPI in here)."""

    def __init__(
        self,
        settings: ManagerSettings,
        *,
        command_runner: Callable[[list[str]], Awaitable[CommandResult]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.run_command = command_runner or SubprocessCommandRunner()
        self.transport = transport
        self._runners: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Platform talk
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return (
            {"X-Runner-Manager-Token": self.settings.token}
            if self.settings.token
            else {}
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.platform_url.rstrip("/"),
            timeout=60.0,
            transport=self.transport,
            headers=self._headers(),
        )

    # ------------------------------------------------------------------
    # Builds
    # ------------------------------------------------------------------

    async def build_once(self) -> bool:
        """Claim and run one pending build. Returns True when work was done."""

        async with self._client() as client:
            response = await client.post("/internal/preset-builds/claim")
            response.raise_for_status()
            payload = response.json()
            if not payload.get("build"):
                return False
            build = payload["build"]
            build_id = str(build["build_id"])
            image_tag = str(payload["image_tag"])
            staging = Path(self.settings.staging_root) / build_id
            try:
                _materialize_packages(staging, payload.get("cli_apps") or [])
                (staging / "Dockerfile").write_text(
                    render_dockerfile(
                        self.settings.base_image,
                        payload.get("cli_apps") or [],
                        list(payload.get("env") or []),
                    ),
                    encoding="utf-8",
                )
                result = await self.run_command(
                    self.docker(
                        "build", "--tag", image_tag, self.transport_path(staging)
                    )
                )
            except Exception as exc:  # noqa: BLE001 - report any build failure
                logger.warning("preset build %s failed: %s", build_id, exc)
                await self._complete(
                    client, build_id, status="FAILED", error=str(exc)
                )
                return True
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            if result.returncode != 0:
                await self._complete(
                    client,
                    build_id,
                    status="FAILED",
                    log_tail=result.log_tail,
                    error=f"docker build exited with {result.returncode}",
                )
            else:
                await self._complete(
                    client,
                    build_id,
                    status="READY",
                    image_tag=image_tag,
                    log_tail=result.log_tail,
                )
        return True

    async def _complete(
        self,
        client: httpx.AsyncClient,
        build_id: str,
        *,
        status: str,
        image_tag: str | None = None,
        log_tail: str = "",
        error: str | None = None,
    ) -> None:
        response = await client.post(
            f"/internal/preset-builds/{build_id}/complete",
            json={
                "status": status,
                "image_tag": image_tag,
                "log_tail": log_tail,
                "error": error,
            },
        )
        response.raise_for_status()
        logger.info("preset build %s -> %s", build_id, status)

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    def docker(self, *arguments: str) -> list[str]:
        transport = self.settings.resolved_transport()
        if transport == "wsl":
            return [
                "wsl",
                "-d",
                self.settings.wsl_distribution,
                "--",
                "docker",
                *arguments,
            ]
        if transport == "context":
            return ["docker", "--context", self.settings.docker_context, *arguments]
        return ["docker", *arguments]

    def transport_path(self, path: Path | str) -> str:
        """Translate a host path into the path form the Docker transport sees.

        The WSL transport runs ``docker`` inside the distribution, where a
        Windows path must be written as ``/mnt/<drive>/...``.
        """

        resolved = str(Path(path).resolve())
        if self.settings.resolved_transport() != "wsl":
            return resolved
        if len(resolved) > 2 and resolved[1] == ":":
            drive = resolved[0].lower()
            return f"/mnt/{drive}{resolved[2:].replace('\\', '/')}"
        return resolved.replace("\\", "/")

    def runners(self) -> list[dict[str, Any]]:
        return [
            {**entry, "endpoint": entry["endpoint"]}
            for entry in sorted(self._runners.values(), key=lambda item: item["tenant_id"])
        ]

    async def ensure_runner(self, tenant_id: str, image_tag: str) -> dict[str, Any]:
        tenant = tenant_id or "default"
        existing = self._runners.get(tenant)
        if existing and await self._container_running(existing["container_id"]):
            existing["last_used_at"] = datetime.now(UTC)
            existing["image_tag"] = image_tag
            return dict(existing)

        name = f"agentsupport-runner-{_slug(tenant)}"
        await self._remove_container(name)
        argv = ["run", "-d", "--name", name]
        argv += ["--label", "agentsupport.runner=1", "--label", f"agentsupport.tenant={tenant}"]
        argv += ["-p", "0:8080"]
        if self.settings.workspace_root:
            argv += [
                "-v",
                f"{self.transport_path(self.settings.workspace_root)}:/workspace-data",
            ]
        env = self.settings.passthrough_runner_env()
        env.update(
            {
                "SESSION_RUNNER_MODE": "trae",
                "SESSION_RUNNER_TENANT_ID": tenant,
                "SESSION_RUNNER_WORKSPACE_ROOTS": "/workspace,/workspace-data",
            }
        )
        for key, value in env.items():
            if value is None:
                continue
            argv += ["-e", f"{key}={value}"]
        argv.append(image_tag)

        created = await self.run_command(self.docker(*argv))
        if created.returncode != 0:
            raise RuntimeError(f"failed to start runner {name}: {created.log_tail}")
        container_id = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
        host_port = await self._published_port(name)
        endpoint = f"{self.settings.endpoint_host.rstrip('/')}:{host_port}"
        await self._wait_ready(endpoint)
        entry = {
            "tenant_id": tenant,
            "container_id": container_id,
            "endpoint": endpoint,
            "image_tag": image_tag,
            "started_at": datetime.now(UTC),
            "last_used_at": datetime.now(UTC),
        }
        self._runners[tenant] = entry
        logger.info("runner ready: tenant=%s endpoint=%s image=%s", tenant, endpoint, image_tag)
        return dict(entry)

    async def reap_idle(self, *, now: datetime | None = None) -> list[str]:
        moment = now or datetime.now(UTC)
        reclaimed: list[str] = []
        for tenant, entry in list(self._runners.items()):
            idle_for = (moment - entry["last_used_at"]).total_seconds()
            if idle_for < self.settings.idle_seconds:
                continue
            await self._remove_container(entry["container_id"])
            self._runners.pop(tenant, None)
            reclaimed.append(tenant)
            logger.info("runner reclaimed: tenant=%s idle=%.0fs", tenant, idle_for)
        return reclaimed

    async def stop_runner(self, tenant_id: str) -> bool:
        entry = self._runners.pop(tenant_id, None)
        if entry is None:
            return False
        await self._remove_container(entry["container_id"])
        return True

    async def _container_running(self, container_id: str) -> bool:
        if not container_id:
            return False
        result = await self.run_command(
            self.docker("inspect", "-f", "{{.State.Running}}", container_id)
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    async def _remove_container(self, reference: str) -> None:
        await self.run_command(self.docker("rm", "-f", reference))

    async def _published_port(self, name: str) -> str:
        result = await self.run_command(
            self.docker(
                "inspect",
                "-f",
                '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostPort}}',
                name,
            )
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(f"cannot determine published port for {name}")
        return result.stdout.strip().splitlines()[-1]

    async def _wait_ready(self, endpoint: str) -> None:
        deadline = (
            asyncio.get_running_loop().time() + self.settings.ready_timeout_seconds
        )
        last_error = "runner did not become ready"
        async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(f"{endpoint}/ready")
                    if response.status_code == 200:
                        return
                    last_error = f"runner /ready returned {response.status_code}"
                except Exception as exc:  # noqa: BLE001 - retry until the deadline
                    last_error = str(exc)
                await asyncio.sleep(1.0)
        raise RuntimeError(f"runner not ready at {endpoint}: {last_error}")


def _materialize_packages(staging: Path, cli_apps: list[dict[str, Any]]) -> None:
    """Write the uploaded CLI packages into ``<staging>/packages/<cli_id>/``."""

    packages_root = staging / "packages"
    for app in cli_apps:
        cli_id = _slug(str(app.get("cli_id") or ""))
        package = str(app.get("package") or "")
        payload = app.get("package_b64") or ""
        if not cli_id or not package:
            raise ValueError("cli app payload is missing its id or package name")
        if Path(package).name != package:
            raise ValueError(f"package must be a plain file name: {package}")
        target = packages_root / cli_id
        target.mkdir(parents=True, exist_ok=True)
        (target / package).write_bytes(base64.b64decode(payload))
        (target / "manifest.json").write_text(
            json.dumps(
                {
                    "cli_id": cli_id,
                    "entry": app.get("entry"),
                    "safe_prefixes": app.get("safe_prefixes") or [],
                    "env": app.get("env") or [],
                    "daemon": app.get("daemon") or None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


__all__ = [
    "CommandResult",
    "RunnerManager",
    "SubprocessCommandRunner",
    "render_dockerfile",
]

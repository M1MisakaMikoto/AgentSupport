from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

PROBE_TIMEOUT = 12.0
POLL_INTERVAL_SECONDS = 5.0
POLL_MAX_SECONDS = 30.0
EVENT_MAXLEN = 3000
JOURNAL_PATTERN = (
    "WaitForBootProcess|InitTerminateInstanceInternal|TerminateInstance|"
    "systemctl poweroff|Failed to schedule shutdown|Call to Reboot failed|"
    "Processing signal 'terminated'|Daemon shutdown"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_to_local_naive(value: datetime) -> str:
    local = value.astimezone().replace(tzinfo=None)
    return local.strftime("%Y-%m-%d %H:%M:%S")


class ControllerLike(Protocol):
    agentsupport_url: str

    def _docker(self, *arguments: str, target: Any = None) -> list[str]: ...

    def _target(self, request: Any = None) -> dict[str, str]: ...


class EnvironmentMonitor:
    """Continuously watches WSL / Docker / the compose stack and the API,
    records a persisted event timeline, and captures evidence whenever the
    environment tears down so the cause can be diagnosed after the fact."""

    def __init__(
        self,
        controller: ControllerLike,
        *,
        project_root: Path | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.controller = controller
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
        self.log_dir = Path(log_dir) if log_dir else self.project_root / ".tmp"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.log_dir / "agentsupport-monitor.jsonl"

        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_MAXLEN)
        self.started_at = _now()
        self.last_poll_at: datetime | None = None
        self.last_poll_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._journal_since_local: str | None = None
        self._journal_last_error: str | None = None
        self._wsl_boot: str | None = None
        self._wsl_available: bool | None = None
        self._docker_service_start: str | None = None
        self._container_states: dict[str, dict[str, str]] = {}
        self._api_ready: bool | None = None
        self._armed: dict[str, Any] | None = None
        self._counts: dict[str, int] = {
            "console_start": 0,
            "wsl_restart": 0,
            "wsl_unavailable": 0,
            "wsl_available": 0,
            "docker_restart": 0,
            "container_transition": 0,
            "api_up": 0,
            "api_down": 0,
            "incident": 0,
            "operation": 0,
            "system_log": 0,
            "probe_timeout": 0,
            "probe_error": 0,
        }
        self._load_persisted()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._poll(), timeout=POLL_MAX_SECONDS)
                self.last_poll_at = _now()
                self.last_poll_error = None
            except TimeoutError:
                self.last_poll_error = "poll cycle exceeded its time budget"
            except Exception as exc:  # noqa: BLE001 - the monitor must never die
                self.last_poll_error = str(exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    # ------------------------------------------------------------- events

    def record_event(
        self,
        event_type: str,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": f"{event_type}-{_now().timestamp():.6f}",
            "ts": _now().isoformat(),
            "type": event_type,
            "level": level,
            "message": message,
            "payload": payload or {},
        }
        self.events.appendleft(event)
        self._counts[event_type] = self._counts.get(event_type, 0) + 1
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return event

    def _load_persisted(self) -> None:
        if not self.jsonl_path.exists():
            return
        try:
            lines = self.jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        restored: list[dict[str, Any]] = []
        for line in lines[-EVENT_MAXLEN:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and "ts" in event:
                restored.append(event)
        self.events.extend(reversed(restored))

    def handle_operation_finished(self, operation: Any) -> None:
        status = getattr(operation, "status", "unknown")
        action = getattr(operation, "action", "unknown")
        error = getattr(operation, "error", None)
        self.record_event(
            "operation",
            "error" if status == "failed" else "info",
            f"操作 {action} {status}" + (f": {error}" if error else ""),
            {
                "operation_id": getattr(operation, "id", None),
                "action": action,
                "status": status,
                "error": error,
                "result": getattr(operation, "result", {}),
            },
        )
        if action in {"deploy", "start"} and status == "succeeded":
            self._armed = {
                "operation_id": getattr(operation, "id", None),
                "deployed_at": _now().isoformat(),
                "ready_observed": False,
            }
        elif action == "stop" and status == "succeeded":
            self._armed = None

    # ------------------------------------------------------------- probes

    async def _run_lines(self, command: list[str], timeout: float = PROBE_TIMEOUT) -> list[str]:
        runner = getattr(self.controller, "command_runner", None)
        if runner is not None:

            async def execute() -> list[str]:
                result = await runner.run(
                    command,
                    cwd=str(self.project_root),
                    env=None,
                    emit=lambda _stream, _message: None,
                )
                return list(result.stdout)

            try:
                return await asyncio.wait_for(execute(), timeout=timeout)
            except TimeoutError:
                self._counts["probe_timeout"] += 1
                raise

        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            self._counts["probe_timeout"] += 1
            raise
        if process.returncode not in (0, None):
            raise RuntimeError(f"command exited with {process.returncode}")
        text = stdout.decode(errors="replace")
        return [line.rstrip() for line in text.splitlines() if line.strip()]

    def _wsl_command(self, script: str) -> list[str]:
        target = self.controller._target(None)
        if target["transport"] != "wsl2":
            raise RuntimeError("WSL probe requested but Docker transport is not wsl2")
        command = ["wsl.exe"]
        if target["wsl_distribution"]:
            command.extend(["--distribution", target["wsl_distribution"]])
        command.extend(["bash", "-c", script])
        return command

    async def _wsl_snapshot(self) -> dict[str, Any]:
        script = "uptime -s; cat /proc/uptime; date +%Y-%m-%dT%H:%M:%S%z"
        lines = await self._run_lines(self._wsl_command(script))
        boot = lines[0] if lines else ""
        uptime_seconds = float(lines[1].split()[0]) if len(lines) > 1 else 0.0
        return {"boot": boot, "uptime_seconds": uptime_seconds}

    async def _docker_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"available": True}
        try:
            version = await self._run_lines(
                self.controller._docker("version", "--format", "{{.Server.Version}}")
            )
            snapshot["server_version"] = version[0] if version else "unknown"
        except Exception as exc:  # noqa: BLE001
            snapshot["available"] = False
            snapshot["error"] = str(exc)
            return snapshot
        if self.controller._target(None)["transport"] == "wsl2":
            try:
                started = await self._run_lines(
                    self._wsl_command(
                        "systemctl show docker --property=ActiveEnterTimestamp --value"
                    )
                )
                if started:
                    match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", started[0])
                    if match:
                        snapshot["service_started_at"] = match.group(0)
            except Exception:  # noqa: BLE001, S110 - systemd metadata is optional
                pass
        return snapshot

    async def _journal_scan(self) -> None:
        if self.controller._target(None)["transport"] != "wsl2":
            return
        now_local = await self._wsl_local_now()
        since = self._journal_since_local
        if not since:
            since = now_local
        try:
            lines = await self._run_lines(
                self._wsl_command(
                    f"journalctl --since '{since}' --no-pager 2>/dev/null | grep -E '{JOURNAL_PATTERN}' || true"
                ),
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            self._journal_last_error = str(exc)
            return
        self._journal_last_error = None
        self._journal_since_local = now_local
        if lines:
            self.record_event(
                "system_log",
                "warn",
                f"检测到 {len(lines)} 行 WSL/Docker 系统日志",
                {"lines": lines[-80:]},
            )

    async def _wsl_local_now(self) -> str:
        try:
            lines = await self._run_lines(
                self._wsl_command("date '+%Y-%m-%d %H:%M:%S'")
            )
            return lines[0] if lines else datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    async def _compose_ps(self) -> list[dict[str, str]]:
        result = await self._run_lines(
            self.controller._docker("compose", "ps", "--format", "json"),
            timeout=15,
        )
        raw = "\n".join(result).strip()
        records: list[dict[str, Any]] = []
        if raw:
            try:
                parsed = json.loads(raw)
                records = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                for line in result:
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return [
            {
                "name": item.get("Name", ""),
                "service": item.get("Service", ""),
                "state": item.get("State", "unknown"),
                "health": item.get("Health", ""),
                "status": item.get("Status", ""),
            }
            for item in records
        ]

    async def _api_probe(self) -> bool:
        try:
            async with httpx.AsyncClient(
                base_url=self.controller.agentsupport_url,
                timeout=2,
            ) as client:
                response = await client.get("/ready")
                if not response.is_success:
                    return False
                payload = response.json()
                return payload.get("status") == "ready"
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------- capture

    async def _container_logs(self, names: list[str], tail: int = 60) -> dict[str, list[str]]:
        captured: dict[str, list[str]] = {}
        for name in names:
            try:
                lines = await self._run_lines(
                    self.controller._docker("logs", "--tail", str(tail), name),
                    timeout=15,
                )
                captured[name] = lines[-tail:]
            except Exception as exc:  # noqa: BLE001
                captured[name] = [f"<log capture failed: {exc}>"]
        return captured

    def _console_logs(self, tail: int = 80) -> dict[str, list[str]]:
        captured: dict[str, list[str]] = {}
        for name in ("dev-console.out.log", "dev-console.err.log"):
            path = self.log_dir / name
            if not path.exists():
                captured[name] = []
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                captured[name] = lines[-tail:]
            except OSError as exc:
                captured[name] = [f"<read failed: {exc}>"]
        return captured

    async def _windows_event_tail(self, start_local: str, end_local: str, tail: int = 25) -> list[str]:
        script = (
            "$start=[datetime]::ParseExact('@@START@@','yyyy-MM-dd HH:mm:ss',"
            "[Globalization.CultureInfo]::InvariantCulture);"
            "$end=[datetime]::ParseExact('@@END@@','yyyy-MM-dd HH:mm:ss',"
            "[Globalization.CultureInfo]::InvariantCulture);"
            "Get-WinEvent -FilterHashtable @{LogName='System';StartTime=$start;EndTime=$end} "
            "-MaxEvents 500 -ErrorAction SilentlyContinue | Where-Object { "
            "$_.ProviderName -match 'WSL|Lxss|Hyper-V|Kernel-Power' -or $_.Id -in 1074,6006,6008,41,42 } | "
            "Select-Object -First @@TAIL@@ TimeCreated,Id,ProviderName,"
            "@{N='M';E={$_.Message.Substring(0,[Math]::Min(160,$_.Message.Length))}} | "
            "Format-Table -AutoSize | Out-String -Width 240"
        )
        script = (
            script.replace("@@START@@", start_local)
            .replace("@@END@@", end_local)
            .replace("@@TAIL@@", str(tail))
        )
        try:
            return await self._run_lines(
                ["powershell.exe", "-NoProfile", "-Command", script],
                timeout=25,
            )
        except Exception as exc:  # noqa: BLE001
            return [f"<windows event capture failed: {exc}>"]

    async def _capture_incident(
        self, reason: str, container_names: list[str]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": reason, "captured_at": _now().isoformat()}
        try:
            payload["containers"] = await self._compose_ps()
        except Exception as exc:  # noqa: BLE001
            payload["containers_error"] = str(exc)
        payload["logs"] = await self._container_logs(container_names, tail=80)
        payload["console"] = self._console_logs(tail=80)
        if self.controller._target(None)["transport"] == "wsl2":
            try:
                payload["journal"] = await self._run_lines(
                    self._wsl_command(
                        f"journalctl --no-pager 2>/dev/null | grep -E '{JOURNAL_PATTERN}' | tail -n 120 || true"
                    ),
                    timeout=15,
                )
            except Exception as exc:  # noqa: BLE001
                payload["journal_error"] = str(exc)
        return payload

    # ------------------------------------------------------------- poll

    async def _poll(self) -> None:
        containers: list[dict[str, str]] = []
        target = self.controller._target(None)

        # WSL availability + boot change detection
        wsl_available = False
        if target["transport"] == "wsl2":
            try:
                snapshot = await self._wsl_snapshot()
                wsl_available = True
                boot = snapshot["boot"]
                if self._wsl_boot and self._wsl_boot != boot:
                    self.record_event(
                        "wsl_restart",
                        "error",
                        f"WSL 实例重启：{self._wsl_boot} -> {boot}",
                        {
                            "previous_boot": self._wsl_boot,
                            "boot": boot,
                            "uptime_seconds": snapshot["uptime_seconds"],
                        },
                    )
                    try:
                        journal = await self._run_lines(
                            self._wsl_command(
                                "journalctl --since '"
                                + self._wsl_boot
                                + "' --no-pager 2>/dev/null | grep -E '"
                                + JOURNAL_PATTERN
                                + "' | tail -n 100 || true"
                            ),
                            timeout=15,
                        )
                        if journal:
                            self.events[0]["payload"]["journal"] = journal
                    except Exception:  # noqa: BLE001, S110
                        pass
                    try:
                        end_local = await self._wsl_local_now()
                        windows = await self._windows_event_tail(self._wsl_boot, end_local)
                        if windows:
                            self.events[0]["payload"]["windows_events"] = windows
                    except Exception:  # noqa: BLE001, S110
                        pass
                    self._journal_since_local = None
                self._wsl_boot = boot
                if self._wsl_available is False:
                    self.record_event("wsl_available", "info", "WSL 恢复可用")
                self._wsl_available = True
            except Exception as exc:  # noqa: BLE001
                if self._wsl_available is not False:
                    self.record_event(
                        "wsl_unavailable",
                        "error",
                        f"WSL 不可用：{exc}",
                    )
                self._wsl_available = False
        else:
            self._wsl_available = None

        # Docker daemon availability + restart detection
        try:
            docker = await self._docker_snapshot()
            service_start = docker.get("service_started_at")
            if service_start and self._docker_service_start and self._docker_service_start != service_start:
                self.record_event(
                    "docker_restart",
                    "error",
                    f"Docker 服务重启：{self._docker_service_start} -> {service_start}",
                    {"previous_start": self._docker_service_start, "started_at": service_start},
                )
            if service_start:
                self._docker_service_start = service_start
        except Exception:  # noqa: BLE001
            self._counts["probe_error"] += 1

        # Journal scan for WSL/Docker teardown evidence
        try:
            await self._journal_scan()
        except Exception as exc:  # noqa: BLE001
            self._journal_last_error = str(exc)

        # Compose stack state + container transitions
        try:
            containers = await self._compose_ps()
            current: dict[str, dict[str, str]] = {}
            for item in containers:
                name = item["name"]
                current[name] = item
                previous = self._container_states.get(name)
                if previous and (
                    previous.get("state") != item["state"]
                    or previous.get("status") != item["status"]
                ):
                    transition = f"{previous.get('state')} -> {item['state']}"
                    self.record_event(
                        "container_transition",
                        "error" if item["state"] not in {"running", "created"} else "info",
                        f"容器 {name} 状态变化：{transition}",
                        {"container": item, "previous": previous},
                    )
                    if item["state"] not in {"running", "created", "restarting"}:
                        logs = await self._container_logs([name], tail=60)
                        self.events[0]["payload"]["logs"] = logs
            self._container_states = current
        except Exception:  # noqa: BLE001
            self._counts["probe_error"] += 1

        # API readiness + unexpected teardown watchdog
        try:
            api_ready = await self._api_probe()
            if api_ready is True and self._api_ready is not True:
                self.record_event("api_up", "info", "API 恢复就绪")
            if api_ready is False and self._api_ready is True:
                self.record_event("api_down", "error", "API 不再就绪")
                if self._armed and self._armed.get("ready_observed"):
                    self.record_event(
                        "incident",
                        "error",
                        "部署后 API 意外下线（疑似环境被终止）",
                        await self._capture_incident(
                            "api_ready_lost_after_deploy",
                            [item["name"] for item in containers],
                        ),
                    )
                    self._armed = None
            self._api_ready = api_ready
            if self._armed and api_ready:
                self._armed["ready_observed"] = True
        except Exception:  # noqa: BLE001
            self._counts["probe_error"] += 1

        # Periodic lightweight snapshot so the timeline survives crashes
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "id": f"snapshot-{_now().timestamp():.6f}",
                            "ts": _now().isoformat(),
                            "type": "snapshot",
                            "level": "info",
                            "message": "monitor snapshot",
                            "payload": {
                                "wsl_boot": self._wsl_boot,
                                "wsl_available": wsl_available,
                                "docker_service_start": self._docker_service_start,
                                "api_ready": self._api_ready,
                                "containers": containers,
                                "armed": self._armed,
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    # ------------------------------------------------------------- API

    def overview(self) -> dict[str, Any]:
        wsl_info: dict[str, Any] = {"available": self._wsl_available}
        if self._wsl_boot:
            wsl_info["boot"] = self._wsl_boot
        docker_info: dict[str, Any] = {"service_started_at": self._docker_service_start}
        return {
            "monitor": {
                "started_at": self.started_at.isoformat(),
                "uptime_seconds": max(0, (_now() - self.started_at).total_seconds()),
                "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
                "last_poll_error": self.last_poll_error,
                "log_path": str(self.jsonl_path),
                "counts": dict(self._counts),
            },
            "wsl": wsl_info,
            "docker": docker_info,
            "api": {
                "ready": self._api_ready,
                "url": self.controller.agentsupport_url,
            },
            "armed": self._armed,
            "journal_last_error": self._journal_last_error,
            "containers": list(self._container_states.values()),
        }

    def event_list(self, limit: int = 300, event_type: str | None = None) -> list[dict[str, Any]]:
        items = list(self.events)
        if event_type == "-snapshot":
            items = [item for item in items if item.get("type") != "snapshot"]
        elif event_type:
            items = [item for item in items if item.get("type") == event_type]
        return items[:limit]

    async def container_logs(self, service: str, tail: int = 200) -> dict[str, Any]:
        try:
            containers = await self._compose_ps()
        except Exception as exc:  # noqa: BLE001
            return {"service": service, "error": str(exc), "containers": [], "lines": []}
        names = [item["name"] for item in containers if item.get("service") == service]
        if not names:
            return {
                "service": service,
                "error": f"没有找到 service={service} 的容器",
                "containers": [item["name"] for item in containers],
                "lines": [],
            }
        captured = await self._container_logs(names, tail=tail)
        lines: list[str] = []
        for name in names:
            lines.extend([f"[{name}] {line}" for line in captured.get(name, [])])
        return {"service": service, "containers": names, "lines": lines, "error": None}

    async def journal_tail(self, lines: int = 150, kind: str = "wsl") -> dict[str, Any]:
        if self.controller._target(None)["transport"] != "wsl2":
            return {"available": False, "error": "Docker transport 不是 wsl2", "lines": []}
        if kind == "wsl":
            script = (
                f"journalctl --no-pager 2>/dev/null | grep -E '{JOURNAL_PATTERN}' | tail -n {lines} || true"
            )
        else:
            script = f"journalctl --no-pager 2>/dev/null | tail -n {lines}"
        try:
            result = await self._run_lines(self._wsl_command(script), timeout=15)
            return {"available": True, "kind": kind, "lines": result, "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"available": True, "kind": kind, "lines": [], "error": str(exc)}

    def console_logs(self, lines: int = 120) -> dict[str, Any]:
        return {"lines": lines, **self._console_logs(tail=lines)}

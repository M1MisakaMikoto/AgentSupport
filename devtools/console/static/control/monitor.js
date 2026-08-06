(function () {
  "use strict";

  const app = window.ConsoleApp;
  if (!app) return;

  const els = app.els([
    "monitor-auto", "monitor-refresh-btn", "monitor-armed",
    "mon-wsl-state", "mon-wsl-boot", "mon-docker-state", "mon-docker-start",
    "mon-api-state", "mon-api-url", "mon-uptime", "mon-counts",
    "mon-stack-target", "mon-container-list",
    "mon-log-service", "mon-log-lines", "mon-log-refresh", "mon-log-view",
    "mon-journal-kind", "mon-journal-refresh", "mon-journal-view",
    "mon-console-refresh", "mon-console-view",
    "mon-event-count", "mon-events",
  ]);

  const typeChip = {
    incident: ["error", "INCIDENT"],
    wsl_restart: ["error", "WSL 重启"],
    wsl_unavailable: ["error", "WSL 不可用"],
    wsl_available: ["ok", "WSL 恢复"],
    docker_restart: ["error", "Docker 重启"],
    api_down: ["error", "API 下线"],
    api_up: ["ok", "API 恢复"],
    container_transition: ["warn", "容器变化"],
    system_log: ["warn", "系统日志"],
    operation: ["blue", "操作"],
    console_start: ["gray", "控制台"],
  };

  function deployTargetQuery() {
    const transport = document.getElementById("docker-transport")?.value || "auto";
    const context = document.getElementById("docker-context")?.value.trim() || "";
    const distribution = document.getElementById("wsl-distribution")?.value.trim() || "";
    return new URLSearchParams({ docker_transport: transport, docker_context: context, wsl_distribution: distribution }).toString();
  }

  function serviceState(service) {
    const raw = `${service.state || ""} ${service.health || ""} ${service.status || ""}`.toLowerCase();
    if (raw.includes("unhealthy") || raw.includes("exit") || raw.includes("dead")) return "error";
    if (raw.includes("starting") || raw.includes("created")) return "warn";
    if (raw.includes("running") || raw.includes("healthy") || raw.includes("up")) return "ok";
    return "gray";
  }

  function renderOverview(payload) {
    const monitor = payload.monitor || {};
    const wsl = payload.wsl || {};
    const docker = payload.docker || {};
    const api = payload.api || {};

    if (wsl.available) {
      els["mon-wsl-state"].textContent = "运行中";
      els["mon-wsl-state"].className = "value ok";
      els["mon-wsl-boot"].textContent = `启动 ${wsl.boot || "-"}`;
    } else {
      els["mon-wsl-state"].textContent = "不可用";
      els["mon-wsl-state"].className = "value error";
      els["mon-wsl-boot"].textContent = wsl.boot || "-";
    }

    if (docker.service_started_at) {
      els["mon-docker-state"].textContent = "运行中";
      els["mon-docker-state"].className = "value ok";
      els["mon-docker-start"].textContent = `服务启动 ${docker.service_started_at}`;
    } else {
      els["mon-docker-state"].textContent = "状态未知";
      els["mon-docker-state"].className = "value";
      els["mon-docker-start"].textContent = docker.error || "-";
    }

    if (api.ready === true) {
      els["mon-api-state"].textContent = "READY";
      els["mon-api-state"].className = "value ok";
    } else if (api.ready === false) {
      els["mon-api-state"].textContent = "DOWN";
      els["mon-api-state"].className = "value error";
    } else {
      els["mon-api-state"].textContent = "未知";
      els["mon-api-state"].className = "value";
    }
    els["mon-api-url"].textContent = api.url || "-";

    if (monitor.started_at) {
      const seconds = Math.max(0, Math.floor(monitor.uptime_seconds || 0));
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      els["mon-uptime"].textContent = h > 0 ? `${h}h ${m}m` : `${m}m`;
    } else {
      els["mon-uptime"].textContent = "-";
    }
    const counts = monitor.counts || {};
    const totalEvents = [
      "console_start", "wsl_restart", "wsl_unavailable", "wsl_available",
      "docker_restart", "api_down", "api_up", "container_transition",
      "system_log", "operation", "incident",
    ].reduce((sum, key) => sum + (counts[key] || 0), 0);
    els["mon-counts"].textContent = [
      `重启 ${counts.wsl_restart || 0}`,
      `Docker 重启 ${counts.docker_restart || 0}`,
      `下线 ${counts.api_down || 0}`,
      `事件 ${totalEvents}`,
    ].join(" · ");

    if (payload.armed) {
      els["monitor-armed"].style.display = "block";
      els["monitor-armed"].innerHTML =
        `<strong>部署守望已启用</strong> — 操作 <code>${app.escapeHtml(payload.armed.operation_id || "-")}</code> 于 ${app.escapeHtml(payload.armed.deployed_at || "-")} 完成，` +
        "若 API 之后意外下线，将自动采集容器日志、系统日志与 Windows 事件。";
    } else {
      els["monitor-armed"].style.display = "none";
    }

    renderContainers(payload.containers || []);
    renderLogServiceOptions(payload.containers || []);
  }

  function renderContainers(containers) {
    els["mon-container-list"].replaceChildren();
    if (!containers.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "没有 Compose 容器（栈未启动或 Docker 不可用）";
      els["mon-container-list"].append(empty);
      return;
    }
    containers
      .slice()
      .sort((a, b) => (a.service || "").localeCompare(b.service || "") || (a.name || "").localeCompare(b.name || ""))
      .forEach((service) => {
        const row = document.createElement("div");
        row.className = "service-row";
        const state = serviceState(service);
        const tone = state === "ok" ? "ok" : state === "error" ? "error" : "warn";
        row.innerHTML = `
          <span class="icon" style="background:var(--${tone}-soft); color:var(--${tone});">${app.escapeHtml((service.service || "?").slice(0, 2).toUpperCase())}</span>
          <div class="info"><div class="name">${app.escapeHtml(service.service || "-")}</div><div class="id">${app.escapeHtml(service.name || "-")}</div></div>
          <span class="chip ${state}">${app.escapeHtml(service.health || service.state || "unknown")}</span>
        `;
        row.addEventListener("click", () => {
          els["mon-log-service"].value = service.service || "";
          loadContainerLogs();
        });
        els["mon-container-list"].append(row);
      });
  }

  function renderLogServiceOptions(containers) {
    const services = [...new Set(containers.map((item) => item.service).filter(Boolean))].sort();
    const current = els["mon-log-service"].value;
    els["mon-log-service"].replaceChildren();
    services.forEach((service) => {
      const option = document.createElement("option");
      option.value = service;
      option.textContent = service;
      els["mon-log-service"].append(option);
    });
    if (services.includes(current)) {
      els["mon-log-service"].value = current;
    }
  }

  function renderEvents(payload) {
    const events = payload.events || [];
    els["mon-event-count"].textContent = `${events.length} 条最近事件`;
    els["mon-events"].replaceChildren();
    if (!events.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无事件";
      els["mon-events"].append(empty);
      return;
    }
    events.forEach((event) => {
      const [chipClass, chipText] = typeChip[event.type] || ["gray", event.type || "事件"];
      const row = document.createElement("details");
      row.className = "monitor-event";
      row.style.borderBottom = "1px solid var(--border)";
      row.style.padding = "8px 2px";
      const time = new Date(event.ts).toLocaleTimeString([], { hour12: false });
      const summary = document.createElement("summary");
      summary.style.cssText = "display:flex; gap:10px; align-items:center; cursor:pointer;";
      summary.innerHTML = `
        <span class="mono" style="color:var(--faint); font-size:11.5px;">${time}</span>
        <span class="chip ${chipClass}">${chipText}</span>
        <span style="flex:1; font-size:13px;">${app.escapeHtml(event.message)}</span>
      `;
      row.append(summary);
      if (event.payload && Object.keys(event.payload).length) {
        const pre = document.createElement("pre");
        pre.className = "terminal-output";
        pre.style.cssText = "margin:8px 0 0 24px; max-height:240px; overflow:auto;";
        pre.textContent = app.formatJson(event.payload);
        row.append(pre);
      }
      els["mon-events"].append(row);
    });
  }

  async function loadContainerLogs() {
    const service = els["mon-log-service"].value;
    const tail = Number(els["mon-log-lines"].value || 300);
    if (!service) {
      els["mon-log-view"].textContent = "// 请先选择一个服务";
      return;
    }
    els["mon-log-view"].textContent = "// 加载中…";
    try {
      const result = await app.consoleRequest(`/api/monitor/logs?service=${encodeURIComponent(service)}&tail=${tail}`);
      if (!result.ok) throw new Error(app.apiError(result, "读取容器日志失败"));
      const payload = result.body;
      els["mon-log-view"].textContent =
        `// service=${payload.service} containers=[${(payload.containers || []).join(", ")}]\n` +
        (payload.error ? `// ${payload.error}\n` : "") +
        (payload.lines || []).join("\n");
      els["mon-log-view"].scrollTop = els["mon-log-view"].scrollHeight;
    } catch (error) {
      els["mon-log-view"].textContent = `// ${error.message}`;
    }
  }

  async function loadJournal() {
    const kind = els["mon-journal-kind"].value;
    els["mon-journal-view"].textContent = "// 加载中…";
    try {
      const result = await app.consoleRequest(`/api/monitor/journal?kind=${encodeURIComponent(kind)}&lines=200`);
      if (!result.ok) throw new Error(app.apiError(result, "读取系统日志失败"));
      const payload = result.body;
      els["mon-journal-view"].textContent =
        (payload.error ? `// ${payload.error}\n` : "") +
        (payload.lines || []).join("\n") ||
        "// 无匹配日志";
    } catch (error) {
      els["mon-journal-view"].textContent = `// ${error.message}`;
    }
  }

  async function loadConsoleLogs() {
    els["mon-console-view"].textContent = "// 加载中…";
    try {
      const result = await app.consoleRequest("/api/monitor/console?lines=120");
      if (!result.ok) throw new Error(app.apiError(result, "读取控制台日志失败"));
      const payload = result.body;
      const blocks = Object.entries(payload)
        .filter(([key]) => key.endsWith(".log"))
        .map(([key, lines]) => `// ===== ${key} =====\n${(lines || []).join("\n")}`)
        .join("\n\n");
      els["mon-console-view"].textContent = blocks || "// 无日志";
      els["mon-console-view"].scrollTop = els["mon-console-view"].scrollHeight;
    } catch (error) {
      els["mon-console-view"].textContent = `// ${error.message}`;
    }
  }

  async function refreshOverview() {
    try {
      const result = await app.consoleRequest("/api/monitor/overview");
      if (!result.ok) throw new Error(app.apiError(result, "读取监控概览失败"));
      renderOverview(result.body);
    } catch (error) {
      els["mon-wsl-state"].textContent = "读取失败";
      els["mon-wsl-state"].className = "value error";
      els["mon-counts"].textContent = error.message;
    }
  }

  async function refreshEvents() {
    try {
      const result = await app.consoleRequest("/api/monitor/events?limit=200");
      if (!result.ok) throw new Error(app.apiError(result, "读取事件时间线失败"));
      renderEvents(result.body);
    } catch (error) {
      els["mon-events"].textContent = error.message;
    }
  }

  let autoTimer = null;

  function init() {
    els["monitor-refresh-btn"].addEventListener("click", () => {
      refreshOverview();
      refreshEvents();
    });
    els["mon-log-refresh"].addEventListener("click", loadContainerLogs);
    els["mon-log-service"].addEventListener("change", loadContainerLogs);
    els["mon-journal-refresh"].addEventListener("click", loadJournal);
    els["mon-journal-kind"].addEventListener("change", loadJournal);
    els["mon-console-refresh"].addEventListener("click", loadConsoleLogs);
    clearInterval(autoTimer);
    autoTimer = setInterval(() => {
      const view = document.getElementById("view-monitor");
      if (view && view.classList.contains("active") && els["monitor-auto"].checked) {
        refreshOverview();
        refreshEvents();
      }
    }, 5000);
  }

  async function refresh() {
    await refreshOverview();
    await refreshEvents();
  }

  app.registerView("monitor", { init, refresh });
})();

const token = document.querySelector('meta[name="dev-console-token"]').content;

const state = {
  activeOperation: localStorage.getItem("agentSupportControlOperation"),
  operationTimer: null,
  statusTimer: null,
  busy: false,
};

const elements = Object.fromEntries(
  [
    "stack-dot", "stack-label", "refresh-status", "stack-form", "api-replicas",
    "worker-replicas", "rebuild-image", "deploy-button", "start-button", "stop-button",
    "include-postgres", "include-task", "accept-button", "operation-title",
    "operation-status", "step-track", "terminal-output", "copy-log", "api-count",
    "worker-count", "queue-depth", "runtime-count", "service-list", "result-time",
    "result-list", "toast",
  ].map((id) => [id, document.getElementById(id)])
);

function showToast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.className = `toast visible${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { elements.toast.className = "toast"; }, 3200);
}

async function request(path, options = {}) {
  const headers = {
    Accept: "application/json",
    "X-Dev-Console-Token": token,
    ...(options.headers || {}),
  };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) {
    throw new Error(body?.detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

function runnerMode() {
  return document.querySelector('input[name="runner-mode"]:checked').value;
}

function stackPayload(rebuild = elements["rebuild-image"].checked) {
  return {
    api_replicas: Number(elements["api-replicas"].value),
    worker_replicas: Number(elements["worker-replicas"].value),
    runner_mode: runnerMode(),
    rebuild,
  };
}

function setBusy(busy) {
  state.busy = busy;
  ["deploy-button", "start-button", "stop-button", "accept-button"].forEach((id) => {
    elements[id].disabled = busy;
  });
  elements["operation-status"].classList.toggle("pulse", busy);
}

function serviceState(service) {
  const raw = `${service.state} ${service.health} ${service.status}`.toLowerCase();
  if (raw.includes("unhealthy") || raw.includes("exit") || raw.includes("dead")) return "error";
  if (raw.includes("starting") || raw.includes("created")) return "starting";
  if (raw.includes("running") || raw.includes("healthy") || raw.includes("up")) return "ok";
  return "neutral";
}

function renderServices(payload) {
  const services = payload.services || [];
  elements["service-list"].replaceChildren();
  if (!services.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="3" class="empty-cell">服务未运行</td>';
    elements["service-list"].append(row);
  } else {
    services
      .sort((a, b) => a.service.localeCompare(b.service) || a.name.localeCompare(b.name))
      .forEach((service) => {
        const row = document.createElement("tr");
        const status = serviceState(service);
        row.innerHTML = `
          <td><strong>${escapeHtml(service.service || "-")}</strong></td>
          <td class="mono-value">${escapeHtml(service.name || "-")}</td>
          <td><span class="service-state ${status}"><i></i>${escapeHtml(service.health || service.state || "unknown")}</span></td>
        `;
        elements["service-list"].append(row);
      });
  }

  const running = services.filter((item) => serviceState(item) === "ok");
  elements["api-count"].textContent = running.filter((item) => item.service === "api").length;
  elements["worker-count"].textContent = running.filter((item) => item.service === "worker").length;
  elements["queue-depth"].textContent = payload.metrics?.queue_ready ?? "-";
  elements["runtime-count"].textContent = payload.metrics?.active_runtimes ?? "-";

  const ready = payload.ready?.status === "ready";
  elements["stack-dot"].className = `status-dot ${ready ? "ok" : services.length ? "starting" : "neutral"}`;
  elements["stack-label"].textContent = ready
    ? `分布式服务已就绪 · ${payload.ready.instance_id}`
    : payload.error || (services.length ? "服务正在启动" : "服务未运行");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function refreshStatus(silent = false) {
  try {
    renderServices(await request("/api/status"));
  } catch (error) {
    elements["stack-dot"].className = "status-dot error";
    elements["stack-label"].textContent = "无法读取服务状态";
    if (!silent) showToast(error.message, true);
  }
}

const actionNames = {
  deploy: "部署分布式服务",
  start: "启动分布式服务",
  stop: "停止分布式服务",
  accept: "执行完整验收",
};

function renderSteps(operation) {
  elements["step-track"].replaceChildren();
  if (!operation.steps.length) {
    const row = document.createElement("li");
    row.className = "step-row running";
    row.innerHTML = '<span class="step-marker">·</span><div><strong>准备操作</strong><small>等待第一个步骤</small></div>';
    elements["step-track"].append(row);
    return;
  }
  operation.steps.forEach((step) => {
    const row = document.createElement("li");
    row.className = `step-row ${step.status}`;
    const marker = step.status === "succeeded" ? "✓" : step.status === "failed" ? "!" : "·";
    const finished = step.finished_at ? new Date(step.finished_at).toLocaleTimeString() : "进行中";
    row.innerHTML = `<span class="step-marker">${marker}</span><div><strong>${escapeHtml(step.name)}</strong><small>${finished}</small></div>`;
    elements["step-track"].append(row);
  });
}

function renderLogs(operation) {
  const terminal = elements["terminal-output"];
  const nearBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 48;
  terminal.textContent = operation.logs
    .map((line) => {
      const time = new Date(line.at).toLocaleTimeString([], { hour12: false });
      const prefix = line.stream === "command" ? "$" : line.stream === "stderr" ? "!" : line.stream === "step" ? "→" : " ";
      return `${time} ${prefix} ${line.message}`;
    })
    .join("\n") || "$ 等待命令";
  if (nearBottom) terminal.scrollTop = terminal.scrollHeight;
}

function renderResult(operation) {
  if (operation.action !== "accept" || operation.status !== "succeeded") return;
  const result = operation.result || {};
  elements["result-time"].textContent = new Date(operation.finished_at).toLocaleTimeString();
  elements["result-list"].innerHTML = `
    <div><dt>API 路由</dt><dd class="pass">${(result.api_instances || []).length} 个实例</dd></div>
    <div><dt>运行指标</dt><dd class="pass">${(result.metrics || []).length} 项</dd></div>
    <div><dt>任务闭环</dt><dd class="${result.conversation_id ? "pass" : "skip"}">${result.conversation_id ? "已完成" : "已跳过"}</dd></div>
  `;
}

function renderOperation(operation) {
  elements["operation-title"].textContent = actionNames[operation.action] || operation.action;
  elements["operation-status"].textContent = operation.status.toUpperCase();
  elements["operation-status"].className = `status-badge ${operation.status}`;
  renderSteps(operation);
  renderLogs(operation);
  renderResult(operation);
  setBusy(["queued", "running"].includes(operation.status));
}

async function pollOperation() {
  clearTimeout(state.operationTimer);
  if (!state.activeOperation) return;
  try {
    const operation = await request(`/api/operations/${state.activeOperation}`);
    renderOperation(operation);
    if (["queued", "running"].includes(operation.status)) {
      state.operationTimer = setTimeout(pollOperation, 700);
      return;
    }
    localStorage.removeItem("agentSupportControlOperation");
    state.activeOperation = null;
    await refreshStatus(true);
    showToast(operation.status === "succeeded" ? "操作已完成" : operation.error || "操作失败", operation.status === "failed");
  } catch (error) {
    setBusy(false);
    showToast(error.message, true);
  }
}

async function beginAction(action, payload) {
  if (state.busy) return;
  setBusy(true);
  try {
    const operation = await request(`/api/actions/${action}`, {
      method: "POST",
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    state.activeOperation = operation.id;
    localStorage.setItem("agentSupportControlOperation", operation.id);
    renderOperation(operation);
    pollOperation();
  } catch (error) {
    setBusy(false);
    showToast(error.message, true);
  }
}

elements["stack-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  beginAction("deploy", stackPayload());
});

elements["start-button"].addEventListener("click", () => beginAction("start", stackPayload(false)));
elements["stop-button"].addEventListener("click", () => {
  if (window.confirm("停止全部 Compose 服务并保留数据卷？")) beginAction("stop");
});
elements["accept-button"].addEventListener("click", () => beginAction("accept", {
  expected_api_replicas: Number(elements["api-replicas"].value),
  include_postgres: elements["include-postgres"].checked,
  include_task_smoke: elements["include-task"].checked,
}));
elements["refresh-status"].addEventListener("click", () => refreshStatus());
elements["copy-log"].addEventListener("click", async () => {
  await navigator.clipboard.writeText(elements["terminal-output"].textContent);
  showToast("日志已复制");
});

refreshStatus(true);
if (state.activeOperation) pollOperation();
state.statusTimer = setInterval(() => refreshStatus(true), 5000);

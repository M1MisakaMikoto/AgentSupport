const terminalStates = new Set(["COMPLETED", "FAILED", "CANCELLED", "LOST"]);

const state = {
  workspace: null,
  session: null,
  conversation: null,
  events: [],
  stream: null,
  reconnectTimer: null,
  busy: false,
};

const elements = Object.fromEntries(
  [
    "api-base", "connect-button", "api-dot", "api-status", "stream-status",
    "run-form", "workspace-name", "task-text", "start-button", "clear-button",
    "workspace-id", "session-id", "conversation-id", "event-count", "refresh-button",
    "cancel-button", "run-state", "last-seq", "run-id", "event-empty", "event-list",
    "interaction-empty", "interaction-content", "interaction-kind", "interaction-id",
    "approval-view", "batch-hash", "tool-calls", "reject-button", "approve-button",
    "input-form", "interaction-value", "raw-output", "copy-button", "toast",
    "control-console-link", "continue-form", "continue-text", "continue-button", "parent-id",
  ].map((id) => [id, document.getElementById(id)])
);

function defaultTask() {
  return "在工作区创建 MANUAL-ACCEPTANCE.txt，内容精确为 MANUAL-ACCEPTANCE-OK，然后读取并确认内容。不要执行其他任务。";
}

function makeKey(scope) {
  return `debug-${scope}-${crypto.randomUUID()}`;
}

function apiBase() {
  return elements["api-base"].value.trim().replace(/\/$/, "");
}

function defaultApiBase() {
  return window.location.pathname.startsWith("/tasks")
    ? `${window.location.origin}/agentsupport`
    : window.location.origin;
}

function showToast(message, isError = false) {
  const toast = elements.toast;
  toast.textContent = message;
  toast.className = `toast visible${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.className = "toast"; }, 3200);
}

function setRaw(value) {
  elements["raw-output"].textContent = JSON.stringify(value ?? {}, null, 2);
}

async function request(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(`${apiBase()}${path}`, { ...options, headers });
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  setRaw(body);
  if (!response.ok) {
    const detail = body?.message || body?.detail || `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return body;
}

function persist() {
  localStorage.setItem("agentSupportDebug", JSON.stringify({
    apiBase: apiBase(),
    workspace: state.workspace,
    session: state.session,
    conversation: state.conversation,
  }));
}

function setBusy(busy) {
  state.busy = busy;
  elements["start-button"].disabled = busy;
  elements["continue-button"].disabled = busy || !state.conversation;
  elements["approve-button"].disabled = busy;
  elements["reject-button"].disabled = busy;
}

function updateResources() {
  elements["workspace-id"].textContent = state.workspace?.id || "-";
  elements["session-id"].textContent = state.session?.id || "-";
  elements["conversation-id"].textContent = state.conversation?.id || "-";
  elements["parent-id"].textContent = state.conversation?.parent_conversation_id || "-";
  elements["continue-button"].disabled = !state.conversation || state.busy;
  const run = state.conversation?.run;
  const runState = run?.state || "IDLE";
  elements["run-state"].textContent = runState;
  elements["run-state"].className = `state-value ${runState.toLowerCase()}`;
  elements["run-id"].textContent = run?.run_id || "-";
  elements["cancel-button"].disabled = !state.conversation || terminalStates.has(runState) || state.busy;
}

function eventClass(type) {
  if (type.includes("failed") || type.includes("lost") || type.includes("cancel")) return "failure";
  if (type === "run.completed") return "terminal";
  if (type.startsWith("tool.")) return "tool";
  if (type.includes("interaction") || type.includes("approval")) return "interaction";
  return "lifecycle";
}

function projectRunState() {
  if (!state.conversation?.run) return;
  let projected = state.conversation.run.state;
  for (const event of state.events) {
    if (event.type === "conversation.queued") projected = "QUEUED";
    if (event.type === "run.running") projected = "RUNNING";
    if (event.type === "interaction.requested") projected = "WAITING_INPUT";
    if (event.type === "interaction.input" || event.type === "approval.decided") projected = "RUNNING";
    if (event.type === "run.paused") projected = "PAUSED";
    if (event.type === "run.resuming") projected = "RESUMING";
    if (event.type === "run.completed") projected = "COMPLETED";
    if (event.type === "run.failed") projected = "FAILED";
    if (event.type === "run.cancelled") projected = "CANCELLED";
    if (event.type === "run.lost") projected = "LOST";
  }
  state.conversation.run.state = projected;
  state.conversation.run.last_seq = state.events.at(-1)?.seq || 0;
}

function renderEvents() {
  state.events.sort((a, b) => a.seq - b.seq);
  projectRunState();
  updateResources();
  elements["event-list"].replaceChildren();
  elements["event-empty"].hidden = state.events.length > 0;
  elements["event-count"].textContent = `${state.events.length} 条`;
  elements["last-seq"].textContent = state.events.at(-1)?.seq || 0;

  for (const event of state.events) {
    const item = document.createElement("li");
    item.className = `event-row ${eventClass(event.type)}`;
    const seq = document.createElement("span");
    seq.className = "event-seq";
    seq.textContent = String(event.seq).padStart(3, "0");
    const rail = document.createElement("span");
    rail.className = "rail";
    rail.setAttribute("aria-hidden", "true");
    const body = document.createElement("article");
    body.className = "event-body";
    const heading = document.createElement("div");
    heading.className = "event-title";
    const title = document.createElement("strong");
    title.textContent = event.type;
    const time = document.createElement("time");
    time.className = "event-time";
    time.textContent = new Date(event.occurred_at).toLocaleTimeString();
    const payload = document.createElement("pre");
    payload.className = "event-payload";
    payload.textContent = JSON.stringify(event.payload, null, 2);
    heading.append(title, time);
    body.append(heading, payload);
    item.append(seq, rail, body);
    elements["event-list"].append(item);
  }
  renderInteraction();
}

function unresolvedInteraction() {
  const decided = new Set(
    state.events
      .filter((event) => event.type === "approval.decided" || event.type === "interaction.input")
      .map((event) => event.payload.approval_id || event.payload.interaction_id)
  );
  return state.events
    .filter((event) => event.type === "interaction.requested")
    .reverse()
    .find((event) => !decided.has(event.payload.interaction_id));
}

function renderInteraction() {
  const event = unresolvedInteraction();
  const terminal = terminalStates.has(state.conversation?.run?.state);
  const visible = Boolean(event && !terminal);
  elements["interaction-empty"].hidden = visible;
  elements["interaction-content"].hidden = !visible;
  if (!visible) return;

  const interaction = event.payload;
  const isApproval = interaction.kind === "approval";
  elements["interaction-kind"].textContent = (interaction.kind || "input").toUpperCase();
  elements["interaction-id"].textContent = interaction.interaction_id;
  elements["approval-view"].hidden = !isApproval;
  elements["input-form"].hidden = isApproval;
  if (!isApproval) return;

  elements["batch-hash"].textContent = interaction.tool_batch_hash || "-";
  elements["tool-calls"].replaceChildren();
  const calls = interaction.tool_batch?.calls || interaction.pending_tool_calls || [];
  for (const call of calls) {
    const block = document.createElement("article");
    block.className = "tool-call";
    const name = document.createElement("div");
    name.className = "tool-name";
    name.textContent = call.name || "unknown tool";
    const args = document.createElement("pre");
    args.textContent = JSON.stringify(call.arguments || call, null, 2);
    block.append(name, args);
    elements["tool-calls"].append(block);
  }
}

function mergeEvent(event) {
  if (!state.events.some((existing) => existing.event_id === event.event_id)) {
    state.events.push(event);
    renderEvents();
  }
}

function closeStream() {
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  state.stream?.close();
  state.stream = null;
  elements["stream-status"].textContent = "事件流未连接";
}

function openStream() {
  closeStream();
  if (!state.conversation) return;
  const after = state.events.at(-1)?.seq || 0;
  const stream = new EventSource(
    `${apiBase()}/conversations/${state.conversation.id}/events/stream?after_seq=${after}`
  );
  state.stream = stream;
  elements["stream-status"].textContent = "事件流连接中";
  stream.onopen = () => { elements["stream-status"].textContent = "事件流已连接"; };
  stream.onmessage = (message) => {
    try { mergeEvent(JSON.parse(message.data)); } catch { showToast("事件数据无法解析", true); }
  };
  stream.onerror = () => {
    if (state.stream !== stream) return;
    stream.close();
    state.stream = null;
    elements["stream-status"].textContent = "事件流重连中";
    state.reconnectTimer = window.setTimeout(openStream, 1500);
  };
}

async function refreshEvents(reopen = false) {
  if (!state.conversation) return;
  const events = await request(`/conversations/${state.conversation.id}/events?after_seq=0`);
  state.events = Array.isArray(events) ? events : [];
  renderEvents();
  if (reopen) openStream();
}

async function checkConnection() {
  try {
    const health = await request("/live");
    elements["api-dot"].className = "status-dot ok";
    elements["api-status"].textContent = health.status === "ok" ? "API 正常" : "API 已响应";
    showToast("API 连接正常");
  } catch (error) {
    elements["api-dot"].className = "status-dot error";
    elements["api-status"].textContent = "API 不可用";
    showToast(error.message, true);
  }
}

async function startRun(event) {
  event.preventDefault();
  setBusy(true);
  closeStream();
  state.events = [];
  renderEvents();
  try {
    state.workspace = await request("/workspaces", {
      method: "POST",
      headers: { "Idempotency-Key": makeKey("workspace") },
      body: JSON.stringify({ name: elements["workspace-name"].value.trim() }),
    });
    updateResources();
    state.session = await request("/sessions", {
      method: "POST",
      headers: { "Idempotency-Key": makeKey("session") },
      body: JSON.stringify({ workspace_id: state.workspace.id }),
    });
    updateResources();
    state.conversation = await request(`/sessions/${state.session.id}/conversations`, {
      method: "POST",
      headers: { "Idempotency-Key": makeKey("conversation") },
      body: JSON.stringify({ task: elements["task-text"].value.trim() }),
    });
    persist();
    updateResources();
    await refreshEvents(true);
    showToast("验收任务已创建");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
    updateResources();
  }
}

async function continueRun(event) {
  event.preventDefault();
  if (!state.session || !state.conversation) {
    showToast("请先创建验收任务", true);
    return;
  }
  const content = elements["continue-text"].value.trim();
  if (!content) return;
  setBusy(true);
  closeStream();
  state.events = [];
  renderEvents();
  try {
    state.conversation = await request(`/sessions/${state.session.id}/conversations`, {
      method: "POST",
      headers: { "Idempotency-Key": makeKey("continue") },
      body: JSON.stringify({
        task: content,
        parent_conversation_id: state.conversation.id,
      }),
    });
    elements["continue-text"].value = "";
    persist();
    updateResources();
    await refreshEvents(true);
    showToast("已在当前会话创建新的继续任务");
  } catch (error) {
    showToast(error.message, true);
    await refreshEvents(true).catch(() => {});
  } finally {
    setBusy(false);
    updateResources();
  }
}

async function decide(decision) {
  const interaction = unresolvedInteraction()?.payload;
  if (!interaction) return;
  setBusy(true);
  try {
    state.conversation = await request(`/conversations/${state.conversation.id}/approval`, {
      method: "POST",
      headers: { "Idempotency-Key": makeKey(`approval-${interaction.interaction_id}`) },
      body: JSON.stringify({
        approval_id: interaction.interaction_id,
        decision,
        expected_seq: state.events.at(-1)?.seq || 0,
      }),
    });
    persist();
    updateResources();
    await refreshEvents(true);
    showToast(decision === "APPROVE_ONCE" ? "已批准本次工具调用" : "已拒绝工具调用");
  } catch (error) {
    showToast(error.message, true);
    await refreshEvents(true).catch(() => {});
  } finally {
    setBusy(false);
    updateResources();
  }
}

async function submitInput(event) {
  event.preventDefault();
  const interaction = unresolvedInteraction()?.payload;
  if (!interaction) return;
  setBusy(true);
  try {
    state.conversation = await request(`/conversations/${state.conversation.id}/input`, {
      method: "POST",
      headers: { "Idempotency-Key": makeKey(`input-${interaction.interaction_id}`) },
      body: JSON.stringify({
        interaction_id: interaction.interaction_id,
        value: elements["interaction-value"].value,
        expected_seq: state.events.at(-1)?.seq || 0,
      }),
    });
    elements["interaction-value"].value = "";
    persist();
    updateResources();
    await refreshEvents(true);
    showToast("回复已提交");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
    updateResources();
  }
}

async function cancelRun() {
  if (!state.conversation) return;
  setBusy(true);
  try {
    state.conversation = await request(`/conversations/${state.conversation.id}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": makeKey("cancel") },
      body: JSON.stringify({ expected_seq: state.events.at(-1)?.seq || 0 }),
    });
    persist();
    updateResources();
    await refreshEvents(true);
    showToast("运行已取消");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
    updateResources();
  }
}

function clearRun() {
  closeStream();
  state.workspace = null;
  state.session = null;
  state.conversation = null;
  state.events = [];
  elements["continue-text"].value = "";
  localStorage.removeItem("agentSupportDebug");
  setRaw({});
  updateResources();
  renderEvents();
}

async function copyRaw() {
  try {
    await navigator.clipboard.writeText(elements["raw-output"].textContent);
    showToast("JSON 已复制");
  } catch { showToast("浏览器未授予剪贴板权限", true); }
}

function restore() {
  const saved = JSON.parse(localStorage.getItem("agentSupportDebug") || "null");
  elements["api-base"].value = saved?.apiBase || defaultApiBase();
  elements["control-console-link"].href = window.location.pathname.startsWith("/tasks")
    ? "/"
    : "http://127.0.0.1:8010/";
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  elements["workspace-name"].value = `manual-${stamp}`;
  elements["task-text"].value = defaultTask();
  if (saved) {
    state.workspace = saved.workspace;
    state.session = saved.session;
    state.conversation = saved.conversation;
  }
  updateResources();
  if (state.conversation) refreshEvents(true).catch((error) => showToast(error.message, true));
}

elements["connect-button"].addEventListener("click", checkConnection);
elements["run-form"].addEventListener("submit", startRun);
elements["continue-form"].addEventListener("submit", continueRun);
elements["refresh-button"].addEventListener("click", () => refreshEvents(true).catch((error) => showToast(error.message, true)));
elements["approve-button"].addEventListener("click", () => decide("APPROVE_ONCE"));
elements["reject-button"].addEventListener("click", () => decide("REJECT"));
elements["input-form"].addEventListener("submit", submitInput);
elements["cancel-button"].addEventListener("click", cancelRun);
elements["clear-button"].addEventListener("click", clearRun);
elements["copy-button"].addEventListener("click", copyRaw);
elements["api-base"].addEventListener("change", () => { persist(); closeStream(); openStream(); });
window.addEventListener("beforeunload", closeStream);

restore();
checkConnection();

(function () {
  "use strict";

  const app = window.ConsoleApp;
  if (!app) return;

  const terminalStates = new Set(["COMPLETED", "FAILED", "CANCELLED", "LOST"]);

  const agent = {
    workspaceId: null,
    sessionId: null,
    conversationId: null,
    conversations: [],
    events: [],
    stream: null,
    reconnectTimer: null,
    busy: false,
  };

  const els = app.els([
    "stat-workspaces", "stat-sessions", "stat-conversations", "stat-active",
    "recent-refresh", "recent-empty", "recent-table", "recent-runs",
    "agent-empty-state", "agent-layout-wrap", "agent-workspace-select",
    "agent-session-select", "agent-new-session-btn", "agent-refresh-btn",
    "agent-conv-count", "agent-conv-empty", "agent-conv-list",
    "agent-chat-scroll", "agent-chat-empty", "agent-task-input", "agent-send-btn",
    "agent-run-chip", "agent-run-state", "agent-run-seq", "agent-run-id",
    "agent-event-empty", "agent-timeline", "agent-cancel-btn",
    "agent-interaction-empty", "agent-interaction", "agent-approval-view",
    "agent-batch-hash", "agent-tool-calls", "agent-reject-btn", "agent-approve-btn",
    "agent-input-form", "agent-interaction-id", "agent-input-value",
  ]);

  const makeKey = (scope) => `demo-${scope}-${app.uuid()}`;

  async function api(path, options = {}) {
    const result = await app.apiRequest(path, options);
    if (!result.ok) throw new Error(app.apiError(result, "API 请求失败"));
    return result.body;
  }

  const store = app.state.demo;
  store.workspaces = [];
  store.sessions = [];
  store.loadedAt = 0;

  async function loadData(force = false) {
    if (!force && store.loadedAt && Date.now() - store.loadedAt < 10000) return;
    const [workspaces, sessions] = await Promise.all([
      api("/workspaces"),
      api("/sessions"),
    ]);
    store.workspaces = Array.isArray(workspaces) ? workspaces : [];
    store.sessions = Array.isArray(sessions) ? sessions : [];
    store.loadedAt = Date.now();
  }

  async function conversationCount() {
    let total = 0;
    for (const session of store.sessions) {
      const conversations = await api(`/sessions/${session.id}/conversations`);
      total += conversations.length;
    }
    return total;
  }

  // ---------------------------------------------------------------- overview
  async function refreshOverview() {
    try {
      await loadData(true);
      els["stat-workspaces"].textContent = store.workspaces.length;
      els["stat-sessions"].textContent = store.sessions.length;
      const total = await conversationCount();
      els["stat-conversations"].textContent = total;
      els["stat-active"].textContent = store.sessions.filter(
        (session) => session.active_run_id
      ).length;
      await renderRecentRuns();
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  async function renderRecentRuns() {
    const rows = [];
    for (const session of store.sessions.slice(0, 6)) {
      let conversations = [];
      try {
        conversations = await api(`/sessions/${session.id}/conversations`);
      } catch {
        continue;
      }
      for (const conversation of conversations.slice(-2)) {
        rows.push({
          id: conversation.id,
          task: conversation.task,
          tenant: session.tenant_id || "-",
          state: conversation.run?.state || "UNKNOWN",
          created_at: conversation.created_at,
        });
      }
    }
    rows.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    const visible = rows.slice(0, 8);
    els["recent-empty"].hidden = visible.length > 0;
    els["recent-table"].hidden = visible.length === 0;
    els["recent-runs"].replaceChildren(
      ...visible.map((row) => {
        const tr = document.createElement("tr");
        const state = row.state;
        tr.innerHTML = `
          <td>${app.escapeHtml(row.task)}</td>
          <td><span class="chip gray">${app.escapeHtml(row.tenant)}</span></td>
          <td><span class="chip ${terminalStates.has(state) ? "" : "ok"}">${app.escapeHtml(state)}</span></td>
          <td class="mono">${app.escapeHtml(row.created_at || "")}</td>`;
        return tr;
      })
    );
  }

  // ---------------------------------------------------------------- agent
  function renderWorkspaceOptions() {
    const select = els["agent-workspace-select"];
    select.replaceChildren();
    for (const workspace of store.workspaces) {
      const option = document.createElement("option");
      option.value = workspace.id;
      option.textContent = `${workspace.name} (${workspace.id.slice(0, 8)})`;
      select.append(option);
    }
    const selected = store.workspaces.some(
      (workspace) => workspace.id === agent.workspaceId
    );
    if (!selected) agent.workspaceId = store.workspaces[0]?.id || null;
    select.value = agent.workspaceId || "";
  }

  function renderSessionOptions() {
    const select = els["agent-session-select"];
    const sessions = store.sessions.filter(
      (session) => session.workspace_id === agent.workspaceId
    );
    select.replaceChildren();
    for (const session of sessions) {
      const option = document.createElement("option");
      option.value = session.id;
      const labels = [session.tenant_id, session.user_id, session.project_id]
        .filter(Boolean)
        .join(" · ");
      option.textContent = labels ? `${labels} (${session.id.slice(0, 8)})` : session.id;
      select.append(option);
    }
    const selected = sessions.some((session) => session.id === agent.sessionId);
    if (!selected) agent.sessionId = sessions[0]?.id || null;
    select.value = agent.sessionId || "";
    els["agent-empty-state"].hidden = store.workspaces.length > 0;
    els["agent-layout-wrap"].hidden = store.workspaces.length === 0;
  }

  async function refreshAgent() {
    try {
      await loadData(true);
      renderWorkspaceOptions();
      renderSessionOptions();
      if (agent.sessionId) await selectConversation(agent.sessionId);
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  async function newSessionModal() {
    app.openFormModal({
      title: "新建会话",
      sub: "先创建 Workspace（显式创建），再创建带标签的 Session；业务实体由上游自行管理。",
      fields: [
        { name: "workspace_name", label: "Workspace 名称", required: true, placeholder: "例如：demo-shop" },
        { name: "tenant_id", label: "tenant_id（可选）", placeholder: "上游租户 ID" },
        { name: "user_id", label: "user_id（可选）", placeholder: "上游用户 ID" },
        { name: "project_id", label: "project_id（可选）", placeholder: "上游项目 ID" },
      ],
      submit: async (values) => {
        const workspace = await api("/workspaces", {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("workspace") },
          body: { name: values.workspace_name },
        });
        const session = await api("/sessions", {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("session") },
          body: {
            workspace_id: workspace.id,
            tenant_id: values.tenant_id || null,
            user_id: values.user_id || null,
            project_id: values.project_id || null,
          },
        });
        await loadData(true);
        agent.workspaceId = workspace.id;
        agent.sessionId = session.id;
        renderWorkspaceOptions();
        renderSessionOptions();
        await selectConversation(session.id);
        app.toast("Workspace 与 Session 已创建");
      },
    });
  }

  async function selectConversation(sessionId) {
    agent.sessionId = sessionId;
    els["agent-session-select"].value = sessionId || "";
    agent.conversations = [];
    agent.conversationId = null;
    agent.events = [];
    stopStream();
    renderConversationList();
    renderChat();
    renderRunState();
    renderTimeline();
    renderInteraction();
    if (!sessionId) return;
    try {
      const conversations = await api(`/sessions/${sessionId}/conversations`);
      agent.conversations = conversations;
      renderConversationList();
      if (conversations.length) {
        await openConversation(conversations[conversations.length - 1].id);
      }
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  function renderConversationList() {
    els["agent-conv-count"].textContent = agent.conversations.length;
    els["agent-conv-empty"].hidden = agent.conversations.length > 0;
    els["agent-conv-list"].replaceChildren(
      ...agent.conversations.map((conversation) => {
        const button = document.createElement("button");
        button.className =
          "conv-item" + (conversation.id === agent.conversationId ? " active" : "");
        button.innerHTML = `
          <div class="conv-task">${app.escapeHtml(conversation.task)}</div>
          <div class="conv-meta">${app.escapeHtml(conversation.run?.state || "")} · ${app.escapeHtml(conversation.created_at || "")}</div>`;
        button.addEventListener("click", () => openConversation(conversation.id));
        return button;
      })
    );
  }

  async function openConversation(conversationId) {
    agent.conversationId = conversationId;
    renderConversationList();
    stopStream();
    try {
      const conversation = await api(`/conversations/${conversationId}`);
      agent.events = [];
      renderChat();
      renderRunState(conversation.run);
      const events = await api(`/conversations/${conversationId}/events`);
      agent.events = events;
      renderChat();
      renderTimeline();
      renderInteraction();
      startStream(conversationId);
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  function startStream(conversationId) {
    stopStream();
    const controller = new AbortController();
    agent.stream = controller;
    const start = async () => {
      try {
        const response = await fetch(
          `${window.location.origin}/agentsupport/conversations/${conversationId}/events/stream`,
          { signal: controller.signal }
        );
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          for (const part of parts) {
            const line = part.split("\n").find((item) => item.startsWith("data:"));
            if (!line) continue;
            try {
              const event = JSON.parse(line.slice(5).trim());
              if (!agent.events.some((item) => item.event_id === event.event_id)) {
                agent.events.push(event);
              }
            } catch {
              // ignore malformed frames
            }
          }
          renderChat();
          renderTimeline();
          renderInteraction();
        }
      } catch {
        // stream closed
      }
    };
    start();
  }

  function stopStream() {
    if (agent.stream) {
      agent.stream.abort();
      agent.stream = null;
    }
    if (agent.reconnectTimer) {
      clearTimeout(agent.reconnectTimer);
      agent.reconnectTimer = null;
    }
  }

  function renderChat() {
    const messages = agent.events.filter(
      (event) => event.type === "message" || event.type === "run.completed" || event.type === "run.failed"
    );
    els["agent-chat-empty"].hidden = messages.length > 0;
    els["agent-chat-scroll"].replaceChildren(
      ...messages.map((event) => {
        const div = document.createElement("div");
        const payload = event.payload || {};
        const content = payload.content ?? payload.input ?? payload.result ?? event.type;
        div.className = "chat-message " + (event.type === "message" ? "user" : "system");
        div.innerHTML = `<div class="bubble">${app.escapeHtml(
          typeof content === "string" ? content : JSON.stringify(content)
        )}</div><div class="meta">${app.escapeHtml(event.type)} · #${event.seq}</div>`;
        return div;
      })
    );
    els["agent-chat-scroll"].scrollTop = els["agent-chat-scroll"].scrollHeight;
  }

  function currentRun() {
    const conversation = agent.conversations.find(
      (item) => item.id === agent.conversationId
    );
    return conversation?.run || null;
  }

  function renderRunState() {
    const run = currentRun();
    const state = run?.state || "IDLE";
    els["agent-run-state"].textContent = state;
    els["agent-run-seq"].textContent = run?.last_seq ?? agent.events.length || 0;
    els["agent-run-id"].textContent = run?.run_id || "-";
    els["agent-run-chip"].textContent = state;
    els["agent-run-chip"].className = `chip ${terminalStates.has(state) ? "gray" : "ok"}`;
    els["agent-cancel-btn"].disabled = !run || terminalStates.has(state);
  }

  function renderTimeline() {
    els["agent-event-empty"].hidden = agent.events.length > 0;
    els["agent-timeline"].hidden = agent.events.length === 0;
    els["agent-timeline"].replaceChildren(
      ...agent.events.slice(-40).map((event) => {
        const div = document.createElement("div");
        div.className = "tl-item";
        div.innerHTML = `<span class="tl-dot"></span>
          <div><code>${app.escapeHtml(event.type)}</code>
          <small>#${event.seq} · ${app.escapeHtml(event.source || "")}</small></div>`;
        return div;
      })
    );
    renderRunState();
  }

  function renderInteraction() {
    const latest = [...agent.events].reverse().find(
      (event) => event.type === "interaction.requested"
    );
    const pending = latest?.payload || null;
    const approval = pending?.kind === "approval" || pending?.approval_id;
    els["agent-interaction-empty"].hidden = !!pending;
    els["agent-interaction"].hidden = !pending;
    if (!pending) return;
    els["agent-approval-view"].hidden = !approval;
    els["agent-input-form"].hidden = approval;
    if (approval) {
      els["agent-batch-hash"].textContent = pending.tool_batch_hash || "-";
      const calls = pending.tool_batch?.calls || [];
      els["agent-tool-calls"].replaceChildren(
        ...calls.map((call) => {
          const span = document.createElement("code");
          span.textContent = `${call.name}(${JSON.stringify(call.arguments || {})})`;
          return span;
        })
      );
    } else {
      els["agent-interaction-id"].textContent = pending.interaction_id || "-";
    }
  }

  async function sendTask() {
    const task = els["agent-task-input"].value.trim();
    if (!task || !agent.sessionId || agent.busy) return;
    agent.busy = true;
    els["agent-send-btn"].disabled = true;
    try {
      const conversation = await api(`/sessions/${agent.sessionId}/conversations`, {
        method: "POST",
        headers: { "Idempotency-Key": makeKey("conversation") },
        body: { task },
      });
      els["agent-task-input"].value = "";
      agent.conversations.push(conversation);
      renderConversationList();
      await openConversation(conversation.id);
    } catch (error) {
      app.toast(error.message, true);
    } finally {
      agent.busy = false;
      els["agent-send-btn"].disabled = false;
    }
  }

  async function submitInput() {
    if (!agent.conversationId) return;
    const value = els["agent-input-value"].value.trim();
    if (!value) return;
    try {
      const interaction = [...agent.events].reverse().find(
        (event) => event.type === "interaction.requested"
      );
      const pending = interaction?.payload || {};
      await api(`/conversations/${agent.conversationId}/input`, {
        method: "POST",
        headers: { "Idempotency-Key": makeKey("input") },
        body: { interaction_id: pending.interaction_id, value, expected_seq: agent.events.length },
      });
      els["agent-input-value"].value = "";
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  async function submitApproval(decision) {
    if (!agent.conversationId) return;
    try {
      const interaction = [...agent.events].reverse().find(
        (event) => event.type === "interaction.requested"
      );
      const pending = interaction?.payload || {};
      await api(`/conversations/${agent.conversationId}/approval`, {
        method: "POST",
        headers: { "Idempotency-Key": makeKey("approval") },
        body: {
          approval_id: pending.approval_id || pending.interaction_id,
          decision,
          expected_seq: agent.events.length,
        },
      });
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  async function cancelRun() {
    if (!agent.conversationId) return;
    try {
      await api(`/conversations/${agent.conversationId}/cancel`, {
        method: "POST",
        headers: { "Idempotency-Key": makeKey("cancel") },
      });
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  function initAgent() {
    els["agent-new-session-btn"].addEventListener("click", newSessionModal);
    els["agent-refresh-btn"].addEventListener("click", refreshAgent);
    els["agent-send-btn"].addEventListener("click", sendTask);
    els["agent-cancel-btn"].addEventListener("click", cancelRun);
    els["agent-approve-btn"].addEventListener("click", () => submitApproval("APPROVE_ONCE"));
    els["agent-reject-btn"].addEventListener("click", () => submitApproval("REJECT"));
    els["agent-workspace-select"].addEventListener("change", () => {
      agent.workspaceId = els["agent-workspace-select"].value || null;
      agent.sessionId = null;
      renderSessionOptions();
      selectConversation(null);
    });
    els["agent-session-select"].addEventListener("change", () => {
      const sessionId = els["agent-session-select"].value || null;
      if (sessionId) selectConversation(sessionId);
    });
    els["agent-task-input"].addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendTask();
      }
    });
    document.querySelectorAll(".hint-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        els["agent-task-input"].value = chip.dataset.hint || "";
        els["agent-task-input"].focus();
      });
    });
  }

  app.registerView("demo-overview", { init: () => {}, refresh: refreshOverview });
  app.registerView("demo-agent", { init: initAgent, refresh: refreshAgent });
})();

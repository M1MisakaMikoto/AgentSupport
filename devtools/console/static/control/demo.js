(function () {
  "use strict";

  const app = window.ConsoleApp;
  if (!app) return;

  const terminalStates = new Set(["COMPLETED", "FAILED", "CANCELLED", "LOST"]);

  const agent = {
    projectId: null,
    sessionId: null,
    conversationId: null,
    conversations: [],
    events: [],
    stream: null,
    reconnectTimer: null,
    busy: false,
  };

  const els = app.els([
    "stat-orgs", "stat-users", "stat-projects", "stat-presets", "stat-active",
    "recent-refresh", "recent-empty", "recent-table", "recent-runs", "preset-overview",
    "new-org-btn", "org-empty", "org-table", "org-rows",
    "new-user-btn", "user-empty", "user-table", "user-rows",
    "new-project-btn", "project-empty", "project-grid",
    "new-preset-btn", "preset-empty", "preset-grid",
    "agent-empty-state", "agent-layout-wrap", "agent-project-select",
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

  async function loadData(force = false) {
    const store = app.state.demo;
    if (!force && store.loadedAt && Date.now() - store.loadedAt < 10000) return;
    if (force) projectStatusCache = {};
    const [orgs, users, projects, presets] = await Promise.all([
      api("/organizations"),
      api("/users"),
      api("/projects"),
      api("/presets"),
    ]);
    store.orgs = Array.isArray(orgs) ? orgs : [];
    store.users = Array.isArray(users) ? users : [];
    store.projects = Array.isArray(projects) ? projects : [];
    store.presets = Array.isArray(presets) ? presets : [];
    store.loadedAt = Date.now();
  }

  async function fetchMetrics() {
    const result = await app.apiRequest("/metrics");
    const metrics = {};
    for (const line of result.text.split("\n")) {
      const [name, value] = line.trim().split(/\s+/);
      if (name) metrics[name.replace(/^agentsupport_/, "")] = Number(value);
    }
    return metrics;
  }

  function userById(id) {
    return app.state.demo.users.find((user) => user.id === id) || null;
  }

  function orgById(id) {
    return app.state.demo.orgs.find((org) => org.id === id) || null;
  }

  function presetById(id) {
    return app.state.demo.presets.find((preset) => preset.id === id) || null;
  }

  // ================================================================ overview
  async function refreshOverview() {
    await loadData();
    const store = app.state.demo;
    els["stat-orgs"].textContent = store.orgs.length;
    els["stat-users"].textContent = store.users.length;
    els["stat-projects"].textContent = store.projects.length;
    els["stat-presets"].textContent = store.presets.length;
    try {
      const metrics = await fetchMetrics();
      els["stat-active"].textContent = metrics.active_runtimes ?? "-";
    } catch {
      els["stat-active"].textContent = "-";
    }
    els["preset-overview"].replaceChildren();
    if (!store.presets.length) {
      els["preset-overview"].innerHTML =
        '<div class="empty" style="padding:10px;">暂无预设</div>';
    } else {
      store.presets.slice(0, 4).forEach((preset) => {
        const definition = preset.definition || {};
        const tools = (definition.tool_policy?.allowed_tools || []).slice(0, 3).join(" · ");
        const line = document.createElement("div");
        line.className = "preset-line";
        line.innerHTML = `
          <span class="chip ${preset.definition?.enabled === false ? "gray" : "blue"}">${app.escapeHtml(preset.name)}</span>
          <span class="tag">${app.escapeHtml(tools || "默认工具集")}</span>
        `;
        els["preset-overview"].append(line);
      });
    }
    await renderRecentRuns();
  }

  async function collectRecentRuns(limit = 5) {
    const runs = [];
    const projects = app.state.demo.projects.slice(0, 6);
    for (const project of projects) {
      try {
        const sessions = await api(`/projects/${project.id}/sessions`);
        for (const session of sessions.slice(-3)) {
          try {
            const conversations = await api(`/sessions/${session.id}/conversations`);
            conversations.forEach((conversation) => {
              runs.push({
                task: conversation.task,
                project: project.name,
                state: conversation.run?.state,
                created_at: conversation.created_at,
                id: conversation.id,
              });
            });
          } catch {
            /* skip session */
          }
        }
      } catch {
        /* skip project */
      }
    }
    runs.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    return runs.slice(0, limit);
  }

  async function renderRecentRuns() {
    els["recent-table"].hidden = true;
    els["recent-empty"].hidden = false;
    try {
      const runs = await collectRecentRuns();
      els["recent-runs"].replaceChildren();
      if (!runs.length) return;
      els["recent-table"].hidden = false;
      els["recent-empty"].hidden = true;
      runs.forEach((run) => {
        const chip = app.runStateChip(run.state);
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><strong>${app.escapeHtml(run.task.slice(0, 42))}${run.task.length > 42 ? "…" : ""}</strong></td>
          <td>${app.escapeHtml(run.project)}</td>
          <td><span class="chip ${chip.cls}">${chip.text}</span></td>
          <td class="mono">${app.escapeHtml(app.relativeTime(run.created_at))}</td>
        `;
        els["recent-runs"].append(row);
      });
    } catch (error) {
      els["recent-empty"].textContent = `无法读取最近运行: ${error.message}`;
    }
  }

  // ================================================================ orgs
  async function refreshOrgs() {
    await loadData();
    const store = app.state.demo;
    els["org-rows"].replaceChildren();
    els["org-empty"].hidden = store.orgs.length > 0;
    els["org-table"].hidden = store.orgs.length === 0;
    store.orgs.forEach((org) => {
      const userIds = store.users.filter((user) => user.organization_id === org.id);
      const projectCount = store.projects.filter((project) =>
        userIds.some((user) => user.id === project.user_id)
      ).length;
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><strong>${app.escapeHtml(org.name)}</strong></td>
        <td class="mono">${app.escapeHtml(org.id)}</td>
        <td>${userIds.length}</td>
        <td>${projectCount}</td>
        <td class="mono">${app.escapeHtml(app.relativeTime(org.created_at))}</td>
        <td><div class="row-actions">
          <button class="btn sm" data-action="view" type="button">查看</button>
          <button class="btn sm" data-action="copy" type="button">复制 ID</button>
        </div></td>
      `;
      row.querySelector('[data-action="view"]').addEventListener("click", () => showOrgDetail(org));
      row.querySelector('[data-action="copy"]').addEventListener("click", () =>
        app.copyText(org.id, "组织 ID 已复制")
      );
      els["org-rows"].append(row);
    });
  }

  function showOrgDetail(org) {
    const store = app.state.demo;
    const users = store.users.filter((user) => user.organization_id === org.id);
    const projects = store.projects.filter((project) =>
      users.some((user) => user.id === project.user_id)
    );
    app.openDetailModal({
      title: `组织 · ${org.name}`,
      sections: [
        {
          html: app.detailListHtml([
            ["ID", org.id, true],
            ["名称", org.name],
            ["创建时间", org.created_at, true],
            ["用户数", users.length],
            ["项目数", projects.length],
          ]),
        },
        {
          heading: `用户 (${users.length})`,
          render: (wrap) => {
            if (!users.length) {
              wrap.innerHTML = '<div class="empty" style="padding:10px;">暂无用户</div>';
              return;
            }
            const table = document.createElement("table");
            table.innerHTML =
              "<thead><tr><th>用户名</th><th>ID</th></tr></thead>";
            const tbody = document.createElement("tbody");
            users.forEach((user) => {
              const row = document.createElement("tr");
              row.innerHTML = `<td><strong>${app.escapeHtml(user.username)}</strong></td><td class="mono">${app.escapeHtml(user.id)}</td>`;
              tbody.append(row);
            });
            table.append(tbody);
            wrap.append(table);
          },
        },
      ],
    });
  }

  function newOrgModal() {
    app.openFormModal({
      title: "新建组织",
      sub: "组织（租户）是资源归属的顶层单元。",
      fields: [
        { name: "name", label: "组织名称", required: true, placeholder: "例如：星辰科技" },
      ],
      submit: async (values) => {
        await api("/organizations", {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("org") },
          body: { name: values.name },
        });
        await loadData(true);
        refreshOrgs();
      },
    });
  }

  // ================================================================ users
  async function refreshUsers() {
    await loadData();
    const store = app.state.demo;
    els["user-rows"].replaceChildren();
    els["user-empty"].hidden = store.users.length > 0;
    els["user-table"].hidden = store.users.length === 0;
    store.users.forEach((user) => {
      const org = orgById(user.organization_id);
      const projects = store.projects.filter((project) => project.user_id === user.id);
      const presets = store.presets.filter((preset) => preset.user_id === user.id);
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><strong>${app.escapeHtml(user.username)}</strong></td>
        <td>${app.escapeHtml(org?.name || "默认组织")}</td>
        <td>${projects.length}</td>
        <td>${presets.length}</td>
        <td class="mono">${app.escapeHtml(app.relativeTime(user.created_at))}</td>
        <td><div class="row-actions">
          <button class="btn sm" data-action="view" type="button">查看</button>
          <button class="btn sm" data-action="copy" type="button">复制 ID</button>
        </div></td>
      `;
      row.querySelector('[data-action="view"]').addEventListener("click", () => showUserDetail(user));
      row.querySelector('[data-action="copy"]').addEventListener("click", () =>
        app.copyText(user.id, "用户 ID 已复制")
      );
      els["user-rows"].append(row);
    });
  }

  function showUserDetail(user) {
    const store = app.state.demo;
    const org = orgById(user.organization_id);
    const projects = store.projects.filter((project) => project.user_id === user.id);
    const presets = store.presets.filter((preset) => preset.user_id === user.id);
    app.openDetailModal({
      title: `用户 · ${user.username}`,
      sections: [
        {
          html: app.detailListHtml([
            ["ID", user.id, true],
            ["用户名", user.username],
            ["组织", org?.name || "默认组织"],
            ["创建时间", user.created_at, true],
            ["项目数", projects.length],
            ["预设数", presets.length],
          ]),
        },
        {
          heading: `项目 (${projects.length})`,
          render: (wrap) => {
            if (!projects.length) {
              wrap.innerHTML = '<div class="empty" style="padding:10px;">暂无项目</div>';
              return;
            }
            const list = document.createElement("div");
            list.className = "tag-row";
            projects.forEach((project) => {
              const tag = document.createElement("span");
              tag.className = "tag";
              tag.textContent = project.name;
              list.append(tag);
            });
            wrap.append(list);
          },
        },
        {
          heading: `预设 (${presets.length})`,
          render: (wrap) => {
            if (!presets.length) {
              wrap.innerHTML = '<div class="empty" style="padding:10px;">暂无预设</div>';
              return;
            }
            const list = document.createElement("div");
            list.className = "tag-row";
            presets.forEach((preset) => {
              const tag = document.createElement("span");
              tag.className = "tag";
              tag.textContent = preset.name;
              list.append(tag);
            });
            wrap.append(list);
          },
        },
      ],
    });
  }

  function newUserModal() {
    app.openFormModal({
      title: "新建用户",
      sub: "用户隶属于组织，拥有预设集合与项目。",
      fields: [
        { name: "username", label: "用户名", required: true, placeholder: "例如：alice" },
        {
          name: "organization_id",
          label: "所属组织",
          type: "select",
          options: [
            { value: "", label: "默认组织" },
            ...app.state.demo.orgs.map((org) => ({ value: org.id, label: org.name })),
          ],
        },
      ],
      submit: async (values) => {
        await api("/users", {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("user") },
          body: {
            username: values.username,
            organization_id: values.organization_id || null,
          },
        });
        await loadData(true);
        refreshUsers();
      },
    });
  }

  // ================================================================ projects
  let projectStatusCache = {};

  async function projectStatus(project) {
    if (projectStatusCache[project.id]) return projectStatusCache[project.id];
    const promise = (async () => {
      const sessions = await api(`/projects/${project.id}/sessions`);
      const states = [];
      for (const session of sessions.slice(-3)) {
        try {
          const conversations = await api(`/sessions/${session.id}/conversations`);
          conversations.forEach((conversation) => {
            if (conversation.run?.state) states.push(conversation.run.state);
          });
        } catch {
          /* skip */
        }
      }
      const active = states.find((state) =>
        ["RUNNING", "STARTING", "QUEUED", "WAITING_INPUT", "RESUMING"].includes(state)
      );
      return {
        sessions: sessions.length,
        state: active || states[0] || "IDLE",
      };
    })();
    projectStatusCache[project.id] = promise;
    return promise;
  }

  async function refreshProjects() {
    await loadData();
    const store = app.state.demo;
    els["project-grid"].replaceChildren();
    els["project-empty"].hidden = store.projects.length > 0;
    for (const project of store.projects) {
      const card = document.createElement("div");
      card.className = "entity-card";
      const user = userById(project.user_id);
      const org = user ? orgById(user.organization_id) : null;
      const preset = presetById(project.preset_id);
      card.innerHTML = `
        <div class="entity-top">
          <span class="entity-avatar" style="background:${avatarColor(project.name)};">${app.escapeHtml(project.name.slice(0, 1))}</span>
          <div><div class="entity-name">${app.escapeHtml(project.name)}</div><div class="entity-meta">${app.escapeHtml(user?.username || "-")} · ${app.escapeHtml(org?.name || "默认组织")}</div></div>
          <span class="chip gray" data-role="status" style="margin-left:auto;">读取中</span>
        </div>
        <div class="entity-desc">项目 ID: ${app.escapeHtml(project.id)}</div>
        <div class="tag-row">
          <span class="tag">预设: ${app.escapeHtml(preset?.name || "部署级默认")}</span>
          <span class="tag" data-role="sessions">会话 -</span>
        </div>
      `;
      card.addEventListener("click", () => showProjectDetail(project));
      els["project-grid"].append(card);
      projectStatus(project)
        .then((status) => {
          const chip = app.runStateChip(status.state);
          card.querySelector('[data-role="status"]').textContent = chip.text;
          card.querySelector('[data-role="status"]').className = `chip ${chip.cls}`;
          card.querySelector('[data-role="sessions"]').textContent = `会话 ${status.sessions}`;
        })
        .catch(() => {});
    }
  }

  function avatarColor(seed) {
    const colors = ["#4f6ef7", "#8b5cf6", "#17a673", "#d97706", "#0e9bb0", "#e05252"];
    let hash = 0;
    for (const char of seed) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    return colors[hash % colors.length];
  }

  function showProjectDetail(project) {
    const user = userById(project.user_id);
    const org = user ? orgById(user.organization_id) : null;
    const preset = presetById(project.preset_id);
    app.openDetailModal({
      title: `项目 · ${project.name}`,
      sections: [
        {
          html: app.detailListHtml([
            ["ID", project.id, true],
            ["所属用户", user?.username || "-"],
            ["组织", org?.name || "默认组织"],
            ["预设", preset?.name || "部署级默认"],
            ["Workspace", project.workspace_id, true],
            ["创建时间", project.created_at, true],
          ]),
        },
        {
          heading: "会话",
          render: async (wrap) => {
            wrap.innerHTML = '<div class="empty" style="padding:10px;">读取中…</div>';
            try {
              const sessions = await api(`/projects/${project.id}/sessions`);
              wrap.replaceChildren();
              if (!sessions.length) {
                wrap.innerHTML = '<div class="empty" style="padding:10px;">暂无会话</div>';
                return;
              }
              const list = document.createElement("div");
              list.className = "detail-list";
              sessions.forEach((session) => {
                const row = document.createElement("div");
                row.innerHTML = `<dt>会话</dt><dd class="mono">${app.escapeHtml(session.id)}</dd>`;
                list.append(row);
              });
              wrap.append(list);
            } catch (error) {
              wrap.innerHTML = `<div class="empty" style="padding:10px;">${app.escapeHtml(error.message)}</div>`;
            }
          },
        },
        {
          heading: "操作",
          render: (wrap) => {
            const actions = document.createElement("div");
            actions.className = "row-actions";
            actions.style.justifyContent = "flex-start";
            const open = document.createElement("button");
            open.className = "btn primary sm";
            open.textContent = "在 Agent 工作台打开";
            open.addEventListener("click", () => {
              document.getElementById("modal-mask").classList.remove("open");
              agentGotoProject(project.id);
            });
            const newSession = document.createElement("button");
            newSession.className = "btn sm";
            newSession.textContent = "新建会话";
            newSession.addEventListener("click", async () => {
              try {
                await api(`/projects/${project.id}/sessions`, {
                  method: "POST",
                  headers: { "Idempotency-Key": makeKey("project-session") },
                });
                app.toast("会话已创建");
              } catch (error) {
                app.toast(error.message, true);
              }
            });
            actions.append(open, newSession);
            wrap.append(actions);
          },
        },
      ],
    });
  }

  function newProjectModal() {
    const store = app.state.demo;
    app.openFormModal({
      title: "新建项目",
      sub: "项目对应一个工作区；可导入预设配置（快照语义）。",
      fields: [
        { name: "name", label: "项目名称", required: true, placeholder: "例如：线上商城重构" },
        {
          name: "user_id",
          label: "所属用户",
          type: "select",
          required: true,
          options: store.users.map((user) => ({ value: user.id, label: user.username })),
        },
        {
          name: "preset_id",
          label: "导入预设",
          type: "select",
          options: [
            { value: "", label: "无（部署级默认）" },
            ...store.presets.map((preset) => ({ value: preset.id, label: preset.name })),
          ],
        },
      ],
      submit: async (values) => {
        await api("/projects", {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("project") },
          body: {
            name: values.name,
            user_id: values.user_id,
            preset_id: values.preset_id || null,
          },
        });
        await loadData(true);
        refreshProjects();
      },
    });
  }

  // ================================================================ presets
  function defaultDefinition() {
    return {
      version: 1,
      skills: [],
      tool_policy: {
        allowed_tools: [
          "bash",
          "str_replace_based_edit_tool",
          "json_edit_tool",
          "sequentialthinking",
          "task_done",
        ],
        approval_required_tools: [],
        tool_descriptors: [],
      },
      resources: { mcp_refs: [], workspace_template: null, env: {} },
      permissions: {
        max_active_sessions: null,
        allow_network: true,
        allow_workspace_write: true,
      },
      enabled: true,
    };
  }

  async function refreshPresets() {
    await loadData();
    const store = app.state.demo;
    els["preset-grid"].replaceChildren();
    els["preset-empty"].hidden = store.presets.length > 0;
    store.presets.forEach((preset) => {
      const definition = preset.definition || {};
      const tools = definition.tool_policy?.allowed_tools || [];
      const approvals = definition.tool_policy?.approval_required_tools || [];
      const enabled = definition.enabled !== false;
      const card = document.createElement("div");
      card.className = "entity-card";
      card.innerHTML = `
        <div class="entity-top">
          <span class="entity-avatar" style="background:${avatarColor(preset.name)};">${app.escapeHtml(preset.name.slice(0, 1))}</span>
          <div><div class="entity-name">${app.escapeHtml(preset.name)}</div><div class="entity-meta">${app.escapeHtml(userById(preset.user_id)?.username || "系统")}</div></div>
          <span class="chip ${enabled ? "ok" : "gray"}" style="margin-left:auto;">${enabled ? "启用" : "停用"}</span>
        </div>
        <div class="entity-desc">${app.escapeHtml(preset.description || "（无描述）")}</div>
        <div class="tag-row">
          ${tools.slice(0, 4).map((tool) => `<span class="tag">${app.escapeHtml(tool)}</span>`).join("")}
        </div>
        <div style="margin-top:10px; padding-top:10px; border-top:1px dashed var(--border); display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
          ${approvals.length ? `<span class="chip warn">审批: ${approvals.length} 项</span>` : ""}
          <span class="chip gray">${definition.permissions?.allow_workspace_write === false ? "只读" : "工作区可写"}</span>
          <span class="btn sm" data-action="toggle" type="button">${enabled ? "停用" : "启用"}</span>
          <span class="btn sm" data-action="edit" type="button">编辑</span>
          <span class="btn sm danger" data-action="delete" type="button">删除</span>
        </div>
      `;
      card.addEventListener("click", (event) => {
        if (event.target.closest("[data-action]")) return;
        showPresetDetail(preset);
      });
      card.querySelector('[data-action="toggle"]').addEventListener("click", () =>
        togglePreset(preset)
      );
      card.querySelector('[data-action="edit"]').addEventListener("click", () =>
        editPresetModal(preset)
      );
      card.querySelector('[data-action="delete"]').addEventListener("click", () =>
        deletePreset(preset)
      );
      els["preset-grid"].append(card);
    });
  }

  function showPresetDetail(preset) {
    app.openDetailModal({
      title: `预设 · ${preset.name}`,
      sections: [
        {
          html: app.detailListHtml([
            ["ID", preset.id, true],
            ["名称", preset.name],
            ["描述", preset.description || "-"],
            ["创建时间", preset.created_at, true],
          ]),
        },
        {
          heading: "定义（JSON）",
          render: (wrap) => {
            const pre = document.createElement("pre");
            pre.className = "response-body";
            pre.style.maxHeight = "260px";
            pre.textContent = app.formatJson(preset.definition || {});
            wrap.append(pre);
          },
        },
      ],
    });
  }

  async function togglePreset(preset) {
    const definition = preset.definition || defaultDefinition();
    try {
      await api(`/presets/${preset.id}`, {
        method: "PATCH",
        body: { definition: { ...definition, enabled: !(definition.enabled !== false) } },
      });
      await loadData(true);
      refreshPresets();
      app.toast("预设状态已更新");
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  async function deletePreset(preset) {
    if (!window.confirm(`确认删除预设「${preset.name}」？删除不影响已导入它的项目（快照语义）。`)) {
      return;
    }
    try {
      await api(`/presets/${preset.id}`, { method: "DELETE" });
      await loadData(true);
      refreshPresets();
      app.toast("预设已删除");
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  function newPresetModal() {
    const store = app.state.demo;
    app.openFormModal({
      title: "新建预设",
      sub: "预设 = Skill + 工具策略 + 资源 + 权限。",
      fields: [
        { name: "name", label: "预设名称", required: true, placeholder: "例如：前端重构预设" },
        {
          name: "user_id",
          label: "所属用户",
          type: "select",
          required: true,
          options: store.users.map((user) => ({ value: user.id, label: user.username })),
        },
        { name: "description", label: "描述", placeholder: "预设用途说明" },
        {
          name: "definition",
          label: "定义（JSON）",
          type: "textarea",
          rows: 10,
          mono: true,
          value: app.formatJson(defaultDefinition()),
        },
      ],
      submit: async (values) => {
        let definition;
        try {
          definition = JSON.parse(values.definition);
        } catch {
          throw new Error("定义不是合法 JSON");
        }
        await api("/presets", {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("preset") },
          body: {
            user_id: values.user_id,
            name: values.name,
            description: values.description,
            definition,
          },
        });
        await loadData(true);
        refreshPresets();
      },
    });
  }

  function editPresetModal(preset) {
    app.openFormModal({
      title: `编辑预设 · ${preset.name}`,
      sub: "修改名称、描述或定义（JSON）。",
      fields: [
        { name: "name", label: "预设名称", required: true, value: preset.name },
        { name: "description", label: "描述", value: preset.description || "" },
        {
          name: "definition",
          label: "定义（JSON）",
          type: "textarea",
          rows: 10,
          mono: true,
          value: app.formatJson(preset.definition || defaultDefinition()),
        },
      ],
      okText: "保存",
      submit: async (values) => {
        let definition;
        try {
          definition = JSON.parse(values.definition);
        } catch {
          throw new Error("定义不是合法 JSON");
        }
        await api(`/presets/${preset.id}`, {
          method: "PATCH",
          body: { name: values.name, description: values.description, definition },
        });
        await loadData(true);
        refreshPresets();
      },
    });
  }

  // ================================================================ agent
  function agentGotoProject(projectId) {
    agent.projectId = projectId;
    agent.sessionId = null;
    agent.conversationId = null;
    app.gotoView("demo-agent");
  }

  async function agentLoadProjects(force = false) {
    await loadData();
    const projects = app.state.demo.projects;
    const select = els["agent-project-select"];
    select.replaceChildren();
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = projects.length ? "选择项目" : "暂无项目";
    select.append(empty);
    projects.forEach((project) => {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = project.name;
      select.append(option);
    });
    if (agent.projectId && projects.some((project) => project.id === agent.projectId)) {
      select.value = agent.projectId;
    } else if (projects.length) {
      select.value = projects[0].id;
      agent.projectId = projects[0].id;
    } else {
      agent.projectId = null;
    }
    els["agent-empty-state"].hidden = projects.length > 0;
    els["agent-layout-wrap"].hidden = projects.length === 0;
    await agentLoadSessions(false, force);
  }

  async function agentLoadSessions(selectNew = false, force = false) {
    const select = els["agent-session-select"];
    select.replaceChildren();
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "该会话列表为空";
    select.append(empty);
    if (!agent.projectId) {
      agent.sessionId = null;
      agent.conversationId = null;
      agent.conversations = [];
      agentRenderConversations();
      return;
    }
    const sessions = await api(`/projects/${agent.projectId}/sessions`);
    sessions.forEach((session) => {
      const option = document.createElement("option");
      option.value = session.id;
      option.textContent = `会话 ${session.id.slice(0, 8)}`;
      select.append(option);
    });
    if (selectNew && sessions.length) {
      agent.sessionId = sessions[sessions.length - 1].id;
      select.value = agent.sessionId;
    } else if (sessions.some((session) => session.id === agent.sessionId)) {
      select.value = agent.sessionId;
    } else if (sessions.length) {
      agent.sessionId = sessions[0].id;
      select.value = agent.sessionId;
    } else {
      agent.sessionId = null;
      agent.conversationId = null;
    }
    agent.conversationId = null;
    await agentLoadConversations(force);
  }

  async function agentLoadConversations(force = false) {
    agent.conversations = [];
    if (agent.sessionId) {
      agent.conversations = await api(`/sessions/${agent.sessionId}/conversations`);
    }
    agentRenderConversations();
    if (agent.conversations.length) {
      const last = agent.conversations[agent.conversations.length - 1];
      if (agent.conversationId && agent.conversations.some((conv) => conv.id === agent.conversationId)) {
        agentSelectConversation(agent.conversationId, force);
      } else {
        agentSelectConversation(last.id, force);
      }
    } else {
      agentClearConversation();
    }
  }

  function agentRenderConversations() {
    const list = els["agent-conv-list"];
    list.replaceChildren();
    els["agent-conv-count"].textContent = agent.conversations.length;
    els["agent-conv-empty"].hidden = agent.conversations.length > 0;
    agent.conversations.forEach((conversation) => {
      const item = document.createElement("div");
      item.className = "conv-item";
      if (conversation.id === agent.conversationId) item.classList.add("active");
      const chip = app.runStateChip(conversation.run?.state);
      item.innerHTML = `
        <span class="conv-avatar" style="background:var(--accent-soft); color:var(--accent);">${app.escapeHtml((conversation.task || "?").slice(0, 1))}</span>
        <div>
          <div class="name">${app.escapeHtml((conversation.task || "").slice(0, 18))}</div>
          <div class="meta">${chip.text} · ${app.escapeHtml(app.relativeTime(conversation.created_at))}</div>
        </div>
      `;
      item.addEventListener("click", () => agentSelectConversation(conversation.id));
      list.append(item);
    });
  }

  function agentClearConversation() {
    agentCloseStream();
    agent.conversationId = null;
    agent.events = [];
    els["agent-chat-scroll"].replaceChildren();
    els["agent-chat-empty"].hidden = false;
    els["agent-timeline"].replaceChildren();
    els["agent-timeline"].hidden = true;
    els["agent-event-empty"].hidden = false;
    els["agent-run-chip"].textContent = "空闲";
    els["agent-run-chip"].className = "chip gray";
    els["agent-run-state"].textContent = "IDLE";
    els["agent-run-seq"].textContent = "0";
    els["agent-run-id"].textContent = "-";
    els["agent-cancel-btn"].disabled = true;
    agentRenderInteraction();
    agentRenderConversations();
  }

  function agentSelectConversation(conversationId, force = false) {
    if (!force && agent.conversationId === conversationId && agent.events.length) return;
    agentCloseStream();
    agent.conversationId = conversationId;
    agent.events = [];
    els["agent-chat-scroll"].replaceChildren();
    els["agent-chat-empty"].hidden = false;
    agentRenderConversations();
    agentLoadEvents();
  }

  async function agentLoadEvents() {
    if (!agent.conversationId) return;
    try {
      const events = await api(`/conversations/${agent.conversationId}/events?after_seq=0`);
      agent.events = Array.isArray(events) ? events : [];
      agentRenderAll();
      agentOpenStream();
    } catch (error) {
      app.toast(error.message, true);
    }
  }

  function agentProjectedState() {
    let projected = "IDLE";
    for (const event of agent.events) {
      if (event.type === "conversation.queued") projected = "QUEUED";
      if (event.type === "run.started" || event.type === "run.running") projected = "RUNNING";
      if (event.type === "interaction.requested") projected = "WAITING_INPUT";
      if (event.type === "interaction.input" || event.type === "approval.decided") projected = "RUNNING";
      if (event.type === "run.paused") projected = "PAUSED";
      if (event.type === "run.resuming") projected = "RESUMING";
      if (event.type === "run.completed") projected = "COMPLETED";
      if (event.type === "run.failed") projected = "FAILED";
      if (event.type === "run.cancelled") projected = "CANCELLED";
      if (event.type === "run.lost") projected = "LOST";
    }
    return projected;
  }

  function agentRenderAll() {
    const state = agentProjectedState();
    const chip = app.runStateChip(state);
    els["agent-run-chip"].textContent = chip.text;
    els["agent-run-chip"].className = `chip ${chip.cls}`;
    els["agent-run-state"].textContent = state;
    els["agent-run-seq"].textContent = agent.events.at(-1)?.seq || 0;
    const runId = agent.events.find((event) => event.payload?.run_id)?.payload?.run_id ||
      agent.conversations.find((conv) => conv.id === agent.conversationId)?.run?.run_id;
    els["agent-run-id"].textContent = runId ? `${String(runId).slice(0, 8)}…` : "-";
    els["agent-cancel-btn"].disabled =
      !agent.conversationId || terminalStates.has(state) || agent.busy;
    agentRenderChat();
    agentRenderTimeline();
    agentRenderInteraction();
  }

  function agentRenderChat() {
    const scroll = els["agent-chat-scroll"];
    scroll.replaceChildren();
    els["agent-chat-empty"].hidden = agent.events.length > 0;
    for (const event of agent.events) {
      const message = agentEventToMessage(event);
      if (!message) continue;
      const msg = document.createElement("div");
      msg.className = `msg ${message.kind || ""}`;
      const role = document.createElement("span");
      role.className = "role";
      role.style.cssText =
        message.kind === "user"
          ? "background:var(--accent); color:#fff;"
          : "background:var(--panel); border:1px solid var(--border); color:var(--muted);";
      role.textContent = message.kind === "user" ? "我" : "Ag";
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.innerHTML = message.html;
      msg.append(role, bubble);
      scroll.append(msg);
    }
    scroll.scrollTop = scroll.scrollHeight;
  }

  function agentEventToMessage(event) {
    const payload = event.payload || {};
    const mono = (text) => `<div class="tool-line">${app.escapeHtml(text)}</div>`;
    switch (event.type) {
      case "conversation.queued":
        return { kind: "system", html: "任务已入队，等待执行容量。" };
      case "run.started":
        return { kind: "system", html: "运行已启动。" };
      case "run.running":
        return { kind: "system", html: `Agent 正在工作（container: ${app.escapeHtml(payload.container_id || "-")}）。` };
      case "message":
        return { html: app.escapeHtml(String(payload.content ?? payload.input ?? "")) };
      case "interaction.requested":
        if (payload.kind === "approval") {
          return {
            kind: "system",
            html: `请求工具审批：${app.escapeHtml(payload.tool_batch_hash || payload.interaction_id || "")}`,
          };
        }
        return { kind: "system", html: `等待你的输入：${app.escapeHtml(payload.question || payload.interaction_id || "")}` };
      case "interaction.input":
        return {
          kind: "user",
          html: app.escapeHtml(typeof payload.value === "string" ? payload.value : app.formatJson(payload.value)),
        };
      case "approval.decided":
        return { kind: "system", html: `工具审批：${app.escapeHtml(payload.decision || "")}` };
      case "tool.result":
        return {
          html: `工具调用完成${mono(`${payload.tool || "tool"} · exit_code=${payload.exit_code ?? "?"}`)}`,
        };
      case "run.completed":
        return {
          html: `任务完成。${mono(app.formatJson(payload.result || {}).slice(0, 220))}`,
        };
      case "run.failed":
        return { kind: "system", html: `任务失败：${app.escapeHtml(payload.message || payload.code || "")}` };
      case "run.cancelled":
        return { kind: "system", html: "运行已取消。" };
      case "run.paused":
        return { kind: "system", html: "运行已暂停（等待超时）。" };
      case "run.resuming":
        return { kind: "system", html: "正在恢复运行。" };
      case "run.lost":
        return { kind: "system", html: "运行丢失，状态未知。" };
      default:
        return null;
    }
  }

  function agentEventClass(type) {
    if (type.includes("failed") || type.includes("lost") || type.includes("cancel")) return "failure";
    if (type === "run.completed") return "done";
    if (type.startsWith("tool.")) return "tool done";
    if (type.includes("interaction") || type.includes("approval")) return "gate";
    return "done";
  }

  function agentRenderTimeline() {
    const timeline = els["agent-timeline"];
    timeline.replaceChildren();
    timeline.hidden = agent.events.length === 0;
    els["agent-event-empty"].hidden = agent.events.length > 0;
    for (const event of agent.events) {
      const row = document.createElement("div");
      row.className = `event ${agentEventClass(event.type)}`;
      row.innerHTML = `
        <span class="seq">${String(event.seq).padStart(3, "0")}</span>
        <span class="rail"></span>
        <div><div class="type">${app.escapeHtml(event.type)}</div><div class="payload">${app.escapeHtml(app.formatJson(event.payload).slice(0, 140))}</div></div>
      `;
      timeline.append(row);
    }
    timeline.scrollTop = timeline.scrollHeight;
  }

  function agentUnresolvedInteraction() {
    const decided = new Set(
      agent.events
        .filter((event) => event.type === "approval.decided" || event.type === "interaction.input")
        .map((event) => event.payload.approval_id || event.payload.interaction_id)
    );
    return agent.events
      .filter((event) => event.type === "interaction.requested")
      .reverse()
      .find((event) => !decided.has(event.payload.interaction_id));
  }

  function agentRenderInteraction() {
    const event = agentUnresolvedInteraction();
    const terminal = terminalStates.has(agentProjectedState());
    const visible = Boolean(event && !terminal);
    els["agent-interaction-empty"].hidden = visible;
    els["agent-interaction"].hidden = !visible;
    if (!visible) return;
    const interaction = event.payload;
    const isApproval = interaction.kind === "approval";
    els["agent-approval-view"].hidden = !isApproval;
    els["agent-input-form"].hidden = isApproval;
    if (isApproval) {
      els["agent-batch-hash"].textContent = interaction.tool_batch_hash || "-";
      els["agent-tool-calls"].replaceChildren();
      const calls = interaction.tool_batch?.calls || interaction.pending_tool_calls || [];
      calls.forEach((call) => {
        const block = document.createElement("div");
        block.className = "tool-call";
        block.innerHTML = `<div class="name">${app.escapeHtml(call.name || "unknown tool")}</div><pre>${app.escapeHtml(app.formatJson(call.arguments || call))}</pre>`;
        els["agent-tool-calls"].append(block);
      });
    } else {
      els["agent-interaction-id"].textContent = interaction.interaction_id;
    }
  }

  async function agentSubmitInteraction(kind, decisionOrValue) {
    const event = agentUnresolvedInteraction();
    if (!event) return;
    const interaction = event.payload;
    const lastSeq = agent.events.at(-1)?.seq || 0;
    agent.busy = true;
    els["agent-cancel-btn"].disabled = true;
    try {
      if (kind === "approval") {
        await api(`/conversations/${agent.conversationId}/approval`, {
          method: "POST",
          headers: { "Idempotency-Key": makeKey(`approval-${interaction.interaction_id}`) },
          body: {
            approval_id: interaction.interaction_id,
            decision: decisionOrValue,
            expected_seq: lastSeq,
          },
        });
        app.toast(decisionOrValue === "APPROVE_ONCE" ? "已批准本次工具调用" : "已拒绝工具调用");
      } else {
        await api(`/conversations/${agent.conversationId}/input`, {
          method: "POST",
          headers: { "Idempotency-Key": makeKey(`input-${interaction.interaction_id}`) },
          body: {
            interaction_id: interaction.interaction_id,
            value: decisionOrValue,
            expected_seq: lastSeq,
          },
        });
        els["agent-input-value"].value = "";
        app.toast("回复已提交");
      }
      await agentLoadEvents();
      await agentLoadConversations();
    } catch (error) {
      app.toast(error.message, true);
    } finally {
      agent.busy = false;
      els["agent-cancel-btn"].disabled = false;
      agentRenderAll();
    }
  }

  async function agentCancelRun() {
    if (!agent.conversationId) return;
    agent.busy = true;
    try {
      await api(`/conversations/${agent.conversationId}/cancel`, {
        method: "POST",
        headers: { "Idempotency-Key": makeKey("cancel") },
        body: { expected_seq: agent.events.at(-1)?.seq || 0 },
      });
      await agentLoadEvents();
      await agentLoadConversations();
      app.toast("运行已取消");
    } catch (error) {
      app.toast(error.message, true);
    } finally {
      agent.busy = false;
      agentRenderAll();
    }
  }

  async function agentSendTask() {
    const text = els["agent-task-input"].value.trim();
    if (!text) return;
    if (!agent.projectId) {
      app.toast("请先选择项目", true);
      return;
    }
    agent.busy = true;
    els["agent-send-btn"].disabled = true;
    try {
      if (!agent.sessionId) {
        const session = await api(`/projects/${agent.projectId}/sessions`, {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("session") },
        });
        agent.sessionId = session.id;
      }
      const body = { task: text };
      if (agent.conversationId) body.parent_conversation_id = agent.conversationId;
      const conversation = await api(`/sessions/${agent.sessionId}/conversations`, {
        method: "POST",
        headers: { "Idempotency-Key": makeKey("conversation") },
        body,
      });
      agent.conversationId = conversation.id;
      agent.events = [];
      els["agent-task-input"].value = "";
      await agentLoadConversations();
      await agentLoadEvents();
      app.toast("任务已提交");
    } catch (error) {
      app.toast(error.message, true);
    } finally {
      agent.busy = false;
      els["agent-send-btn"].disabled = false;
    }
  }

  function agentOpenStream() {
    agentCloseStream();
    if (!agent.conversationId) return;
    const after = agent.events.at(-1)?.seq || 0;
    const stream = new EventSource(
      `${app.apiBase}/conversations/${agent.conversationId}/events/stream?after_seq=${after}`
    );
    agent.stream = stream;
    stream.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        if (!agent.events.some((existing) => existing.event_id === event.event_id)) {
          agent.events.push(event);
          agentRenderAll();
        }
      } catch {
        /* ignore malformed event */
      }
    };
    stream.onerror = () => {
      if (agent.stream !== stream) return;
      stream.close();
      agent.stream = null;
      clearTimeout(agent.reconnectTimer);
      agent.reconnectTimer = setTimeout(agentOpenStream, 1500);
    };
  }

  function agentCloseStream() {
    clearTimeout(agent.reconnectTimer);
    agent.reconnectTimer = null;
    agent.stream?.close();
    agent.stream = null;
  }

  function initAgent() {
    els["agent-project-select"].addEventListener("change", () => {
      agent.projectId = els["agent-project-select"].value || null;
      agent.sessionId = null;
      agent.conversationId = null;
      agentLoadSessions();
    });
    els["agent-session-select"].addEventListener("change", () => {
      agent.sessionId = els["agent-session-select"].value || null;
      agent.conversationId = null;
      agentLoadConversations();
    });
    els["agent-new-session-btn"].addEventListener("click", async () => {
      if (!agent.projectId) {
        app.toast("请先选择项目", true);
        return;
      }
      try {
        await api(`/projects/${agent.projectId}/sessions`, {
          method: "POST",
          headers: { "Idempotency-Key": makeKey("session") },
        });
        await agentLoadSessions(true);
        app.toast("会话已创建");
      } catch (error) {
        app.toast(error.message, true);
      }
    });
    els["agent-refresh-btn"].addEventListener("click", () => agentLoadProjects(true));
    els["agent-send-btn"].addEventListener("click", agentSendTask);
    els["agent-task-input"].addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        agentSendTask();
      }
    });
    document.querySelectorAll(".hint-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        els["agent-task-input"].value = chip.dataset.hint;
        els["agent-task-input"].focus();
      });
    });
    els["agent-cancel-btn"].addEventListener("click", agentCancelRun);
    els["agent-approve-btn"].addEventListener("click", () =>
      agentSubmitInteraction("approval", "APPROVE_ONCE")
    );
    els["agent-reject-btn"].addEventListener("click", () =>
      agentSubmitInteraction("approval", "REJECT")
    );
    els["agent-input-form"].addEventListener("submit", (event) => {
      event.preventDefault();
      const value = els["agent-input-value"].value.trim();
      if (!value) return;
      agentSubmitInteraction("input", value);
    });
    window.addEventListener("beforeunload", agentCloseStream);
  }

  async function refreshAgent() {
    await agentLoadProjects();
  }

  // ================================================================ register
  app.registerView("demo-overview", { init: () => {}, refresh: refreshOverview });
  app.registerView("demo-orgs", { init: () => {}, refresh: refreshOrgs });
  app.registerView("demo-users", { init: () => {}, refresh: refreshUsers });
  app.registerView("demo-projects", { init: () => {}, refresh: refreshProjects });
  app.registerView("demo-presets", { init: () => {}, refresh: refreshPresets });
  app.registerView("demo-agent", { init: initAgent, refresh: refreshAgent });

  els["recent-refresh"].addEventListener("click", renderRecentRuns);
  els["new-org-btn"].addEventListener("click", newOrgModal);
  els["new-user-btn"].addEventListener("click", newUserModal);
  els["new-project-btn"].addEventListener("click", newProjectModal);
  els["new-preset-btn"].addEventListener("click", newPresetModal);
})();

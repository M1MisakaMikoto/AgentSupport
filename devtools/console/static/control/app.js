(function () {
  "use strict";

  const token = document.querySelector('meta[name="dev-console-token"]').content;

  const app = {
    token,
    apiBase: "/agentsupport",
    views: {},
    state: {
      demo: { orgs: [], users: [], projects: [], presets: [], loadedAt: 0 },
      operation: null,
      envTimer: null,
    },
  };

  app.els = (ids) =>
    Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

  app.escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  app.uuid = () =>
    (crypto.randomUUID && crypto.randomUUID()) ||
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });

  let toastTimer = null;
  app.toast = (message, isError = false) => {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.className = `toast show${isError ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = "toast"), 3200);
  };

  app.formatJson = (value) => JSON.stringify(value, null, 2);

  app.copyText = async (text, label = "已复制") => {
    try {
      await navigator.clipboard.writeText(text);
      app.toast(label);
    } catch {
      app.toast("浏览器未授予剪贴板权限", true);
    }
  };

  app.relativeTime = (iso) => {
    if (!iso) return "-";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "-";
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return "刚刚";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.floor(hours / 24)} 天前`;
  };

  app.runStateChip = (state) => {
    const map = {
      QUEUED: ["排队", "warn"],
      STARTING: ["启动中", "warn"],
      RUNNING: ["运行中", "blue"],
      WAITING_INPUT: ["待交互", "warn"],
      SUSPENDING: ["挂起中", "gray"],
      PAUSED: ["已暂停", "gray"],
      RESUMING: ["恢复中", "warn"],
      COMPLETED: ["已完成", "ok"],
      FAILED: ["失败", "error"],
      CANCELLED: ["已取消", "gray"],
      LOST: ["丢失", "error"],
    };
    const [text, cls] = map[state] || [state || "空闲", "gray"];
    return { text, cls };
  };

  app.apiRequest = async (path, options = {}) => {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    let body = options.body;
    if (body !== undefined && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    } else if (typeof body === "string" && body !== "") {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(app.apiBase + path, {
      ...options,
      headers,
      body,
    });
    const text = await response.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = text;
    }
    return {
      response,
      status: response.status,
      ok: response.ok,
      headers: Object.fromEntries(response.headers.entries()),
      body: parsed,
      text,
    };
  };

  // Console control-plane endpoints live on the console itself, not behind
  // the /agentsupport API proxy.
  app.consoleRequest = async (path, options = {}) => {
    const headers = {
      Accept: "application/json",
      ...(options.headers || {}),
      "X-Dev-Console-Token": token,
    };
    let body = options.body;
    if (body !== undefined && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    } else if (typeof body === "string" && body !== "") {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, { ...options, headers, body });
    const text = await response.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = text;
    }
    return {
      response,
      status: response.status,
      ok: response.ok,
      headers: Object.fromEntries(response.headers.entries()),
      body: parsed,
      text,
    };
  };

  app.apiError = (result, fallback = "请求失败") => {
    const body = result.body;
    return (
      (typeof body === "object" && (body.message || body.detail)) ||
      fallback ||
      `${result.status} ${result.response.statusText}`
    );
  };

  // ------------------------------------------------------------- routing
  const primaryButtons = () => [...document.querySelectorAll(".primary-btn")];
  const sidebars = { demo: "sidebar-demo", deploy: "sidebar-deploy" };
  const firstView = { demo: "demo-overview", deploy: "deploy-status" };

  function activateView(name) {
    document.querySelectorAll(".view").forEach((view) => {
      view.classList.toggle("active", view.id === `view-${name}`);
    });
    document.querySelectorAll(".side-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.view === name);
    });
    const primary = name.startsWith("demo") ? "demo" : "deploy";
    primaryButtons().forEach((btn) =>
      btn.classList.toggle("active", btn.dataset.primary === primary)
    );
    Object.entries(sidebars).forEach(([key, id]) => {
      document.getElementById(id).hidden = key !== primary;
    });
    app.state.primary = primary;
  }

  app.setPrimary = (name) => {
    const target = firstView[name] || "demo-overview";
    app.gotoView(target);
  };

  app.gotoView = (name) => {
    if (!document.getElementById(`view-${name}`)) {
      name = firstView[app.state.primary] || "demo-overview";
    }
    activateView(name);
    if (window.location.hash !== `#${name}`) {
      try {
        window.history.replaceState(null, "", `#${name}`);
      } catch {
        window.location.hash = name;
      }
    }
    const mod = app.views[name];
    if (mod) {
      if (!mod.started) {
        mod.started = true;
        Promise.resolve(mod.init?.()).catch((error) =>
          console.error("view init failed:", error)
        );
      }
      Promise.resolve(mod.refresh?.()).catch((error) =>
        console.error("view refresh failed:", error)
      );
    }
  };

  app.registerView = (name, mod) => {
    app.views[name] = mod;
    const run = () => {
      if (!mod.started) {
        mod.started = true;
        Promise.resolve(mod.init?.()).catch((error) =>
          console.error("view init failed:", error)
        );
      }
      Promise.resolve(mod.refresh?.()).catch((error) =>
        console.error("view refresh failed:", error)
      );
    };
    if (document.getElementById(`view-${name}`)?.classList.contains("active")) {
      run();
    }
  };

  // ------------------------------------------------------------- modal
  function modalEls() {
    return {
      mask: document.getElementById("modal-mask"),
      title: document.getElementById("modal-title"),
      sub: document.getElementById("modal-sub"),
      fields: document.getElementById("modal-fields"),
      ok: document.getElementById("modal-ok"),
      cancel: document.getElementById("modal-cancel"),
    };
  }

  function fieldControl(field) {
    if (field.type === "textarea") {
      const el = document.createElement("textarea");
      el.rows = field.rows || 4;
      if (field.placeholder) el.placeholder = field.placeholder;
      if (field.value !== undefined) el.value = field.value;
      if (field.mono) el.style.fontFamily = "var(--mono)";
      return el;
    }
    if (field.type === "select") {
      const el = document.createElement("select");
      (field.options || []).forEach((option) => {
        const o = document.createElement("option");
        o.value = option.value ?? option;
        o.textContent = option.label ?? option;
        el.append(o);
      });
      if (field.value) el.value = field.value;
      return el;
    }
    const el = document.createElement("input");
    el.type = field.type || "text";
    if (field.placeholder) el.placeholder = field.placeholder;
    if (field.value !== undefined) el.value = field.value;
    if (field.required) el.required = true;
    return el;
  }

  app.openFormModal = ({ title, sub, fields, submit, okText = "创建" }) => {
    const els = modalEls();
    els.title.textContent = title;
    els.sub.textContent = sub || "";
    els.fields.replaceChildren();
    const controls = [];
    fields.forEach((field) => {
      const wrap = document.createElement("div");
      wrap.className = "field";
      const label = document.createElement("label");
      label.textContent = field.label;
      const control = fieldControl(field);
      controls.push(control);
      wrap.append(label, control);
      els.fields.append(wrap);
    });
    els.ok.textContent = okText;
    els.ok.disabled = false;
    els.mask.classList.add("open");

    const close = () => {
      els.mask.classList.remove("open");
      els.ok.onclick = null;
      els.cancel.onclick = null;
    };
    els.cancel.onclick = close;
    els.ok.onclick = async () => {
      const values = {};
      let valid = true;
      fields.forEach((field, index) => {
        const value = controls[index].value.trim();
        if (field.required && !value) valid = false;
        values[field.name] = value;
      });
      if (!valid) {
        app.toast("请填写必填项", true);
        return;
      }
      els.ok.disabled = true;
      els.ok.textContent = "提交中…";
      try {
        await submit(values);
        close();
        app.toast("操作成功");
      } catch (error) {
        app.toast(error.message || "提交失败", true);
        els.ok.disabled = false;
        els.ok.textContent = okText;
      }
    };
    els.mask.onclick = (event) => {
      if (event.target === els.mask) close();
    };
  };

  app.openDetailModal = ({ title, sections }) => {
    const els = modalEls();
    els.title.textContent = title;
    els.sub.textContent = "";
    els.fields.replaceChildren();
    sections.forEach((section) => {
      if (section.heading) {
        const heading = document.createElement("h4");
        heading.textContent = section.heading;
        heading.style.cssText =
          "margin:12px 0 6px; font-size:12px; color:var(--faint); text-transform:uppercase; letter-spacing:.6px;";
        els.fields.append(heading);
      }
      if (section.html) {
        const wrap = document.createElement("div");
        wrap.innerHTML = section.html;
        els.fields.append(wrap);
      }
      if (section.render) {
        const wrap = document.createElement("div");
        section.render(wrap);
        els.fields.append(wrap);
      }
    });
    els.ok.textContent = "关闭";
    els.ok.disabled = false;
    els.ok.onclick = () => {
      els.mask.classList.remove("open");
      els.ok.onclick = null;
    };
    els.cancel.onclick = els.ok.onclick;
    els.mask.onclick = (event) => {
      if (event.target === els.mask) els.ok.onclick();
    };
    els.mask.classList.add("open");
  };

  app.detailListHtml = (pairs) =>
    `<div class="detail-list">${pairs
      .map(
        ([key, value, mono = false]) =>
          `<div><dt>${app.escapeHtml(key)}</dt><dd class="${mono ? "mono" : ""}">${app.escapeHtml(String(value ?? "-"))}</dd></div>`
      )
      .join("")}</div>`;

  // ------------------------------------------------------- operation dock
  const actionNames = {
    deploy: "部署分布式服务",
    start: "启动分布式服务",
    stop: "停止分布式服务",
    accept: "执行回归验收",
    "api-accept": "执行 API 契约验收",
  };

  function dockEls() {
    return {
      dock: document.getElementById("op-dock"),
      title: document.getElementById("op-dock-title"),
      status: document.getElementById("op-dock-status"),
      collapse: document.getElementById("op-dock-collapse"),
      close: document.getElementById("op-dock-close"),
      body: document.getElementById("op-dock-body"),
      steps: document.getElementById("op-dock-steps"),
      log: document.getElementById("op-dock-log"),
      report: document.getElementById("op-dock-report"),
      copy: document.getElementById("op-dock-copy"),
    };
  }

  function renderDockSteps(operation, els) {
    els.steps.replaceChildren();
    if (!operation.steps.length) {
      const row = document.createElement("li");
      row.className = "step-row running";
      row.innerHTML =
        '<span class="step-marker">·</span><div><strong>准备操作</strong><small>等待第一个步骤</small></div>';
      els.steps.append(row);
      return;
    }
    operation.steps.forEach((step) => {
      const row = document.createElement("li");
      row.className = `step-row ${step.status}`;
      const marker =
        step.status === "succeeded" ? "✓" : step.status === "failed" ? "!" : "·";
      const finished = step.finished_at
        ? new Date(step.finished_at).toLocaleTimeString()
        : "进行中";
      row.innerHTML = `<span class="step-marker">${marker}</span><div><strong>${app.escapeHtml(step.name)}</strong><small>${finished}</small></div>`;
      els.steps.append(row);
    });
  }

  function renderDockLog(operation, els) {
    const nearBottom =
      els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 48;
    els.log.textContent =
      operation.logs
        .map((line) => {
          const time = new Date(line.at).toLocaleTimeString([], { hour12: false });
          const prefix =
            line.stream === "command"
              ? "$"
              : line.stream === "stderr"
                ? "!"
                : line.stream === "result"
                  ? "·"
                  : line.stream === "step"
                    ? "→"
                    : " ";
          return `${time} ${prefix} ${line.message}`;
        })
        .join("\n") || "$ 等待命令";
    if (nearBottom) els.log.scrollTop = els.log.scrollHeight;
  }

  app.renderApiAcceptReport = (container, report) => {
    container.hidden = false;
    container.replaceChildren();
    const summary = report.summary || {};
    const coverage = report.coverage || {};
    const heading = document.createElement("div");
    heading.className = "card-head";
    heading.innerHTML = "<h2>API 契约验收报告</h2>";
    container.append(heading);
    const strip = document.createElement("div");
    strip.className = "report-summary";
    strip.innerHTML = `
      <div><span>用例总数</span><strong>${summary.total ?? 0}</strong></div>
      <div><span>通过</span><strong style="color:var(--ok)">${summary.passed ?? 0}</strong></div>
      <div><span>失败</span><strong style="color:var(--error)">${summary.failed ?? 0}</strong></div>
      <div><span>操作覆盖</span><strong>${coverage.operations_covered ?? 0}/${coverage.operations_total ?? 0}</strong></div>
    `;
    container.append(strip);
    const coverageBlock = document.createElement("div");
    coverageBlock.innerHTML = `
      <div class="coverage"><div class="bar"><span style="width:${coverage.coverage_pct ?? 0}%"></span></div>
      <div class="meta"><span>OpenAPI 操作覆盖率 ${coverage.coverage_pct ?? 0}%</span><span>未覆盖 ${(coverage.uncovered || []).length} 项</span></div></div>
    `;
    if (coverage.uncovered?.length) {
      coverageBlock.innerHTML += `<div class="tag-row">${coverage.uncovered
        .map((item) => `<span class="tag">${app.escapeHtml(item)}</span>`)
        .join("")}</div>`;
    }
    container.append(coverageBlock);
    const table = document.createElement("table");
    table.className = "checks-table";
    table.innerHTML =
      "<thead><tr><th>用例</th><th>接口</th><th>结果</th><th>说明</th></tr></thead>";
    const tbody = document.createElement("tbody");
    (report.checks || []).forEach((check) => {
      const row = document.createElement("tr");
      const cls = check.status === "pass" ? "ok" : check.status === "skip" ? "gray" : "error";
      row.innerHTML = `
        <td><code>${app.escapeHtml(check.id)}</code><br><span class="entity-meta">${app.escapeHtml(check.title)}</span></td>
        <td><code>${app.escapeHtml(check.method)} ${app.escapeHtml(check.operation)}</code></td>
        <td><span class="chip ${cls}">${check.status.toUpperCase()}</span></td>
        <td class="check-detail">${app.escapeHtml(check.detail)}<br><span class="entity-meta">${check.duration_ms} ms</span></td>
      `;
      tbody.append(row);
    });
    table.append(tbody);
    container.append(table);
  };

  app.renderDeployReport = (container, report) => {
    container.hidden = false;
    container.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "card-head";
    heading.innerHTML = "<h2>部署回归验收结果</h2>";
    container.append(heading);
    const rows = [
      ["API 实例", (report.api_instances || []).join(", ")],
      ["必需指标", (report.metrics || []).join(", ")],
      ["任务闭环", report.conversation_id ? `已完成 (${report.conversation_id})` : "已跳过"],
      ["事件序列", (report.event_types || []).join(", ")],
    ];
    container.insertAdjacentHTML(
      "beforeend",
      app.detailListHtml(rows.map(([key, value]) => [key, value, true]))
    );
  };

  function renderDockResult(operation, els) {
    els.report.hidden = true;
    if (operation.status !== "succeeded" || !operation.result) return;
    if (operation.action === "api-accept") {
      app.renderApiAcceptReport(els.report, operation.result);
    } else if (operation.action === "accept") {
      app.renderDeployReport(els.report, operation.result);
    } else {
      els.report.hidden = false;
      els.report.innerHTML =
        '<pre class="response-body">' +
        app.escapeHtml(JSON.stringify(operation.result, null, 2)) +
        "</pre>";
    }
  }

  function renderDockOperation(operation) {
    const els = dockEls();
    els.title.textContent = actionNames[operation.action] || operation.action;
    els.status.textContent = operation.status.toUpperCase();
    els.status.className = `chip ${
      operation.status === "succeeded"
        ? "ok"
        : operation.status === "failed"
          ? "error"
          : ["queued", "running"].includes(operation.status)
            ? "warn"
            : "gray"
    }`;
    renderDockSteps(operation, els);
    renderDockLog(operation, els);
    renderDockResult(operation, els);
  }

  async function pollOperation() {
    const operation = app.state.operation;
    if (!operation) return;
    clearTimeout(operation.timer);
    try {
      const result = await app.consoleRequest(`/api/operations/${operation.id}`);
      if (!result.ok) throw new Error(app.apiError(result, "读取操作状态失败"));
      renderDockOperation(result.body);
      if (["queued", "running"].includes(result.body.status)) {
        operation.timer = setTimeout(pollOperation, 700);
        return;
      }
      localStorage.removeItem("agentSupportActiveOperation");
      app.toast(
        result.body.status === "succeeded" ? "操作已完成" : result.body.error || "操作失败",
        result.body.status === "failed"
      );
      app.onOperationDone?.(result.body);
    } catch (error) {
      app.toast(error.message, true);
      operation.timer = setTimeout(pollOperation, 2500);
    }
  }

  app.openDock = (operation) => {
    app.state.operation = operation;
    dockEls().dock.hidden = false;
    renderDockOperation({ ...operation, steps: [], logs: [], status: "queued", result: {} });
  };

  app.beginAction = async (action, payload) => {
    try {
      const result = await app.consoleRequest(`/api/actions/${action}`, {
        method: "POST",
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
      if (!result.ok) throw new Error(app.apiError(result, "操作提交失败"));
      const operation = { id: result.body.id, action };
      localStorage.setItem("agentSupportActiveOperation", JSON.stringify(operation));
      app.openDock(operation);
      pollOperation();
      return operation;
    } catch (error) {
      app.toast(error.message, true);
      return null;
    }
  };

  function initDock() {
    const els = dockEls();
    els.collapse.addEventListener("click", () => {
      els.body.classList.toggle("collapsed");
      els.collapse.textContent = els.body.classList.contains("collapsed") ? "+" : "−";
    });
    els.close.addEventListener("click", () => {
      els.dock.hidden = true;
      if (app.state.operation) clearTimeout(app.state.operation.timer);
    });
    els.copy.addEventListener("click", () =>
      app.copyText(els.log.textContent, "日志已复制")
    );
    try {
      const saved = JSON.parse(localStorage.getItem("agentSupportActiveOperation") || "null");
      if (saved?.id) {
        app.openDock(saved);
        pollOperation();
      }
    } catch {
      localStorage.removeItem("agentSupportActiveOperation");
    }
  }

  // ------------------------------------------------------- deployment views
  function deployTargetPayload() {
    return {
      docker_transport: document.getElementById("docker-transport").value,
      docker_context: document.getElementById("docker-context").value.trim(),
      wsl_distribution: document.getElementById("wsl-distribution").value.trim(),
    };
  }

  function deployTargetQuery() {
    return new URLSearchParams(deployTargetPayload()).toString();
  }

  function initDeployActions() {
    const els = app.els([
      "docker-transport", "docker-context", "docker-context-field",
      "wsl-distribution", "wsl-distribution-field", "wsl-distributions",
      "connection-dot", "connection-label", "probe-connection",
      "deploy-button", "start-button", "stop-button",
    ]);
    const updateFields = () => {
      const transport = els["docker-transport"].value;
      els["wsl-distribution-field"].hidden = !["auto", "wsl2"].includes(transport);
      els["docker-context-field"].hidden = transport !== "context";
    };
    els["docker-transport"].addEventListener("change", updateFields);
    els["probe-connection"].addEventListener("click", async () => {
      els["connection-dot"].className = "dot warn";
      els["connection-label"].textContent = "正在检测";
      try {
        const result = await app.consoleRequest(
          `/api/environment?${deployTargetQuery()}`
        );
        const environment = result.ok ? result.body : null;
        if (!environment) throw new Error(app.apiError(result, "检测失败"));
        els["wsl-distributions"].replaceChildren();
        (environment.distributions || []).forEach((distribution) => {
          const option = document.createElement("option");
          option.value = distribution;
          els["wsl-distributions"].append(option);
        });
        if (!environment.available) throw new Error(environment.error || "Docker 连接不可用");
        els["connection-dot"].className = "dot ok";
        els["connection-label"].textContent =
          `${environment.target.label} · Docker ${environment.docker_version} · Compose ${environment.compose_version}`;
        app.toast("Docker 连接可用");
      } catch (error) {
        els["connection-dot"].className = "dot error";
        els["connection-label"].textContent = error.message;
        app.toast(error.message, true);
      }
    });
    els["deploy-button"].addEventListener("click", () => {
      app.beginAction("deploy", {
        ...deployTargetPayload(),
        api_replicas: Number(document.getElementById("api-replicas").value),
        worker_replicas: Number(document.getElementById("worker-replicas").value),
        runner_mode: document.querySelector('input[name="runner-mode"]:checked').value,
        rebuild: document.getElementById("rebuild-image").checked,
      });
    });
    els["start-button"].addEventListener("click", () => {
      app.beginAction("start", {
        ...deployTargetPayload(),
        api_replicas: Number(document.getElementById("api-replicas").value),
        worker_replicas: Number(document.getElementById("worker-replicas").value),
        runner_mode: document.querySelector('input[name="runner-mode"]:checked').value,
        rebuild: false,
      });
    });
    els["stop-button"].addEventListener("click", () => {
      if (window.confirm("停止全部 Compose 服务并保留数据卷？")) {
        app.beginAction("stop", deployTargetPayload());
      }
    });
    updateFields();
  }

  function serviceState(service) {
    const raw = `${service.state} ${service.health} ${service.status}`.toLowerCase();
    if (raw.includes("unhealthy") || raw.includes("exit") || raw.includes("dead")) return "error";
    if (raw.includes("starting") || raw.includes("created")) return "warn";
    if (raw.includes("running") || raw.includes("healthy") || raw.includes("up")) return "ok";
    return "gray";
  }

  function renderServiceStatus(payload) {
    const list = document.getElementById("service-list");
    const services = payload.services || [];
    list.replaceChildren();
    if (!services.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "服务未运行";
      list.append(empty);
    } else {
      services
        .sort((a, b) => a.service.localeCompare(b.service) || a.name.localeCompare(b.name))
        .forEach((service) => {
          const row = document.createElement("div");
          row.className = "service-row";
          const state = serviceState(service);
          row.innerHTML = `
            <span class="icon" style="background:var(--${state === "ok" ? "ok" : state === "error" ? "error" : "warn"}-soft); color:var(--${state === "ok" ? "ok" : state === "error" ? "error" : "warn"});">${app.escapeHtml((service.service || "?").slice(0, 2).toUpperCase())}</span>
            <div class="info"><div class="name">${app.escapeHtml(service.service || "-")}</div><div class="id">${app.escapeHtml(service.name || "-")}</div></div>
            <span class="chip ${state}">${app.escapeHtml(service.health || service.state || "unknown")}</span>
          `;
          list.append(row);
        });
    }
    const running = services.filter((item) => serviceState(item) === "ok");
    document.getElementById("api-count").textContent = running.filter((item) => item.service === "api").length;
    document.getElementById("worker-count").textContent = running.filter((item) => item.service === "worker").length;
    document.getElementById("queue-depth").textContent = payload.metrics?.queue_ready ?? "-";
    document.getElementById("runtime-count").textContent = payload.metrics?.active_runtimes ?? "-";
    document.getElementById("stack-target").textContent =
      `DOCKER COMPOSE · ${payload.target?.label || "auto"}`;
  }

  async function refreshDeployStatus() {
    try {
      const result = await app.consoleRequest(`/api/status?${deployTargetQuery()}`);
      if (!result.ok) throw new Error(app.apiError(result, "无法读取服务状态"));
      renderServiceStatus(result.body);
    } catch (error) {
      document.getElementById("service-list").innerHTML =
        `<div class="empty">${app.escapeHtml(error.message)}</div>`;
    }
    await refreshHealthCards();
  }

  async function refreshHealthCards() {
    const cards = document.querySelectorAll("#health-cards .health-card");
    for (const card of cards) {
      const endpoint = card.dataset.endpoint;
      const state = card.querySelector(".state");
      try {
        const result = await app.apiRequest(endpoint);
        if (result.ok) {
          const body = result.body;
          state.textContent =
            typeof body === "object"
              ? Object.keys(body).slice(0, 3).map((key) => `${key}=${body[key]}`).join(" ")
              : String(body).slice(0, 70);
          state.classList.remove("error");
        } else {
          state.textContent = `${result.status}`;
          state.classList.add("error");
        }
      } catch {
        state.textContent = "不可用";
        state.classList.add("error");
      }
    }
  }

  function refreshHeaderEnv() {
    const apiDot = document.getElementById("env-api-dot");
    const apiLabel = document.getElementById("env-api-label");
    app.apiRequest("/ready")
      .then((result) => {
        const ready = result.ok && result.body?.status === "ready";
        apiDot.className = `dot ${ready ? "ok" : "error"}`;
        apiLabel.textContent = ready ? `API 就绪 · ${result.body.instance_id}` : `API 异常 (${result.status})`;
      })
      .catch(() => {
        apiDot.className = "dot error";
        apiLabel.textContent = "API 不可达";
      });
    app.consoleRequest(`/api/status?${deployTargetQuery()}`)
      .then((result) => {
        const dockerDot = document.getElementById("env-docker-dot");
        const dockerLabel = document.getElementById("env-docker-label");
        if (result.body?.error) {
          dockerDot.className = "dot error";
          dockerLabel.textContent = "Docker 不可用";
        } else {
          dockerDot.className = "dot ok";
          dockerLabel.textContent = "Docker 已连接";
        }
      })
      .catch(() => {});
  }

  function initDeployStatus() {
    document.getElementById("deploy-refresh-btn").addEventListener("click", refreshDeployStatus);
    refreshDeployStatus();
  }

  // ------------------------------------------------------------- boot
  document.querySelectorAll(".primary-btn").forEach((btn) => {
    btn.addEventListener("click", () => app.setPrimary(btn.dataset.primary));
  });
  document.querySelectorAll(".side-item").forEach((item) => {
    item.addEventListener("click", () => app.gotoView(item.dataset.view));
  });
  window.addEventListener("hashchange", () => {
    const name = window.location.hash.slice(1);
    if (name && document.getElementById(`view-${name}`)) {
      activateView(name);
    }
  });

  app.registerView("deploy-actions", { init: initDeployActions });
  app.registerView("deploy-status", { init: initDeployStatus });

  initDock();
  const initial = window.location.hash.slice(1);
  app.gotoView(
    document.getElementById(`view-${initial}`) ? initial : "demo-overview"
  );
  refreshHeaderEnv();
  app.state.envTimer = setInterval(refreshHeaderEnv, 8000);

  window.ConsoleApp = app;
  window.App = {
    gotoView: app.gotoView,
    setPrimary: app.setPrimary,
    toast: app.toast,
    openFormModal: app.openFormModal,
    openDetailModal: app.openDetailModal,
    beginAction: app.beginAction,
  };
})();

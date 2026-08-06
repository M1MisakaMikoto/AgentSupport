(function () {
  "use strict";

  const app = window.ConsoleApp;
  if (!app) return;

  const state = {
    spec: null,
    operations: [],
    selected: null,
    history: [],
  };

  const els = app.els([
    "reload-spec", "explorer-search", "explorer-tag", "explorer-count",
    "operation-list", "request-title", "operation-summary", "request-builder",
    "request-method", "request-path", "path-params", "query-params",
    "idempotency-key", "correlation-id", "regenerate-keys",
    "body-editor", "body-required-badge", "reset-body",
    "send-request", "copy-curl", "request-status",
    "reference-body", "response-title", "response-status", "response-duration",
    "copy-response", "response-headers-text", "response-body",
  ]);

  function deref(schema) {
    let current = schema;
    const seen = new Set();
    while (current && typeof current.$ref === "string" && !seen.has(current.$ref)) {
      seen.add(current.$ref);
      const pointer = current.$ref.replace(/^#\//, "").split("/");
      current = pointer.reduce((node, key) => node?.[key], state.spec);
    }
    return current;
  }

  function exampleFromSchema(schema, depth = 0) {
    if (!schema || depth > 5) return null;
    schema = deref(schema);
    if (schema.example !== undefined) return schema.example;
    if (schema.default !== undefined) return schema.default;
    if (schema.enum?.length) return schema.enum[0];
    if (schema.anyOf) return exampleFromSchema(schema.anyOf[0], depth + 1);
    if (schema.oneOf) return exampleFromSchema(schema.oneOf[0], depth + 1);
    if (schema.allOf) {
      const merged = {};
      for (const sub of schema.allOf) {
        const value = exampleFromSchema(sub, depth + 1);
        if (value && typeof value === "object") Object.assign(merged, value);
      }
      return Object.keys(merged).length ? merged : null;
    }
    switch (schema.type) {
      case "object": {
        const obj = {};
        for (const [key, sub] of Object.entries(schema.properties || {})) {
          if (!schema.required || schema.required.includes(key)) {
            obj[key] = exampleFromSchema(sub, depth + 1);
          }
        }
        return obj;
      }
      case "array":
        return schema.items ? [exampleFromSchema(schema.items, depth + 1)] : [];
      case "string":
        if (schema.format === "uuid") return app.uuid();
        if (schema.format === "date-time") return new Date().toISOString();
        return "example";
      case "integer":
      case "number":
        return schema.minimum !== undefined ? schema.minimum : 1;
      case "boolean":
        return true;
      default:
        return null;
    }
  }

  function schemaDescriptor(schema, depth = 0) {
    if (!schema) return "any";
    schema = deref(schema);
    if (schema.enum) return schema.enum.map((item) => `"${item}"`).join(" | ");
    if (schema.type === "object") {
      const props = Object.entries(schema.properties || {});
      if (!props.length) return "object";
      return `object { ${props
        .slice(0, 8)
        .map(
          ([key, value]) =>
            `${key}${schema.required?.includes(key) ? "*" : ""}: ${schemaDescriptor(value, depth + 1)}`
        )
        .join(", ")}${props.length > 8 ? ", …" : ""} }`;
    }
    if (schema.type === "array") return `${schemaDescriptor(schema.items, depth + 1)}[]`;
    if (schema.type === "string") return schema.format ? `string(${schema.format})` : "string";
    if (schema.type === "integer" || schema.type === "number") return schema.type;
    if (schema.type === "boolean") return "boolean";
    if (schema.anyOf) return schema.anyOf.map((item) => schemaDescriptor(item, depth + 1)).join(" | ");
    return schema.type || "any";
  }

  function methodClass(method) {
    return ["get", "post", "put", "patch", "delete"].includes(method) ? method : "other";
  }

  function operationLabel(operation) {
    return `${operation.method.toUpperCase()} ${operation.path}`;
  }

  function filteredOperations() {
    const query = els["explorer-search"].value.trim().toLowerCase();
    const tag = els["explorer-tag"].value;
    return state.operations.filter((operation) => {
      if (tag && !(operation.tags || []).includes(tag)) return false;
      if (!query) return true;
      const haystack =
        `${operation.method} ${operation.path} ${operation.summary || ""} ${(operation.tags || []).join(" ")}`.toLowerCase();
      return haystack.includes(query);
    });
  }

  function renderList() {
    const container = els["operation-list"];
    container.replaceChildren();
    const items = filteredOperations();
    els["explorer-count"].textContent = `${items.length} / ${state.operations.length} 个操作`;
    const byTag = new Map();
    for (const operation of items) {
      const tag = (operation.tags || ["default"])[0] || "default";
      if (!byTag.has(tag)) byTag.set(tag, []);
      byTag.get(tag).push(operation);
    }
    for (const [tag, operations] of byTag) {
      const heading = document.createElement("div");
      heading.className = "operation-tag";
      heading.textContent = tag;
      container.append(heading);
      operations.forEach((operation) => {
        const item = document.createElement("div");
        item.className = "operation-item";
        if (operation === state.selected) item.classList.add("selected");
        const badge = document.createElement("span");
        badge.className = `method-badge ${methodClass(operation.method)}`;
        badge.textContent = operation.method.toUpperCase();
        const text = document.createElement("div");
        text.innerHTML = `<code>${app.escapeHtml(operation.path)}</code>`;
        if (operation.summary) {
          const summary = document.createElement("div");
          summary.className = "operation-summary";
          summary.textContent = operation.summary;
          text.append(summary);
        }
        item.append(badge, text);
        item.addEventListener("click", () => selectOperation(operation));
        container.append(item);
      });
    }
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "没有匹配的操作";
      container.append(empty);
    }
  }

  function parameterInput(parameter) {
    const schema = deref(parameter.schema || {});
    const wrap = document.createElement("div");
    wrap.className = "field";
    wrap.style.margin = "0";
    const label = document.createElement("label");
    label.textContent = `${parameter.name}${parameter.required ? " *" : ""}`;
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.paramName = parameter.name;
    input.placeholder = parameter.description || schema.description || parameter.name;
    wrap.append(label, input);
    return wrap;
  }

  function renderReference(operation) {
    const body = els["reference-body"];
    body.replaceChildren();
    const parameters = operation.parameters || [];
    if (parameters.length) {
      const heading = document.createElement("h4");
      heading.textContent = "参数";
      body.append(heading);
      const table = document.createElement("table");
      table.innerHTML =
        "<thead><tr><th>名称</th><th>位置</th><th>类型</th><th>必填</th><th>说明</th></tr></thead>";
      const tbody = document.createElement("tbody");
      parameters.forEach((parameter) => {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><code>${app.escapeHtml(parameter.name)}</code></td>
          <td>${app.escapeHtml(parameter.in)}</td>
          <td><code>${app.escapeHtml(schemaDescriptor(parameter.schema || {}))}</code></td>
          <td class="${parameter.required ? "required" : "optional"}">${parameter.required ? "是" : "否"}</td>
          <td>${app.escapeHtml(parameter.description || "")}</td>
        `;
        tbody.append(row);
      });
      table.append(tbody);
      body.append(table);
    }
    const bodySchema = operation.requestBody?.content?.["application/json"]?.schema;
    if (bodySchema) {
      const heading = document.createElement("h4");
      heading.textContent = `请求体（${operation.requestBody?.required ? "必填" : "可选"}）`;
      body.append(heading);
      const schemaLine = document.createElement("p");
      schemaLine.innerHTML = `<code>${app.escapeHtml(schemaDescriptor(bodySchema))}</code>`;
      body.append(schemaLine);
      const example = exampleFromSchema(bodySchema);
      if (example !== null && example !== undefined) {
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(example, null, 2);
        body.append(pre);
      }
    }
    const responses = Object.entries(operation.responses || {});
    if (responses.length) {
      const heading = document.createElement("h4");
      heading.textContent = "响应";
      body.append(heading);
      const table = document.createElement("table");
      table.innerHTML = "<thead><tr><th>状态码</th><th>说明</th><th>内容</th></tr></thead>";
      const tbody = document.createElement("tbody");
      responses.forEach(([code, response]) => {
        const schema =
          response.content?.["application/json"]?.schema ||
          response.content?.["text/plain"]?.schema;
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><code>${app.escapeHtml(code)}</code></td>
          <td>${app.escapeHtml(response.description || "")}</td>
          <td>${schema ? `<code>${app.escapeHtml(schemaDescriptor(schema))}</code>` : "-"}</td>
        `;
        tbody.append(row);
      });
      table.append(tbody);
      body.append(table);
    }
    if (!parameters.length && !bodySchema && !responses.length) {
      body.innerHTML = '<div class="empty" style="padding:10px;">该操作没有额外参考信息</div>';
    }
  }

  function renderBuilder() {
    const operation = state.selected;
    if (!operation) return;
    els["request-title"].textContent = operation.summary || operationLabel(operation);
    els["operation-summary"].textContent =
      operation.description || "选择参数并发送请求，响应会显示完整状态码、响应头与响应体。";
    els["request-builder"].hidden = false;
    els["request-method"].textContent = operation.method.toUpperCase();
    els["request-method"].className = `method-badge ${methodClass(operation.method)}`;
    els["request-path"].textContent = operation.path;

    const pathParams = (operation.parameters || []).filter((p) => p.in === "path");
    const queryParams = (operation.parameters || []).filter((p) => p.in === "query");
    els["path-params"].replaceChildren();
    els["query-params"].replaceChildren();
    els["path-params"].hidden = !pathParams.length;
    els["query-params"].hidden = !queryParams.length;
    if (pathParams.length) {
      const title = document.createElement("span");
      title.className = "entity-meta";
      title.textContent = "路径参数";
      els["path-params"].append(title);
      pathParams.forEach((parameter) => els["path-params"].append(parameterInput(parameter)));
    }
    if (queryParams.length) {
      const title = document.createElement("span");
      title.className = "entity-meta";
      title.textContent = "查询参数";
      els["query-params"].append(title);
      queryParams.forEach((parameter) => els["query-params"].append(parameterInput(parameter)));
    }

    const bodySchema = operation.requestBody?.content?.["application/json"]?.schema;
    const bodyRequired = Boolean(operation.requestBody?.required);
    const bodyBlock = document.getElementById("body-editor-block");
    if (bodySchema) {
      bodyBlock.hidden = false;
      els["body-required-badge"].textContent = bodyRequired ? "必填" : "可选";
      els["body-required-badge"].className = `chip ${bodyRequired ? "error" : "gray"}`;
      const example = exampleFromSchema(bodySchema);
      els["body-editor"].value =
        example === null || example === undefined ? "{}" : JSON.stringify(example, null, 2);
    } else {
      bodyBlock.hidden = true;
      els["body-editor"].value = "";
    }

    els["idempotency-key"].value = operation.method === "get" ? "" : `demo-${app.uuid()}`;
    els["correlation-id"].value = app.uuid();
    els["request-status"].textContent = "READY";
    els["request-status"].className = "chip gray";
    renderReference(operation);
  }

  function selectOperation(operation) {
    state.selected = operation;
    renderBuilder();
    renderList();
  }

  function buildUrl() {
    let path = els["request-path"].textContent;
    els["path-params"].querySelectorAll("input").forEach((input) => {
      const name = input.dataset.paramName;
      const value = input.value.trim();
      if (!value) throw new Error(`路径参数 ${name} 不能为空`);
      path = path.replaceAll(`{${name}}`, encodeURIComponent(value));
    });
    const query = new URLSearchParams();
    els["query-params"].querySelectorAll("input").forEach((input) => {
      const value = input.value.trim();
      if (value) query.append(input.dataset.paramName, value);
    });
    const queryString = query.toString();
    return queryString ? `${path}?${queryString}` : path;
  }

  function curlCommand(url, method, body) {
    const lines = [
      `curl -X ${method.toUpperCase()} '${window.location.origin}${app.apiBase}${url}'`,
      "  -H 'Accept: application/json'",
    ];
    const idempotency = els["idempotency-key"].value.trim();
    if (idempotency) lines.push(`  -H 'Idempotency-Key: ${idempotency}'`);
    const correlation = els["correlation-id"].value.trim();
    if (correlation) lines.push(`  -H 'X-Correlation-ID: ${correlation}'`);
    if (body) {
      lines.push("  -H 'Content-Type: application/json'", `  -d '${body}'`);
    }
    return lines.join(" \\\n");
  }

  async function sendRequest() {
    const operation = state.selected;
    if (!operation) return;
    let url;
    try {
      url = buildUrl();
    } catch (error) {
      app.toast(error.message, true);
      return;
    }
    const bodyText = els["body-editor"].value.trim();
    if (bodyText) {
      try {
        JSON.parse(bodyText);
      } catch {
        app.toast("请求体不是合法 JSON", true);
        return;
      }
    }
    const headers = {};
    const idempotency = els["idempotency-key"].value.trim();
    if (idempotency) headers["Idempotency-Key"] = idempotency;
    const correlation = els["correlation-id"].value.trim();
    if (correlation) headers["X-Correlation-ID"] = correlation;

    els["request-status"].textContent = "SENDING";
    els["request-status"].className = "chip warn";
    const started = performance.now();
    try {
      const result = await app.apiRequest(url, {
        method: operation.method.toUpperCase(),
        headers,
        body: bodyText || undefined,
      });
      const duration = Math.round(performance.now() - started);
      renderResponse(result, duration);
      els["request-status"].textContent = "DONE";
      els["request-status"].className = `chip ${result.ok ? "ok" : "error"}`;
    } catch (error) {
      els["request-status"].textContent = "ERROR";
      els["request-status"].className = "chip error";
      els["response-body"].textContent = `请求失败: ${error.message}`;
      app.toast(error.message, true);
    }
  }

  function renderResponse(result, duration) {
    els["response-title"].textContent = operationLabel(state.selected);
    els["response-status"].textContent = `${result.status} ${result.response.statusText}`;
    els["response-status"].className = `chip ${result.ok ? "ok" : "error"}`;
    els["response-duration"].textContent = `${duration} ms`;
    els["response-headers-text"].textContent = JSON.stringify(result.headers, null, 2);
    const isStream = (result.headers["content-type"] || "").includes("text/event-stream");
    if (isStream || typeof result.body !== "object" || result.body === null) {
      els["response-body"].textContent = result.text || "(空响应)";
    } else {
      els["response-body"].textContent = app.formatJson(result.body);
    }
  }

  async function loadSpec() {
    els["explorer-count"].textContent = "载入中…";
    try {
      const result = await app.apiRequest("/openapi.json");
      if (!result.ok) throw new Error(result.text || `${result.status}`);
      state.spec = result.body;
      state.operations = [];
      for (const [path, item] of Object.entries(state.spec.paths || {})) {
        for (const method of ["get", "post", "put", "patch", "delete"]) {
          const definition = item[method];
          if (!definition) continue;
          state.operations.push({
            method,
            path,
            summary: definition.summary || "",
            description: definition.description || "",
            operationId: definition.operationId || "",
            tags: definition.tags || [],
            parameters: definition.parameters || [],
            requestBody: definition.requestBody,
            responses: definition.responses || {},
          });
        }
      }
      state.operations.sort(
        (a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method)
      );
      const tags = new Set();
      state.operations.forEach((operation) =>
        (operation.tags || []).forEach((tag) => tags.add(tag))
      );
      const select = els["explorer-tag"];
      select.replaceChildren();
      const all = document.createElement("option");
      all.value = "";
      all.textContent = "全部";
      select.append(all);
      [...tags].sort().forEach((tag) => {
        const option = document.createElement("option");
        option.value = tag;
        option.textContent = tag;
        select.append(option);
      });
      renderList();
      app.toast(`OpenAPI 已载入：${state.operations.length} 个操作`);
    } catch (error) {
      els["explorer-count"].textContent = "载入失败";
      els["operation-list"].innerHTML =
        `<div class="empty">无法载入 OpenAPI: ${app.escapeHtml(error.message)}</div>`;
      app.toast(`无法载入 OpenAPI: ${error.message}`, true);
    }
  }

  function init() {
    els["explorer-search"].addEventListener("input", renderList);
    els["explorer-tag"].addEventListener("change", renderList);
    els["reload-spec"].addEventListener("click", loadSpec);
    els["regenerate-keys"].addEventListener("click", () => {
      els["idempotency-key"].value = `demo-${app.uuid()}`;
      els["correlation-id"].value = app.uuid();
    });
    els["reset-body"].addEventListener("click", () => {
      const operation = state.selected;
      if (!operation) return;
      const schema = operation.requestBody?.content?.["application/json"]?.schema;
      if (schema) {
        const example = exampleFromSchema(schema);
        els["body-editor"].value =
          example === null || example === undefined ? "{}" : JSON.stringify(example, null, 2);
      }
    });
    els["send-request"].addEventListener("click", sendRequest);
    els["copy-curl"].addEventListener("click", () => {
      if (!state.selected) return;
      try {
        const url = buildUrl();
        const body = els["body-editor"].value.trim();
        app.copyText(curlCommand(url, state.selected.method, body), "cURL 已复制");
      } catch (error) {
        app.toast(error.message, true);
      }
    });
    els["copy-response"].addEventListener("click", () =>
      app.copyText(els["response-body"].textContent, "响应已复制")
    );
    loadSpec();
  }

  app.registerView("deploy-api", { init });
})();

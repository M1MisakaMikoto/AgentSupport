(function () {
  "use strict";

  const app = window.ConsoleApp;
  if (!app) return;

  const els = app.els([
    "include-postgres", "include-task", "accept-button",
    "api-accept-negative", "api-accept-sse", "api-accept-task", "api-accept-button",
    "last-report-badge", "last-report-empty", "last-report",
  ]);

  function deployTargetPayload() {
    return {
      docker_transport: document.getElementById("docker-transport").value,
      docker_context: document.getElementById("docker-context").value.trim(),
      wsl_distribution: document.getElementById("wsl-distribution").value.trim(),
    };
  }

  async function beginDeployAcceptance() {
    await app.beginAction("accept", {
      ...deployTargetPayload(),
      expected_api_replicas: Number(document.getElementById("api-replicas").value),
      include_postgres: els["include-postgres"].checked,
      include_task_smoke: els["include-task"].checked,
    });
  }

  async function beginApiAcceptance() {
    await app.beginAction("api-accept", {
      include_task_flow: els["api-accept-task"].checked,
      include_negative: els["api-accept-negative"].checked,
      include_sse: els["api-accept-sse"].checked,
      timeout_seconds: 30,
    });
  }

  function renderLastReport(operation) {
    if (operation.status !== "succeeded") {
      els["last-report-badge"].textContent = "失败";
      els["last-report-badge"].className = "chip error";
      els["last-report-empty"].textContent = operation.error || "操作失败";
      els["last-report-empty"].hidden = false;
      els["last-report"].hidden = true;
      return;
    }
    els["last-report-empty"].hidden = true;
    els["last-report"].hidden = false;
    els["last-report"].replaceChildren();
    if (operation.action === "api-accept") {
      const report = operation.result;
      const summary = report.summary || {};
      const coverage = report.coverage || {};
      els["last-report-badge"].textContent =
        summary.failed === 0 ? `通过 ${summary.passed}/${summary.total}` : `失败 ${summary.failed}`;
      els["last-report-badge"].className = `chip ${summary.failed === 0 ? "ok" : "error"}`;
      const grid = document.createElement("div");
      grid.className = "grid stats-grid";
      grid.style.gridTemplateColumns = "repeat(4, 1fr)";
      grid.style.marginBottom = "8px";
      grid.innerHTML = `
        <div class="stat"><div class="label">用例总数</div><div class="value">${summary.total ?? 0}</div></div>
        <div class="stat"><div class="label">通过</div><div class="value ok">${summary.passed ?? 0}</div></div>
        <div class="stat"><div class="label">失败</div><div class="value ${summary.failed ? "error" : ""}">${summary.failed ?? 0}</div></div>
        <div class="stat"><div class="label">操作覆盖</div><div class="value accent">${coverage.operations_covered ?? 0}/${coverage.operations_total ?? 0}</div></div>
      `;
      els["last-report"].append(grid);
      const coverageBlock = document.createElement("div");
      coverageBlock.className = "coverage";
      coverageBlock.innerHTML = `
        <div class="bar"><span style="width:${coverage.coverage_pct ?? 0}%"></span></div>
        <div class="meta"><span>OpenAPI 操作覆盖率 ${coverage.coverage_pct ?? 0}%</span><span>未覆盖 ${(coverage.uncovered || []).length} 项</span></div>
      `;
      els["last-report"].append(coverageBlock);
    } else {
      const report = operation.result;
      els["last-report-badge"].textContent = "通过";
      els["last-report-badge"].className = "chip ok";
      els["last-report"].insertAdjacentHTML(
        "beforeend",
        app.detailListHtml([
          ["API 实例", (report.api_instances || []).join(", "), true],
          ["必需指标", (report.metrics || []).join(", "), true],
          ["任务闭环", report.conversation_id ? `已完成 (${report.conversation_id})` : "已跳过", true],
          ["事件序列", (report.event_types || []).join(", "), true],
        ])
      );
    }
  }

  function init() {
    els["accept-button"].addEventListener("click", beginDeployAcceptance);
    els["api-accept-button"].addEventListener("click", beginApiAcceptance);
    app.onOperationDone = renderLastReport;
  }

  app.registerView("deploy-acceptance", { init });
})();

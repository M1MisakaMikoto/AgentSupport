"""Interactive demo orchestrator for AgentSupport skill generation.

Run after starting the AgentSupport stack (API on :8000, runner on :8080):

    .venv/Scripts/python.exe demo/demo_server.py

Then open http://127.0.0.1:8900
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

AGENTSUPPORT_URL = "http://127.0.0.1:8000"
TENANT = "t-demo"
POLL_INTERVAL = 2.0
APPROVE_INTERVAL = 1.0

ROUNDS = [
    (
        "round1-create",
        (
            "你是办公室文档专员，正在听导师指导。导师第一个任务：在 workspace 用文档工具创建三个文件：\n"
            "1. report.docx：标题『季度销售报告』，3 个段落（背景简介、数据说明、结论预告）；\n"
            "2. data.xlsx：工作表名为 Sales，表头 Product/Qty/Price，3 行数据（A: 3, 100；B: 5, 200；C: 2, 150）；\n"
            "3. summary.pdf：标题『季度销售摘要』，2 个要点段落。\n"
            "创建完成后调用 task_done 结束，并回复三个文件路径。 禁止使用 bash 命令；文件操作只能使用 "
            "word_edit_tool、excel_edit_tool、pdf_tool 或 document_convert_tool。"
        ),
    ),
    (
        "round2-modify",
        (
            "导师看了三个文件，提出修改要求：\n"
            "1. 给 report.docx 追加一段结论：『本季度总销量 10 件，总销售额 1250 元。』；\n"
            "2. 在 data.xlsx 的 Sales 表追加一行：['D', 1, 300]；\n"
            "3. 用 excel_edit_tool 的 read 回读 data.xlsx，核对最后一行内容。\n"
            "完成后 task_done 并回复关键改动。 禁止使用 bash 命令；文件操作只能使用 "
            "word_edit_tool、excel_edit_tool、pdf_tool 或 document_convert_tool。"
        ),
    ),
    (
        "round3-convert",
        (
            "导师要求：用 document_convert_tool 的 docx_to_pdf 把 report.docx 转换为 report.pdf，"
            "输出到同一目录。完成后 task_done 并回复输出文件路径。 禁止使用 bash 命令；文件操作只能使用 "
            "word_edit_tool、excel_edit_tool、pdf_tool 或 document_convert_tool。"
        ),
    ),
    (
        "round4-summary",
        (
            "导师最后指导：把这次文档操作经验整理成 3 条可复用规则（工具选择、回读核对、转换注意点），"
            "直接回复即可，task_done 结束。 禁止使用 bash 命令。"
        ),
    ),
]

ACTIVATE_TASK = (
    "你是一名办公室文档专员。{file} 的 Sales 表只有明细行，缺少合计行。"
    "请用 excel_edit_tool 在最后一行下方追加合计行：Qty 列求和、Amount 列求和"
    "（Amount = Qty × Price），不要改动明细行。文件操作只能使用 word_edit_tool、excel_edit_tool、"
    "pdf_tool 或 document_convert_tool，禁止 bash。完成后 task_done 并简述你做了什么。"
)

app = FastAPI(title="AgentSupport Skill Demo")


class DemoState:
    def __init__(self) -> None:
        self.status = "idle"
        self.session_id: str | None = None
        self.round = 0
        self.rounds: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.generation: dict[str, Any] | None = None
        self.draft: dict[str, Any] | None = None
        self.review: dict[str, Any] | None = None
        self.activation: dict[str, Any] | None = None
        self.error: str | None = None
        self.active_skills: list[str] = []
        self.auto_approve = True
        self.pending_approval: dict[str, Any] | None = None
        self.summary: dict[str, Any] | None = None
        self.summary: dict[str, Any] | None = None
        self.pending_approval: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "session_id": self.session_id,
                "round": self.round,
                "rounds": list(self.rounds),
                "approvals": list(self.approvals),
                "generation": self.generation,
                "draft": self.draft,
                "review": self.review,
                "activation": self.activation,
                "error": self.error,
                "auto_approve": self.auto_approve,
                "pending_approval": self.pending_approval,
                "summary": self.summary,
                "active_skills": list(self.active_skills),
            }


STATE = DemoState()
client = httpx.Client(timeout=600)


def _post(path: str, body: dict[str, Any] | None = None, *, tenant: str = TENANT) -> dict[str, Any]:
    resp = client.post(
        f"{AGENTSUPPORT_URL}{path}",
        json=body or {},
        headers={"X-Tenant-Id": tenant},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"{path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _get(path: str, *, tenant: str = TENANT, params: dict[str, Any] | None = None) -> Any:
    resp = client.get(
        f"{AGENTSUPPORT_URL}{path}", headers={"X-Tenant-Id": tenant}, params=params
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _conversation_events(conversation_id: str) -> list[dict[str, Any]]:
    return _get(f"/conversations/{conversation_id}/events")


def _friendly(payload: dict[str, Any], limit: int = 220) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text[:limit] + ("…" if len(text) > limit else "")


def _round_summary(conversation_id: str) -> dict[str, Any]:
    events = _conversation_events(conversation_id)
    types = [e["type"] for e in events]
    tool_calls = [e["payload"].get("name") for e in events if e["type"] == "tool.call"]
    messages = [e["payload"].get("content", "") for e in events if e["type"] == "message"]
    usage = None
    for e in events:
        if e["type"] == "run.completed":
            result = e["payload"].get("result") or {}
            if isinstance(result, dict):
                usage = result.get("usage")
    return {
        "conversation_id": conversation_id,
        "state": "completed" if "run.completed" in types else "not-completed",
        "event_types": types,
        "tool_calls": tool_calls,
        "message": (
            messages[-1][:500]
            if messages
            else "（agent 本轮直接完成，未输出文本）"
        ),
        "usage": usage,
    }


class Approver(threading.Thread):
    """Auto-approve pending tool authorizations while a round is running."""

    def __init__(self, session_id: str) -> None:
        super().__init__(daemon=True)
        self.session_id = session_id
        self.stop_event = threading.Event()
        self.seen: set[tuple[str, str]] = set()
        self.retries: dict[tuple[str, str], int] = {}

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                convs = _get(f"/sessions/{self.session_id}/conversations")
                for conv in convs:
                    cid = conv["id"]
                    for e in _conversation_events(cid):
                        if e["type"] != "interaction.requested":
                            continue
                        payload = e["payload"]
                        if payload.get("kind") != "approval":
                            continue
                        key = (cid, payload.get("interaction_id"))
                        if key in self.seen:
                            continue
                        if self.retries.get(key, 0) >= 4:
                            continue
                        if not STATE.auto_approve:
                            STATE.pending_approval = {
                                "conversation_id": cid,
                                "approval_id": payload["interaction_id"],
                                "tools": [c.get("name") for c in (payload.get("tool_batch") or {}).get("calls", [])],
                            }
                            continue
                        resp = client.post(
                            f"{AGENTSUPPORT_URL}/conversations/{cid}/approval",
                            json={"approval_id": payload["interaction_id"], "decision": "APPROVE_ONCE"},
                            headers={"X-Tenant-Id": TENANT},
                            timeout=60,
                        )
                        self.retries[key] = self.retries.get(key, 0) + 1
                        if resp.status_code == 200:
                            self.seen.add(key)
                            STATE.pending_approval = None
                            STATE.pending_approval = None
                            batch = payload.get("tool_batch") or {}
                            names = [c.get("name") for c in batch.get("calls", [])]
                            STATE.approvals.append({"conversation": cid[:8], "tools": names, "auto": True})
            except Exception:
                pass
            time.sleep(APPROVE_INTERVAL)

    def stop(self) -> None:
        self.stop_event.set()


_current_approver: Approver | None = None
_pending_approval: dict[str, Any] | None = None


def _wait_terminal(conversation_id: str, timeout: float = 1200) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = _conversation_events(conversation_id)
        types = [e["type"] for e in events]
        if "run.completed" in types:
            return {"state": "completed"}
        if any(t in types for t in ("run.failed", "run.cancelled")):
            return {"state": "failed"}
        time.sleep(POLL_INTERVAL)
    return {"state": "timeout"}


def _run_round(n: int, task: str | None = None) -> None:
    try:
        assert STATE.session_id is not None
        task_text = task if task else ROUNDS[n][1]
        body: dict[str, Any] = {"task": task_text}
        if n > 0 and STATE.rounds and STATE.rounds[-1].get("conversation_id"):
            body["parent_conversation_id"] = STATE.rounds[-1]["conversation_id"]
        conv = _post(f"/sessions/{STATE.session_id}/conversations", body)
        STATE.round = n + 1
        result = _wait_terminal(conv["id"])
        summary = _round_summary(conv["id"])
        summary["state"] = result["state"]
        summary["task"] = task_text
        STATE.rounds.append(summary)
        STATE.status = "waiting_next" if result["state"] == "completed" else "error"
        if result["state"] != "completed":
            STATE.error = f"round {n + 1} ended with {result['state']}"
    except Exception as exc:  # noqa: BLE001
        STATE.status = "error"
        STATE.error = str(exc)


def _run_generation() -> None:
    try:
        assert STATE.session_id is not None
        g = _post(f"/sessions/{STATE.session_id}/skills/generate")
        gid = g["id"]
        deadline = time.time() + 1500
        status = g
        while time.time() < deadline:
            status = _get(f"/sessions/{STATE.session_id}/skills/generations/{gid}")
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(POLL_INTERVAL)
        STATE.generation = status
        if status.get("status") == "completed":
            drafts = _get("/skill-drafts", tenant=TENANT)
            if drafts:
                STATE.draft = {k: drafts[-1].get(k) for k in ("id", "skill_id", "status", "frontmatter")}
            STATE.status = "generated"
    except Exception as exc:  # noqa: BLE001
        STATE.status = "error"
        STATE.error = str(exc)


def _seed_sales_xlsx(path: str) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Product", "Qty", "Price", "Amount"])
    for product, qty, price in (("A", 3, 100), ("B", 5, 200), ("C", 2, 150)):
        sheet.append([product, qty, price, qty * price])
    workbook.save(path)


def _run_activation_case(label: str, file_name: str, skill_id: str | None) -> dict[str, Any]:
    _seed_sales_xlsx(rf"D:\workspace\{file_name}")
    workspace = _post("/workspaces", {"name": f"demo-{label}"}, tenant=TENANT)
    session = _post(
        "/sessions",
        {"workspace_id": workspace["id"], "tenant_id": TENANT, "name": label},
        tenant=TENANT,
    )
    body = {"task": ACTIVATE_TASK.format(file=file_name)}
    if skill_id:
        body["skills"] = [{"skill_id": skill_id, "enabled": True}]
    conv = _post(f"/sessions/{session['id']}/conversations", body)
    _wait_terminal(conv["id"], timeout=1200)
    from openpyxl import load_workbook

    workbook = load_workbook(rf"D:\workspace\{file_name}")
    sheet = workbook["Sales"]
    rows = list(sheet.iter_rows(values_only=True))
    data_rows = rows[1:]
    last = rows[-1] if rows else None
    if last and str(last[0]) == "合计":
        data_rows = rows[1:-1]
    qty_total = sum(row[1] for row in data_rows if isinstance(row[1], (int, float)))
    amount_total = sum(row[3] for row in data_rows if isinstance(row[3], (int, float)))
    summary = _round_summary(conv["id"])
    return {
        "label": label,
        "state": summary["state"],
        "approvals": len([a for a in STATE.approvals if a.get("label") == label]),
        "usage": summary["usage"],
        "final_rows": len(rows),
        "final_has_total": bool(last) and str(last[0]) == "合计",
        "final_qty_total": qty_total,
        "final_amount_total": amount_total,
    }


def _run_summarize() -> None:
    """Generate SKILL.md, auto-publish and report the file.

    The real model is occasionally flaky (finishes without embedding the
    markdown, or the provider is slow enough to trip the control-plane
    timeout), so retry a few times before reporting failure.
    """
    last_error: str | None = None
    for attempt in range(1, 4):
        try:
            assert STATE.session_id is not None
            STATE.summary = {
                "progress": "analyzing", "status": "running",
                "skill_id": None, "file_path": None, "attempt": attempt,
            }
            g = _post(f"/sessions/{STATE.session_id}/skills/generate")
            gid = g["id"]
            conversation_id = g.get("conversation_id")
            STATE.summary["progress"] = "generating"
            deadline = time.time() + 1500
            status = g
            while time.time() < deadline:
                status = _get(f"/sessions/{STATE.session_id}/skills/generations/{gid}")
                if status["status"] in ("completed", "failed"):
                    break
                time.sleep(POLL_INTERVAL)
            if status.get("status") != "completed":
                last_error = status.get("error") or f"generation failed on attempt {attempt}"
                time.sleep(2)
                continue
            drafts = _get("/skill-drafts", tenant=TENANT)
            if not drafts:
                last_error = "no draft produced"
                time.sleep(2)
                continue
            draft = max(drafts, key=lambda d: d["created_at"])
            STATE.summary["progress"] = "validating"
            STATE.summary["skill_id"] = draft["skill_id"]
            resp = client.post(
                f"{AGENTSUPPORT_URL}/skill-drafts/{draft['id']}/review",
                json={"decision": "approve", "note": "demo summarize"},
                headers={"X-Tenant-Id": TENANT},
                timeout=60,
            )
            if resp.status_code != 200:
                last_error = resp.text[:300]
                time.sleep(2)
                continue
            STATE.review = resp.json()
            STATE.draft = {k: draft.get(k) for k in ("id", "skill_id", "status", "frontmatter")}
            skill_id = draft["skill_id"]
            file_path = rf"skills\tenants\{TENANT}\{skill_id}\SKILL.md"
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()
            usage = None
            if conversation_id:
                for e in _conversation_events(conversation_id):
                    if e["type"] == "run.completed":
                        result = e["payload"].get("result") or {}
                        if isinstance(result, dict):
                            usage = result.get("usage")
            STATE.summary = {
                "progress": "done",
                "status": "completed",
                "skill_id": skill_id,
                "file_path": file_path,
                "content": content,
                "usage": usage,
                "attempt": attempt,
            }
            STATE.status = "reviewed"
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(2)
    STATE.summary = {"progress": "failed", "status": "failed", "error": last_error}
    STATE.status = "error"


def _run_activation() -> None:
    try:
        skill_id = (STATE.draft or {}).get("skill_id")
        results = []
        for label, file_name, sid in (
            ("with-skill", "orders4.py", skill_id),
            ("without-skill", "orders5.py", None),
        ):
            results.append(_run_activation_case(label, file_name, sid))
        STATE.activation = results
        STATE.status = "activated"
    except Exception as exc:  # noqa: BLE001
        STATE.status = "error"
        STATE.error = str(exc)


def _parse_frontmatter(content: str) -> dict[str, Any]:
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    frontmatter: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip().strip('"').strip("'")
    return frontmatter


def _published_skills() -> list[dict[str, Any]]:
    """List SKILL.md files published under the demo tenant (reviewed drafts)."""
    base = Path("skills") / "tenants" / TENANT
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        markdown = child / "SKILL.md"
        if not markdown.is_file():
            continue
        frontmatter = _parse_frontmatter(markdown.read_text(encoding="utf-8"))
        items.append(
            {
                "skill_id": child.name,
                "name": frontmatter.get("name") or child.name,
                "description": frontmatter.get("description") or "",
            }
        )
    return items


def _session_history_summary(session: dict[str, Any]) -> dict[str, Any]:
    """One session with its rounds, for the history browser."""
    convs = _get(f"/sessions/{session['id']}/conversations")
    rounds: list[dict[str, Any]] = []
    for conv in convs:
        summary = _round_summary(conv["id"])
        summary["task"] = conv.get("task")
        rounds.append(summary)
    return {
        "session_id": session["id"],
        "name": session.get("name") or f"会话 {session['id'][:8]}",
        "created_at": session.get("created_at"),
        "rounds": rounds,
    }


class StartBody(BaseModel):
    auto_approve: bool = True
    skills: list[str] = Field(default_factory=list)


class RoundBody(BaseModel):
    task: str | None = None


class ReviewBody(BaseModel):
    decision: str = "approve"


class ActivateBody(BaseModel):
    skill_id: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse("demo/skill-generation-demo.html")


@app.get("/api/state")
def state() -> dict[str, Any]:
    return STATE.snapshot()


@app.get("/api/history")
def history() -> list[dict[str, Any]]:
    sessions = _get("/sessions", params={"tenant_id": TENANT})
    return [_session_history_summary(s) for s in sessions[-20:]]


@app.get("/api/history/{session_id}")
def history_detail(session_id: str) -> dict[str, Any]:
    session = _get(f"/sessions/{session_id}")
    return _session_history_summary(session)


@app.get("/api/skills")
def skills() -> list[dict[str, Any]]:
    return _published_skills()


@app.post("/api/start")
def start(body: StartBody) -> dict[str, Any]:
    global _current_approver
    STATE.auto_approve = body.auto_approve
    workspace = _post("/workspaces", {"name": "demo"})
    config = None
    if body.skills:
        config = {
            "version": 1,
            "skills": [{"skill_id": skill_id, "enabled": True} for skill_id in body.skills],
        }
    session = _post(
        "/sessions",
        {
            "workspace_id": workspace["id"],
            "tenant_id": TENANT,
            "name": "demo-session",
            "config": config,
        },
    )
    STATE.session_id = session["id"]
    STATE.active_skills = list(body.skills)
    STATE.status = "ready"
    STATE.rounds = []
    STATE.approvals = []
    STATE.generation = None
    STATE.draft = None
    STATE.review = None
    STATE.activation = None
    STATE.summary = None
    STATE.error = None
    if _current_approver is not None:
        _current_approver.stop()
    _current_approver = Approver(session["id"])
    _current_approver.start()
    return STATE.snapshot()


@app.post("/api/round")
def round_step(body: RoundBody | None = None) -> dict[str, Any]:
    n = len(STATE.rounds)
    custom = bool(body and body.task and body.task.strip())
    if not custom and n >= len(ROUNDS):
        return STATE.snapshot()
    if n >= 30:
        raise RuntimeError("round limit reached (30)")
    if STATE.status not in ("ready", "waiting_next"):
        raise RuntimeError(f"cannot start round from status {STATE.status}")
    STATE.status = "running_round"
    threading.Thread(target=_run_round, args=(n, body.task if custom else None), daemon=True).start()
    return STATE.snapshot()


@app.post("/api/generate")
def generate() -> dict[str, Any]:
    if STATE.status not in ("waiting_next", "generated", "error"):
        raise RuntimeError(f"cannot generate from {STATE.status}")
    STATE.status = "running_generate"
    threading.Thread(target=_run_generation, daemon=True).start()
    return STATE.snapshot()


@app.get("/api/pending")
def pending() -> dict[str, Any]:
    return {"pending_approval": STATE.pending_approval}


@app.post("/api/approve-tool")
def approve_tool() -> dict[str, Any]:
    pending = STATE.pending_approval
    if not pending:
        raise RuntimeError("no pending tool approval")
    resp = client.post(
        AGENTSUPPORT_URL + "/conversations/" + pending["conversation_id"] + "/approval",
        json={"approval_id": pending["approval_id"], "decision": "APPROVE_ONCE"},
        headers={"X-Tenant-Id": TENANT},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(resp.text[:300])
    STATE.approvals.append({
        "conversation": pending["conversation_id"][:8],
        "tools": pending["tools"],
        "auto": False,
    })
    STATE.pending_approval = None
    return STATE.snapshot()


@app.post("/api/summarize")
def summarize() -> dict[str, Any]:
    if STATE.status not in ("waiting_next", "reviewed", "error"):
        raise RuntimeError(f"cannot summarize from {STATE.status}")
    STATE.summary = {"progress": "starting", "status": "running", "skill_id": None, "file_path": None}
    STATE.status = "running_generate"
    threading.Thread(target=_run_summarize, daemon=True).start()
    return STATE.snapshot()


@app.post("/api/review")
def review(body: ReviewBody) -> dict[str, Any]:
    draft = STATE.draft
    if not draft:
        raise RuntimeError("no draft")
    resp = client.post(
        f"{AGENTSUPPORT_URL}/skill-drafts/{draft['id']}/review",
        json={"decision": body.decision, "note": "demo"},
        headers={"X-Tenant-Id": TENANT},
    )
    if resp.status_code != 200:
        raise RuntimeError(resp.text[:300])
    STATE.review = resp.json()
    STATE.status = "reviewed"
    return STATE.snapshot()


@app.post("/api/activate")
def activate(body: ActivateBody) -> dict[str, Any]:
    if body.skill_id:
        STATE.draft = {**(STATE.draft or {}), "skill_id": body.skill_id}
    if STATE.status not in ("reviewed", "activated", "error"):
        raise RuntimeError(f"cannot activate from {STATE.status}")
    STATE.status = "running_activation"
    threading.Thread(target=_run_activation, daemon=True).start()
    return STATE.snapshot()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8900, log_level="info")

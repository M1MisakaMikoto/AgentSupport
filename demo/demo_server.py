"""Interactive demo orchestrator for AgentSupport skill generation.

Run after starting the AgentSupport stack (API on :8000, runner on :8080):

    .venv/Scripts/python.exe demo/demo_server.py

Then open http://127.0.0.1:8900
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

AGENTSUPPORT_URL = os.getenv("AGENTSUPPORT_DEMO_CONTROL_URL", "http://127.0.0.1:8000")
RUNNER_URL = os.getenv("AGENTSUPPORT_DEMO_RUNNER_URL", "http://127.0.0.1:8080")
TENANT = "t-demo"
UPLOAD_LOG_DIR = (
    Path(os.getenv("AGENTSUPPORT_WORKSPACE_ROOT", "workspace-data")) / ".demo" / "uploads"
)
POLL_INTERVAL = 2.0
APPROVE_INTERVAL = 1.0
LIVE_POLL_INTERVAL = 0.4

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
        self.round_cancel_requested = False
        self.uploads: list[dict[str, Any]] = []
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
        self.live: dict[str, Any] | None = None
        self.generation_busy = False
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            summary = self.summary
            if isinstance(summary, dict):
                summary = {
                    **summary,
                    "log": list(summary.get("log") or []),
                }
            return {
                "status": self.status,
                "session_id": self.session_id,
                "round": self.round,
                "rounds": list(self.rounds),
                "round_cancel_requested": self.round_cancel_requested,
                "uploads": list(self.uploads),
                "approvals": list(self.approvals),
                "generation": self.generation,
                "draft": self.draft,
                "review": self.review,
                "activation": self.activation,
                "error": self.error,
                "auto_approve": self.auto_approve,
                "pending_approval": self.pending_approval,
                "summary": summary,
                "active_skills": list(self.active_skills),
                "live": self.live,
            }


STATE = DemoState()
client = httpx.Client(timeout=600)

ALLOWED_UPLOAD_EXTENSIONS = {
    ".xlsx",
    ".xls",
    ".csv",
    ".docx",
    ".pdf",
    ".txt",
    ".md",
    ".json",
    ".log",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


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
    # The events API caps at list_default_limit (100) by default. A round that
    # drifts (many bash calls) can exceed that, hiding the newest tool approval
    # from the auto-approver and deadlocking the run. Ask for the full stream.
    return _get(f"/conversations/{conversation_id}/events", params={"limit": 1000})


def _friendly(payload: dict[str, Any], limit: int = 220) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text[:limit] + ("…" if len(text) > limit else "")


def _looks_like_file(path_value: str) -> bool:
    """True for paths that are real files or carry a file extension (skip dirs)."""
    try:
        candidate = Path(path_value).expanduser()
    except OSError:
        return False
    if candidate.suffix:
        return True
    try:
        return candidate.is_file()
    except OSError:
        return False


def _tool_summary(arguments: dict[str, Any] | None) -> str:
    args = arguments or {}
    parts: list[str] = []
    command = args.get("command")
    if isinstance(command, str) and command:
        parts.append(command)
    path = args.get("path") or args.get("input_path") or args.get("output_path")
    if isinstance(path, str) and path:
        parts.append(path)
    title = args.get("title")
    if isinstance(title, str) and title:
        parts.append(title)
    if "rows" in args:
        rows = args.get("rows") or []
        if isinstance(rows, list):
            parts.append(f"rows={len(rows)}")
    if args.get("content") is not None:
        text = str(args["content"])
        parts.append(text[:60] + ("…" if len(text) > 60 else ""))
    return " ".join(parts)[:180]


def _approval_was_auto(approval_id: str) -> bool | None:
    for entry in STATE.approvals or []:
        if entry.get("approval_id") == approval_id:
            return bool(entry.get("auto"))
    return None


#: approval_id -> auto flag, remembered so the live relay can mark a batch as
#: approved even when the approval lands before the batch row appears in STATE.live.
_live_approved: dict[str, bool] = {}


def _mark_live_approved(approval_id: str, auto: bool) -> None:
    """Write an approval decision back into the live tool batch.

    The runner's in-memory event stream carries interaction.requested and tool
    results but never approval.decided, so batches created by _live_relay would
    stay stuck in "审批中" unless the demo marks them itself. The approval id is
    recorded up front so a batch that appears later is still marked correctly.
    """
    _live_approved[approval_id] = auto
    with STATE._lock:
        live = STATE.live
        if not live:
            return
        for entry in live.get("batches") or []:
            if entry.get("approval_id") == approval_id:
                entry["approved"] = True
                entry["auto"] = auto


def _tool_batches(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild parallel tool batches from the conversation event stream.

    Batch boundaries are defined by approval interactions (tool_batch.calls);
    tool.call events arrive *before* their authorization, so they are not used
    to define batches. Results attach by call_id; calls that never went through
    an interaction (e.g. task_done) land in a trailing legacy batch.
    """
    batches: list[dict[str, Any]] = []
    by_hash: dict[str, dict[str, Any]] = {}
    call_map: dict[str, dict[str, Any]] = {}
    for event in events:
        etype = event["type"]
        payload = event.get("payload") or {}
        if etype == "interaction.requested" and payload.get("kind") == "approval":
            calls = (payload.get("tool_batch") or {}).get("calls") or []
            batch_hash = payload.get("tool_batch_hash") or f"b{len(batches)}"
            entry = by_hash.get(batch_hash)
            if entry is None:
                entry = {
                    "batch_hash": batch_hash,
                    "approval_id": payload.get("interaction_id"),
                    "tools": [],
                    "approved": False,
                    "auto": None,
                }
                by_hash[batch_hash] = entry
                batches.append(entry)
            for call in calls:
                tool = {
                    "call_id": call.get("call_id"),
                    "name": call.get("name"),
                    "summary": _tool_summary(call.get("arguments")),
                    "status": "pending",
                }
                entry["tools"].append(tool)
                call_map[tool["call_id"]] = tool
        elif etype == "approval.decided":
            approval_id = payload.get("approval_id")
            for entry in by_hash.values():
                if entry.get("approval_id") == approval_id:
                    entry["approved"] = True
                    entry["auto"] = _approval_was_auto(approval_id)

    leftover: list[dict[str, Any]] = []
    for event in events:
        etype = event["type"]
        payload = event.get("payload") or {}
        if etype == "tool.call":
            call_id = payload.get("call_id")
            if call_id not in call_map:
                leftover.append(
                    {
                        "call_id": call_id,
                        "name": payload.get("name"),
                        "summary": _tool_summary(payload.get("arguments")),
                        "status": "pending",
                    }
                )
        elif etype == "tool.result":
            tool = call_map.get(payload.get("call_id"))
            if tool is not None:
                tool["status"] = "ok" if payload.get("success") else "error"
                if payload.get("error"):
                    tool["error"] = str(payload["error"])[:120]
    if leftover:
        batches.append(
            {
                "batch_hash": f"b{len(batches)}",
                "approval_id": None,
                "tools": leftover,
                "approved": None,
                "auto": None,
            }
        )
    return batches


def _round_summary(conversation_id: str) -> dict[str, Any]:
    events = _conversation_events(conversation_id)
    types = [e["type"] for e in events]
    tool_calls = [e["payload"].get("name") for e in events if e["type"] == "tool.call"]
    file_paths: list[str] = []
    for e in events:
        if e["type"] != "tool.call":
            continue
        arguments = e["payload"].get("arguments") or {}
        if not isinstance(arguments, dict):
            continue
        for key in ("path", "input_path", "output_path"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip() and _looks_like_file(value.strip()):
                file_paths.append(value.strip())
    files = list(dict.fromkeys(file_paths))
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
        "tool_batches": _tool_batches(events),
        "files": files,
        "message": (
            messages[-1]
            if messages
            else "（agent 本轮直接完成，未输出文本）"
        ),
        "usage": usage,
    }


def _gen_note(summary: dict[str, Any], kind: str, text: str) -> None:
    """Append a state note to the live generation log (never touches rounds)."""
    with STATE._lock:
        if STATE.summary is not summary:
            return
        summary.setdefault("log", []).append(
            {"id": f"note:{time.monotonic_ns()}", "kind": kind, "text": text}
        )


def _pending_generation_question(conversation_id: str | None) -> dict[str, Any] | None:
    """Return the currently unanswered ask_user question of the generation run."""

    if not conversation_id:
        return None
    try:
        events = _conversation_events(conversation_id)
    except Exception:  # noqa: BLE001 - question feed is best effort
        return None
    pending: dict[str, Any] | None = None
    for event in events:
        etype = event.get("type", "")
        payload = event.get("payload") or {}
        if etype == "interaction.requested" and payload.get("kind") == "question":
            pending = {
                "conversation_id": conversation_id,
                "interaction_id": payload.get("interaction_id"),
                "question": payload.get("question") or "请确认总结范围。",
            }
        elif (
            etype == "interaction.input"
            and pending is not None
            and payload.get("interaction_id") == pending.get("interaction_id")
        ):
            pending = None
    return pending


def _merge_generation_log(
    summary: dict[str, Any],
    conversation_id: str | None,
    from_seq: int,
) -> int:
    """Merge new generation-conversation events (seq > from_seq) into the log.

    The generation conversation is separate from the demo rounds, so its
    tool trajectory is streamed into the panel log only; nothing is appended
    to STATE.rounds (the chat area).
    """
    if not conversation_id:
        return from_seq
    try:
        events = _conversation_events(conversation_id)
    except Exception:  # noqa: BLE001 - progress feed is best effort
        return from_seq
    max_seq = from_seq
    with STATE._lock:
        if STATE.summary is not summary:
            return max_seq
        log = summary.setdefault("log", [])
        by_call: dict[str, str] = {}
        for entry in log:
            call_id = entry.get("call_id")
            if call_id:
                by_call[call_id] = entry["id"]
        for event in events:
            seq = event.get("seq")
            if not isinstance(seq, int) or seq <= from_seq:
                continue
            max_seq = max(max_seq, seq)
            etype = event.get("type", "")
            payload = event.get("payload") or {}
            if etype == "tool.call":
                call_id = payload.get("call_id")
                entry_id = f"tool:{call_id or seq}"
                log.append(
                    {
                        "id": entry_id,
                        "call_id": call_id,
                        "kind": "tool",
                        "name": payload.get("name") or "工具",
                        "summary": _tool_summary(payload.get("arguments")),
                        "status": "running",
                    }
                )
                if call_id:
                    by_call[call_id] = entry_id
            elif etype == "tool.result":
                call_id = payload.get("call_id")
                entry_id = by_call.get(call_id) if call_id else None
                if entry_id:
                    for entry in log:
                        if entry.get("id") == entry_id:
                            entry["status"] = "ok" if payload.get("success") else "error"
                            if payload.get("error"):
                                entry["error"] = str(payload["error"])[:160]
                            break
                else:
                    log.append(
                        {
                            "id": f"res:{seq}",
                            "kind": "tool",
                            "name": payload.get("name") or "工具",
                            "summary": "工具完成",
                            "status": "ok" if payload.get("success") else "error",
                        }
                    )
            elif etype == "message":
                content = (payload.get("content") or "").strip()
                if content:
                    log.append(
                        {"id": f"msg:{seq}", "kind": "agent", "text": content[:300]}
                    )
            elif etype == "interaction.requested" and payload.get("kind") == "question":
                log.append(
                    {
                        "id": f"ask:{seq}",
                        "kind": "ask",
                        "text": payload.get("question") or "请确认总结范围。",
                    }
                )
    return max_seq


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
                            _mark_live_approved(payload["interaction_id"], True)
                            STATE.pending_approval = None
                            STATE.pending_approval = None
                            batch = payload.get("tool_batch") or {}
                            names = [c.get("name") for c in batch.get("calls", [])]
                            STATE.approvals.append(
                                {
                                    "conversation": cid[:8],
                                    "tools": names,
                                    "auto": True,
                                    "approval_id": payload["interaction_id"],
                                }
                            )
            except Exception:
                pass
            time.sleep(APPROVE_INTERVAL)

    def stop(self) -> None:
        self.stop_event.set()


_current_approver: Approver | None = None
_pending_approval: dict[str, Any] | None = None


def _wait_terminal(
    conversation_id: str,
    timeout: float = 1200,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cancel_check is not None and cancel_check():
            return {"state": "cancelled"}
        events = _conversation_events(conversation_id)
        types = [e["type"] for e in events]
        if "run.completed" in types:
            return {"state": "completed"}
        if any(t in types for t in ("run.failed", "run.cancelled")):
            return {"state": "failed"}
        time.sleep(POLL_INTERVAL)
    return {"state": "timeout"}


def _live_relay(run_id: str, conversation_id: str) -> None:
    """Poll the runner's in-memory event stream and mirror it into STATE.live."""
    cursor = 0
    while True:
        with STATE._lock:
            live = STATE.live
        if not live or live.get("run_id") != run_id:
            return
        try:
            resp = client.get(
                f"{RUNNER_URL}/runs/{run_id}/events",
                params={"after_seq": cursor},
                timeout=10,
            )
            if resp.status_code != 200:
                time.sleep(LIVE_POLL_INTERVAL)
                continue
            events = resp.json()
            if not events:
                time.sleep(LIVE_POLL_INTERVAL)
                continue
            with STATE._lock:
                live = STATE.live
                if not live or live.get("run_id") != run_id:
                    return
                for event in events:
                    seq = int(event.get("seq") or 0)
                    if seq <= cursor:
                        continue
                    cursor = seq
                    etype = event.get("type", "")
                    payload = event.get("payload") or {}
                    if etype == "interaction.requested" and payload.get("kind") == "approval":
                        calls = (payload.get("tool_batch") or {}).get("calls") or []
                        approval_id = payload.get("interaction_id")
                        auto = _live_approved.get(approval_id)
                        live.setdefault("batches", []).append(
                            {
                                "batch_hash": payload.get("tool_batch_hash")
                                or f"b{len(live['batches'])}",
                                "approval_id": approval_id,
                                "approved": auto is not None,
                                "auto": auto,
                                "tools": [
                                    {
                                        "call_id": call.get("call_id"),
                                        "name": call.get("name"),
                                        "summary": _tool_summary(call.get("arguments")),
                                        "status": "pending",
                                    }
                                    for call in calls
                                ],
                            }
                        )
                    elif etype == "tool.call":
                        name = payload.get("name")
                        if name:
                            live.setdefault("tool_calls", []).append(name)
                        arguments = payload.get("arguments") or {}
                        if isinstance(arguments, dict):
                            for key in ("path", "input_path", "output_path"):
                                value = arguments.get(key)
                                if (
                                    isinstance(value, str)
                                    and value.strip()
                                    and _looks_like_file(value.strip())
                                ):
                                    path = value.strip()
                                    if path not in live.setdefault("files", []):
                                        live["files"].append(path)
                    elif etype == "tool.result":
                        call_id = payload.get("call_id")
                        for entry in reversed(live.get("batches") or []):
                            for tool in entry.get("tools", []):
                                if tool.get("call_id") == call_id:
                                    tool["status"] = "ok" if payload.get("success") else "error"
                                    if payload.get("error"):
                                        tool["error"] = str(payload["error"])[:120]
                                    break
                    elif etype == "message.delta":
                        delta = payload.get("delta") or ""
                        if delta:
                            live["text"] = live.get("text", "") + delta
                    elif etype == "message":
                        content = payload.get("content") or ""
                        if not live.get("text") or len(live.get("text", "")) < len(content) * 0.6:
                            live["text"] = content
                    elif etype in ("run.completed", "run.failed", "run.cancelled"):
                        live["status"] = "done"
                live["updated_at"] = time.time()
        except Exception:  # noqa: BLE001 - live relay is best-effort
            pass
        time.sleep(LIVE_POLL_INTERVAL)


def _run_round(n: int, task: str | None = None) -> None:
    try:
        assert STATE.session_id is not None
        task_text = task if task else ROUNDS[n][1]
        body: dict[str, Any] = {"task": task_text}
        if n > 0 and STATE.rounds and STATE.rounds[-1].get("conversation_id"):
            body["parent_conversation_id"] = STATE.rounds[-1]["conversation_id"]
        conv = _post(f"/sessions/{STATE.session_id}/conversations", body)
        run_id = ((conv.get("run") or {}).get("run_id")) or conv.get("run_id")
        if not run_id:
            detail = _get(f"/conversations/{conv['id']}")
            run_id = ((detail.get("run") or {}).get("run_id")) or detail.get("run_id")
        STATE.round = n + 1
        with STATE._lock:
            STATE.live = {
                "run_id": run_id,
                "conversation_id": conv["id"],
                "text": "",
                "tool_calls": [],
                "files": [],
                "status": "running",
                "updated_at": time.time(),
            }
        if run_id:
            threading.Thread(
                target=_live_relay, args=(run_id, conv["id"]), daemon=True
            ).start()
        result = _wait_terminal(
            conv["id"], cancel_check=lambda: STATE.round_cancel_requested
        )
        summary = _round_summary(conv["id"])
        summary["state"] = result["state"]
        summary["task"] = task_text
        cancelled = STATE.round_cancel_requested or result["state"] == "cancelled"
        if cancelled:
            summary["cancelled"] = True
        STATE.rounds.append(summary)
        with STATE._lock:
            if STATE.live and STATE.live.get("run_id") == run_id:
                STATE.live["status"] = "done"
                STATE.live["updated_at"] = time.time()
        if cancelled:
            # 用户主动取消：会话保持可用，不进入异常状态
            STATE.status = "waiting_next"
            STATE.error = None
        else:
            STATE.status = "waiting_next" if result["state"] == "completed" else "error"
            if result["state"] != "completed":
                STATE.error = f"round {n + 1} ended with {result['state']}"
    except Exception as exc:  # noqa: BLE001
        if STATE.round_cancel_requested:
            STATE.status = "waiting_next"
            STATE.error = None
        else:
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
    workspace = _post("/workspaces", {"name": f"demo-{label}"}, tenant=TENANT)
    workspace_root = Path(workspace["root_path"])
    _seed_sales_xlsx(workspace_root / file_name)
    session = _post(
        "/sessions",
        {
            "workspace_id": workspace["id"],
            "tenant_id": TENANT,
            "name": label,
            # The candidate pool lives on the session; per-conversation picking is gone.
            **(
                {
                    "config": {
                        "version": 1,
                        "skills": [{"skill_id": skill_id, "enabled": True}],
                    }
                }
                if skill_id
                else {}
            ),
        },
        tenant=TENANT,
    )
    body = {"task": ACTIVATE_TASK.format(file=file_name)}
    conv = _post(f"/sessions/{session['id']}/conversations", body)
    _wait_terminal(conv["id"], timeout=1200)
    from openpyxl import load_workbook

    workbook = load_workbook(workspace_root / file_name)
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


def _run_summarize_impl() -> None:
    """Generate SKILL.md, auto-publish and report the file.

    The real model is occasionally flaky (finishes without embedding the
    markdown, or the provider is slow enough to trip the control-plane
    timeout), so retry a few times before reporting failure.

    The generation runs in its own conversation; its progress is streamed
    through STATE.summary.log (rendered in the panel above the input), never
    into STATE.rounds (the chat area).
    """
    last_error: str | None = None
    summary: dict[str, Any] | None = None
    for attempt in range(1, 4):
        if summary is not None and summary.get("cancel_requested"):
            return
        try:
            assert STATE.session_id is not None
            STATE.status = "running_generate"
            with STATE._lock:
                summary = {
                    "progress": "analyzing",
                    "status": "running",
                    "skill_id": None,
                    "file_path": None,
                    "attempt": attempt,
                    "gen_id": None,
                    "conversation_id": None,
                    "cancel_requested": False,
                    "log": [],
                }
                STATE.summary = summary
            _gen_note(
                summary,
                "info",
                f"开始分析原会话 {STATE.session_id[:8]} 历史（只读引用，不写入原会话）",
            )
            g = _post(f"/sessions/{STATE.session_id}/skills/generate")
            gid = g["id"]
            conversation_id = g.get("conversation_id")
            gen_id = "g-" + gid[:8]
            with STATE._lock:
                if STATE.summary is summary:
                    summary["gen_id"] = gen_id
                    summary["conversation_id"] = conversation_id
            _gen_note(
                summary,
                "ok",
                f"生成会话 {gen_id} 已创建：把原会话总结成 SKILL，过程与产物相互隔离",
            )
            summary["progress"] = "generating"
            _gen_note(summary, "info", "生成 agent 正在读取事件文件并编写 SKILL.md…")
            deadline = time.time() + 1500
            status = g
            seen_seq = 0
            awaiting_question = False
            while time.time() < deadline:
                if summary.get("cancel_requested"):
                    _gen_note(summary, "warn", "已取消本次生成")
                    STATE.status = "waiting_next"
                    return
                status = _get(f"/sessions/{STATE.session_id}/skills/generations/{gid}")
                seen_seq = _merge_generation_log(summary, conversation_id, seen_seq)
                question = _pending_generation_question(conversation_id)
                if question is not None:
                    summary["pending_question"] = question
                    if not awaiting_question:
                        _gen_note(summary, "ask", "等待你确认意图…")
                    awaiting_question = True
                elif awaiting_question:
                    awaiting_question = False
                    summary.pop("pending_question", None)
                    _gen_note(summary, "ok", "已收到你的确认，继续生成…")
                if status["status"] in ("completed", "failed"):
                    break
                time.sleep(POLL_INTERVAL)
            if summary.get("cancel_requested"):
                STATE.status = "waiting_next"
                return
            if status.get("status") != "completed":
                last_error = status.get("error") or f"generation failed on attempt {attempt}"
                _gen_note(summary, "warn", f"生成未完成：{last_error}")
                time.sleep(2)
                continue
            drafts = _get("/skill-drafts", tenant=TENANT)
            if not drafts:
                last_error = "no draft produced"
                _gen_note(summary, "warn", "未生成草稿")
                time.sleep(2)
                continue
            draft = max(drafts, key=lambda d: d["created_at"])
            summary["progress"] = "validating"
            summary["skill_id"] = draft["skill_id"]
            _gen_note(summary, "ok", "SKILL.md 内容校验通过，正在生成草稿…")
            resp = client.post(
                f"{AGENTSUPPORT_URL}/skill-drafts/{draft['id']}/review",
                json={"decision": "approve", "note": "demo summarize"},
                headers={"X-Tenant-Id": TENANT},
                timeout=60,
            )
            if resp.status_code != 200:
                last_error = resp.text[:300]
                _gen_note(summary, "warn", f"草稿审核失败：{last_error}")
                time.sleep(2)
                continue
            STATE.review = resp.json()
            STATE.draft = {k: draft.get(k) for k in ("id", "skill_id", "status", "frontmatter")}
            skill_id = draft["skill_id"]
            skills_root = Path(os.environ.get("AGENTSUPPORT_SKILLS_ROOT", "skills"))
            file_path = skills_root / "tenants" / TENANT / skill_id / "SKILL.md"
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read()
            usage = None
            if conversation_id:
                for e in _conversation_events(conversation_id):
                    if e["type"] == "run.completed":
                        result = e["payload"].get("result") or {}
                        if isinstance(result, dict):
                            usage = result.get("usage")
            summary.update(
                {
                    "progress": "done",
                    "status": "completed",
                    "skill_id": skill_id,
                    "file_path": str(file_path),
                    "content": content,
                    "usage": usage,
                    "attempt": attempt,
                }
            )
            _gen_note(summary, "ok", f"草稿已入库：{skill_id} → {file_path}")
            _gen_note(
                summary,
                "ok",
                "原会话聊天框全程零写入（生成内容只显示在独立面板）",
            )
            STATE.status = "reviewed"
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if summary is not None:
                _gen_note(summary, "warn", f"生成过程异常：{last_error}")
            time.sleep(2)
    if summary is not None:
        with STATE._lock:
            summary.update({"progress": "failed", "status": "failed", "error": last_error})
        _gen_note(summary, "warn", f"总结失败：{last_error}")
    STATE.status = "error"


def _run_summarize() -> None:
    """Wrapper that marks the generation thread busy until it fully exits."""

    with STATE._lock:
        STATE.generation_busy = True
    try:
        _run_summarize_impl()
    finally:
        with STATE._lock:
            STATE.generation_busy = False


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
    # 生成会话以 mode=silent 创建（skill 提炼 agent）。它们属于该 session 的
    # 记录：不进聊天框/轮次，但在历史里单独列为「生成会话（不可继续）」。
    conversations = _get(f"/sessions/{session['id']}/conversations")
    rounds: list[dict[str, Any]] = []
    generations: list[dict[str, Any]] = []
    for conv in conversations:
        run = conv.get("run") or {}
        if (conv.get("mode") or "default") != "default":
            error = run.get("error") or {}
            generations.append(
                {
                    "conversation_id": conv["id"],
                    "created_at": conv.get("created_at"),
                    "state": run.get("state") or "UNKNOWN",
                    "error": error.get("message") if isinstance(error, dict) else None,
                }
            )
            continue
        summary = _round_summary(conv["id"])
        summary["task"] = conv.get("task")
        rounds.append(summary)
    return {
        "session_id": session["id"],
        "name": session.get("name") or f"会话 {session['id'][:8]}",
        "created_at": session.get("created_at"),
        "rounds": rounds,
        "generations": generations,
        "uploads": _read_upload_records(session.get("workspace_id")),
    }


def _upload_log_path(workspace_id: str | None) -> Path | None:
    if not workspace_id:
        return None
    return UPLOAD_LOG_DIR / f"{workspace_id}.json"


def _read_upload_records(workspace_id: str | None) -> list[dict[str, Any]]:
    """Upload records for one workspace, newest last (empty when none)."""

    path = _upload_log_path(workspace_id)
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _append_upload_record(workspace_id: str | None, record: dict[str, Any]) -> None:
    path = _upload_log_path(workspace_id)
    if path is None:
        return
    records = _read_upload_records(workspace_id)
    records.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class StartBody(BaseModel):
    auto_approve: bool = True
    skills: list[str] = Field(default_factory=list)
    file_ref_format: bool = True


class RoundBody(BaseModel):
    task: str | None = None


class SkillsSelectBody(BaseModel):
    skills: list[str] = Field(default_factory=list)


class ReviewBody(BaseModel):
    decision: str = "approve"


class ActivateBody(BaseModel):
    skill_id: str | None = None


class AnswerBody(BaseModel):
    value: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent / "skill-generation-demo.html")


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


def _resolve_skill_markdown(skill_id: str) -> Path:
    base = Path("skills") / "tenants" / TENANT
    markdown = base / skill_id / "SKILL.md"
    try:
        resolved = markdown.resolve()
        base_resolved = base.resolve()
    except OSError as exc:
        raise RuntimeError("skill not found") from exc
    if (
        not str(resolved).startswith(str(base_resolved))
        or not markdown.is_file()
    ):
        raise RuntimeError("skill not found")
    return markdown


@app.get("/api/skills/{skill_id}")
def skill_detail(skill_id: str) -> dict[str, Any]:
    """Return one published skill's frontmatter and SKILL.md content."""

    markdown = _resolve_skill_markdown(skill_id)
    content = markdown.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(content)
    return {
        "skill_id": skill_id,
        "name": frontmatter.get("name") or skill_id,
        "description": frontmatter.get("description") or "",
        "content": content,
    }


@app.get("/api/skills/{skill_id}/download")
def skill_download(skill_id: str) -> FileResponse:
    """Download a published SKILL.md file with its original content."""

    markdown = _resolve_skill_markdown(skill_id)
    return FileResponse(
        markdown,
        media_type="text/markdown",
        filename=f"{skill_id}-SKILL.md",
    )


@app.post("/api/start")
def start(body: StartBody) -> dict[str, Any]:
    global _current_approver
    if STATE.generation_busy or STATE.status in ("running_round", "running_generate"):
        raise RuntimeError("cannot start a new session while a round/generation is running")
    STATE.auto_approve = body.auto_approve
    workspace = _post("/workspaces", {"name": "demo"})
    config = None
    if body.skills or body.file_ref_format:
        config = {
            "version": 1,
            "skills": [{"skill_id": skill_id, "enabled": True} for skill_id in body.skills],
            "file_ref_format": body.file_ref_format,
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
    STATE.uploads = []
    STATE.approvals = []
    _live_approved.clear()
    STATE.live = None
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


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a file into the active session's workspace (demo analysis inputs)."""

    if STATE.status in ("running_round", "running_generate") or STATE.generation_busy:
        raise HTTPException(409, "cannot upload while a round/generation is running")
    if not STATE.session_id:
        raise HTTPException(400, "no active session")
    raw_name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not raw_name or raw_name in (".", ".."):
        raise HTTPException(400, "invalid filename")
    extension = Path(raw_name).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            415,
            "unsupported file type; allowed: "
            + ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS)),
        )
    session = _get(f"/sessions/{STATE.session_id}", tenant=TENANT)
    workspace = _get(f"/workspaces/{session['workspace_id']}", tenant=TENANT)
    root = Path(str(workspace.get("root_path") or ""))
    if not root.is_dir():
        raise HTTPException(500, f"workspace directory not found: {root}")
    target = root / raw_name
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, "file too large (max 25MB)")
                out.write(chunk)
    finally:
        await file.close()
    record = {
        "filename": raw_name,
        "path": str(target),
        "size": size,
        "uploaded_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "session_id": STATE.session_id,
        "workspace_id": session["workspace_id"],
    }
    _append_upload_record(str(session["workspace_id"]), record)
    with STATE._lock:
        STATE.uploads = _read_upload_records(session["workspace_id"])
    return {**record, "uploads": list(STATE.uploads)}


@app.post("/api/round")
def round_step(body: RoundBody | None = None) -> dict[str, Any]:
    n = len(STATE.rounds)
    custom = bool(body and body.task and body.task.strip())
    if not custom and n >= len(ROUNDS):
        return STATE.snapshot()
    if n >= 30:
        raise RuntimeError("round limit reached (30)")
    # error 状态也允许继续：上一轮异常后应能直接补发指令，不必绕历史会话
    if STATE.status not in ("ready", "waiting_next", "error"):
        raise RuntimeError(f"cannot start round from status {STATE.status}")
    STATE.status = "running_round"
    STATE.live = None
    STATE.error = None
    STATE.round_cancel_requested = False
    threading.Thread(target=_run_round, args=(n, body.task if custom else None), daemon=True).start()
    return STATE.snapshot()


@app.post("/api/cancel-round")
def cancel_round() -> dict[str, Any]:
    """取消正在运行的轮次：本地立即放行，远端 run 尽力一并取消。"""

    if STATE.status != "running_round":
        raise RuntimeError("no running round")
    STATE.round_cancel_requested = True
    live = STATE.live or {}
    conversation_id = live.get("conversation_id")
    STATE.status = "waiting_next"
    STATE.error = None
    remote = "untracked"
    if conversation_id:
        try:
            resp = client.post(
                f"{AGENTSUPPORT_URL}/conversations/{conversation_id}/cancel",
                headers={"X-Tenant-Id": TENANT},
                timeout=30,
            )
            remote = "ok" if resp.status_code == 200 else f"http {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            remote = f"error {exc}"
    # 等本轮记录落地（_run_round 会在下一个轮询点结束并写入 cancelled 标记），
    # 这样前端收到响应后重绘就能直接显示「本轮已取消」。
    deadline = time.time() + 15
    while time.time() < deadline:
        if STATE.rounds and STATE.rounds[-1].get("cancelled"):
            break
        time.sleep(0.2)
    snapshot = STATE.snapshot()
    snapshot["round_cancel"] = {
        "conversation_id": conversation_id,
        "remote": remote,
    }
    return snapshot


@app.post("/api/skills-select")
def skills_select(body: SkillsSelectBody) -> dict[str, Any]:
    if STATE.status in ("running_round", "running_generate"):
        raise RuntimeError(f"cannot change skills while {STATE.status}")
    published = {item["skill_id"] for item in _published_skills()}
    unknown = sorted(set(body.skills) - published)
    if unknown:
        raise RuntimeError(f"unknown skills: {', '.join(unknown)}")
    STATE.active_skills = list(dict.fromkeys(body.skills))
    return STATE.snapshot()


@app.post("/api/generate")
def generate() -> dict[str, Any]:
    if STATE.status not in ("waiting_next", "generated", "error"):
        raise RuntimeError(f"cannot generate from {STATE.status}")
    STATE.status = "running_generate"
    STATE.live = None
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
        "approval_id": pending["approval_id"],
    })
    _mark_live_approved(pending["approval_id"], False)
    STATE.pending_approval = None
    return STATE.snapshot()


@app.post("/api/summarize")
def summarize() -> dict[str, Any]:
    if STATE.generation_busy:
        raise RuntimeError("generation is still shutting down; cancel or wait a moment")
    if STATE.status not in ("waiting_next", "reviewed", "error"):
        raise RuntimeError(f"cannot summarize from {STATE.status}")
    STATE.summary = {
        "progress": "starting",
        "status": "running",
        "skill_id": None,
        "file_path": None,
        "attempt": 1,
        "gen_id": None,
        "conversation_id": None,
        "cancel_requested": False,
        "log": [],
    }
    STATE.status = "running_generate"
    STATE.live = None
    threading.Thread(target=_run_summarize, daemon=True).start()
    return STATE.snapshot()


@app.post("/api/answer-question")
def answer_question(body: AnswerBody) -> dict[str, Any]:
    pending = (STATE.summary or {}).get("pending_question")
    if not pending:
        raise RuntimeError("no pending question")
    resp = client.post(
        f"{AGENTSUPPORT_URL}/conversations/{pending['conversation_id']}/input",
        json={
            "interaction_id": pending["interaction_id"],
            "value": body.value,
        },
        headers={"X-Tenant-Id": TENANT},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(resp.text[:300])
    with STATE._lock:
        summary = STATE.summary
        if summary is not None:
            summary.pop("pending_question", None)
            summary.setdefault("log", []).append(
                {
                    "id": f"answer:{time.monotonic_ns()}",
                    "kind": "ok",
                    "text": f"你已回复：{body.value}",
                }
            )
    return STATE.snapshot()


@app.post("/api/cancel-generation")
def cancel_generation() -> dict[str, Any]:
    """Cancel the running skill-generation conversation and stop retries."""

    summary = STATE.summary
    if not summary or summary.get("status") != "running":
        raise RuntimeError("no running generation")
    conversation_id = summary.get("conversation_id") or (
        (summary.get("pending_question") or {}).get("conversation_id")
    )
    with STATE._lock:
        summary["cancel_requested"] = True
        summary.pop("pending_question", None)
        summary.update(
            {
                "progress": "cancelled",
                "status": "failed",
                "error": "用户取消",
            }
        )
    _gen_note(summary, "warn", "已收到取消请求，正在停止生成会话…")
    # 本地意图是取消的唯一依据：生成会话可能还没建立（点得太早），
    # 远端取消只是加速手段，失败不影响本次取消结果。
    if not conversation_id:
        _gen_note(summary, "warn", "生成会话尚未建立，已按本地取消处理")
    else:
        try:
            resp = client.post(
                f"{AGENTSUPPORT_URL}/conversations/{conversation_id}/cancel",
                headers={"X-Tenant-Id": TENANT},
                timeout=60,
            )
            if resp.status_code != 200:
                _gen_note(
                    summary,
                    "warn",
                    f"远端取消返回 {resp.status_code}，已按本地取消处理",
                )
        except Exception as exc:  # noqa: BLE001
            _gen_note(summary, "warn", f"远端取消失败（{exc}），已按本地取消处理")
    STATE.status = "waiting_next"
    STATE.live = None
    return STATE.snapshot()


@app.post("/api/sessions/{session_id}/continue")
def continue_session(session_id: str) -> dict[str, Any]:
    """Load a history session as the active demo session and keep chatting."""

    global _current_approver
    if STATE.generation_busy or STATE.status in ("running_round", "running_generate"):
        raise RuntimeError("cannot switch session while a round/generation is running")
    session = _get(f"/sessions/{session_id}", tenant=TENANT)
    if not session:
        raise RuntimeError("session not found")
    tenant = session.get("tenant_id")
    if tenant not in (None, TENANT):
        raise RuntimeError("session belongs to another tenant")
    config = session.get("config") or {}
    skills: list[str] = []
    for entry in config.get("skills") or []:
        if isinstance(entry, dict) and entry.get("enabled") and entry.get("skill_id"):
            skills.append(str(entry["skill_id"]))
    history = _session_history_summary(session)
    STATE.session_id = session_id
    STATE.rounds = history["rounds"]
    STATE.uploads = list(history.get("uploads") or [])
    STATE.round = len(STATE.rounds)
    STATE.active_skills = skills
    STATE.status = "waiting_next"
    STATE.error = None
    STATE.live = None
    STATE.generation = None
    STATE.summary = None
    STATE.draft = None
    STATE.review = None
    STATE.activation = None
    STATE.pending_approval = None
    _live_approved.clear()
    if _current_approver is not None:
        _current_approver.stop()
    _current_approver = Approver(session_id)
    _current_approver.start()
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


# File preview (demo-local): read files inside workspace roots only.
WORKSPACE_ROOTS = [
    Path(os.environ.get("AGENTSUPPORT_RUNNER_WORKSPACE_ROOT", r"D:\workspace")).resolve(),
    Path(os.environ.get("AGENTSUPPORT_WORKSPACE_ROOT", "workspace-data")).resolve(),
]
TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".tsv", ".log", ".html", ".htm", ".css", ".js", ".xml", ".sql",
    ".sh", ".ps1", ".bat", ".dockerfile",
}
IMAGE_EXTENSIONS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
TEXT_PREVIEW_LIMIT = 200_000
TABLE_PREVIEW_ROWS = 300


def _resolve_workspace_file(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = WORKSPACE_ROOTS[0] / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
    if not any(resolved == root or root in resolved.parents for root in WORKSPACE_ROOTS):
        raise HTTPException(status_code=400, detail="path outside workspace roots")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if resolved.name.lower() == ".env":
        raise HTTPException(status_code=400, detail="not allowed")
    return resolved


@app.get("/api/file")
def file_preview(path: str) -> dict[str, Any]:
    resolved = _resolve_workspace_file(path)
    size = resolved.stat().st_size
    base = {"name": resolved.name, "path": str(resolved), "size": size}
    suffix = resolved.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        payload = base64.b64encode(resolved.read_bytes()).decode("ascii")
        return {
            **base,
            "kind": "image",
            "data_uri": f"data:{IMAGE_EXTENSIONS[suffix]};base64,{payload}",
        }
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(resolved, read_only=True, data_only=True)
        sheet = workbook.active
        rows: list[list[str]] = []
        for idx, row in enumerate(sheet.iter_rows(values_only=True)):
            if idx >= TABLE_PREVIEW_ROWS:
                break
            rows.append(["" if value is None else str(value) for value in row])
        workbook.close()
        return {**base, "kind": "table", "table": rows}
    if suffix == ".docx":
        import docx

        document = docx.Document(str(resolved))
        parts: list[str] = []
        for paragraph in document.paragraphs:
            if paragraph.text.strip():
                parts.append(paragraph.text)
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return {**base, "kind": "text", "text": "\n".join(parts)}
    if suffix == ".pdf":
        import fitz

        pages: list[str] = []
        with fitz.open(str(resolved)) as document:
            for page in document:
                pages.append(page.get_text())
        return {**base, "kind": "text", "text": "\n".join(pages)}
    if suffix in TEXT_EXTENSIONS or size <= TEXT_PREVIEW_LIMIT:
        data = resolved.read_bytes()
        text = data.decode("utf-8", errors="replace")
        return {**base, "kind": "text", "text": text}
    return {**base, "kind": "binary", "note": "该文件类型暂不支持预览，仅显示路径与大小"}


@app.get("/api/file-download")
def file_download(path: str) -> FileResponse:
    """Download a workspace file with its original name (demo-local)."""

    resolved = _resolve_workspace_file(path)
    return FileResponse(resolved, filename=resolved.name)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("AGENTSUPPORT_DEMO_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENTSUPPORT_DEMO_PORT", "8900")),
        log_level="info",
    )

"""Skill generation operations.

A manual request turns a session's event history into a SKILL.md draft by
running a dedicated agent conversation; the draft then requires human review
before it is published into the tenant-scoped skill library.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ..domain import (
    TERMINAL_STATES,
    Conversation,
    ConversationMode,
    DraftStatus,
    ExecutionState,
    GenerationStatus,
    Session,
    SkillDraft,
    SkillGenerationRequest,
)
from .common import ServiceError

#: 事件文件在 session 工作区内的相对路径（runner 挂载工作区，agent 可读）。
_GENERATION_EVENT_DIR = ".agentsupport"
_GENERATION_EVENT_FILE = "events.jsonl"

_GENERATION_PROMPT = """你是 AgentSupport 的 skill 提炼 agent。请分析下方给定的 session 历史事件，
把其中真实出现、且值得复用的**业务知识**提炼成一份标准 SKILL.md。

## 总结对象（按优先级）

1. 业务口径与规则：用户在对话中纠正或确认过的定义、口径、计算方式、命名约定
   （例如「毛利=收入−成本且成本不含税」「金额=Qty×Price」「报表统一叫『季度销售报告』」）。
2. 文档 / 报告写作规范：结构、章节与标题层级、术语用法、格式与措辞要求。
3. 数据分析方法：指标定义、计算步骤、数据整理、核对与验证方式。
4. 业务适用场景与边界：这个 skill 适合什么角色 / 任务，明确不覆盖什么。

## 不要总结的内容

- 平台执行机制不作为正文内容：task_done、工具审批（approval / checkpoint）、文件引用格式
  （[[file:...]]）、runner / 工作区路径探测等一律不写入正文；确会影响业务执行时，只在
  「边界条件」里用一句话说明。
- 不要编造事件历史中不存在的业务内容。

## 流程

1. 先用文件工具读取事件文件的全部内容。
2. 提炼准备写入 SKILL 的范围，调用一次 ask_user 工具确认意图：question 参数用中文完整列出
   ①业务适用场景 ②要固化的口径 / 规范 / 方法 ③边界；不要把工具过程当作确认范围。
3. 用户回复为「同意，按此范围生成」时按确认的范围继续；回复为其他文本时，
   把用户文本当作纠正意见，按其调整范围后再继续；纠正后可直接继续，无需再次确认。
4. 确认后写出 SKILL.md，输出必须是 JSON 对象，唯一字段为 skill_markdown，
   值为完整的 SKILL.md 文本。
5. SKILL.md 必须以 frontmatter 开头：name（kebab-case 英文短名）、description
   （说明业务适用场景与价值）。
6. 推荐分区：
   # 目标
   # 业务规则与口径（含用户纠正过的定义）
   # 方法与步骤（按业务动作描述，必要时标注输入与核对方式）
   # 边界条件
   只有平台操作确实影响业务结果时，才在「方法与步骤」或「边界条件」中给出必要说明。

## 输出格式示例（正例）

期望输出：

{"skill_markdown": "---\\nname: quarterly-sales-margin-report\\ndescription: 适用于按公司统一口径计算季度销售毛利并出具报告\\n---\\n\\n# 目标\\n...\\n\\n# 业务规则与口径\\n- 毛利 = 收入 − 成本，且成本按不含税金额计算。\\n\\n# 方法与步骤\\n...\\n\\n# 边界条件\\n..."}

只输出 JSON，不要输出其他内容。
"""

ASK_USER_TOOL = "ask_user"


def is_skill_generation_task(task: str | None) -> bool:
    """Whether ``task`` is a control-plane skill-generation run task."""
    return bool(task and task.startswith(_GENERATION_PROMPT))


def _strip_markdown_fence(text: str) -> str:
    """Strip a fenced code block around JSON/SKILL.md if present."""
    match = re.match(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", text, re.DOTALL)
    return match.group(1) if match else text

class SkillGenerationOpsMixin:
    """Manual generation, draft review and publication."""

    async def generate_skill(
        self,
        session_id: UUID,
        *,
        tenant_id: str | None = None,
    ) -> SkillGenerationRequest:
        session = self.get_session(session_id)
        self._check_generation_tenant(session.tenant_id, tenant_id)
        events = self.session_events(session_id)
        if self.repository is not None:
            conversations = self.repository.list_conversations(session_id=session_id)
            silent_run_ids = {
                str(conv.run.run_id)
                for conv in conversations
                if getattr(conv, "mode", None) == ConversationMode.SILENT
                and conv.run is not None
                and conv.run.run_id is not None
            }
            if silent_run_ids:
                events = [
                    event
                    for event in events
                    if str(event.run_id) not in silent_run_ids
                ]
        request = SkillGenerationRequest(
            session_id=session_id,
            tenant_id=session.tenant_id,
            project_id=session.project_id,
        )
        event_path = self._write_event_file(session, request.id, events)
        task = (
            _GENERATION_PROMPT
            + "\n\n## 事件文件\n\n"
            + f"完整 session 历史事件已写入工作区文件：`{event_path}`\n"
            + "请先用文件工具读取该文件的全部内容，再按要求提炼 SKILL.md。"
        )
        conversation = await self.create_conversation(
            session_id,
            task,
            workspace_id=session.workspace_id,
            skills=None,
            mode=ConversationMode.SILENT,
        )
        request.conversation_id = conversation.id
        self._persist_generation(request)
        self._finalize_generation(request)
        return request

    def get_skill_generation(
        self,
        request_id: UUID,
        *,
        tenant_id: str | None = None,
    ) -> SkillGenerationRequest:
        request = self._get_generation(request_id)
        self._check_generation_tenant(request.tenant_id, tenant_id)
        self._finalize_generation(request)
        return request

    def list_skill_drafts(
        self,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SkillDraft]:
        self._finalize_pending_generations(tenant_id)
        if self.repository:
            return self.repository.list_skill_drafts(
                tenant_id=tenant_id, status=status, limit=limit, offset=offset
            )
        items = [draft for draft in self.skill_drafts.values()]
        if tenant_id is not None:
            items = [draft for draft in items if draft.tenant_id == tenant_id]
        if status is not None:
            items = [draft for draft in items if draft.status.value == status]
        items = sorted(items, key=lambda draft: draft.created_at, reverse=True)
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def review_skill_draft(
        self,
        draft_id: UUID,
        *,
        tenant_id: str | None = None,
        decision: str,
        note: str | None = None,
    ) -> SkillDraft:
        draft = self._get_draft(draft_id)
        self._check_generation_tenant(draft.tenant_id, tenant_id)
        if draft.status not in {DraftStatus.DRAFT, DraftStatus.REVIEW}:
            raise ServiceError(
                "DRAFT_NOT_REVIEWABLE", f"draft is {draft.status.value}", 409
            )
        if decision == "approve":
            self.skill_provider.install_skill(
                draft.skill_id,
                {"SKILL.md": draft.skill_content.encode("utf-8")},
                tenant_id=draft.tenant_id,
            )
            draft.status = DraftStatus.PUBLISHED
        elif decision == "reject":
            draft.status = DraftStatus.REJECTED
        else:
            raise ServiceError(
                "DRAFT_REVIEW_INVALID", "decision must be 'approve' or 'reject'", 422
            )
        draft.reviewed_at = datetime.now(UTC)
        draft.review_note = note
        self._persist_draft(draft)
        return draft

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _check_generation_tenant(
        owner_tenant: str | None, caller_tenant: str | None
    ) -> None:
        if caller_tenant is not None and owner_tenant != caller_tenant:
            raise ServiceError("SESSION_NOT_FOUND", "session does not exist", 404)

    def _write_event_file(
        self,
        session: Session,
        request_id: UUID,
        events: list[Any],
    ) -> str:
        """把 session 事件历史完整写入工作区文件，供 agent 自行读取。

        runner 会把 session 工作区挂载进容器，因此 agent 用文件工具即可读到
        全部事件；事件不做截断、不抽样、不降级。返回工作区相对路径（POSIX），
        该路径写进生成任务的 prompt。
        """
        workspace = self.get_workspace(session.workspace_id)
        rel_path = (
            Path(_GENERATION_EVENT_DIR)
            / f"skill-generation-{request_id}"
            / _GENERATION_EVENT_FILE
        )
        target_root = Path(self.config.runner_workspace_root or workspace.root_path)
        target = target_root / rel_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as handle:
                for event in events:
                    row = {
                        "seq": event.seq,
                        "type": event.type,
                        "payload": event.payload,
                        "source": event.source,
                        "occurred_at": event.occurred_at.isoformat(),
                    }
                    handle.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
        except OSError as exc:
            raise ServiceError(
                "GENERATION_EVENT_FILE_FAILED",
                f"failed to write session events to workspace: {exc}",
                500,
            ) from exc
        return rel_path.as_posix()

    def _finalize_pending_generations(self, tenant_id: str | None = None) -> None:
        requests = self._list_generations(tenant_id=tenant_id, status="pending")
        for request in requests:
            self._finalize_generation(request)

    def _finalize_generation(self, request: SkillGenerationRequest) -> None:
        if request.status != GenerationStatus.PENDING or request.conversation_id is None:
            return
        conversation = self._generation_conversation(request.conversation_id)
        if conversation is None:
            return
        if conversation.run.state not in TERMINAL_STATES:
            return
        if conversation.run.state == ExecutionState.COMPLETED:
            content = self._extract_skill_content(conversation.run.result_summary)
            frontmatter = _parse_skill_frontmatter(content) if content else {}
            if not content or not frontmatter.get("name") or not frontmatter.get("description"):
                request.status = GenerationStatus.FAILED
                request.error = (
                    "generated SKILL.md must contain frontmatter name and description"
                )
            else:
                if self._draft_for_generation(request.id) is None:
                    draft = SkillDraft(
                        skill_id=_slugify_skill_id(str(frontmatter["name"])),
                        tenant_id=request.tenant_id,
                        project_id=request.project_id,
                        source_session_id=request.session_id,
                        source_conversation_id=request.conversation_id,
                        generation_id=request.id,
                        skill_content=content,
                        frontmatter=frontmatter,
                    )
                    self._persist_draft(draft)
                request.status = GenerationStatus.COMPLETED
                request.error = None
        else:
            request.status = GenerationStatus.FAILED
            request.error = f"generation run ended with {conversation.run.state.value}"
        request.completed_at = datetime.now(UTC)
        self._persist_generation(request)

    @staticmethod
    def _extract_skill_content(result: Any) -> str | None:
        """Unwrap the runner result envelope down to the SKILL.md text.

        The runner wraps agent output in a structured result
        (``{status, content, steps, usage}``) and the agent emits the skill as
        a JSON string (``{"skill_markdown": "..."}``) inside ``content``, so
        extraction recurses through both layers before validating markdown.
        """
        if isinstance(result, str):
            text = result.strip()
            if not text:
                return None
            # Models frequently wrap the JSON envelope in a fenced code
            # block (```json ... ```) despite being asked for raw JSON;
            # strip the fence before parsing so a valid envelope is kept.
            stripped = _strip_markdown_fence(text)
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped if stripped.startswith("---") else None
            return SkillGenerationOpsMixin._extract_skill_content(parsed)
        if isinstance(result, dict):
            for key in ("skill_markdown", "skill_content", "content"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return SkillGenerationOpsMixin._extract_skill_content(value)
        return None

    def _generation_conversation(self, conversation_id: UUID) -> Conversation | None:
        if self.repository:
            conversation = self.repository.get_conversation(conversation_id)
            if conversation:
                self.conversations[conversation.id] = conversation
                return conversation
        return self.conversations.get(conversation_id)

    def _persist_generation(self, request: SkillGenerationRequest) -> None:
        if self.repository:
            if self.repository.get_skill_generation_request(request.id) is None:
                self.repository.create_skill_generation_request(request)
            else:
                self.repository.update_skill_generation_request(request)
        self.skill_generations[request.id] = request

    def _get_generation(self, request_id: UUID) -> SkillGenerationRequest:
        if self.repository:
            request = self.repository.get_skill_generation_request(request_id)
            if request:
                self.skill_generations[request.id] = request
                return request
        request = self.skill_generations.get(request_id)
        if request is None:
            raise ServiceError(
                "GENERATION_NOT_FOUND", "skill generation does not exist", 404
            )
        return request

    def _list_generations(
        self,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
    ) -> list[SkillGenerationRequest]:
        if self.repository:
            return self.repository.list_skill_generation_requests(
                tenant_id=tenant_id, status=status
            )
        items = [request for request in self.skill_generations.values()]
        if tenant_id is not None:
            items = [request for request in items if request.tenant_id == tenant_id]
        if status is not None:
            items = [request for request in items if request.status.value == status]
        return sorted(items, key=lambda request: request.created_at)

    def _persist_draft(self, draft: SkillDraft) -> None:
        if self.repository:
            if self.repository.get_skill_draft(draft.id) is None:
                self.repository.create_skill_draft(draft)
            else:
                self.repository.update_skill_draft(draft)
        self.skill_drafts[draft.id] = draft

    def _get_draft(self, draft_id: UUID) -> SkillDraft:
        if self.repository:
            draft = self.repository.get_skill_draft(draft_id)
            if draft:
                self.skill_drafts[draft.id] = draft
                return draft
        draft = self.skill_drafts.get(draft_id)
        if draft is None:
            raise ServiceError("DRAFT_NOT_FOUND", "skill draft does not exist", 404)
        return draft

    def _draft_for_generation(self, generation_id: UUID) -> SkillDraft | None:
        if self.repository:
            drafts = self.repository.list_skill_drafts(generation_id=generation_id)
            return drafts[0] if drafts else None
        for draft in self.skill_drafts.values():
            if draft.generation_id == generation_id:
                return draft
        return None


def _parse_skill_frontmatter(content: str) -> dict[str, Any]:
    lines = content.splitlines()
    frontmatter: dict[str, Any] = {}
    if not lines or lines[0].strip() != "---":
        return frontmatter
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        frontmatter[key.strip()] = value.strip().strip('"').strip("'")
    return frontmatter


def _slugify_skill_id(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip().lower()).strip("-.")
    if not slug or not re.match(r"^[A-Za-z0-9]", slug):
        slug = "generated-skill"
    return slug

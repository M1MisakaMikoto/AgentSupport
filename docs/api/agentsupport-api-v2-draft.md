# AgentSupport API v0.2 契约草案（B+ 方案）

状态：草案，与 [ADR-002](../adr/002-platform-boundary-and-api-v2.md) 配套；
评审通过前不代表已实现，仅用于定边界。

## 1. 范围

公共 API 只面向业务调用方（上游）。Runner 注册协议为内部契约，见第 9 节，
不挂在公共路由上。

## 2. 通用约定

- 请求体 / 响应体：`application/json`；SSE：`text/event-stream`。
- UUID、ISO 8601（带时区）、`Idempotency-Key`、`X-Correlation-ID` 与现版本一致。
- 标签字段：`tenant_id` / `user_id` / `project_id` 可选，不校验存在性；`metadata` 为任意键值对象。
- 生产建议由网关注入身份头 `X-Tenant-Id` / `X-User-Id` / `X-Project-Id`，优先级高于请求体。
- 平台无鉴权（`AGENTSUPPORT_API_AUTH_MODE` 仍仅支持 `none`），认证与限流由网关注入。
- auto-create：仅当调用方显式传入且不存在的 `workspace_id` / `session_id` 时以其 ID 补建；
  未传入的字段绝不自动生成。补建在 201 响应中以 `auto_created` 显式回传，并产生
  `workspace.created` / `session.created` 事件；`parent_conversation_id` 等引用不参与补建。
  默认开启，生产部署可配置关闭（`AGENTSUPPORT_AUTO_CREATE_MISSING=false`）。调用方传入 ID
  的准确性由调用方负责，平台不区分故意补建与拼写错误。

## 3. 资源 API

### Workspace

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/workspaces` | 创建（可带 `name`、可选标签） |
| `GET` | `/workspaces` | 列表，支持标签过滤 |
| `GET` | `/workspaces/{workspace_id}` | 详情 |
| `PATCH` | `/workspaces/{workspace_id}` | 改 `name` / `metadata` |
| `DELETE` | `/workspaces/{workspace_id}` | 删除；存在 Session 时返回 `409 WORKSPACE_HAS_SESSIONS` |

### Session

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions` | 创建（`workspace_id`、可选标签、可选执行配置） |
| `GET` | `/sessions` | 列表，支持 `workspace_id` / `tenant_id` / `user_id` / `project_id` / `state` 过滤 |
| `GET` | `/sessions/{session_id}` | 详情 |
| `PATCH` | `/sessions/{session_id}` | 改 `name` / 标签 / 执行配置 |
| `DELETE` | `/sessions/{session_id}` | 删除；存在 Conversation 时返回 `409 SESSION_HAS_CONVERSATIONS` |
| `POST` | `/sessions/{session_id}/pause` | 暂停（释放执行容量，保留检查点） |
| `POST` | `/sessions/{session_id}/resume` | 从检查点恢复 |
| `GET` | `/sessions/{session_id}/conversations` | 会话下对话列表 |

### Conversation

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions/{session_id}/conversations` | 提交任务（`task`、可选 `parent_conversation_id`、可选配置） |
| `GET` | `/conversations/{conversation_id}` | 详情（含 Run 状态） |
| `POST` | `/conversations/{conversation_id}/input` | 提交用户输入 |
| `POST` | `/conversations/{conversation_id}/approval` | 提交工具审批 |
| `POST` | `/conversations/{conversation_id}/cancel` | 取消 |
| `DELETE` | `/conversations/{conversation_id}` | 仅终态可删（roadmap） |

## 4. 请求示例

```http
POST /sessions
Idempotency-Key: 1f2e3d4c-...
X-Tenant-Id: t-1
X-User-Id: u-1
X-Project-Id: p-2

{
  "workspace_id": "ws-1",
  "name": "重构",
  "metadata": { "team": "platform" },
  "config": {
    "skills": [{"skill_id": "review", "enabled": true}],
    "tools": {
      "allowed_tools": ["bash", "str_replace_based_edit_tool", "task_done"],
      "approval_required_tools": ["bash"],
      "tool_descriptors": []
    },
    "resources": { "mcp_refs": [], "workspace_template": null, "env": {} },
    "permissions": { "max_active_sessions": null, "allow_network": true, "allow_workspace_write": true }
  }
}
```

`tenant_id` / `user_id` / `project_id` 可由网关身份头注入，也可在本地开发时放请求体。

## 5. 执行配置（替代 preset）

- 配置结构沿用现有 `ProjectConfig`：`skills` / `tools` / `resources` / `permissions`。
- 由调用方在 Session 创建或更新时传入；平台做结构校验、透传给 Runner。
- 平台不再提供预设模板实体与导入 / 归属语义；模板由上游自行保存，调用时展开传入。

## 6. 事件与 SSE

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/conversations/{conversation_id}/events` | 对话事件轮询（`after_seq`） |
| `GET` | `/conversations/{conversation_id}/events/stream` | 对话事件 SSE |
| `GET` | `/sessions/{session_id}/events` | 会话聚合事件轮询（新增） |
| `GET` | `/sessions/{session_id}/events/stream` | 会话聚合事件 SSE（新增） |

事件顶层固定携带 `tenant_id` / `user_id` / `project_id`（缺失时为空），并保留 `schema_version`，
消费方以字段为准、容忍未知事件类型。

## 7. 交互 API

`input` / `approval` / `cancel` 语义与现版本一致：`interaction_id` / `approval_id` 来自事件，
`expected_seq` 做乐观并发保护，支持 `Idempotency-Key`。

## 8. 运维 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/live` | 存活 |
| `GET` | `/ready` | 就绪与当前配置 |
| `GET` | `/metrics` | Prometheus 指标 |
| `GET` | `/cores` | 动态列出已注册 Runner 的能力与版本 |

## 9. Runner 注册协议（内部）

控制面与 Runner 之间新增内部契约，不对外暴露：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/runners/register` | 注册：`provider`、`endpoint`、`version`、`capabilities`；返回 `runner_id` 与 token |
| `POST` | `/runners/{runner_id}/heartbeat` | 心跳：状态、负载 |
| `DELETE` | `/runners/{runner_id}` | 注销 |

- 鉴权：`X-Runner-Token`（共享 token，先落地；后续可升级 mTLS）。
- 控制面按 `capabilities` 路由 Run（run / input / checkpoint / cancel / events / resume）。
- Runner 丢失由心跳超时 + Reconciler 判定，沿用现有 fence / checkpoint 恢复语义。

## 10. 删除清单（破坏性变更）

- `/organizations`、`/users`、`/presets`、`/projects` 全部路由。
- 业务实体自动补全与 `auto_created` 中的业务实体字段。
- 错误码：`PRESET_NOT_OWNED`、`PRESET_DISABLED`、`USER_NOT_FOUND`、`PROJECT_NOT_FOUND`、
  `ORGANIZATION_NOT_FOUND`（保留 `WORKSPACE_NOT_FOUND` / `SESSION_NOT_FOUND` /
  `CONVERSATION_NOT_FOUND` / `PARENT_NOT_FOUND`）。
- 配置项：废弃 `AGENTSUPPORT_DEFAULT_WORKSPACE_ID`、`AGENTSUPPORT_DEFAULT_USER_ID`、
  `AGENTSUPPORT_AUTO_RESOURCE_NAME`；`AGENTSUPPORT_AUTO_CREATE_SCOPES` 仅剩
  `workspace,session`。

## 11. 版本策略与未决问题

版本策略（已确认）：直接替换根路径，不提供 `/v2` 前缀，也不保留 v1 兼容路由；
v0.2 发布即破坏性切换。

生命周期 API（PATCH / DELETE / pause / resume）已确认本轮仅写入契约，不实现。

Runner 注册鉴权已确认：共享 token（`X-Runner-Token`）先行，契约保留升级 mTLS 的空间。

# AgentSupport API 参考

本文档描述 AgentSupport `0.2.0` 实际实现的 HTTP API。平台由两层组成：

- **AgentSupport API（公共）**：面向业务调用方的控制面 API，负责 Workspace / Session /
  Conversation 执行资源、事件与交互。
- **Session Runner（私有执行面）**：控制面与 Runner 之间的内部 API，负责运行、检查点、
  恢复与 Runner 自注册，不应直接暴露给外部调用方。

平台只管理执行资源；租户 / 用户 / 项目等业务实体由上游系统管理，平台以可选标签透传，
不做存在性校验、不提供业务 CRUD。从 v0.1 迁移见[上游迁移指南](../migration/v0.1-to-v0.2.md)。

## 1. 访问入口

Compose 环境的 AgentSupport API 默认地址为 `http://localhost:8000`（经 Nginx 网关）。
本地直启 Uvicorn 同样监听 `8000`。FastAPI 提供 `/docs`、`/redoc`、`/openapi.json`。

开发控制台监听 `http://127.0.0.1:8010`，通过 `/agentsupport/` 前缀代理到服务，
本文后续路径均以直连 API 为准（不含代理前缀）。

## 2. 通用约定

### 2.1 请求与响应

- 请求体 / 普通响应：`application/json`；SSE：`text/event-stream`。
- UUID 使用标准字符串格式；时间字段使用带时区的 ISO 8601。
- 未特别说明的成功状态码为 `200`；资源创建接口返回 `201`。

### 2.2 幂等

所有公共 `POST` 接口接受可选请求头：

```http
Idempotency-Key: <调用方生成的稳定唯一值>
```

相同 Key + 相同请求返回第一次操作的资源或结果，不重复执行；相同 Key 用于不同请求时返回
`409 IDEMPOTENCY_CONFLICT`。生产调用方应为每个逻辑写操作生成 Key，并在网络重试时复用。

### 2.3 关联 ID

调用方可传 `X-Correlation-ID`；未传时平台自动生成。每个响应包含：

- `X-Correlation-ID`：本次请求关联 ID。
- `X-AgentSupport-Instance`：处理请求的平台实例 ID。

### 2.4 标签（tenant / user / project）

- Session 可携带 `tenant_id` / `user_id` / `project_id`：可选、不校验存在性、允许为空。
- `metadata`：任意键值对象，平台透传记录，不参与配额 / 隔离。
- 生产建议由网关注入身份头，优先级高于请求体：

```http
X-Tenant-Id: t-1
X-User-Id: u-1
X-Project-Id: p-2
```

- 事件与审计顶层携带三个标签字段；Conversation 继承所属 Session 的标签。
- 无标签资源计入 `default/unknown` 无头统计，不强制要求标签。

### 2.5 鉴权

公共 API 有意不提供 token / API Key 等鉴权（`AGENTSUPPORT_API_AUTH_MODE` 仅支持 `none`）。
正式对公网开放前，请在网关注入身份头、限流与认证。

### 2.6 auto-create（显式 ID 补建）

仅当调用方在请求中**显式传入且不存在**的 `workspace_id` / `session_id` 时，平台以该 ID
补建；调用方未传入的字段绝不自动生成（不再有默认 Workspace / 默认用户）。

- 补建通过创建类接口 `201` 响应中的 `auto_created` 字段显式回传，并产生
  `workspace.created` / `session.created` 事件。
- `parent_conversation_id` 等引用不参与补建，一律严格 `404`。
- 并发补建同一 ID 原子收敛，同一 ID 只产生一个实体。
- 配置：`AGENTSUPPORT_AUTO_CREATE_MISSING`（默认 `true`，生产建议 `false`）、
  `AGENTSUPPORT_AUTO_CREATE_SCOPES`（默认 `workspace,session`）。
- 调用方传入 ID 的准确性由调用方负责；平台不区分故意补建与拼写错误。

## 3. 资源 API

### 3.1 Workspace

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/workspaces` | 创建 |
| `GET` | `/workspaces` | 列表 |
| `GET` | `/workspaces/{workspace_id}` | 详情 |

```http
POST /workspaces
Idempotency-Key: 6f9c2d3a-...

{ "name": "demo" }
```

`name` 长度为 1-120。成功返回 `201`：

```json
{
  "id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "name": "demo",
  "root_path": "/workspace/d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "created_at": "2026-08-12T08:00:00Z"
}
```

### 3.2 Session

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions` | 创建（可自动补建缺失 Workspace） |
| `GET` | `/sessions` | 列表，支持标签 / 工作区过滤 |
| `GET` | `/sessions/{session_id}` | 详情 |

```http
POST /sessions
Idempotency-Key: 1f2e3d4c-...
X-Tenant-Id: t-1
X-User-Id: u-1
X-Project-Id: p-2

{
  "workspace_id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "name": "重构",
  "metadata": { "team": "platform" },
  "config": {
    "skills": [{"skill_id": "review", "enabled": true}],
    "tool_policy": {
      "allowed_tools": ["bash", "str_replace_based_edit_tool", "task_done"],
      "approval_required_tools": ["bash"],
      "tool_descriptors": []
    },
    "resources": { "mcp_refs": [], "workspace_template": null, "env": {} },
    "permissions": { "max_active_sessions": null, "allow_network": true, "allow_workspace_write": true }
  }
}
```

- `workspace_id` 必填；不存在且 auto-create 开启时以该 ID 补建 Workspace。
- `name` 可选；`tenant_id` / `user_id` / `project_id` 可选标签（身份头优先）。
- `config`（skills / tools / resources / permissions）为执行配置，透传给 Runner；
  缺省使用部署默认配置。配置结构可参考
  [会话执行配置](#34-会话执行配置)。

列表过滤：

```http
GET /sessions?workspace_id=<uuid>&tenant_id=t-1&user_id=u-1&project_id=p-2
```

Workspace 缺失且 auto-create 关闭时返回 `404 WORKSPACE_NOT_FOUND`；补建开启时 `201`
响应追加 `auto_created`：

```json
{
  "auto_created": {
    "workspace": { "id": "d93d3e3f-...", "name": "auto", "root_path": "/workspace/..." }
  }
}
```

### 3.3 Conversation

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/sessions/{session_id}/conversations` | 提交任务 |
| `GET` | `/sessions/{session_id}/conversations` | 会话下对话列表 |
| `GET` | `/conversations/{conversation_id}` | 对话详情（含 Run 状态） |

```http
POST /sessions/3cb62872-d517-40e6-92ec-d50045e40b26/conversations
Idempotency-Key: a1b2c3d4-...

{ "task": "分析项目并修复测试失败", "parent_conversation_id": null }
```

- `task` 不能为空。
- `parent_conversation_id` 可选，用于从同一 Session 已有对话派生；父对话不存在或
  不属于当前 Session 时返回 `404 PARENT_NOT_FOUND`（不参与补建）。
- `workspace_id` 可选：仅当 Session 不存在且 auto-create 开启时，用于决定新 Session
  所属 Workspace；未传时返回 `404 SESSION_NOT_FOUND`。

成功返回 `201`：

```json
{
  "id": "a27884b2-b20f-43d0-927b-005e00e4bb4a",
  "session_id": "3cb62872-d517-40e6-92ec-d50045e40b26",
  "parent_conversation_id": null,
  "task": "分析项目并修复测试失败",
  "created_at": "2026-08-12T08:00:02Z",
  "run": {
    "run_id": "a9b35c51-37e7-413f-983a-8b973c3475ce",
    "state": "RUNNING",
    "last_seq": 2,
    "pending_interaction": null,
    "result_summary": null,
    "checkpoint_id": null
  }
}
```

返回时的 Run 可能处于 `QUEUED` / `WAITING_INPUT` / 终态等，调用方不应假设固定为
`RUNNING`。Session 缺失且 auto-create 开启时自动补建 Session（沿用路径中的
`session_id`）并返回 `auto_created`。

### 3.4 会话执行配置

`config` 替代 v0.1 的 preset 导入，由调用方随 Session 请求传入：

```json
{
  "skills": [{"skill_id": "review", "enabled": true}],
  "tool_policy": {
    "allowed_tools": ["bash", "str_replace_based_edit_tool", "json_edit_tool", "sequentialthinking", "task_done"],
    "approval_required_tools": ["bash", "str_replace_based_edit_tool", "json_edit_tool"],
    "tool_descriptors": []
  },
  "resources": { "mcp_refs": [], "workspace_template": null, "env": {} },
  "permissions": { "max_active_sessions": null, "allow_network": true, "allow_workspace_write": true }
}
```

平台做结构校验后透传给 Runner；模板由上游自行保存，调用时展开传入。

## 4. 事件与 SSE

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/conversations/{conversation_id}/events` | 对话事件轮询（`after_seq`） |
| `GET` | `/conversations/{conversation_id}/events/stream` | 对话事件 SSE |
| `GET` | `/sessions/{session_id}/events` | 会话聚合事件轮询 |
| `GET` | `/sessions/{session_id}/events/stream` | 会话聚合事件 SSE |

事件结构：

```json
{
  "schema_version": "1",
  "event_id": "0019c3af-bff6-4aef-a28b-8e220d059f11",
  "run_id": "a9b35c51-37e7-413f-983a-8b973c3475ce",
  "seq": 1,
  "type": "run.started",
  "payload": {},
  "tenant_id": "t-1",
  "user_id": "u-1",
  "project_id": "p-2",
  "source": "agentsupport",
  "occurred_at": "2026-08-12T08:00:02Z"
}
```

- `after_seq=N` 为排他游标，只返回 `seq > N`；默认 `0`，不能为负。
- SSE 每条消息以 `seq` 作为 `id`，完整事件作为 `data`；断线重连时把最后成功处理的
  `seq` 作为新的 `after_seq`，平台先重放遗漏事件再继续等待。
- `seq` 是 Conversation 级游标；Session 级接口按 `occurred_at` 聚合排序。
- 调用方应容忍未知事件类型和新增字段，以 `schema_version` 作为兼容演进依据。

## 5. 交互 API

交互 ID 来自事件或 Conversation 的 `run.pending_interaction`，调用方不应自行生成。

### 5.1 提交用户输入

```http
POST /conversations/{conversation_id}/input
```

```json
{
  "interaction_id": "input-1",
  "value": "继续执行并保留现有配置",
  "expected_seq": 5
}
```

`value` 可以是任意 JSON 值。成功返回更新后的 Conversation。

### 5.2 提交审批

```http
POST /conversations/{conversation_id}/approval
```

```json
{
  "approval_id": "approval-1",
  "decision": "APPROVE_ONCE",
  "expected_seq": 5
}
```

`decision` 仅支持 `APPROVE_ONCE`（批准本次工具调用批次）和 `REJECT`（拒绝）；
其他值返回 `422 INVALID_DECISION`。

### 5.3 取消

```http
POST /conversations/{conversation_id}/cancel
```

请求体可省略，也可带 `{"expected_seq": 5}` 做并发保护。重复取消配合相同
`Idempotency-Key` 不会重复执行命令。

## 6. 运维 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/live` | 存活检查 `{"status":"ok"}` |
| `GET` | `/ready` | 就绪状态、执行/持久化模式、实例 ID |
| `GET` | `/metrics` | Prometheus 文本指标（`agentsupport_` 前缀） |
| `GET` | `/cores` | 已注册 Runner 的能力与版本（动态） |

`/cores` 响应示例（空目录返回 `[]`）：

```json
[
  {
    "runner_id": "3d9e1c2a-...",
    "type": "deterministic",
    "version": "0.1.0",
    "capabilities": ["run", "input", "checkpoint", "cancel", "events", "resume"],
    "status": "READY"
  }
]
```

## 7. Runner 注册协议（内部）

以下端点属于控制面与 Runner 之间的内部契约，**不在公共 API 中**（OpenAPI 不包含），
调用方不应使用：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/runners/register` | 注册：`provider`、`endpoint`、`version`、`capabilities`；返回 `runner_id` 与专属 token |
| `POST` | `/runners/{runner_id}/heartbeat` | 心跳：状态、负载 |
| `DELETE` | `/runners/{runner_id}` | 注销 |

- 注册使用共享 bootstrap token（`X-Runner-Token`，控制面 `AGENTSUPPORT_RUNNER_TOKEN`，
  Runner `SESSION_RUNNER_TOKEN`）；之后的心跳 / 注销使用注册时返回的专属 token。
- 控制面只存 token 哈希；心跳超时（`AGENTSUPPORT_RUNNER_HEARTBEAT_TIMEOUT_SECONDS`，
  默认 30s）后由 Reconciler / 内联健康进程清理过期 Runner。
- 控制面按 `capabilities` 路由 Run；`/cores` 动态反映已注册 Runner。
- token 未配置时注册接口返回 `503 RUNNER_REGISTRATION_DISABLED`，栈回退到静态
  `AGENTSUPPORT_CORE_RUNNER_URL`。

## 8. 运行状态

Conversation 的 `run.state` 可能为：

| 状态 | 含义 |
| --- | --- |
| `QUEUED` | 等待执行容量或 Workspace 写租约 |
| `STARTING` | 正在启动 Runner |
| `RUNNING` | 正在执行 |
| `WAITING_INPUT` | 等待用户输入或审批 |
| `SUSPENDING` | 正在创建检查点并暂停 |
| `PAUSED` | 已暂停，可从检查点恢复 |
| `RESUMING` | 正在恢复 |
| `COMPLETED` | 执行成功完成 |
| `FAILED` | 执行失败 |
| `CANCELLED` | 已取消 |
| `LOST` | Runner 丢失且无法确认状态 |

`COMPLETED`、`FAILED`、`CANCELLED` 和 `LOST` 为终态。

## 9. 错误响应

平台业务错误采用统一结构：

```json
{
  "code": "CONFLICT",
  "message": "expected_seq does not match conversation",
  "retryable": false,
  "operation": "POST /conversations/a27884b2-.../input",
  "correlation_id": "4f1f7eca-...",
  "details": null
}
```

主要错误类别：

| 状态码 | 典型错误码 | 处理建议 |
| --- | --- | --- |
| `404` | `WORKSPACE_NOT_FOUND`、`SESSION_NOT_FOUND`、`CONVERSATION_NOT_FOUND`、`PARENT_NOT_FOUND` | 检查资源 ID；创建类接口在 auto-create 开启时对缺失 Workspace/Session 返回 `201` + `auto_created`，只读与运行态接口保持 404 |
| `409` | `CONFLICT`、`INVALID_STATE`、`IDEMPOTENCY_CONFLICT`、`COMMAND_CONFLICT`、`CHECKPOINT_INVALID` | 刷新事件或修正请求，不要盲目重试 |
| `422` | `INVALID_DECISION` 或 FastAPI 参数校验错误 | 修正请求字段 |
| `429` | `RESOURCE_EXHAUSTED` | 按退避策略重试 |
| `5xx` | 内部或依赖服务错误 | 使用同一幂等 Key 重试，并记录关联 ID |

统一错误体中的 `retryable` 对 `429` 和 `5xx` 为 `true`。FastAPI 自身的请求校验错误使用
标准 `422` 格式，不一定包含上述字段。

## 10. 版本边界

### 已移除（v0.1 → v0.2）

- `/organizations`、`/users`、`/presets`、`/projects` 全部路由。
- 业务实体自动补全与 `PRESET_*` / `USER_*` / `PROJECT_*` / `ORGANIZATION_*` 错误码。
- 配置项 `AGENTSUPPORT_DEFAULT_WORKSPACE_ID` / `AGENTSUPPORT_DEFAULT_USER_ID` /
  `AGENTSUPPORT_AUTO_RESOURCE_NAME`。

### 尚未实现（仅契约项）

- Workspace / Session 的 PATCH / DELETE，Session 的 pause / resume。
- 按 tenant 的并发配额与运行时调整 `max_active_sessions`。

迁移与升级说明见[上游迁移指南](../migration/v0.1-to-v0.2.md)。

# AgentSupport API 参考

本文档描述 AgentSupport `0.1.0` 当前实际实现的 HTTP API。平台由两层组成：

- **AgentSupport API**：面向业务调用方的控制面 API，负责 Workspace、Session、Conversation、事件和交互。
- **Session Runner**：平台内部使用的执行面 API，负责运行、检查点和恢复，不应直接暴露给外部调用方。

## 1. 访问入口

Compose 环境的 AgentSupport API 默认地址为：

```text
http://localhost:8000
```

直接启动平台服务时，Uvicorn 默认也监听 `8000` 端口。FastAPI 同时提供：

| 地址 | 用途 |
| --- | --- |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | OpenAPI 描述 |
| `/debug/` | 任务调试界面，不属于业务 API |

本地开发控制台监听 `http://127.0.0.1:8010` 时，会通过同源路径 `/agentsupport/` 代理到服务；本文后续路径均以直连 AgentSupport API 为准，不包含该代理前缀。

开发控制台按“示范 / 部署”两个大板块组织：示范板块通过真实 API 提供组织、用户、项目、预设管理
与 Agent 工作台（会话、事件轨道、输入/审批/取消）；部署板块内置 API 契约验收，可对本文全部公共
路径执行结构化断言并输出覆盖率报告，“API 参考”页直接从运行时 `/openapi.json` 生成全部接口的
请求表单与参考文档（含参数、请求体示例、响应与 cURL），支持 `Idempotency-Key` 与
`X-Correlation-ID` 的自动生成和回显。

## 2. 推荐调用流程

```text
创建用户（可选，未创建时使用默认组织）
    -> 创建预设（可选，复用 Skill/工具/资源/权限设定）
        -> 创建项目（可导入预设）
            -> 在项目内创建 Session
                -> 创建 Conversation（提交任务）
                    -> 查询或订阅事件
                        -> 按事件要求提交 input / approval
                            -> 等待完成，或主动 cancel
```

创建资源时应保存响应中的 `user.id`、`preset.id`、`project.id`、`session.id` 和 `conversation.id`。
业务层级为 `租户 -> 用户 -> 项目 -> 会话 -> 对话`；旧接口 `POST /workspaces`、`POST /sessions`
仍然可用，创建的资源没有项目归属，运行使用部署级默认配置。

## 3. 通用约定

### 3.1 请求和响应

- 请求体和普通响应采用 `application/json`。
- SSE 接口采用 `text/event-stream`。
- UUID 使用标准字符串格式。
- 时间字段使用带时区的 ISO 8601 格式。
- 未特别说明的成功状态码为 `200`；资源创建接口返回 `201`。

### 3.2 幂等

所有公共 `POST` 接口都接受可选请求头：

```http
Idempotency-Key: <由调用方生成的稳定唯一值>
```

相同 Key 与相同请求会返回第一次操作对应的资源或结果，不重复执行。相同 Key 用于不同请求时返回 `409 IDEMPOTENCY_CONFLICT`。生产调用方应为每个逻辑写操作生成 Key，并在网络重试时复用。

### 3.3 关联 ID

调用方可以传入：

```http
X-Correlation-ID: <trace-id>
```

未传入时平台自动生成。每个响应都包含：

- `X-Correlation-ID`：本次请求的关联 ID。
- `X-AgentSupport-Instance`：处理请求的平台实例 ID。

### 3.4 乐观并发

提交输入、审批或取消时，可以传 `expected_seq`。该值应等于客户端最后看到的 Conversation `run.last_seq` 或最新事件的 `seq`。不匹配时返回 `409 CONFLICT`，客户端应刷新事件后再决定是否重试。

### 3.5 事件游标

事件接口的 `after_seq=N` 采用排他语义，只返回 `seq > N` 的事件。`after_seq` 默认为 `0`，且不能为负数。

## 4. 资源 API

### 4.1 创建 Workspace

```http
POST /workspaces
```

请求：

```json
{
  "name": "demo"
}
```

`name` 长度为 1 至 120 个字符。成功返回 `201`：

```json
{
  "id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "name": "demo",
  "root_path": "/workspace/d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "created_at": "2026-07-31T08:00:00Z"
}
```

### 4.2 创建 Session

```http
POST /sessions
```

请求：

```json
{
  "workspace_id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a"
}
```

成功返回 `201`：

```json
{
  "id": "3cb62872-d517-40e6-92ec-d50045e40b26",
  "workspace_id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "lease_epoch": 0,
  "active_container_id": null,
  "active_run_id": null,
  "created_at": "2026-07-31T08:00:01Z"
}
```

Workspace 不存在时返回 `404 WORKSPACE_NOT_FOUND`。

### 4.3 创建 Conversation 并提交任务

```http
POST /sessions/{session_id}/conversations
```

请求：

```json
{
  "task": "分析项目并修复测试失败",
  "parent_conversation_id": null
}
```

- `task` 不能为空。
- `parent_conversation_id` 可选，用于从同一 Session 的已有 Conversation 派生新任务。
- 创建后平台会立即排队或启动 Run。

成功返回 `201`：

```json
{
  "id": "a27884b2-b20f-43d0-927b-005e00e4bb4a",
  "session_id": "3cb62872-d517-40e6-92ec-d50045e40b26",
  "parent_conversation_id": null,
  "task": "分析项目并修复测试失败",
  "created_at": "2026-07-31T08:00:02Z",
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

返回时的 Run 也可能处于 `QUEUED`、`WAITING_INPUT`、终态或其他中间状态，调用方不应假设固定为 `RUNNING`。

常见错误：

| 状态码 | 错误码 | 含义 |
| --- | --- | --- |
| `404` | `SESSION_NOT_FOUND` | Session 不存在 |
| `404` | `PARENT_NOT_FOUND` | 父 Conversation 不存在或不属于当前 Session |
| `429` | `RESOURCE_EXHAUSTED` | 等待队列或执行容量已满 |

### 4.4 组织与用户 API

组织（租户）接口：

```http
POST /organizations
GET  /organizations
GET  /organizations/{organization_id}
GET  /organizations/{organization_id}/users
```

`POST /organizations` 请求：`{"name": "acme"}`，成功返回 `201`。
`GET /organizations/{organization_id}/users` 返回该组织下的用户列表。

用户接口：

```http
POST /users
GET  /users
GET  /users/{user_id}
```

请求：

```json
{
  "username": "alice",
  "organization_id": null
}
```

- `username` 长度为 1 至 120 个字符，同一部署内唯一。
- `organization_id` 可选，缺省时使用部署的默认组织。

成功返回 `201`：`{"id": "…", "organization_id": "…", "username": "alice", "created_at": "…"}`。

### 4.5 预设 API

预设是用户级可复用配置包（Skill、工具、资源、权限、启用状态）。相关接口：

```http
POST   /presets
GET    /presets?user_id=<可选>
GET    /users/{user_id}/presets
GET    /presets/{preset_id}
PATCH  /presets/{preset_id}
DELETE /presets/{preset_id}
```

`POST /presets` 请求：

```json
{
  "user_id": "…",
  "name": "代码审查预设",
  "description": "标准代码审查配置",
  "definition": {
    "skills": [{"skill_id": "review", "enabled": true}],
    "tools": {
      "allowed_tools": ["bash", "str_replace_based_edit_tool", "json_edit_tool", "sequentialthinking", "task_done"],
      "approval_required_tools": ["bash", "str_replace_based_edit_tool", "json_edit_tool"],
      "tool_descriptors": []
    },
    "resources": {"mcp_refs": [], "workspace_template": null, "env": {}},
    "permissions": {"max_active_sessions": null, "allow_network": true, "allow_workspace_write": true},
    "enabled": true
  }
}
```

`PATCH /presets/{preset_id}` 支持部分更新 `name`、`description`、`definition`。
删除预设不影响已导入它的项目（快照语义）。

常见错误：

| 状态码 | 错误码 | 含义 |
| --- | --- | --- |
| `404` | `USER_NOT_FOUND` | 用户不存在 |
| `404` | `PRESET_NOT_FOUND` | 预设不存在 |
| `409` | `IDEMPOTENCY_CONFLICT` | 幂等键冲突 |

### 4.6 项目 API

项目是用户拥有的业务容器，每个项目对应一个工作区，会话直接属于项目：

```http
POST /projects
GET  /projects?user_id=<可选>
GET  /users/{user_id}/projects
GET  /projects/{project_id}
PATCH /projects/{project_id}
DELETE /projects/{project_id}
POST /projects/{project_id}/preset
POST /projects/{project_id}/sessions
GET  /projects/{project_id}/sessions
```

`POST /projects` 请求：

```json
{
  "user_id": "…",
  "name": "线上商城重构",
  "preset_id": "…"
}
```

- `preset_id` 可选；提供时从预设导入配置快照（Skill、工具、资源、权限），
  之后修改预设不影响已创建项目。
- 不提供预设时，项目使用部署级默认配置。

`POST /projects/{project_id}/preset` 重新导入预设，覆盖项目当前配置，
响应包含 `project` 与 `previous_config`。

`POST /projects/{project_id}/sessions` 在项目内创建会话，等价于带
`project_id` 的旧 `POST /sessions`。

`PATCH /projects/{project_id}` 支持改名与直接编辑配置：

```json
{
  "name": "shop-v2",
  "config": {
    "skills": [{"skill_id": "debug", "enabled": true}],
    "tool_policy": {"allowed_tools": [], "approval_required_tools": [], "tool_descriptors": []},
    "resources": {"mcp_refs": [], "workspace_template": null, "env": {}},
    "permissions": {"max_active_sessions": null, "allow_network": true, "allow_workspace_write": true}
  }
}
```

`DELETE /projects/{project_id}` 在项目仍有会话时返回 `409 PROJECT_HAS_SESSIONS`。

### 4.7 会话与对话查询

```http
GET /sessions/{session_id}
GET /sessions/{session_id}/conversations
GET /conversations/{conversation_id}
```

`GET /sessions/{session_id}` 返回会话详情（含 `workspace_id`、`project_id`）。
`GET /sessions/{session_id}/conversations` 返回该会话下的对话列表；
`GET /conversations/{conversation_id}` 返回单个对话及其 Run 状态。

常见错误：

| 状态码 | 错误码 | 含义 |
| --- | --- | --- |
| `403` | `PRESET_NOT_OWNED` | 预设不属于该用户 |
| `404` | `PROJECT_NOT_FOUND` | 项目不存在 |
| `422` | `PRESET_DISABLED` | 预设已停用，不能导入 |

## 5. 事件 API

### 5.1 查询 Conversation 事件

```http
GET /conversations/{conversation_id}/events?after_seq=0
```

返回按 Conversation 顺序编号的事件数组。事件结构为：

```json
{
  "schema_version": "1",
  "event_id": "0019c3af-bff6-4aef-a28b-8e220d059f11",
  "run_id": "a9b35c51-37e7-413f-983a-8b973c3475ce",
  "seq": 1,
  "type": "run.started",
  "payload": {},
  "source": "agentsupport",
  "occurred_at": "2026-07-31T08:00:02Z"
}
```

`payload` 由事件类型决定。调用方应容忍未知事件类型和新增字段，并以 `schema_version` 作为后续兼容演进依据。

### 5.2 SSE 订阅 Conversation 事件

```http
GET /conversations/{conversation_id}/events/stream?after_seq=0
Accept: text/event-stream
```

每条消息使用事件序号作为 SSE `id`，完整事件作为 `data`：

```text
id: 2
data: {"schema_version":"1","event_id":"...","run_id":"...","seq":2,"type":"message","payload":{},"source":"runner","occurred_at":"..."}

```

断线重连时，将最后成功处理的 `seq` 作为新的 `after_seq`，平台会先重放遗漏事件，再继续等待新事件。

### 5.3 查询 Session 事件

```http
GET /sessions/{session_id}/events?after_seq=0
```

返回该 Session 下所有 Conversation 中满足 `seq > after_seq` 的事件，并按 `occurred_at` 排序。注意 `seq` 是 Conversation 级游标，不是 Session 全局游标；需要严格可靠消费单个任务时，应优先使用 Conversation 事件接口。

## 6. 交互 API

交互 ID 来自事件或 Conversation 的 `run.pending_interaction`，调用方不应自行生成。

### 6.1 提交用户输入

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

### 6.2 提交审批

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

`decision` 仅支持：

- `APPROVE_ONCE`：仅批准本次工具调用批次。
- `REJECT`：拒绝本次工具调用批次。

其他值返回 `422 INVALID_DECISION`。成功返回更新后的 Conversation。

### 6.3 取消 Conversation

```http
POST /conversations/{conversation_id}/cancel
```

请求体可以省略，也可以进行并发保护：

```json
{
  "expected_seq": 5
}
```

成功返回更新后的 Conversation。重复取消配合相同 `Idempotency-Key` 使用时不会重复执行命令。

## 7. 运维 API

| 方法 | 路径 | 响应与用途 |
| --- | --- | --- |
| `GET` | `/live` | `{"status":"ok"}`，仅表示 HTTP 进程存活 |
| `GET` | `/ready` | 返回 `status`、`execution_mode`、`persistence_mode`、`instance_id`，并检查持久化连接 |
| `GET` | `/metrics` | Prometheus 文本指标，名称以 `agentsupport_` 开头 |
| `GET` | `/cores` | 返回可用执行核心及其版本和声明能力 |

`/cores` 当前响应为：

```json
[
  {
    "type": "session_runner",
    "version": "0.1.0",
    "capabilities": ["run", "input", "checkpoint", "cancel", "events"]
  }
]
```

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
  "operation": "POST /conversations/a27884b2-b20f-43d0-927b-005e00e4bb4a/input",
  "correlation_id": "4f1f7eca-a978-4a79-b32d-015e888ac674",
  "details": null
}
```

主要错误类别：

| 状态码 | 典型错误码 | 处理建议 |
| --- | --- | --- |
| `404` | `WORKSPACE_NOT_FOUND`、`SESSION_NOT_FOUND`、`CONVERSATION_NOT_FOUND` | 检查资源 ID 和创建顺序 |
| `409` | `CONFLICT`、`INVALID_STATE`、`IDEMPOTENCY_CONFLICT`、`COMMAND_CONFLICT`、`CHECKPOINT_INVALID` | 刷新事件或修正请求，不要盲目重试 |
| `422` | `INVALID_DECISION` 或 FastAPI 参数校验错误 | 修正请求字段 |
| `429` | `RESOURCE_EXHAUSTED` | 按退避策略重试 |
| `5xx` | 内部或依赖服务错误 | 使用同一幂等 Key 重试，并记录关联 ID |

统一错误体中的 `retryable` 对 `429` 和 `5xx` 为 `true`。FastAPI 自身产生的请求校验错误使用其标准 `422` 响应格式，不一定具有上述统一字段。

## 10. 私有 Session Runner API

以下接口只用于 AgentSupport 控制面与 Session Runner 之间的内部通信：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/live` | Runner 存活检查 |
| `GET` | `/ready` | Runner 就绪状态和执行模式 |
| `POST` | `/runs` | 启动 Run |
| `POST` | `/runs/{run_id}/input` | 向 Run 提交输入 |
| `POST` | `/runs/{run_id}/approval` | 向 Run 提交工具审批 |
| `POST` | `/runs/{run_id}/checkpoint` | 创建检查点 |
| `POST` | `/runs/{run_id}/cancel` | 取消 Run |
| `POST` | `/runs/{run_id}/resume` | 从检查点恢复或替换 Runner 后恢复 |
| `GET` | `/runs/{run_id}/events` | 查询 Runner 原始事件 |

Runner 的 input、approval、cancel 和 resume 命令支持通过 `command_id` 实现幂等，启动操作按 `run_id` 去重；同时使用 `lease_epoch`、`fence_epoch` 和检查点哈希防止旧 Runner 或无效检查点继续写入。业务客户端应始终调用 AgentSupport 公共 API，不应依赖该私有契约。

## 11. 当前版本边界

已提供用户、预设与项目的基础 API，但仍未提供：

- 正式身份认证和租户授权。
- Workspace、Session、Conversation 的更新和删除 API（查询、列表已支持；Project 支持编辑与删除）。
- Session 级 SSE 接口。
- 生产级 MCP/Skill 市场 API。
- 对象存储、版本备份和恢复 API。

因此，在正式对公网开放前，需要在网关或平台层补充认证授权、限流和租户隔离。

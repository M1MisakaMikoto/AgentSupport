# AgentSupport API 参考

本文档描述 AgentSupport `0.2.0` 实际实现的全部 HTTP API。平台由两层组成：

- **AgentSupport API（公共）**：面向业务调用方的控制面 API，负责 Workspace / Session /
  Conversation 执行资源、事件与交互（第 4-9 章）。
- **Session Runner（私有执行面）**：控制面与 Runner 之间的内部 API，负责运行、检查点、
  恢复与 Runner 自注册（第 12 章），不应直接暴露给外部调用方。

平台只管理执行资源；租户 / 用户 / 项目等业务实体由上游系统管理，平台以可选标签透传，
不做存在性校验、不提供业务 CRUD。从 v0.1 迁移见[上游迁移指南](../migration/v0.1-to-v0.2.md)；
边界决策见 [ADR-002](../adr/002-platform-boundary-and-api-v2.md)。

## 1. 访问入口

Compose 环境的 AgentSupport API 默认地址为 `http://localhost:8000`（经 Nginx 网关）。
本地直启 Uvicorn 同样监听 `8000`。FastAPI 提供 `/docs`、`/redoc`、`/openapi.json`。

开发控制台监听 `http://127.0.0.1:8010`，通过 `/agentsupport/` 前缀代理到服务；
本文后续路径均以直连 API 为准（不含代理前缀）。

## 2. 完整 API 清单

### 2.1 公共 API

| 方法 | 路径 | 章节 |
| --- | --- | --- |
| `POST` | `/workspaces` | [4.1](#41-post-workspaces) |
| `GET` | `/workspaces` | [4.2](#42-get-workspaces) |
| `GET` | `/workspaces/{workspace_id}` | [4.3](#43-get-workspacesworkspace_id) |
| `POST` | `/sessions` | [5.1](#51-post-sessions) |
| `GET` | `/sessions` | [5.2](#52-get-sessions) |
| `GET` | `/sessions/{session_id}` | [5.3](#53-get-sessionssession_id) |
| `POST` | `/sessions/{session_id}/conversations` | [6.1](#61-post-sessionssession_idconversations) |
| `GET` | `/sessions/{session_id}/conversations` | [6.2](#62-get-sessionssession_idconversations) |
| `GET` | `/conversations/{conversation_id}` | [6.3](#63-get-conversationsconversation_id) |
| `POST` | `/skills` | [7.1](#71-post-skills) |
| `GET` | `/skills` | [7.2](#72-get-skills) |
| `GET` | `/skills/{skill_id}` | [7.3](#73-get-skillsskill_id) |
| `DELETE` | `/skills/{skill_id}` | [7.4](#74-delete-skillsskill_id) |
| `POST` | `/mcp-servers` | [8.1](#81-post-mcp-servers) |
| `GET` | `/mcp-servers` | [8.2](#82-get-mcp-servers) |
| `GET` | `/mcp-servers/{server_id}` | [8.3](#83-get-mcp-serversserver_id) |
| `PATCH` | `/mcp-servers/{server_id}` | [8.4](#84-patch-mcp-serversserver_id) |
| `DELETE` | `/mcp-servers/{server_id}` | [8.5](#85-delete-mcp-serversserver_id) |
| `GET` | `/conversations/{conversation_id}/events` | [9.1](#91-get-conversationsconversation_idevents) |
| `GET` | `/conversations/{conversation_id}/events/stream` | [9.2](#92-get-conversationsconversation_ideventsstream) |
| `GET` | `/sessions/{session_id}/events` | [9.3](#93-get-sessionssession_idevents) |
| `GET` | `/sessions/{session_id}/events/stream` | [9.4](#94-get-sessionssession_ideventsstream) |
| `POST` | `/conversations/{conversation_id}/input` | [10.1](#101-post-conversationsconversation_idinput) |
| `POST` | `/conversations/{conversation_id}/approval` | [10.2](#102-post-conversationsconversation_idapproval) |
| `POST` | `/conversations/{conversation_id}/cancel` | [10.3](#103-post-conversationsconversation_idcancel) |
| `GET` | `/live` | [11.1](#111-get-live) |
| `GET` | `/ready` | [11.2](#112-get-ready) |
| `GET` | `/metrics` | [11.3](#113-get-metrics) |
| `GET` | `/cores` | [11.4](#114-get-cores) |

### 2.2 内部 API（Runner 注册协议，不在 OpenAPI 中）

| 方法 | 路径 | 章节 |
| --- | --- | --- |
| `POST` | `/runners/register` | [12.1](#121-post-runnersregister) |
| `POST` | `/runners/{runner_id}/heartbeat` | [12.2](#122-post-runnersrunner_idheartbeat) |
| `DELETE` | `/runners/{runner_id}` | [12.3](#123-delete-runnersrunner_id) |

## 3. 通用约定

### 3.1 请求与响应

- 请求体 / 普通响应：`application/json`；SSE：`text/event-stream`。
- UUID 使用标准字符串格式；时间字段使用带时区的 ISO 8601。
- 未特别说明的成功状态码为 `200`；资源创建接口返回 `201`；删除返回 `204`。

### 3.2 幂等

所有公共 `POST` 接口接受可选请求头：

```http
Idempotency-Key: <调用方生成的稳定唯一值>
```

相同 Key + 相同请求返回第一次操作的资源或结果，不重复执行；相同 Key 用于不同请求时返回
`409 IDEMPOTENCY_CONFLICT`。生产调用方应为每个逻辑写操作生成 Key，并在网络重试时复用。

### 3.3 关联 ID

调用方可传 `X-Correlation-ID`；未传时平台自动生成。每个响应包含：

- `X-Correlation-ID`：本次请求关联 ID。
- `X-AgentSupport-Instance`：处理请求的平台实例 ID。

### 3.4 标签（tenant / user / project）

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

### 3.5 鉴权

公共 API 有意不提供 token / API Key 等鉴权（`AGENTSUPPORT_API_AUTH_MODE` 仅支持 `none`）。
正式对公网开放前，请在网关注入身份头、限流与认证。Runner 内部通道的鉴权见第 12 章。

### 3.6 auto-create（显式 ID 补建）

仅当调用方在请求中**显式传入且不存在**的 `workspace_id` / `session_id` 时，平台以该 ID
补建；调用方未传入的字段绝不自动生成（不再有默认 Workspace / 默认用户）。

- 补建通过创建类接口 `201` 响应中的 `auto_created` 字段显式回传，并产生
  `workspace.created` / `session.created` 事件。
- `parent_conversation_id` 等引用不参与补建，一律严格 `404`。
- 并发补建同一 ID 原子收敛，同一 ID 只产生一个实体。
- 配置：`AGENTSUPPORT_AUTO_CREATE_MISSING`（默认 `true`，生产建议 `false`）、
  `AGENTSUPPORT_AUTO_CREATE_SCOPES`（默认 `workspace,session`）。
- 调用方传入 ID 的准确性由调用方负责；平台不区分故意补建与拼写错误。

## 4. Workspace API

### 4.1 POST /workspaces

创建 Workspace（执行工作区）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 名称，1-120 字符 |

**请求头**：`Idempotency-Key`（可选）。

```http
POST /workspaces
Idempotency-Key: 6f9c2d3a-4b5c-4d6e-8f70-9a1b2c3d4e5f

{ "name": "demo" }
```

**成功响应 `201`**：

```json
{
  "id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "name": "demo",
  "root_path": "/workspace/d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "created_at": "2026-08-12T08:00:00Z"
}
```

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `409` | `IDEMPOTENCY_CONFLICT` | 相同 Key 用于不同请求 |
| `422` | — | 参数校验失败（如 `name` 为空） |

### 4.2 GET /workspaces

返回全部 Workspace 列表，按创建时间升序。无查询参数。

**成功响应 `200`**：

```json
[
  {
    "id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
    "name": "demo",
    "root_path": "/workspace/d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
    "created_at": "2026-08-12T08:00:00Z"
  }
]
```

### 4.3 GET /workspaces/{workspace_id}

返回单个 Workspace。

**路径参数**：`workspace_id`（UUID）。

**成功响应 `200`**：与 [4.1](#41-post-workspaces) 响应体相同。

**错误**：`404 WORKSPACE_NOT_FOUND`（不存在）。

## 5. Session API

### 5.1 POST /sessions

创建 Session（会话，执行配置与标签的载体）。

**请求头**：`Idempotency-Key`（可选）、`X-Tenant-Id` / `X-User-Id` / `X-Project-Id`（可选，
优先级高于请求体）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `workspace_id` | UUID | 是 | 所属 Workspace；不存在且 auto-create 开启时以该 ID 补建 |
| `name` | string | 否 | 名称；Workspace 补建时作为名称提示 |
| `tenant_id` | string | 否 | 上游租户标签（≤120 字符） |
| `user_id` | string | 否 | 上游用户标签（≤120 字符） |
| `project_id` | string | 否 | 上游项目标签（≤120 字符） |
| `metadata` | object | 否 | 任意键值透传 |
| `config` | object | 否 | 执行配置，见[第 13 章](#13-会话执行配置) |

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

**成功响应 `201`**：

```json
{
  "id": "3cb62872-d517-40e6-92ec-d50045e40b26",
  "workspace_id": "d93d3e3f-a066-44c3-a5e0-5f2718fcfa6a",
  "tenant_id": "t-1",
  "user_id": "u-1",
  "project_id": "p-2",
  "metadata": { "team": "platform" },
  "config": null,
  "lease_epoch": 0,
  "active_container_id": null,
  "active_run_id": null,
  "created_at": "2026-08-12T08:00:01Z"
}
```

Workspace 缺失且 auto-create 开启时，响应追加：

```json
{
  "auto_created": {
    "workspace": { "id": "d93d3e3f-...", "name": "auto", "root_path": "/workspace/..." }
  }
}
```

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `404` | `WORKSPACE_NOT_FOUND` | Workspace 不存在且 auto-create 关闭 |
| `409` | `IDEMPOTENCY_CONFLICT` | 相同 Key 用于不同请求 |
| `422` | — | 参数校验失败（如 `workspace_id` 非 UUID） |

### 5.2 GET /sessions

返回 Session 列表，按创建时间升序，支持过滤。

**查询参数**（均可选，多个条件为 AND）：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `workspace_id` | UUID | 按 Workspace 过滤 |
| `tenant_id` | string | 按租户标签过滤 |
| `user_id` | string | 按用户标签过滤 |
| `project_id` | string | 按项目标签过滤 |

```http
GET /sessions?workspace_id=<uuid>&tenant_id=t-1&project_id=p-2
```

**成功响应 `200`**：Session 对象数组，字段同 [5.1](#51-post-sessions) 响应体。

### 5.3 GET /sessions/{session_id}

返回单个 Session。

**路径参数**：`session_id`（UUID）。

**成功响应 `200`**：Session 对象，字段同 [5.1](#51-post-sessions) 响应体。

**错误**：`404 SESSION_NOT_FOUND`（不存在）。

## 6. Conversation API

### 6.1 POST /sessions/{session_id}/conversations

提交任务并创建 Conversation；创建后立即排队或启动 Run。

**路径参数**：`session_id`（UUID）。

**请求头**：`Idempotency-Key`（可选）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task` | string | 是 | 任务描述，不能为空 |
| `parent_conversation_id` | UUID | 否 | 从同一 Session 的已有对话派生 |
| `workspace_id` | UUID | 否 | 仅当 Session 不存在且 auto-create 开启时用于决定新 Session 的 Workspace |
| `skills` | object[] | 否 | 本次对话激活的 skill 列表（`skill_id` + `enabled`）；缺省继承 Session 配置；显式传空数组表示本次不启用任何 skill |
| `mcp_refs` | object[] | 否 | 本次对话激活的 MCP server 引用（`{"server_id": "..."}`）；缺省继承 Session 配置；显式传空数组表示本次不启用任何 MCP |

```http
POST /sessions/3cb62872-d517-40e6-92ec-d50045e40b26/conversations
Idempotency-Key: a1b2c3d4-...

{
  "task": "分析项目并修复测试失败",
  "parent_conversation_id": null,
  "skills": [{"skill_id": "review", "enabled": true}]
}
```

`skills` 在创建时预检（缺失返回 `404 SKILL_NOT_FOUND`）；运行时会话内可动态选择，
例如同一 Session 的不同对话启用不同 skill。

**成功响应 `201`**：

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
`session_id`）并返回 `auto_created`（含 `session`、必要时含 `workspace`）。

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `404` | `SESSION_NOT_FOUND` | Session 不存在（auto-create 关闭，或未提供 `workspace_id`） |
| `404` | `PARENT_NOT_FOUND` | 父对话不存在或不属于当前 Session |
| `404` | `SKILL_NOT_FOUND` | `skills` 中引用了不存在的 skill |
| `404` | `MCP_SERVER_NOT_FOUND` | `mcp_refs` 中引用了不存在的 server |
| `422` | `MCP_SERVER_DISABLED` | 引用的 MCP server 已停用 |
| `409` | `IDEMPOTENCY_CONFLICT` | 相同 Key 用于不同请求 |
| `429` | `RESOURCE_EXHAUSTED` | 等待队列或执行容量已满 |

### 6.2 GET /sessions/{session_id}/conversations

返回该 Session 下的对话列表（按创建时间升序）。

**路径参数**：`session_id`（UUID）。

**成功响应 `200`**：Conversation 对象数组，字段同 [6.1](#61-post-sessionssession_idconversations)
响应体（不含 `auto_created`）。

**错误**：`404 SESSION_NOT_FOUND`（Session 不存在）。

### 6.3 GET /conversations/{conversation_id}

返回单个 Conversation 及其 Run 状态。

**路径参数**：`conversation_id`（UUID）。

**成功响应 `200`**：Conversation 对象，字段同 [6.1](#61-post-sessionssession_idconversations)
响应体。

**错误**：`404 CONVERSATION_NOT_FOUND`（不存在）。

## 7. Skill API

Skill 自服务接口：上游随时上传新 skill，平台存储到共享 skills 卷并只读挂载进 Runner。
skill 是"目录 + `SKILL.md`"的本地包；会话执行配置里的 `config.skills` 引用
`skill_id`，创建会话时平台会预检 skill 是否存在。

### 7.1 POST /skills

上传 skill。支持两种内容：

- 单个 `SKILL.md` 文件（multipart 字段 `file`）；
- zip 压缩包（包内须含根级 `SKILL.md`，可带附属文件；解包安全校验：拒绝绝对路径与
  `..` 穿越、单文件 ≤2MiB、总解包 ≤10MiB、条目 ≤200）。

同名 `skill_id` 上传视为新版本覆盖。

**multipart 字段**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `skill_id` | string | 是 | 1-120 字符，仅 `[A-Za-z0-9][A-Za-z0-9._-]*` |
| `file` | file | 是 | `SKILL.md` 或 `.zip`；上传上限 2MiB |

```http
POST /skills
Content-Type: multipart/form-data

skill_id=review
file=@SKILL.md
```

**成功响应 `201`**：

```json
{
  "skill_id": "review",
  "content_hash": "3a5f8c9b...",
  "mount_path": "/opt/agent-skills/review"
}
```

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `413` | `SKILL_TOO_LARGE` | 上传超过 2MiB |
| `422` | `SKILL_INVALID_PAYLOAD` | skill_id 非法、zip 缺少 `SKILL.md`、路径穿越或条目超限 |

### 7.2 GET /skills

返回全部可用 skill 清单（仅含 `SKILL.md` 的目录）。

**成功响应 `200`**：清单数组，元素同 [7.1](#71-post-skills) 响应体，按 `skill_id` 排序。

### 7.3 GET /skills/{skill_id}

返回 skill 详情与文件清单。

**路径参数**：`skill_id`（string）。

**成功响应 `200`**：

```json
{
  "skill_id": "review",
  "content_hash": "3a5f8c9b...",
  "mount_path": "/opt/agent-skills/review",
  "files": [
    { "path": "SKILL.md", "size": 128 },
    { "path": "references/guide.md", "size": 512 }
  ]
}
```

**错误**：`404 SKILL_NOT_FOUND`（不存在）。

### 7.4 DELETE /skills/{skill_id}

下架 skill（删除目录；已引用它的会话不受影响，新会话将无法引用）。

**路径参数**：`skill_id`（string）。

**成功响应 `204`**（无内容）。

**错误**：`404 SKILL_NOT_FOUND`。

## 8. MCP Server API

MCP server 是**上游自行运行的外部服务**；平台只注册"连接定义"并授权，不托管 server 生命周期。
注册后可在 Session 配置 `resources.mcp_refs` 或 Conversation 请求的 `mcp_refs` 中引用
（缺省继承 / 显式覆盖 / 空数组禁用），创建时预检。

### 8.1 POST /mcp-servers

注册 MCP server 定义。同名 `server_id` 重复注册视为覆盖（新版本）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `server_id` | string | 是 | 1-120 字符，仅 `[A-Za-z0-9][A-Za-z0-9._-]*` |
| `name` | string | 是 | 名称，1-120 字符 |
| `transport` | string | 是 | `http`（streamable http）或 `sse` |
| `http_url` | string | 条件必填 | `transport=http` 时的 MCP 端点 |
| `sse_url` | string | 条件必填 | `transport=sse` 时的 SSE 端点 |
| `headers` | object | 否 | 请求头；值支持字面量或 `$ENV_VAR` 环境变量引用（secret 不落库、不回显） |
| `description` | string | 否 | 描述 |
| `enabled` | boolean | 否 | 是否可用，默认 `true` |

```http
POST /mcp-servers

{
  "server_id": "gitlab",
  "name": "GitLab MCP",
  "transport": "http",
  "http_url": "http://mcp-gitlab:8000/mcp",
  "headers": { "Authorization": "$GITLAB_TOKEN" }
}
```

**成功响应 `201`**：完整定义（`headers` 保留环境变量名，不回显值）。

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `422` | `MCP_SERVER_INVALID` | server_id 非法或 transport 缺少对应端点 |
| `422` | `MCP_TRANSPORT_UNSUPPORTED` | transport 不是 `http` / `sse` |

> `headers` 中以 `$ENV_VAR` 引用的环境变量需在 Runner 部署环境（容器环境变量 / Secret）中
> 配置，缺失时该次运行会失败并给出明确错误。

### 8.2 GET /mcp-servers

返回全部已注册 server（含 `enabled` 状态），按 `server_id` 排序。

### 8.3 GET /mcp-servers/{server_id}

返回单个 server 定义。

**错误**：`404 MCP_SERVER_NOT_FOUND`。

### 8.4 PATCH /mcp-servers/{server_id}

部分更新 `name` / `http_url` / `sse_url` / `headers` / `description` / `enabled`。

**成功响应 `200`**：更新后的定义。

**错误**：`404 MCP_SERVER_NOT_FOUND`。

### 8.5 DELETE /mcp-servers/{server_id}

下架 server。已引用它的会话不受影响；新引用将 `404`。

**成功响应 `204`**。

**错误**：`404 MCP_SERVER_NOT_FOUND`。

## 9. 事件与 SSE API

### 9.1 GET /conversations/{conversation_id}/events

按对话顺序号查询事件。

**路径参数**：`conversation_id`（UUID）。

**查询参数**：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `after_seq` | integer | `0` | 排他游标，只返回 `seq > after_seq`；不能为负 |

**成功响应 `200`**：事件对象数组：

```json
[
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
]
```

**错误**：`404 CONVERSATION_NOT_FOUND`。

### 9.2 GET /conversations/{conversation_id}/events/stream

订阅对话事件（SSE）。参数与 [9.1](#91-get-conversationsconversation_idevents) 相同。

每条消息以 `seq` 作为 SSE `id`，完整事件作为 `data`：

```text
id: 2
data: {"schema_version":"1","event_id":"...","run_id":"...","seq":2,"type":"message","payload":{},"tenant_id":"t-1","user_id":"u-1","project_id":"p-2","source":"runner","occurred_at":"..."}

```

断线重连时，将最后成功处理的 `seq` 作为新的 `after_seq`，平台先重放遗漏事件再继续等待。

### 9.3 GET /sessions/{session_id}/events

返回该 Session 下所有 Conversation 中满足 `seq > after_seq` 的事件，按 `occurred_at`
聚合排序。参数同 [9.1](#91-get-conversationsconversation_idevents)。

注意：`seq` 是 Conversation 级游标，不是 Session 全局游标；需要严格可靠消费单个任务时，
应优先使用 Conversation 事件接口。

**错误**：`404 SESSION_NOT_FOUND`。

### 9.4 GET /sessions/{session_id}/events/stream

订阅会话聚合事件（SSE）。参数同 [9.3](#93-get-sessionssession_idevents)，消息格式同
[9.2](#92-get-conversationsconversation_ideventsstream)。断开重连同样从
`after_seq` 续传。

## 10. 交互 API

交互 ID 来自事件或 Conversation 的 `run.pending_interaction`，调用方不应自行生成。

### 10.1 POST /conversations/{conversation_id}/input

提交用户输入。

**路径参数**：`conversation_id`（UUID）。

**请求头**：`Idempotency-Key`（可选）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `interaction_id` | string | 是 | 来自事件的交互 ID |
| `value` | any | 是 | 任意 JSON 值 |
| `expected_seq` | integer | 否 | 乐观并发保护，应等于最近事件的 `seq` |

```http
POST /conversations/{conversation_id}/input

{ "interaction_id": "input-1", "value": "继续执行并保留现有配置", "expected_seq": 5 }
```

**成功响应 `200`**：更新后的 Conversation。

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `404` | `CONVERSATION_NOT_FOUND` | 对话不存在 |
| `409` | `CONFLICT` | `expected_seq` 不匹配，或交互 ID 不匹配 |
| `409` | `INVALID_STATE` | Run 当前不在等待输入状态 |

### 10.2 POST /conversations/{conversation_id}/approval

提交工具审批。

**路径参数**：`conversation_id`（UUID）。

**请求头**：`Idempotency-Key`（可选）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `approval_id` | string | 是 | 来自事件的审批 ID |
| `decision` | string | 是 | `APPROVE_ONCE` 或 `REJECT` |
| `expected_seq` | integer | 否 | 乐观并发保护 |

```http
POST /conversations/{conversation_id}/approval

{ "approval_id": "approval-1", "decision": "APPROVE_ONCE", "expected_seq": 5 }
```

**成功响应 `200`**：更新后的 Conversation。

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `404` | `CONVERSATION_NOT_FOUND` | 对话不存在 |
| `409` | `CONFLICT` / `INVALID_STATE` | 游标不匹配或未处于等待审批状态 |
| `422` | `INVALID_DECISION` | `decision` 不是 `APPROVE_ONCE` / `REJECT` |

### 10.3 POST /conversations/{conversation_id}/cancel

取消 Conversation 的 Run。

**路径参数**：`conversation_id`（UUID）。

**请求头**：`Idempotency-Key`（可选）。

**请求体**（可省略）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `expected_seq` | integer | 否 | 乐观并发保护 |

```http
POST /conversations/{conversation_id}/cancel

{ "expected_seq": 5 }
```

**成功响应 `200`**：更新后的 Conversation。重复取消配合相同 `Idempotency-Key`
不会重复执行命令。

**错误**：`404 CONVERSATION_NOT_FOUND`；`409 CONFLICT`（游标不匹配）。

## 11. 运维 API

### 11.1 GET /live

存活检查，仅表示 HTTP 进程存活。

**成功响应 `200`**：

```json
{ "status": "ok" }
```

### 11.2 GET /ready

就绪状态与当前配置，并检查持久化连接。

**成功响应 `200`**：

```json
{
  "status": "ready",
  "execution_mode": "distributed",
  "persistence_mode": "postgres",
  "instance_id": "80c32b9f2ae5:1"
}
```

### 11.3 GET /metrics

Prometheus 文本指标（`Content-Type: text/plain`），指标名以 `agentsupport_` 开头，例如：

```text
agentsupport_active_runtimes 0
agentsupport_queue_ready 0
agentsupport_jobs_running 0
agentsupport_claims_expired 0
agentsupport_outbox_pending 0
```

### 11.4 GET /cores

动态列出已注册 Runner 的能力与版本；目录为空时返回 `[]`。

**成功响应 `200`**：

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

## 12. Runner 注册协议（内部）

以下端点属于控制面与 Runner 之间的内部契约，不在公共 OpenAPI 中，调用方不应使用。
鉴权见 [12.4](#124-token-与安全)。

### 12.1 POST /runners/register

Runner 启动时注册自身。

**请求头**：`X-Runner-Token`（共享 bootstrap token）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `provider` | string | 是 | 提供方标识（如 `trae`、`deterministic`） |
| `endpoint` | string | 是 | Runner 私有 API 可达地址 |
| `version` | string | 否 | 版本，默认 `0.1.0` |
| `capabilities` | string[] | 否 | 能力：`run` / `input` / `checkpoint` / `cancel` / `events` / `resume` |
| `metadata` | object | 否 | 附加元数据 |

```http
POST /runners/register
X-Runner-Token: <shared-bootstrap-token>

{
  "provider": "deterministic",
  "endpoint": "http://runner:8080",
  "version": "0.1.0",
  "capabilities": ["run", "input", "checkpoint", "cancel", "events", "resume"]
}
```

**成功响应 `201`**：

```json
{
  "runner_id": "3d9e1c2a-...",
  "token": "<per-runner-token>"
}
```

**错误**：

| 状态码 | 错误码 | 说明 |
| --- | --- | --- |
| `401` | `RUNNER_TOKEN_INVALID` | bootstrap token 缺失或错误 |
| `409` | `RUNNER_ALREADY_REGISTERED` | 同一提供方 + 端点已注册 |
| `503` | `RUNNER_REGISTRATION_DISABLED` | 控制面未配置 `AGENTSUPPORT_RUNNER_TOKEN` |

### 12.2 POST /runners/{runner_id}/heartbeat

Runner 周期性上报心跳，维持注册有效。

**路径参数**：`runner_id`（UUID）。

**请求头**：`X-Runner-Token`（注册返回的专属 token）。

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 否 | `READY`（默认）等状态 |
| `load` | integer | 否 | 当前负载（活跃 Run 数） |
| `capabilities` | string[] | 否 | 可选能力刷新 |

**成功响应 `200`**：更新后的 Runner 注册对象。

**错误**：`401 RUNNER_TOKEN_INVALID`；`404 RUNNER_NOT_FOUND`（已注销或不存在）。

### 12.3 DELETE /runners/{runner_id}

Runner 注销（如进程退出）。

**路径参数**：`runner_id`（UUID）。

**请求头**：`X-Runner-Token`（注册返回的专属 token）。

**成功响应 `204`**（无内容）。

**错误**：`401 RUNNER_TOKEN_INVALID`；`404 RUNNER_NOT_FOUND`。

### 12.4 Token 与安全

- 注册使用共享 bootstrap token：控制面 `AGENTSUPPORT_RUNNER_TOKEN`，Runner
  `SESSION_RUNNER_TOKEN`（同一值）。
- 注册成功后控制面签发专属 token，之后的心跳 / 注销使用专属 token。
- 控制面只存 token 哈希（sha256），明文不进日志。
- 心跳超时（`AGENTSUPPORT_RUNNER_HEARTBEAT_TIMEOUT_SECONDS`，默认 30s）后由
  Reconciler / 内联健康进程清理过期 Runner。
- token 未配置时注册接口返回 `503`，栈回退到静态 `AGENTSUPPORT_CORE_RUNNER_URL`。
- 跨网络部署必须经 TLS 入口传输 token。

## 13. 会话执行配置

`config` 替代 v0.1 的 preset 导入，由调用方随 Session 请求传入；平台做结构校验后透传给
Runner，模板由上游自行保存。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version` | integer | 配置版本，默认 `1` |
| `skills` | object[] | `[{ "skill_id": "review", "enabled": true }]` |
| `tool_policy.allowed_tools` | string[] | 允许的工具名 |
| `tool_policy.approval_required_tools` | string[] | 需要审批的工具名 |
| `tool_policy.tool_descriptors` | object[] | 自定义工具描述 |
| `resources.mcp_refs` | object[] | MCP 引用 |
| `resources.workspace_template` | string | 工作区模板引用 |
| `resources.env` | object | 注入的环境变量 |
| `permissions.max_active_sessions` | integer | 并发上限（null 表示部署默认） |
| `permissions.allow_network` | boolean | 是否允许网络 |
| `permissions.allow_workspace_write` | boolean | 是否允许写工作区 |

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

## 14. 运行状态

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

## 15. 错误响应

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

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 业务错误码 |
| `message` | string | 人类可读说明 |
| `retryable` | boolean | `429` 与 `5xx` 为 `true` |
| `operation` | string | 触发错误的请求方法与路径 |
| `correlation_id` | string | 关联 ID |
| `details` | object | 附加详情（可为 null） |

### 全部错误码

| 状态码 | 错误码 | 场景 |
| --- | --- | --- |
| `404` | `WORKSPACE_NOT_FOUND` | Workspace 不存在（auto-create 关闭） |
| `404` | `SESSION_NOT_FOUND` | Session 不存在 |
| `404` | `CONVERSATION_NOT_FOUND` | Conversation 不存在 |
| `404` | `PARENT_NOT_FOUND` | 父对话不存在或不属于当前 Session |
| `404` | `SKILL_NOT_FOUND` | skill 不存在（详情/删除/会话预检） |
| `404` | `MCP_SERVER_NOT_FOUND` | MCP server 不存在（详情/更新/删除/预检） |
| `404` | `RUNNER_NOT_FOUND` | Runner 未注册（内部） |
| `409` | `IDEMPOTENCY_CONFLICT` | 幂等 Key 复用且请求不同 |
| `409` | `CONFLICT` | `expected_seq` 不匹配等并发冲突 |
| `409` | `INVALID_STATE` | Run 状态不允许当前操作 |
| `409` | `COMMAND_CONFLICT` | 命令状态冲突 |
| `409` | `CHECKPOINT_INVALID` | 检查点无效 |
| `409` | `EVENT_CONFLICT` | 事件序号冲突 |
| `409` | `RUNNER_ALREADY_REGISTERED` | Runner 重复注册（内部） |
| `422` | `INVALID_DECISION` | 审批决策非法 |
| `422` | `SKILL_INVALID_PAYLOAD` | skill 包不合法（缺 SKILL.md / 路径穿越 / 条目超限） |
| `422` | `MCP_SERVER_INVALID` | MCP server 定义不合法 |
| `422` | `MCP_TRANSPORT_UNSUPPORTED` | MCP transport 不支持 |
| `422` | `MCP_SERVER_DISABLED` | 引用的 MCP server 已停用 |
| `422` | `MCP_REF_INVALID` | `mcp_refs` 引用缺少 `server_id` |
| `429` | `RESOURCE_EXHAUSTED` | 队列或容量已满 |
| `401` | `RUNNER_TOKEN_INVALID` | Runner token 无效（内部） |
| `413` | `SKILL_TOO_LARGE` | skill 上传超过大小上限 |
| `503` | `RUNNER_REGISTRATION_DISABLED` | 未配置 Runner token（内部） |

FastAPI 自身的请求校验错误使用标准 `422` 格式，不一定包含上述统一字段。

## 16. 版本边界

### 已移除（v0.1 → v0.2）

- `/organizations`、`/users`、`/presets`、`/projects` 全部路由。
- 业务实体自动补全与 `PRESET_*` / `USER_*` / `PROJECT_*` / `ORGANIZATION_*` 错误码。
- 配置项 `AGENTSUPPORT_DEFAULT_WORKSPACE_ID` / `AGENTSUPPORT_DEFAULT_USER_ID` /
  `AGENTSUPPORT_AUTO_RESOURCE_NAME`。

### 尚未实现（仅契约项）

- Workspace / Session 的 PATCH / DELETE，Session 的 pause / resume。
- 按 tenant 的并发配额与运行时调整 `max_active_sessions`。

迁移与升级说明见[上游迁移指南](../migration/v0.1-to-v0.2.md)。

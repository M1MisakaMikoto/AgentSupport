# 项目（Project）与预设（Preset）业务需求与设计

> 状态：v1 设计定稿，代码已按本设计落地首版
> 目标层级：`租户 -> 用户 -> 项目 -> 会话 -> 对话`

## 1. 背景与目标

AgentSupport 当前的实际资源层级是：

```text
组织(organizations) -> 工作区(workspaces) -> 会话(sessions) -> 对话(conversations)
```

用户表已存在，但工作区没有绑定用户；Skill 与工具策略是部署级全局配置
（`AGENTSUPPORT_ENABLED_SKILLS` + 写死在服务里的默认工具策略），不随用户变化。

本次改造要达到两个业务目标：

1. **引入“项目”层级**，形成 `租户 -> 用户 -> 项目 -> 会话 -> 对话` 的业务层级，
   让用户在同一租户下拥有多个相互隔离的项目，每个项目有独立的会话与对话。
2. **引入“预设”概念**：把原先绑定在用户下的 Skill、工具、资源、权限、启用状态等
   设定独立成可复用的“预设”。用户拥有预设与项目；**创建项目时可以从自己的预设导入**
   这些设定，作为项目的初始配置快照。

## 2. 现状盘点与差异

| 维度 | 现状 | 目标 |
| --- | --- | --- |
| 资源层级 | 组织 -> 工作区 -> 会话 -> 对话 | 租户 -> 用户 -> 项目 -> 会话 -> 对话 |
| 用户与资源归属 | 用户表存在，工作区未绑定用户 | 项目/预设强绑定用户 |
| Skill | 部署级全局开关 | 预设/项目级声明，物理文件仍由全局目录预授权 |
| 工具策略 | 写死的默认策略 | 预设/项目级可配置，未配置时回退默认 |
| 资源（MCP 等） | 无 | 预设/项目级声明 |
| 权限 | 无 | 预设/项目级权限声明（配额、网络、写盘） |

## 3. 领域模型

### 3.1 项目（Project）

项目是用户可见、可管理的业务容器，位于用户与会话之间：

- 每个项目归属唯一的 `(organization_id, user_id)`。
- 每个项目对应一个**工作区**（存储沙箱，含 `root_path`、写入租约、容器挂载等执行细节）。
  工作区成为项目的实现细节，不暴露在业务层级中，业务层级里“会话直接属于项目”。
- 会话属于项目（`sessions.project_id`），同时保留 `workspace_id` 供运行时使用。
- 项目携带一份**配置快照** `config`，是该项目运行时 Skill/工具/资源/权限的事实来源。
- `preset_id` 记录项目创建时导入的预设；只用于追溯，不构成实时引用。

### 3.2 预设（Preset）

预设是用户级可复用定义包，包含以下内容：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `skills` | `[{skill_id, enabled}]` | 声明启用哪些 Skill，`enabled=false` 表示保留但停用 |
| `tools.allowed_tools` | `[string]` | 允许的工具集 |
| `tools.approval_required_tools` | `[string]` | 需要人工审批的工具集 |
| `tools.tool_descriptors` | `[object]` | 自定义工具描述（透传） |
| `resources.mcp_refs` | `[{name, url, enabled}]` | MCP 服务引用 |
| `resources.workspace_template` | `string?` | 项目工作区初始化模板 |
| `resources.env` | `{string: string}` | 注入项目运行环境变量 |
| `permissions.max_active_sessions` | `int?` | 项目并发会话上限覆盖 |
| `permissions.allow_network` | `bool` | 是否允许网络访问 |
| `permissions.allow_workspace_write` | `bool` | 是否允许写工作区 |
| `enabled` | `bool` | 预设总开关，`false` 时禁止用于新项目 |

## 4. 关键语义决策

### 4.1 导入 = 快照（Snapshot），不是引用

- 创建项目导入预设时，把预设定义**深拷贝**为项目的 `config` 快照。
- 之后修改预设**不影响**已创建的项目，保证运行行为稳定、可追溯。
- 项目支持“重新导入”操作（`POST /projects/{id}/preset`），显式用预设覆盖项目配置。
  重新导入是幂等的，但会替换项目当前的自定义配置，接口返回旧的 config 供调用方确认。

### 4.2 归属与隔离

- 预设、项目均以 `(organization_id, user_id)` 为归属键；创建项目时只能导入自己的预设。
- 会话、对话继承项目归属；查询/操作资源时按归属校验（当前版本无认证，先做对象级校验，
  后续接入请求级身份后在同一处注入）。
- 未绑定用户/项目的存量资源回退为全局默认配置，保证兼容。

### 4.3 Skill 与工具解析

- 运行时按 `session.project_id -> project.config` 解析启用的 Skill 列表与工具策略。
- 物理 Skill 文件仍由全局 `skills_root`（预授权目录）提供，项目只声明“启用哪些”。
- 项目配置缺失或为空时回退到部署级默认（`enabled_skills` + 默认工具策略）。
- 无项目的旧会话同样回退默认，旧行为不变。

## 5. 生命周期

| 对象 | 创建 | 编辑 | 删除 | 其他 |
| --- | --- | --- | --- | --- |
| 预设 | `POST /presets` | `PATCH /presets/{id}` | `DELETE /presets/{id}` | 被项目引用过仍可编辑（快照语义） |
| 项目 | `POST /projects`（可带 `preset_id`） | `PATCH /projects/{id}` 改名/改配置 | `DELETE /projects/{id}`（有会话时拒绝） | `POST /projects/{id}/preset` 重新导入 |
| 会话 | `POST /projects/{id}/sessions`（兼容 `POST /sessions`） | - | - | 受项目并发上限约束 |
| 对话 | `POST /sessions/{id}/conversations` | - | - | 不变 |

## 6. API 设计

### 6.1 用户

```http
POST /users
GET  /users/{user_id}
GET  /users
GET  /users/{user_id}/projects
GET  /users/{user_id}/presets
```

`POST /users` 请求：`{"username": "alice", "organization_id": "<可选>"}`。
`organization_id` 缺省时使用当前部署的默认组织（兼容单租户部署）。

组织（租户）接口：

```http
POST /organizations
GET  /organizations
GET  /organizations/{organization_id}
GET  /organizations/{organization_id}/users
```

### 6.2 预设

```http
POST   /presets
GET    /presets?user_id=<可选>
GET    /presets/{preset_id}
PATCH  /presets/{preset_id}
DELETE /presets/{preset_id}
```

`POST /presets` 请求：

```json
{
  "user_id": "…",
  "name": "代码审查预设",
  "description": "用于代码审查项目的标准配置",
  "definition": {
    "skills": [{"skill_id": "review", "enabled": true}],
    "tools": {
      "allowed_tools": ["bash", "str_replace_based_edit_tool", "json_edit_tool", "sequentialthinking", "task_done"],
      "approval_required_tools": ["bash", "str_replace_based_edit_tool", "json_edit_tool"],
      "tool_descriptors": []
    },
    "resources": {"mcp_refs": [], "workspace_template": null, "env": {}},
    "permissions": {"max_active_sessions": 2, "allow_network": true, "allow_workspace_write": true},
    "enabled": true
  }
}
```

### 6.3 项目

```http
POST /projects
GET  /projects?user_id=<可选>
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
  "preset_id": "<可选，导入的预设>"
}
```

响应示例：

```json
{
  "id": "…",
  "organization_id": "…",
  "user_id": "…",
  "workspace_id": "…",
  "name": "线上商城重构",
  "preset_id": "…",
  "config": {
    "version": 1,
    "skills": [{"skill_id": "review", "enabled": true}],
    "tool_policy": { "allowed_tools": ["…"], "approval_required_tools": ["…"], "tool_descriptors": [] },
    "resources": { "mcp_refs": [], "workspace_template": null, "env": {} },
    "permissions": { "max_active_sessions": 2, "allow_network": true, "allow_workspace_write": true }
  },
  "created_at": "…",
  "updated_at": "…"
}
```

## 7. 数据模型与迁移

### 7.1 新增表

- `presets`：`id, organization_id, user_id, name, description, definition(JSON), created_at, updated_at`
- `projects`：`id, organization_id, user_id, workspace_id, name, preset_id?, config(JSON), created_at, updated_at`

### 7.2 变更表

- `sessions` 增加 `project_id VARCHAR(36)`（可空，带索引）。

### 7.3 存量数据回填

迁移脚本按以下规则回填，保证升级不丢数据：

1. 每个组织若没有用户，创建 `default` 用户。
2. 每个存量工作区创建一个同名项目，归属该组织第一个用户，`config` 取部署级默认快照。
3. 存量会话回填 `project_id` 为其工作区对应的项目。

### 7.4 兼容

- 旧接口 `POST /workspaces`、`POST /sessions` 保留，创建的资源无项目归属，运行仍走全局默认配置。
- 会话的 `workspace_id` 字段保留，运行时租约/容器逻辑零改动。

## 8. 落地清单

首版已落地：

- [x] 领域模型：`User`、`Preset`、`Project`、`ProjectConfig`
- [x] 数据模型：`presets`、`projects` 表，`sessions.project_id`
- [x] Alembic 迁移与 `create_schema` 回填
- [x] 仓储层 CRUD（用户/预设/项目/会话归属）
- [x] 服务层：建用户、建预设、CRUD 预设、建项目（导入预设）、按项目解析 Skill/工具策略
- [x] HTTP API：用户/预设/项目路由，项目内建会话
- [x] 组织（租户）API：创建/查询/列表，按组织查用户
- [x] 查询与列表 API：用户/预设/项目/会话/对话
- [x] 项目编辑（改名/改配置）与删除（带会话保护）
- [x] 分布式 Worker 按项目解析 Skill/工具策略
- [x] 单元测试与文档

后续待办（超出首版范围）：

- [ ] 请求级认证/身份注入，把对象级归属校验升级为租户+用户隔离
- [ ] 项目配置的版本历史（当前为直接覆盖）
- [ ] MCP 资源的实际下发（`mcp_refs` 目前随 context 透传）
- [ ] 预设的版本号管理与发布/归档
- [ ] 项目级配额（会话数/存储/算力）的强制与统计

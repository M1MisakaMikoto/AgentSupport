# ADR-002：平台边界与 v0.2 API 契约（B+ 方案）

## 状态

Accepted（2026-08-11 评审确认；v0.2.0 已实现）

## 背景

- 项目定位：对 agent 服务做封装、管理、并发支持，不承担业务租户管理。
- 当前代码已出现 Organization / User / Project / Preset 业务 CRUD 与前置条件自动补全，边界模糊；
  且平台本身无鉴权，业务数据托管在裸 API 后面，生产不可持续。
- 已确定方向：provider 抽象与 Runner 注册发现「现在做」；公共 API 契约「先定边界」。
- 工作区存在未提交的 retention / idempotency 改动，属于管理面方向，本方案保持兼容。

## 决策

采用 B+ 方案：

1. 平台只管理执行资源（Workspace / Session / Conversation）与执行配置，业务实体完全交给上游。
2. 平台内置 `tenant_id` / `user_id` / `project_id` 三个结构化可选标签，并提供通用 `metadata`
   对象兜底额外业务维度；标签不校验存在性、无业务 CRUD。
3. auto-create 语义收窄：仅当调用方在请求中显式传入且不存在的 Workspace / Session ID 时，
   以该 ID 补建；调用方未传入的字段绝不自动生成（无默认 Workspace、无随机补建 ID）。
   默认开启（沿用现状），生产部署可配置关闭。
4. 执行配置（skills / tools / resources / permissions）由调用方随会话请求传入，平台透传并做
   结构校验；平台不再管理预设模板实体。
5. Provider 抽象与 Runner 注册 / 发现 / 心跳 / 能力路由纳入平台内部契约，公共 API 面不暴露。
6. 公共 API 升级为 v0.2，属于破坏性变更；删除清单见下。
7. 版本策略（已确认）：v0.2 直接替换根路径，不提供 `/v2` 前缀，也不保留 v1 兼容路由；
   发布为破坏性版本 `0.2.0`。
8. Runner 注册鉴权（已确认）：共享 token（`X-Runner-Token`）先行，契约保留升级 mTLS 的空间。

## 边界清单

### 平台管（in scope）

- Workspace / Session / Conversation 生命周期与查询。
- 队列、并发上限、Workspace 单写者租约、分布式 claim / fence、伸缩。
- 幂等、乐观并发、checkpoint、cancel / pause / resume、Runner 替换与 failover。
- 事件历史、SSE、审计（事件顶层携带标签字段）。
- 执行配置透传与工具 / 审批策略。
- Runner provider 抽象、注册 / 发现、健康心跳、按能力路由。
- 部署与伸缩（memory / docker / kubernetes）、指标与就绪。

### 平台不管（out of scope）

- 租户 / 用户 / 项目 / 预设的业务生命周期与数据一致性。
- 认证、授权与租户隔离（由网关注入身份头；平台未来按标签做强制隔离，本轮不实现）。
- Skill / MCP 市场运营、对象存储、备份恢复、计费与配额业务语义。
- 上游的业务规则、报表与领域建模。

## 标签约定

- Session 可携带 `tenant_id` / `user_id` / `project_id`：可选、不校验存在性、允许为空。
- `metadata`：任意键值对象，平台透传记录，不参与配额 / 隔离；配额只按内置字段执行。
- 生产路径由网关注入身份头 `X-Tenant-Id` / `X-User-Id` / `X-Project-Id`，优先级高于请求体；
  本地开发允许在请求体传标签。
- 事件、审计、列表过滤均支持这三个字段；Conversation 继承所属 Session 的标签。
- 标签缺失即空；无标签资源计入 `default/unknown` 无头统计桶，不强制要求标签，
  但配额与租户隔离只对带标签资源严格生效。

## 自动补建细则

- 仅作用于引用型资源：`workspace_id`、`session_id`（路径或请求体中显式传入）。
- 仅当该 ID 不存在时补建，且使用调用方传入的 ID，绝不自行生成 UUID。
- 调用方未传入的字段绝不补建：例如 Session 不存在且请求未带 `workspace_id` 时返回
  `404 SESSION_NOT_FOUND`，不再存在默认 Workspace。
- `parent_conversation_id` 等业务引用不参与补建，一律严格 404。
- 补建资源缺失标签时归入 `default/unknown` 桶；补建行为通过 `auto_created` 响应字段与
  `workspace.created` / `session.created` 事件显式暴露。
- 并发补建同一 ID 必须原子收敛（唯一约束 / advisory lock），同一 ID 只产生一个实体。
- 调用方显式传入 ID 的准确性由上游负责；平台不区分故意补建与拼写错误，补建行为均通过
  `auto_created` 与 created 事件显式暴露。

## 删除清单（v0.2 破坏性变更）

- `/organizations`、`/users`、`/presets`、`/projects` 全部 CRUD 路由。
- 业务实体自动补全逻辑与相关错误码（`PRESET_NOT_OWNED`、`PRESET_DISABLED`、
  `USER_NOT_FOUND` 等）。
- preset 导入 / 归属校验、项目配置快照语义。
- `AGENTSUPPORT_AUTO_CREATE_SCOPES` 收窄为 `workspace,session`。
- 废弃 `AGENTSUPPORT_DEFAULT_WORKSPACE_ID`、`AGENTSUPPORT_DEFAULT_USER_ID`、
  `AGENTSUPPORT_AUTO_RESOURCE_NAME`（不再存在默认资源生成）。

## 新增清单

- Session 级 SSE：`GET /sessions/{session_id}/events/stream`。
- 生命周期 API：Workspace / Session 的 GET / PATCH / DELETE，Session 的 pause / resume
  （本轮仅写入契约，实现排入后续迭代）。
- Runner 注册协议（内部）：register / heartbeat / deregister，`X-Runner-Token` 鉴权；
  `/cores` 动态反映已注册 Runner。
- 容量 / 配额控制（roadmap）：按 tenant 的并发配额、运行时调整 `max_active_sessions`。

## 数据与迁移

- `organizations` / `users` / `presets` / `projects` 表冻结并下线：先把 ID 迁入
  Session 标签列，再删除表与相关查询。
- 保留 Workspace / Session / Conversation / 事件 / 协调（jobs、claims、commands、outbox）结构。
- 未提交的 retention / idempotency 改动与本文档兼容。

## 后果

- 上游：业务实体自管，平台只收标签；公共 API 面收敛，学习成本与攻击面下降。
- 平台：职责清晰，未来可安全加入网关级身份注入与按标签隔离。
- 代价：失去业务侧便利（auto-create、预设管理、归属校验），需要上游配合传递标签；
  预设模板能力转移到上游自己的系统。

## 待确认

（无：边界相关决策已全部收敛；实施细节见 `.dev/plans/v0.2-boundary-implementation.md`）

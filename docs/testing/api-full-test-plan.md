# AgentSupport API 全量测试设计

本文定义 AgentSupport HTTP API 的完整测试边界、用例矩阵、执行方式和通过标准。API
字段与状态含义以 [API 参考](../api/agentsupport-api.md) 和运行时生成的
`/openapi.json` 为准。

## 1. 当前开发者控制台能覆盖什么

开发者控制台（`devtools/console`，默认 `http://127.0.0.1:8010`）同时作为示范、体验与参考平台：

| 入口 | 当前能力 | API 覆盖 |
| --- | --- | --- |
| 示范 · 总览 / Agent 工作台 | 执行资源概览与标签模型；Workspace/Session 创建（带标签）、任务下发（可选 `skills` / `mcp_refs`）、SSE 事件轨道、input/approval/cancel | 主任务链（workspace/session/conversation + 标签） |
| 部署 · 服务状态 | 健康端点 `/live` `/ready` `/metrics` `/cores` | 运维类路径 |
| 部署 · API 契约验收 | 结构化断言运维、资源、事件、交互四类用例，含错误路径、幂等、乐观并发、SSE 断线续传；按 OpenAPI 输出操作覆盖率 | 30 个公共路径登记；skills 与 mcp-servers 由仓库契约套件覆盖 |
| 部署 · API 参考 | 由运行时 OpenAPI 渲染参数表、请求体、响应与 cURL | 全部公共路径 |

“API 契约验收”在控制台内通过真实 HTTP 请求对每个用例给出通过/失败、耗时与说明，并展示 OpenAPI
操作覆盖率；任何未覆盖操作会显式列出。控制台适合作为开发、演示与现场验收入口，但正式放行结论
仍必须来自第 5 节规划的仓库内契约套件与第 6 节 Compose 黑盒场景——页面验收不替代 CI 门禁。

## 2. 全量测试的定义

一次完整 API 验收必须同时满足：

1. 运行时 OpenAPI 中每个公开 operation 都被测试清单登记，新增或删除接口时覆盖门禁失败。
2. 30 个 AgentSupport 公共路径都有正常、校验失败和相关业务错误断言。
3. 所有公共 `POST` 都验证幂等重放和 Key 冲突；三个交互接口验证 `expected_seq` 冲突。
4. 普通响应、统一错误响应、关联 ID、实例 ID、状态码和 Content-Type 都符合契约。
5. SSE 验证历史重放、排他游标、顺序、去重和断线续传。
6. 9 个 Session Runner 私有路径通过独立契约套件，不能由公共 API 的页面冒烟替代；
   另有 3 个控制面内部 Runner 注册端点（`/runners/register`、heartbeat、注销）由独立契约测试覆盖。
7. 内存模式和真实 Compose 分布式模式都通过；PostgreSQL 并发与进程重启场景通过。
8. 公共 API 无 token 鉴权：OpenAPI 不含 `securitySchemes`，所有操作无 `security` 声明；
   请求不带任何 `Authorization` 头也能通过（`tests/contract/agentsupport_api/test_no_auth.py`）。
9. auto-create 仅对调用方显式传入且不存在的 `workspace_id` / `session_id` 以其 ID 补建，
   在响应中回传 `auto_created`；未传入的字段绝不自动生成；关闭开关
   （`AGENTSUPPORT_AUTO_CREATE_MISSING=false`）后必须恢复严格 `404` 行为。
10. Session / Conversation 引用 `skills` 与 `mcp_refs` 时在创建时预检：不存在的 skill 或
    MCP server 返回 `404`，停用的 MCP server 返回 `422`。

“全量”指当前公开契约和明确支持的失败行为，不要求通过不存在的资源查询、认证或租户 API。

## 3. 公共 API 用例矩阵

### 3.1 运维接口

| 编号 | 接口 | 必测断言 |
| --- | --- | --- |
| OP-01 | `GET /live` | `200`、固定 JSON、关联 ID 和实例 ID 响应头 |
| OP-02 | `GET /ready` | `200`、ready 字段、执行/持久化模式、实例 ID；数据库不可用时不得误报 ready |
| OP-03 | `GET /metrics` | `200 text/plain`、所有必需 `agentsupport_` 指标存在且值可解析 |
| OP-04 | `GET /cores` | `200`、核心类型、版本、能力集合符合契约 |

### 3.2 资源接口

| 编号 | 接口 | 正向场景 | 反向与边界场景 |
| --- | --- | --- | --- |
| RS-01 | `POST /workspaces` | 创建并验证 UUID、名称、路径、时间 | 空名称、121 字符、缺字段、额外/错误类型字段策略 |
| RS-02 | `POST /sessions` | 绑定已存在 Workspace | 无效 UUID；Workspace 不存在（默认自动创建并返回 `auto_created`，关闭自动补全后 `404 WORKSPACE_NOT_FOUND`） |
| RS-03 | `POST /sessions/{id}/conversations` | 根任务、同 Session 子任务、运行状态和初始事件 | Session 不存在（默认自动创建，关闭后 `404 SESSION_NOT_FOUND`）；空任务、父任务不存在、跨 Session 父任务、容量耗尽 `429` |

RS-01 至 RS-03 都必须使用相同 `Idempotency-Key` 重放相同请求，确认返回同一资源且没有新增事件；
再用相同 Key 提交不同请求，确认返回 `409 IDEMPOTENCY_CONFLICT`。

自动补全路径必须额外断言：

- 缺失资源沿用调用方传入的 UUID；重复调用同一 UUID 收敛到同一实体，不产生重复资源。
- 201 响应包含 `auto_created`（含 `workspace`、`session`），未发生自动补全的响应不得包含该字段。
- 只读查询与流式/运行态接口（事件轮询、SSE、input/approval/cancel）不触发自动创建，保持 `404`。

| RS-04 | `/skills` 与 `/skills/{skill_id}` | 上传（SKILL.md / zip）后可在 `/skills` 发现、`GET` 详情；同名覆盖为新版本 | 非法 skill_id、zip 缺 SKILL.md、路径穿越、大小超限 `413/422`；删除后 `404` |
| RS-05 | `/mcp-servers` 与 `/mcp-servers/{server_id}` | 注册 http/sse server 后可在清单发现、`PATCH` 更新、`DELETE` 下架 | 非法 server_id / transport、缺端点 `422`；引用不存在 server `404 MCP_SERVER_NOT_FOUND`、停用 `422 MCP_SERVER_DISABLED` |

Session 与 Conversation 的 `skills` / `mcp_refs` 引用必须断言继承 / 覆盖 / 空数组禁用三种语义。

### 3.3 事件接口

| 编号 | 接口 | 必测断言 |
| --- | --- | --- |
| EV-01 | `GET /conversations/{id}/events` | 默认游标、`after_seq` 排他语义、升序、事件结构、资源不存在、负游标 `422` |
| EV-02 | `GET /conversations/{id}/events/stream` | `text/event-stream`、SSE `id == seq`、历史重放、新事件到达、断线后续传、不重复、资源不存在 |
| EV-03 | `GET /sessions/{id}/events` | 聚合多个 Conversation、时间排序、游标语义、空 Session、资源不存在、负游标 `422` |
| EV-04 | `GET /sessions/{id}/events/stream` | `text/event-stream`、多 Conversation 聚合、断线从 `after_seq` 续传、资源不存在 |

SSE 测试必须设置有限超时，并在收到目标事件后主动关闭连接，不能依赖永不结束的流自然退出。

### 3.4 交互接口

| 编号 | 接口 | 正向场景 | 反向与边界场景 |
| --- | --- | --- | --- |
| IN-01 | `POST /conversations/{id}/input` | WAITING_INPUT 提交任意 JSON 值并继续运行 | 错误 interaction ID、错误状态、资源不存在、陈旧 `expected_seq` |
| IN-02 | `POST /conversations/{id}/approval` | `APPROVE_ONCE`、`REJECT`，并验证副作用是否执行 | 非法 decision `422`、错误 approval ID、错误状态、资源不存在、陈旧序号 |
| IN-03 | `POST /conversations/{id}/cancel` | 取消活动任务、空请求体、带序号请求、清除 pending interaction | 已终止任务、资源不存在、陈旧序号、Runner 停止未确认 |

IN-01 至 IN-03 都必须验证相同 Key 重放不产生第二个命令或事件，以及相同 Key 不同 payload 返回
`409 IDEMPOTENCY_CONFLICT`。

### 3.5 横切契约

每个类别至少包含以下通用断言：

- 客户端传入 `X-Correlation-ID` 时原值回传；未传入时生成非空 UUID。
- 每个响应都带 `X-AgentSupport-Instance`。
- 业务错误体包含 `code`、`message`、`retryable`、`operation`、`correlation_id` 和 `details`。
- FastAPI 参数校验错误固定为 `422`，测试不把它误认为统一业务错误体。
- JSON Schema/OpenAPI 响应校验通过；未知字段策略由契约测试固定，避免框架升级时静默变化。

### 3.6 鉴权与前置条件自动补全契约

- 无鉴权契约：`openapi.json` 无 `securitySchemes`；每个公开 operation 无 `security`；
  `/live`、`/ready`、`/metrics`、`/cores` 与资源创建接口在无 `Authorization` 头时均可用；
  参数校验错误为 `422` 而非 `401/403`。
- 自动补全矩阵：分别在 `AGENTSUPPORT_AUTO_CREATE_MISSING=true`（默认）与 `false` 两种配置下，
  覆盖 `workspace` / `session` 两条缺失链路（仅限调用方显式传入且不存在的 ID），断言
  201+`auto_created`（含 `workspace`、`session`）或严格 404，以及
  `AGENTSUPPORT_AUTO_CREATE_SCOPES` 作用域收窄。
- `AGENTSUPPORT_API_AUTH_MODE` 配置为非 `none` 时必须启动失败（fail-fast）。

## 4. Session Runner 私有契约

Runner 套件独立覆盖以下 9 个路径：

| 范围 | 接口与重点 |
| --- | --- |
| 健康 | `GET /live`、`GET /ready` 的模式和状态 |
| 运行 | `POST /runs` 的启动、重复 run、fence/lease 校验 |
| 交互 | `POST /runs/{id}/input`、`approval` 的 command ID 幂等和交互匹配 |
| 生命周期 | `checkpoint`、`cancel`、`resume` 的状态机、检查点哈希和跨进程恢复 |
| 事件 | `GET /runs/{id}/events` 的游标、顺序、资源不存在 |

已有 `tests/contract/runner_api/` 作为基础；覆盖门禁应同样从 Runner 的 OpenAPI 自动核对 9 个路径，
防止新增接口没有测试。

## 5. 测试分层与文件规划

| 层级 | 建议位置 | 运行环境 | 目的 |
| --- | --- | --- | --- |
| OpenAPI 覆盖门禁 | `tests/contract/agentsupport_api/test_openapi_coverage.py` | 进程内 ASGI | operation 与用例清单一一对应 |
| 公共 HTTP 契约 | `tests/contract/agentsupport_api/` | 进程内 ASGI、确定性 Runner | 30 个公共路径 + 3 个内部 Runner 注册路径、错误体、Headers、幂等、SSE |
| Runner HTTP 契约 | `tests/contract/runner_api/` | 进程内 ASGI | 9 个私有路径和 fencing/checkpoint |
| 服务集成 | `tests/e2e/agentsupport/` | 内存与 SQLite | 状态机、资源租约、故障映射 |
| 分布式黑盒 | `tests/e2e/compose/test_public_api.py` | Compose、PostgreSQL、Redis、多 API/Worker | 真实网络、持久化、跨实例命令、重启与并发 |
| 浏览器冒烟 | `tests/e2e/devtools/` | Playwright 或现有 HTTP 客户端 | 控制台入口、代理、SSE 和结果展示 |

公共契约测试不调用 Python service 方法构造状态；前置资源也通过 HTTP 创建。只有需要精确注入故障时才使用
测试替身，并在用例名中明确这是故障映射测试。

## 6. Compose 黑盒场景

在控制台已经部署的确定性 Runner 环境中执行以下场景：

1. 健康、就绪、指标和核心发现全部通过。
2. 创建 Workspace -> Session -> Conversation，轮询并订阅事件直至 COMPLETED。
3. 创建等待输入任务，从另一个 API 实例提交 input，确认只生成一次事件并完成。
4. 创建等待审批任务，分别验证批准和拒绝；拒绝路径确认工具副作用没有发生。
5. 活动任务取消，确认最终状态、事件、租约和运行时指标归零。
6. 两个 Session 竞争同一 Workspace，确认单写者租约和队列接替。
7. PostgreSQL 并发提交同一幂等 Key，只创建一个资源；并发追加事件序号连续且唯一。
8. SSE 消费中断后使用最后 `seq` 重连，遗漏事件完整、无重复。
9. 重启 API/Worker/Runner 后验证持久化数据、待处理命令和检查点恢复。

每个场景使用唯一前缀和 Idempotency Key。当前 API 没有删除资源的接口，因此测试数据库应按运行隔离，
完整验收结束后由 Compose 测试环境统一销毁数据卷，不能复用开发数据卷执行破坏性测试。

## 7. 执行入口

当前仓库测试可直接运行：

```powershell
$env:TEST_TMP=(New-Item -ItemType Directory -Force .pytest-tmp).FullName
$env:TEMP=$env:TEST_TMP
$env:TMP=$env:TEST_TMP
.venv\Scripts\python.exe -m ruff check src tests alembic devtools
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

需要本机 PostgreSQL 测试库时：

```powershell
$env:RUN_POSTGRES_DISTRIBUTED_TESTS="1"
.venv\Scripts\python.exe -m pytest tests/integration/persistence/test_repository.py -q -p no:cacheprovider
```

开发者控制台的“回归验收”可以执行上述仓库测试和在线任务冒烟，但在第 5 节规划的公共契约与
Compose 黑盒套件全部实现前，结果只能标记为“回归通过”，不能标记为“API 全量通过”。

## 8. 放行标准

- OpenAPI operation 覆盖率为 100%，公共 30/30、Runner 9/9。
- 所有必测用例通过，无 xfail；环境不满足时只能显式 skip 并使完整验收不通过。
- 语句覆盖率不是唯一目标；API 路由、错误码、状态迁移和事件类型矩阵不得缺项。
- 并发/SSE 场景连续运行 3 次无不稳定失败。
- 报告至少保存 JUnit XML、失败请求的 correlation ID、Compose 服务日志和测试环境版本。
- 控制台只读取结构化测试报告展示结果，不以日志关键字判断成功。

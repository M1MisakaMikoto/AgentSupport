> 简体中文 | [English](README.en.md)

# AgentSupport

AgentSupport 是一个自托管服务，通过 HTTP API 运行编码智能体（coding agent）。它在控制面管理
Workspace、Session 和 Conversation，而隔离的 Session Runner 负责执行任务、工具调用和模型交互。

核心能力：

- 持久的 Workspace、Session 和 Conversation 资源。
- 有序事件历史与 Server-Sent Events（SSE）回放。
- 幂等命令、乐观并发、审批、取消与检查点恢复。
- 内联开发模式与基于 PostgreSQL 的分布式执行。
- 受控的 Trae、MCP、Skill 和工作区工具集成。
- Docker Compose 与 Kubernetes 部署清单。

## 文档

中文为默认文档语言：

- [AgentSupport API 参考](docs/api/agentsupport-api.md)
- [上游迁移指南 v0.1 → v0.2](docs/migration/v0.1-to-v0.2.md)
- [可观测性](docs/observability.md)
- [API 全量测试设计](docs/testing/api-full-test-plan.md)
- [目录架构](docs/architecture/directory-structure.md)
- [架构决策记录（ADR）](docs/adr/)

英文版本：

- [README（English）](README.en.md)
- [Directory architecture（English）](docs/architecture/directory-structure.en.md)
- [ADR-001（English）](docs/adr/001-src-layout-and-runtime-boundaries.en.md)

## 项目结构

生产包采用 `src/` 布局：

| 包 | 职责 |
| --- | --- |
| `agentsupport` | 控制面 API、编排、持久化与分布式 Worker |
| `session_runner` | 私有执行面 HTTP 服务，以及 Trae/MCP/工具适配器 |
| `agent_runner_contracts` | 控制面与执行面共享的传输模型（wire models） |
| `devtools` | 本地部署与任务调试控制台 |

## 环境要求

- Python 3.12
- 分布式栈需要带 Compose 插件的 Docker Engine
- 在 Compose 之外运行分布式服务时需要 PostgreSQL 16 和 Redis 7
- Kubernetes 部署需要 Kubernetes 集群和 `kubectl`

## 本地开发

创建虚拟环境并安装开发依赖：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

运行测试套件。使用仓库本地的临时目录可避免 Windows 临时目录的权限问题：

```powershell
$env:TEST_TMP=(New-Item -ItemType Directory -Force .pytest-tmp).FullName
$env:TEMP=$env:TEST_TMP
$env:TMP=$env:TEMP
.venv\Scripts\python.exe -m pytest -q
```

以默认的内存、内联执行模式启动 API：

```powershell
.venv\Scripts\python.exe -m uvicorn agentsupport.main:app --reload
```

常用本地端点：

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/docs` | OpenAPI 界面（顶部内嵌完整 API 参考） |
| `http://127.0.0.1:8000/debug/` | 任务调试界面 |
| `http://127.0.0.1:8000/live` | 存活检查 |
| `http://127.0.0.1:8000/ready` | 就绪状态与当前配置 |
| `http://127.0.0.1:8000/metrics` | Prometheus 指标 |

## API 鉴权与前置条件自动补全

AgentSupport 公共 API **有意不提供 token / API Key 等鉴权措施**，这是被契约测试
（`tests/contract/agentsupport_api/test_no_auth.py`）固定的部署模式：
`AGENTSUPPORT_API_AUTH_MODE` 仅支持 `none`，配置为其他值会启动失败。正式对公网开放前，
请在网关层补充限流（当前仓库不提供正式认证和租户隔离）。

平台只管理执行资源（Workspace / Session / Conversation）。租户 / 用户 / 项目等业务实体
由上游系统管理，平台通过 `tenant_id` / `user_id` / `project_id` 标签透传（不校验存在性），
并带进事件与审计；执行配置随 Session 请求传入，不再有预设模板实体。

**auto-create 语义（v0.2）**：仅当调用方在请求中显式传入且不存在的
`workspace_id` / `session_id` 时，平台以该 ID 补建；调用方未传入的字段绝不自动生成
（不再有默认 Workspace / 默认用户）。补建通过 201 响应中的 `auto_created` 字段显式回传，
并产生 `workspace.created` / `session.created` 事件。
流式/运行态接口（SSE 事件流、事件轮询、input/approval/cancel）仍要求资源已存在；
只读查询不触发自动创建，保持 404。

自动补全默认开启，生产建议关闭：

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `AGENTSUPPORT_AUTO_CREATE_MISSING` | `true` | 总开关；`false` 恢复严格 404 行为 |
| `AGENTSUPPORT_AUTO_CREATE_SCOPES` | `workspace,session` | 仅执行资源参与补建 |

Runner 自注册（内部契约）使用共享 token：

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `AGENTSUPPORT_RUNNER_TOKEN` | 空 | 控制面 Runner 注册 token；留空关闭注册 |
| `AGENTSUPPORT_RUNNER_HEARTBEAT_TIMEOUT_SECONDS` | `30` | 心跳超时，过期 Runner 被清理 |
| `SESSION_RUNNER_TOKEN` | 空 | Runner 侧同名共享 token |
| `AGENTSUPPORT_CONTROL_PLANE_URL` | 空 | Runner 注册目标地址 |
| `SESSION_RUNNER_ENDPOINT` | 空 | Runner 自报可达地址 |

在开发 Runner 的 HTTP 契约时，可以用确定性模式单独启动私有 Runner：

```powershell
$env:SESSION_RUNNER_MODE="deterministic"
.venv\Scripts\python.exe -m uvicorn session_runner.main:app --port 8080
```

## 开发控制台

在 Windows 上启动本地部署控制台：

```powershell
.\start-console.ps1
```

使用 `-NoBrowser` 可不打开浏览器启动。等效的直接命令是：

```powershell
.venv\Scripts\python.exe -m devtools.console
```

打开 `http://127.0.0.1:8010`。控制台负责 Compose 栈管理、服务扩缩容、状态、日志、任务调试，
以及仓库预定义的验证套件。它运行在 Compose 栈之外，因此容器替换期间仍然可用。

控制台按 AI 软件形态组织为仅有的两个大板块：

| 板块 | 子页面 | 能力 |
| --- | --- | --- |
| 示范 | 总览 / Agent 工作台 | 执行资源概览（Workspace / Session / Conversation）与标签模型；Agent 工作台与任务调试页提供 Workspace / Session 选择与新建（带 `tenant_id` / `user_id` / `project_id` 标签）、对话式任务下发（可勾选 Skill 与 MCP Server 引用，随 Conversation 动态启用）、SSE 事件轨道、input/approval/cancel 人工关口，以及模型连通性自检（从 Runner 侧探测模型 API 的 TLS 证书）；业务实体由上游管理，控制台不再提供组织/用户/项目/预设管理页 |
| 部署 | 服务状态 / 部署操作 / 验收中心 / API 参考 | Compose 服务拓扑与健康端点（`/live` `/ready` `/metrics` `/cores`）、部署/启动/停止与扩缩容、部署回归验收与 API 契约验收（结构化断言全部公共接口、错误路径、幂等、乐观并发、SSE 断线续传，并输出 OpenAPI 操作覆盖率报告）、由运行时 `/openapi.json` 自动生成的交互式 API 参考 |

API 契约验收是控制台内可重复的自动检查入口；仓库测试与发布标准仍以
[API 全量测试设计](docs/testing/api-full-test-plan.md) 为准。

控制台支持 WSL2 Docker Engine、本地 `docker` CLI 和命名 Docker context。当自动检测不适用时，
可配置默认传输方式：

```powershell
$env:AGENTSUPPORT_DEV_DOCKER_TRANSPORT="wsl2" # wsl2、local 或 context
$env:AGENTSUPPORT_DEV_WSL_DISTRIBUTION="Ubuntu-24.04"
$env:AGENTSUPPORT_DEV_DOCKER_CONTEXT=""
$env:AGENTSUPPORT_DEV_LAN_FORWARD="auto"     # auto（默认）或 off；wsl2 部署成功后自动开放局域网访问
.\start-console.ps1
```

对于位于 Windows 驱动器上的仓库，建议使用控制台进行 WSL2 构建；它会将 Docker 构建输入暂存到
WSL 文件系统，避免 DrvFS 元数据限制。

## 示例 Skill

仓库根目录的 `skills/` 提供两个可直接上传使用的示例 Skill（`review` 与 `docs-writing`）。
本地开发默认 `AGENTSUPPORT_SKILLS_ROOT=skills`，启动后即可在控制台勾选；Docker Compose
使用独立的 `skills-data` 命名卷，需要先通过 `POST /skills`（multipart，`SKILL.md` 或 zip）
上传一次，之后控制台与任务调试页的勾选列表会显示可用 Skill。启用的 Skill 会随运行请求
把 `SKILL.md` 内容注入 Trae agent 的 system prompt（超出约 20K 字符截断），并只读挂载
`skills-data` 到 Runner，供 agent 按指引执行或读取附属文件。

## Docker Compose

Compose 栈包含 API 网关、PostgreSQL、Redis、数据库迁移任务、API、Worker、Reconciler、
Event Publisher 和 Session Runner。

无需模型访问的确定性执行：

```powershell
$env:SESSION_RUNNER_MODE="deterministic"
docker compose up -d --build
```

Trae 执行需要先配置模型提供方，再启动栈：

```powershell
$env:SESSION_RUNNER_MODE="trae"
$env:TRAE_PROVIDER="anthropic"
$env:TRAE_MODEL="claude-sonnet-4-20250514"
$env:TRAE_API_KEY="<api-key>"
docker compose up -d --build
```

如果公司网络对模型域名做 SNI 级 TLS 审计（症状：Runner 报
`TLS: CERTIFICATE_VERIFY_FAILED ... key too weak`，控制台自检显示 1024 位伪证书），
可以把 `TRAE_MODEL_BASE_URL` 指向模型 CDN 的真实 CNAME、用 `TRAE_MODEL_HOST` 保留原始
Host 头，并给 Runner 配置 `HTTPS_PROXY` 出网（详见 `.env.example` 注释）。

API 暴露在 `http://localhost:8000`，PostgreSQL 位于 `localhost:5432`。检查或停止栈：

```powershell
docker compose ps
docker compose logs --tail 200 api worker runner
docker compose down
```

除非显式传入 `--volumes`，`docker compose down` 会保留命名数据卷。

### 局域网访问（WSL2 部署）

WSL2 使用 NAT，栈启动后只有 Windows 本机可通过 `http://localhost:8000/docs` 访问；
局域网其他机器无法直接路由到 WSL 的 IP。控制台使用 wsl2 transport 执行
“部署”或“启动”时，会在 API 就绪后自动运行仓库根目录的 `wsl-lan-forward.ps1`
（首次会弹出 UAC 确认，建议勾选记住授权），在 Windows 上创建
端口转发（局域网 IP:8000 → WSL IP:8000）和仅限本地子网的防火墙放行规则。
之后同一局域网内的机器可打开：

```text
http://<Windows主机局域网IP>:8000/docs
```

WSL 每次重启后 IP 会变化，届时重新运行一次该脚本即可刷新映射。
也可以通过控制台环境变量关闭自动开放（`AGENTSUPPORT_DEV_LAN_FORWARD=off`），
或手动运行 `.\wsl-lan-forward.ps1`。
注意：公共 API 默认无鉴权（`AGENTSUPPORT_API_AUTH_MODE=none`），
防火墙规则已限定本地子网；如需更严格限制，可加 `-LanAddress` 指定监听地址，
或手动收紧防火墙规则。

### 其他模型提供方

DeepSeek 的 Anthropic 兼容端点使用专门的提供方实现：

```powershell
$env:TRAE_PROVIDER="deepseek_anthropic"
$env:TRAE_MODEL_BASE_URL="https://api.deepseek.com/anthropic"
$env:TRAE_MODEL="<model-name>"
$env:TRAE_API_KEY="<api-key>"
```

OpenAI 兼容端点：

```powershell
$env:TRAE_PROVIDER="openai"
$env:TRAE_MODEL_BASE_URL="https://example.com/v1"
$env:TRAE_MODEL="<model-name>"
$env:TRAE_API_KEY="<api-key>"
```

模型凭据只传递给 Runner 服务。请勿将凭据提交到 `.env` 或其他仓库文件。

## 配置

`.env.example` 列出了支持的本地和分布式设置。重要变量包括：

| 变量 | 用途 |
| --- | --- |
| `AGENTSUPPORT_PERSISTENCE_MODE` | `memory` 或 `postgres` 持久化 |
| `AGENTSUPPORT_EXECUTION_MODE` | `inline`、`distributed` 或 `temporal` 执行 |
| `AGENTSUPPORT_DATABASE_URL` | SQLAlchemy PostgreSQL URL |
| `AGENTSUPPORT_REDIS_URL` | 可选的 Redis 事件通知 URL |
| `AGENTSUPPORT_TEMPORAL_HOST` | Temporal 服务地址（temporal 模式） |
| `AGENTSUPPORT_TEMPORAL_NAMESPACE` | Temporal namespace（默认 `default`） |
| `AGENTSUPPORT_TEMPORAL_TASK_QUEUE` | Temporal task queue（默认 `agentsupport`） |
| `AGENTSUPPORT_RUNTIME_DRIVER` | `memory`、`docker_cli` 或 `kubernetes` 运行时 |
| `AGENTSUPPORT_WORKSPACE_ROOT` | 工作区数据目录 |
| `AGENTSUPPORT_MAX_ACTIVE_SESSIONS` | 并发活跃 Session 上限 |
| `AGENTSUPPORT_MAX_QUEUED_CONVERSATIONS` | Conversation 队列上限 |
| `AGENTSUPPORT_AUTO_CREATE_MISSING` | 缺失前置条件自动补全总开关（默认开启） |
| `AGENTSUPPORT_API_AUTH_MODE` | API 鉴权模式，仅支持 `none` |
| `AGENTSUPPORT_RUNNER_TOKEN` | Runner 自注册共享 token（留空关闭注册） |
| `SESSION_RUNNER_MODE` | `deterministic` 或 `trae` Runner 模式 |
| `TRAE_PROVIDER` | 模型提供方实现 |
| `TRAE_MODEL`、`TRAE_MODEL_BASE_URL`、`TRAE_API_KEY` | Runner 模型配置 |

`temporal` 执行模式是阶段 0/1 的原型形态：每个 run 由一个
`RunSessionWorkflow` 驱动（workflow_id = run_id），Signal 代替命令队列，
事件历史承担检查点职责；暂停/恢复、取消、幂等、崩溃恢复语义与自研
`distributed` 模式等价且恢复粒度更细。启动方式见
`docs/migration/v0.2-to-v0.3.md`，设计决策见 `docs/adr/003-temporal-execution.md`。
| `SESSION_RUNNER_TRAE_PROMPT_FILE` | 可选的 Trae 系统提示词文件路径；未配置时使用内置提示词 |

## 数据库迁移

Compose 会在启动 API 和 Worker 前应用 Alembic 迁移。对于外部 PostgreSQL 实例，设置
`AGENTSUPPORT_DATABASE_URL` 后运行：

```powershell
$env:AGENTSUPPORT_AUTO_CREATE_SCHEMA="false"
.venv\Scripts\python.exe -m alembic upgrade head
```

生产与分布式部署应保持自动建表关闭。

## 分布式运行

API 和 Worker 进程无状态，可以独立扩缩容。PostgreSQL 是任务、事件、命令、检查点和租约的事实来源；
Redis 仅加速订阅者唤醒。

```powershell
$env:SESSION_RUNNER_MODE="deterministic"
docker compose up -d --build --scale api=2 --scale worker=3
Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready
Invoke-WebRequest -UseBasicParsing http://localhost:8000/metrics
```

Worker 使用带过期时间的认领（expiring claims）和栅栏纪元（fence epochs）。暂停的 Run 会释放
执行容量，替换的 Runner 会从已验证的检查点继续。

## Kubernetes

Kubernetes 部署需要外部 PostgreSQL 数据库以及已发布的 AgentSupport API 和 Runner 镜像。先创建
命名空间和密钥，运行迁移 Job，再应用服务：

```powershell
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl -n agentsupport create secret generic agentsupport-secrets --from-literal=AGENTSUPPORT_DATABASE_URL='<postgresql+psycopg URL>'
kubectl -n agentsupport create secret generic agentsupport-runner-secrets --from-literal=TRAE_API_KEY='<api-key>'
kubectl apply -f deploy/kubernetes/migrate-job.yaml
kubectl -n agentsupport wait --for=condition=complete job/agentsupport-db-migrate --timeout=5m
kubectl apply -f deploy/kubernetes/agentsupport.yaml
```

在应用 `deploy/kubernetes/keda-worker.yaml` 之前先安装 KEDA。没有 KEDA 时，配置固定的 Worker
副本数。Workspace PVC 需要兼容 `ReadWriteOnce` 的 StorageClass。

## 生产注意事项

当前仓库不提供正式认证、租户隔离、托管 MCP/Skill 市场集成、对象存储备份或多区域协调。生产使用前，
请将其部署在经过认证的网关之后，并制定数据库、工作区卷和密钥的备份策略。由于 API 无鉴权且 auto-create
默认开启（仅对显式传入的 workspace/session ID 补建），公网暴露前必须在网关配置限流，生产建议关闭
自动补全（`AGENTSUPPORT_AUTO_CREATE_MISSING=false`）。

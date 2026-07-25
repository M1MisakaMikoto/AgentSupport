# 首期实施结论提案

更新时间：2026-07-24

本文只记录实验结论和待审批的首期实现决策，不替代 `accepted.md` 中已经确认的事项。审批通过后，再将决策合并到正式计划。

## 一、实验结论

| 能力 | 证据 | 结论 |
|---|---|---|
| 真实 Core 普通运行 | `agent-state-experiment/results/real-trae-success-6steps.json` | Qwen 3.6 Plus 经 Trae CLI 0.1.0 能写入 Workspace、校验结果并完成 trajectory |
| 真实工具审批 | `tool-approval-experiment/real-trae-approval-approve_once.json`、`real-trae-approval-reject.json` | Trae `_tool_caller` 是真实模型运行中的统一拦截边界；批准和拒绝均可在副作用前生效 |
| 跨进程恢复 | `tool-approval-experiment/real-trae-checkpoint-resume-result.json` | 新 Trae 实例可从平台检查点恢复，工具只执行一次并完成任务 |
| 检查点完整性 | `test_real_trae_checkpoint_resume.py` 的反例 | 只保存 pending ToolCall 或 ToolResult 不够，必须保存完整 Context Bundle |
| Docker 基础运行时 | Docker Desktop 29.6.2 最小容器实验 | Workspace 挂载、租约标签、SIGTERM 正常退出和 SIGKILL 超时均可观察 |
| Trae 自带 Docker 模式 | `tool-approval-experiment/report.md` | Trae 0.1.0 的 `--docker-image` 在当前 Windows 环境存在 Unix 构建脚本和路径问题，不能直接作为平台 RuntimeDriver |

## 二、待审批的首期架构决策

**首期采用“Session 容器内的 Trae Core Runner + 可注入 ToolGateway”，不直接依赖原始 `trae-cli run` 作为审批恢复入口。**

具体约束：

1. 平台构建并固定 Session 镜像，镜像内包含 Trae 源码、依赖和 `core_runner`。
2. `core_runner` 在容器内实例化 Trae Agent，并替换其 `_tool_caller` 为平台适配的 `ToolGatewayExecutor`。
3. bash、文件编辑、Git、MCP 和第三方 Sidecar 工具始终在 Session 容器或同 Pod 网络内执行。
4. 平台负责授权、审批、检查点、恢复、租约和事件持久化，不直接执行容器内工具。
5. 原始 `trae-cli run` 保留为普通运行 Smoke Test，不作为首期审批/暂停/恢复的正式入口。

## 三、首期 CoreRuntime 契约

```text
run(request, event_sink)
accept_input(run_id, interaction_id, input)
checkpoint(run_id, reason)
resume(checkpoint_ref, input, event_sink)
cancel(run_id)
health()
```

`request_user_input` 作为平台授权的交互工具，统一产生 `interaction.requested` 事件；工具审批和用户提问都进入 `WAITING_INPUT`。等待 30 分钟后进入 `SUSPENDING -> PAUSED`，用户反馈后通过新 Core 实例恢复。

检查点至少包含：

```text
checkpoint_id
conversation_id
run_id
last_event_seq
context_bundle
pending_interaction
pending_tool_calls
tool_batch_hash
workspace_ref
workspace_write_lease_epoch
core_type
core_version
```

## 四、实施前必须保留的验收项

- 真实 Trae 批准工具调用时，副作用发生前必须产生审批事件。
- 真实 Trae 拒绝工具调用时，所有批次工具均不得产生副作用。
- 进程 A 创建检查点并退出后，进程 B 使用完整 Context Bundle 恢复，工具只执行一次。
- 检查点上下文哈希、租约代次和工具参数哈希不匹配时，恢复必须拒绝。
- Docker RuntimeDriver 必须区分 SIGTERM 正常退出和 SIGKILL 超时。
- Workspace 旧租约未确认停止时，新 Session 不得挂载可写 Workspace。
- Trae CLI 自带 Docker 模式不作为首期支持依据。

## 五、Runner 最小 RPC 契约

Runner 只在 Session 容器私有网络暴露，平台是唯一调用方。首期可以使用 HTTP/JSON；后续替换为 gRPC 不改变语义。

```text
GET  /live
GET  /ready
POST /runs
POST /runs/{run_id}/input
POST /runs/{run_id}/checkpoint
POST /runs/{run_id}/cancel
GET  /runs/{run_id}/events?after_seq=
```

`POST /runs` 接收 `run_id`、`conversation_id`、`context_bundle`、`workspace_ref`、`tool_policy` 和 `core_version`。Runner 不接收平台数据库连接串，不保存平台业务数据。

Runner 到平台的事件统一使用：

```text
schema_version/event_id/run_id/seq/type/payload/source/occurred_at
```

其中 `type` 至少包括：`run.started`、`message`、`tool.call`、`interaction.requested`、`checkpoint.created`、`tool.result`、`run.completed`、`run.failed`、`run.lost`。

审批流程为：

```text
Runner -> interaction.requested -> Platform
Platform -> approval/input decision -> Runner
Runner -> tool execution/result -> Platform
```

平台到 Runner 的请求必须携带 `run_id`、`session_id`、`container_id`、`lease_epoch`、`fence_epoch` 和 `correlation_id`；过期租约一律拒绝。Runner 的控制接口只允许容器启动时注入的短期认证令牌访问。

## 六、实现拆分建议

首期代码按以下边界拆分：

```text
platform/core_runtime.py        # 平台侧 CoreRuntime Port
platform/trae_adapter.py        # Runner RPC 客户端和事件转换
session_runner/server.py        # 容器内 HTTP/RPC 服务
session_runner/trae_executor.py # Trae 实例、ToolGateway 注入和恢复循环
session_runner/checkpoint.py    # Context Bundle 和 pending ToolCall 持久化
```

`session_runner/trae_executor.py` 不允许导入平台业务数据库模型；平台侧只接收标准事件和结果。首期 Docker 镜像固定 Trae 源码版本、Python 依赖和 Runner 入口，第三方 Sidecar 通过容器网络或同 Pod 方式加入。

## 七、审批项

请审批以下单一决策：

> 同意首期使用 Session 容器内的 Trae Core Runner 注入 ToolGateway；原始 Trae CLI 仅保留普通运行验证用途，不作为审批、用户输入暂停和跨容器恢复的正式运行入口。

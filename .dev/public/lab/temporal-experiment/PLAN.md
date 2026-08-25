# AgentSupport 落地 Temporal 方案（v2）

> 状态：方案已获确认。本文件把实验证据与前期讨论整合为一份可执行方案。
> 实验代码见 `experiments/`（6/6 通过，证据在 `experiments/evidence/`）。
> 本目录已通过 `.gitignore` 的 `.dev/` 规则排除在版本控制之外。

## 0. 阶段进度

- **阶段 0（并行原型）：已完成**。新增 `execution_mode=temporal`，控制面通过
  `TemporalRunCoordinator` 启动 workflow / 发信号；`RunSessionWorkflow` +
  四个 Activity 驱动真实 `CoreRuntime`（session runner HTTP 适配器），事件与
  状态经 `repository.append_event` 落库，对外契约不变。
- **验收证据（阶段 0）**：
  - 全量测试 `200 passed / 7 skipped / 1 failed`，唯一失败
    `test_distributed_worker_uses_session_config` 为 HEAD 上已存在的问题，
    与本改动无关（stash 验证过）。
  - 新增 `tests/integration/temporal/test_temporal_execution.py` 4/4 通过
    （需本地 dev server，无 server 时自动 skip）：暂停/恢复 + 幂等审批、
    无关口直通完成、关口取消、运行中取消。
  - `ruff check` 通过。
- **阶段 1（灰度切流）：已完成（部署与验收就绪）**。
  - 新增 `docker-compose.yml` 的 `temporal`（dev server）与 `temporal-worker`
    服务；`deploy/kubernetes/temporal-worker.yaml`；`docker compose config`
    校验通过。
  - 新增 `docs/migration/v0.2-to-v0.3.md`（共存约束/切换/回滚）与
    `docs/adr/003-temporal-execution.md`；README 补充 temporal 配置。
  - 新增 `tests/integration/temporal/test_dual_run_parity.py`：旧（inline）与
    新（temporal）执行层驱动**同一个确定性 runner**，里程碑事件序列与终态
    一致（5/5 temporal 测试通过）。
  - 全量测试 `201 passed / 7 skipped / 1 failed`（唯一失败为 HEAD 既有问题）。
  - 待办：在真实环境灰度观察后按迁移文档推进切流。
- **阶段 1 验收补充（已提交）**：
  - `test_activity_crash_retries_without_rerunning_completed_work`：在途活动
    失败只重试一次、已完成段不重跑（段级恢复门禁）。
  - `test_temporal_mode_http_api_contract`：HTTP 契约（workspace/session/
    conversation/approval/events）在 temporal 模式下零变化。
  - Temporal 集成测试 7/7 通过；全量 `203 passed / 7 skipped / 1 failed`。
- **阶段 2（退役）：已完成并经用户确认（git 可回退）**。
  - 删除 `execution_jobs` / `run_commands` / `runner_endpoints` /
    `runtime_slots` 表与 22 个 repository 方法（alembic
    `20260817_0001_drop_distributed_execution_tables`）。
  - **保留 `conversation_checkpoints`**（inline 暂停 + Temporal 段 checkpoint
    共用；对字面计划的偏离已记录在迁移文档 §4）。
  - `worker` 入口改为 Temporal worker；compose 移除 `worker` 服务、K8s 移除
    `agentsupport-worker` Deployment 与基于 `execution_jobs` 的 KEDA；
    API/ConfigMap 切换 `execution_mode=temporal`。
  - `reconciler` 收敛为 Runner 自注册心跳过期清理；`execution_mode=distributed`
    不再支持。
  - 验收：全量测试 **189 passed / 4 skipped**（原 HEAD 唯一失败随其测试文件
    一并退役）；temporal 集成测试 7/7；`docker compose config` 通过。

## 1. 结论

**用 Temporal 替换 AgentSupport 自研的"编排 + checkpoint"层，保留对外契约与
Agent 会话重建协议。** 核心判断：

1. **编排层**：`ExecutionJob` / `RunCommand` / `RunnerEndpoint` / checkpoint 表
   与 worker/reconciler 轮询逻辑，全部由 Temporal 的 Workflow + Activity +
   Signal/Query 替代。Temporal 是这些能力的超集，且恢复粒度更细。
2. **会话层**：Agent（Trae）的"记忆"Temporal 管不到，必须靠"数据重建协议"。
   现状 `trae.py:423 resume` 已经证明这条路可行；落地时把它从"关口快照"升级为
   "段级快照"，并抽象成与 CLI 无关的适配器接口。
3. **对外契约**：事件表 + outbox + SSE、Workspace 生命周期、API 幂等/乐观并发
   全部保留，Temporal 不碰。

## 2. 实验证据（已运行验证）

`experiments/run_all.py` → `6/6 experiments passed`（Windows 本地
`temporal.exe` dev server，`temporalio 1.31.0`）。

| 实验 | 验证的能力 | 关键证据 |
| --- | --- | --- |
| exp01 | 暂停=workflow 状态、恢复=信号、幂等命令 | 等待期 `pending_activities` 为空（无容器存活）；重复信号只生效一次 |
| exp02 | 取消经心跳投递、workflow 终止 CANCELLED | 活动被取消后 `long_run_done` 计数为 0 |
| exp03 | 幂等键去重 + 提前投递可靠缓冲 | 同键双信号，`apply_input` 只执行 1 次 |
| exp04 | Worker 崩溃只重试在途活动 | `step_a=1`（不重跑）、`step_b=2`（被杀死后重试）、`step_b_done=1` |
| exp05 | checkpoint 结合：重建包存 workflow 状态 | 重试的活动收到**字节一致**的重建包（两次哈希相同） |
| exp06 | 心跳超时替代租约/过期巡检 | 挂死的活动 3.3s 内被判定失败 |

## 3. 两层恢复模型（方案的理论基础）

中断恢复必须拆成两层，混为一谈会导致误判：

| 层 | 回答的问题 | 由谁提供 | 是否 CLI 相关 |
| --- | --- | --- | --- |
| **编排层** | "这个 run 进行到哪、在等什么、下一步该干什么、谁负责推进" | Temporal（Workflow 事件历史 + 重放） | 否，完全无关 |
| **会话层** | "Agent 此刻的记忆怎么重建" | Agent 运行时的恢复接口（Trae 重建 / CLI `--resume`） | 是，每个 CLI 一个适配器 |

**编排层原理**：Workflow 代码是"run 的进行位置"的编码。事件历史持久化在
Server；崩溃后引擎把 Workflow 代码从头重放，已完成 Activity 从历史取结果、
不重执行，只有失败的 Activity 重试。

**会话层原理**：Agent 会话是进程内存，任何方案都无法让死进程复活。恢复 =
把"任务 + 消息历史 + next_step + 待处理工具批"重建给一个新进程。
`trae.py:423 resume` 已经是这个模式（`new_task` → 工具结果消息 →
从 `next_step` 重放 `_run_llm_step`）。

## 4. 目标架构

```
控制面 API (FastAPI，改为 Temporal Client)
   │ start_workflow(run_id) / signal(SubmitInput|SubmitApproval|CancelRun) / query(GetRunStatus)
   ▼
RunSessionWorkflow  ← run 本身（workflow_id = run_id）
   ├─ Activity: StartRunner        （runtime_driver.start + register_runner）
   ├─ Activity: ExecuteTraeSegment （调 Runner POST /runs，返回终态或"关口段快照"）
   ├─ Activity: ResumeTraeSegment  （调 Runner POST /runs/{id}/resume）
   ├─ Activity: StopRunner         （runtime_driver.stop）
   └─ 等待：wait_condition（等价 PAUSED，零资源占用）
   ▼
ConversationEventStore（Postgres，保留）→ outbox → Redis → SSE（对外契约不变）
```

## 5. run 级恢复：与现状的原理差异

| | 现状 checkpoint | Temporal |
| --- | --- | --- |
| run 的"当前位置" | 数据库行（`execution_jobs.state` + `run_commands` + checkpoint 表） | 事件历史（不可变、有序、即事实） |
| 暂停 | 显式序列化 checkpoint + 改状态 + 停容器 | `wait_condition` 挂起，代码停在那一行 |
| 恢复 | 轮询 claim → 读表 → 分支判断 → 调 resume | 引擎重放代码，Signal 事件让 `wait_condition` 通过 |
| 恢复驱动 | worker/reconciler 轮询（lease/token/heartbeat 手写） | 引擎重放（Server 保证单 worker 执行） |
| 崩溃粒度 | 只能从"显式保存过的关口"恢复；运行中崩溃=整跑重来 | 已完成 Activity 不重做，只重试在途段 |

**代码形态差异**：现状的状态机是隐式的（5 张表 + worker 的 if/else +
reconciler 轮询）；Temporal 的状态机是显式的可重放函数：

```python
@workflow.defn
class RunSessionWorkflow:
    @workflow.run
    async def run(self, run_request):
        await workflow.execute_activity(StartRunner, ...)
        state = await workflow.execute_activity(ExecuteTraeSegment, ...)
        while state["status"] == "waiting":            # 关口循环
            await workflow.wait_condition(lambda: self._decision is not None)
            state = await workflow.execute_activity(
                ResumeTraeSegment, args=[state["checkpoint"], self._decision], ...)
        await workflow.execute_activity(StopRunner, ...)
        return state
```

## 6. Trae 适配细节（落地必须补的三件事）

1. **段级快照周期性产出**：现在 `/runs/{id}/checkpoint`（`app.py:387`）只在
   主动调用时产出。改为在每次工具调用后产出"段快照"（`next_step` + 消息计数 +
   上下文引用），并塞进 Activity 的 `activity.heartbeat(details)`；重试时从
   `heartbeat_details` 取最近快照重建，损失上限 = 一个段。
2. **中间消息序列化**：`resume` 目前只用 `initial_messages` + 工具结果重建。
   要支持非关口处恢复，需把 `_run_llm_step` 产生的中间 `messages` 序列化进段快照
   （推广 `trae.py` resume 里 `messages.extend(LLMMessage(...))` 的模式）。
3. **`/runs/{id}/run` 幂等重入**：同一 `run_id` 重复调用应返回当前状态而非再起
   agent 循环（已有 `fence_epoch`/`correlation_id`，补"运行中 → 409/当前快照"）。

段粒度是可配置取舍：关口级 = 与现状损失相同但编排全自动化；每 N 步 = 损失上限
更小但快照成本更高。

## 7. 换 Agent CLI 的通用性设计

编排层与 CLI 无关，天然通用。会话层按 CLI 能力分档：

| CLI | 会话恢复方式 | 适配器 |
| --- | --- | --- |
| Trae（Python 库） | 数据重建（现状 resume） | 段快照 + resume |
| Claude Code / Codex 等独立 CLI | 原生 `--resume <session>`，会话文件落盘 | 重启进程 + resume 参数（约 20 行） |
| 无任何恢复接口的 CLI | 只能重跑段/任务 | 编排层仍受益（worker 死不丢 run、命令/取消可靠） |

**统一接口**：`RunnerAdapter.start(segment) / execute(segment) / resume(segment,
decision) / stop()`。Temporal 只调接口，不感知具体 CLI。这是比现状（深耦合
`trae_agent` 内部）更通用的形态。

## 8. 落地路线（可回滚）

1. **阶段 0 · 并行原型**：新增 `execution_mode=temporal`，与 `distributed` 并存；
   用确定性 Runner（`SESSION_RUNNER_MODE=deterministic`）+ 本地 dev server 打通
   全链路；契约测试双跑；`run_all.py` 作为回归基线。
2. **阶段 1 · 灰度切流**：新 run 走 Temporal，存量 run 留在旧 Worker；文档记录
   共存期约束；监控对比两类 run 的事件序列与完成率。
3. **阶段 2 · 退役**：稳定后删除 `ExecutionJob` / `RunCommand` / `RunnerEndpoint` /
   `ConversationCheckpoint` 表与对应 repository 方法；reconciler 收敛为孤儿容器
   清理；Worker 变为纯 Temporal Worker。

## 9. 变更点清单（代码级）

- `pyproject.toml`：新增 `temporalio` 依赖。
- 新增 `src/agentsupport/execution/temporal/`：workflows / activities / client。
- `src/agentsupport/application/service.py`：dispatch/pause/cancel/resume 改调
  Temporal Client；删除 `create_checkpoint` 相关逻辑（`service.py:1548` 等）。
- `src/agentsupport/processes/worker.py`：`_execute_claim_inner`（`worker.py:211`）
  拆为四个 Activity；Worker 变为 Temporal Worker。
- `src/agentsupport/processes/reconciler.py`：收敛。
- `src/agent_runner_contracts/checkpoint.py`：`ContextBundle`/`Checkpoint` 降级为
  "段快照数据契约"。
- `src/session_runner/`：段快照产出、中间消息序列化、`/run` 幂等重入。
- `alembic/`：灰度期兼容列 + 退役期 drop 表。
- 部署：compose/K8s 增加 Temporal（自托管或 Cloud）；可观测性面板补 Temporal。
- 测试：分布式协调/worker 测试改写为 temporalite 驱动；契约测试不动。
- 文档：ADR-003、迁移指南、README。

## 10. 风险与决策点

- **运维成本**：自托管 Temporal 需维护其 Server + DB；可先用 Temporal Cloud 评估。
- **确定性纪律**：workflow 代码禁止网络/随机/时钟 IO，全部下沉 Activity
  （实验已示范沙箱约束）。
- **历史上限**：超长 run 需 `ContinueAsNew` 或定期 compact。
- **Signal 至少一次**：幂等去重在 workflow 内显式实现（exp01/exp03 已示范）。
- **段粒度取舍**：决定"容器死损失上限"，影响 Runner 快照成本。
- **Trae 重建保真度**：重放会重新采样，结果与崩溃前不一定逐字一致；等价性度量
  是"任务最终完成"。

## 11. 验证门禁

- `experiments/run_all.py` 全绿是接入的前提。
- 对外契约（API、SSE、事件序列）零变化。
- 灰度期旧/新执行层对同一确定性任务给出相同事件序列。
- 阶段 1 结束后，崩溃恢复场景（杀 Worker、杀容器）在验收中可重复演示。

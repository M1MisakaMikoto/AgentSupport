# ADR-003：以 Temporal 作为执行编排层

- 状态：已接受
- 日期：2026-08-17
- 关联：ADR-001（src 布局与运行边界）、ADR-002（平台边界与 API v2）

## 背景

AgentSupport v0.2 自研了完整的"执行编排 + 检查点"层：`ExecutionJob` /
`RunCommand` / `RunnerEndpoint` / `ConversationCheckpoint` 四张表，配合
`worker` 轮询、`reconciler` 巡检、lease/fence/heartbeat 手写协调。该层存在
结构性限制：

- 检查点只在"人机关口"主动暂停时产生；运行中崩溃 = 整个 run 从头重跑。
- 编排逻辑隐式分布在数据库行 + 轮询分支中，状态一致性与可恢复性依赖自研
  协调原语。
- 会话恢复（`trae.py` resume）深耦合 Trae 内部结构，换 agent CLI 需重写。

## 决策

1. **编排层改用 Temporal**：每个 run 一个 `RunSessionWorkflow`
   （workflow_id = run_id），事件历史承担"进度即事实"；`wait_condition` +
   Signal 替代暂停/命令队列；Activity heartbeat/retry 替代 lease/heartbeat
   reconciler。崩溃恢复粒度从"整个 run"细化到"在途 Activity"。
2. **会话层保留"数据重建协议"**：Temporal 不保存 Agent 进程内存；
   `ContextBundle` / 段快照作为 Activity 输入/输出在 workflow 状态中流转，
   容器崩溃后由重建协议续跑。
3. **对外契约不变**：conversation 事件表 + outbox + SSE、Workspace 生命周期、
   API 幂等/乐观并发继续由控制面提供。
4. **按阶段落地**：阶段 0 并行原型（已完成）→ 阶段 1 灰度切流 → 阶段 2 退役
   自研编排表。见 `docs/migration/v0.2-to-v0.3.md`。

## 取舍

- **替代**：四张编排表、worker/reconciler 轮询、lease/fence 自研原语。
- **保留**：事件存储/SSE、会话重建协议、Workspace/Skill/MCP、API 契约。
- **代价**：新增 Temporal 依赖（自托管需维护其 Server + DB，或使用
  Temporal Cloud）；workflow 代码必须确定性（非确定性动作全部下沉 Activity）；
  超长 run 需 `ContinueAsNew` 或 compact。

## 验证

- `experiments/` 6 个实验证明 Temporal 覆盖 checkpoint 全部功能（含崩溃恢复）。
- `tests/integration/temporal/` 验证暂停/恢复/幂等/取消/双执行层事件一致性。
- 全量测试无回归；对外契约套件保持全绿。

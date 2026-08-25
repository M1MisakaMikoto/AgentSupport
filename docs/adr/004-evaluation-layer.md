# ADR-004：以 canonical 数据为基础的跨 Runner 评估层

- 状态：已接受
- 日期：2026-08-19
- 关联：ADR-001（src 布局与运行边界）、ADR-002（平台边界与 API v2）、ADR-003（Temporal 执行层）
- 支撑文档：[Verifier 契约](../evaluation/verifier-contract.md)、[canonical 事件词汇表](../evaluation/event-vocabulary.md)

## 背景

AgentSupport 的测试体系覆盖"平台自身 HTTP 契约正确性"，但无法回答"agent 任务完成得怎么样"。
评估需要可复现（干净的 Workspace 基线）、可回归（模型 / 提示词 / skill 变更对比）、并且能
持续使用。

Runner 是可替换的执行面（确定性 Runner、Trae、未来其他 agent CLI）。runner 特有数据
（trajectory 文件、内部步数、工具集细节）会让评估在换 runner 时静默失效或不可比。
"换 runner 后评估仍然有效"是评估层设计的第一约束，而非事后补偿。

## 决策

1. **评估只依赖三类 canonical 数据**：
   - 跑后 Workspace 状态（文件系统终态）；
   - 平台事件流（`EventEnvelope` 词汇表，见
     [事件词汇表](../evaluation/event-vocabulary.md)）；
   - 归一化 usage + 最终结果（`result.usage` 契约）。
   runner 特有数据一律不进评估主链路；新 runner 通过适配器"翻译"成同一套 canonical 数据。
2. **新增评估资源模型与 API**：`EvalDataset` / `EvalCase` / `EvalRun` /
   `EvalCaseResult`；编排复用现有执行链路（Workspace 快照克隆 + Session/Conversation +
   Temporal），不造第二条执行链路。
3. **Verifier 目录化**：每项 verifier 声明 `scope`（cross-runner / runner-specific）与
   `status`（active / deprecated）。active 才能进 CI 门禁、聚合报告与对比；deprecated
   仅在同一 runner 指纹下作为调试诊断，不进主报告。
4. **对比限同指纹**：每个 `EvalRun` 记录 runner 指纹（runner 模式 + 版本 + 工具集哈希 +
   模型 + prompt 哈希）。默认只允许同指纹对比；跨指纹仅限结果正确性维度，并显式标注
   不可比范围。
5. **接入门禁**：新 runner 必须通过"评估就绪"契约检查（canonical 事件、usage 归一化、
   工具调用必须经 `ToolGateway`），否则评估判 `ERROR` 拒绝出分，杜绝"换了 runner 悄悄
   跑出不可比分数"的中间态。
6. **安全合规构造性成立**：所有 runner 适配器必须经 `ToolGatewayExecutor` 授权/审批，
   安全合规维度因此与具体 runner 无关。

## 取舍

- **放弃（弃用）**：轨迹逐步分析、步数 / 收敛步数评分、工具名级调用统计评分、跨模型
  cache token 细节对比等 runner 特有评估——换取换 runner 后启用项全部保值。
- **保留（启用）**：结果正确性（测试套件 / 产物 diff / 终态与内容规则）、过程质量
  （canonical 事件上的工具失败率 / 重复调用 / 收敛性 / 错误事件数）、成本（归一化
  usage + 价格表）、安全合规（ToolGateway 强制 + 沙箱边界）、稳定性（同指纹重复运行）、
  LLM-as-judge（只喂 canonical 证据包）。
- **代价**：深度调试信息（trajectory 逐步回放、工具名级统计）不在评估主报告；需要时由
  可观测性层（OTLP / Phoenix 可选）查看。

## 落地阶段

- **Phase 1**：数据集 / 用例 CRUD + `EvalRun` 编排（确定性 Runner）+ 测试套件 / 规则
  Verifier + 报告与对比视图。
- **Phase 2**：LLM-as-judge（canonical 证据包）、对比报告进入 CI 门禁（阈值判定）。
- **Phase 3**：事件词汇表校验强制化、多 runner 指纹管理、可选 OTLP / Phoenix 对接。

## 验证

- Verifier 目录状态矩阵：启用项全部基于 canonical 数据；弃用项不进入门禁、聚合报告与对比。
- 同指纹与跨指纹对比规则有测试覆盖（跨指纹只比结果正确性，报告标注不可比维度）。
- 确定性 Runner 上全量评估链路跑通；接入第二个真实 Runner 时，启用项无失效（作为新
  runner 接入验收门禁）。

## 表结构决策（2026-08-24 补充）

- `eval_run_cases.case_id` 建立索引：跨 run 按 case 查询历史表现是核心路径，不能全表扫。
- `eval_runs.status` 与 `eval_run_cases.verdict` 增加 CHECK 约束，防止非法状态/结论入库。
- 外键：延续平台全库无 FK 的既有约定，`dataset_id` / `run_id` / `case_id` /
  `workspace_id` 以 `String(36)` 引用 + 应用层校验保证一致性；不引入 FK，避免与既有
  schema 风格不一致。若未来需要数据库级引用完整性，再统一为全平台加 FK 的独立迁移。

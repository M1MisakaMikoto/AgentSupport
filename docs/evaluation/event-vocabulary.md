# Canonical 事件词汇表（评估层）

本文是 ADR-004 的支撑文档之一，定义评估层可以消费的平台事件词汇表。任何 runner
适配器都必须把执行过程翻译成这套事件；评估规则只认词汇表内的事件。

## 1. 通用字段

每个事件都是 `EventEnvelope`，通用字段：`event_id`、`run_id`、`seq`、`type`、`payload`、
`source`、`occurred_at`、`tenant_id` / `user_id` / `project_id`（标签透传）。

跨 runner 不变量：

- `seq` 单调递增且唯一（平台已保证）；
- 终态事件（`run.completed` / `run.failed` / `run.cancelled` / `run.lost`）每个 run
  恰好一次；
- 同一 `call_id` 必须先有 `tool.call` 再有 `tool.result`；
- `interaction.requested` 后，继续执行前必须有 `approval.decided` 或 `interaction.input`；
- `run.completed` 的 `result` 可选携带归一化 `usage`。

## 2. 词汇表

| 事件类型 | 评估用途 | payload 要点 | 状态 |
| --- | --- | --- | --- |
| `run.started` | 生命周期 | `conversation_id` | ✅ canonical |
| `run.running` | 生命周期 | `container_id` | ✅ canonical |
| `run.completed` | 结果 + 成本 | `result`（含可选 `usage`） | ✅ canonical |
| `run.failed` | 结果 + 过程 | `code`、`message` | ✅ canonical |
| `run.cancelled` | 结果 | — | ✅ canonical |
| `run.lost` | 结果（异常终态） | `container_id`、`health_failures` | ✅ canonical |
| `message` | 过程 | `content` | ✅ canonical |
| `tool.call` | 过程 + 安全 | `call_id`、`name`、`arguments` | ✅ canonical |
| `tool.result` | 过程 | `call_id`、`name`、`success`、`output`、`error` | ✅ canonical |
| `tool.authorization` | 安全 | `batch_hash`、`status`、`approval_id` | ✅ canonical |
| `interaction.requested` | 交互（描述性） | `kind`、`interaction_id` | ✅ canonical |
| `approval.decided` | 交互（描述性） | `decision` | ✅ canonical |
| `interaction.input` | 交互（描述性） | `value` | ✅ canonical |
| `checkpoint.created` / `checkpoint.restored` | 恢复路径（调试） | `checkpoint_id` | ✅ canonical |
| `trajectory.recorded` / `trajectory.missing` | 诊断 | `path`、`sha256`、`steps` | ⚠️ 仅诊断，不进评估主链路 |

`trajectory.*` 事件与 Trae 特有文件强相关：评估规则不得依赖其 payload，仅可在单指纹
调试时查看。

### 流式过程事件（非 canonical）

增量事件只服务于实时呈现，**不进评估主链路**，其 payload 变化不算契约破坏：

| 事件类型 | 用途 | payload 要点 |
| --- | --- | --- |
| `message.delta` | 逐块转发模型输出，供前端做打字效果 | `delta` |
| `message.reset` | 某次模型调用因读超时重试、输出重新开始——消费方应丢弃已累积的文本 | `reason`（`stream_retry`）、`attempt` |

重试由 `retry_utils.retry_with` 整段重跑，所以 `message.reset` 之前的 `message.delta`
属于被丢弃的那次尝试；canonical 的 `message` 才是权威结果。

## 3. 归一化 usage 契约

`run.completed.result.usage`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `input_tokens` | 是 | 输入 token |
| `output_tokens` | 是 | 输出 token |
| `reasoning_tokens` | 是 | 推理 token（无则为 0） |
| `cache_creation_input_tokens` | 否 | 缓存写入 token |
| `cache_read_input_tokens` | 否 | 缓存读取 token |

规则：

- 新 runner 适配器必须做同一归一化（复用 `_usage_payload` 语义）；
- 采集不到时该用例的 usage 标 `unavailable`，**不得填 0**（避免把缺失当省钱）；
- 成本估算只在 usage 可用且价格表覆盖该模型时输出；否则成本维度标"无数据"。

## 4. 适配器职责（接入门禁）

新 runner 要进入评估链路，必须通过"评估就绪"检查：

1. 通过既有 runner 契约套件（9 个私有路径 + 事件语义）；
2. 只输出本词汇表内的事件类型（未知类型 = 契约违规，评估判 `ERROR`）；
3. 工具调用必须经 `ToolGatewayExecutor` 授权/审批，并发出 `tool.call` /
   `tool.authorization` / `tool.result`；
4. usage 按第 3 节归一化。

## 5. 校验行为

评估规则引擎在消费事件前做 schema 校验：

- 未知事件类型 → `ERROR`（`EVENT_VOCABULARY_VIOLATION`），该用例不进通过率分母；
- 词汇表内事件 payload 字段缺失（如 `tool.call` 缺 `call_id`）→ `ERROR`；
- 终态事件重复/缺失 → `ERROR`。

校验失败与用例失败严格分离：前者是"适配器坏了"，后者是"agent 没做好"。

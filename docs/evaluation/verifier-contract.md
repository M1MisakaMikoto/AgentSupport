# Verifier 契约与评估目录

本文是 ADR-004 的支撑文档之一，定义评估层的 Verifier 接口、评估目录（启用/弃用）、
数据集/用例格式、运行配置与结果合成规则。事件契约见
[canonical 事件词汇表](event-vocabulary.md)。

## 1. 核心不变式

- 评估只读取三类 canonical 数据：跑后 Workspace 状态、平台事件流、归一化 usage + 最终结果。
- 每项 Verifier 声明 `scope` 与 `status`；`active + cross-runner` 才能进入门禁与对比。
- 同一 `EvalRun` 只比较同一 runner 指纹；跨指纹仅比结果正确性。

## 2. Verifier 接口

```python
from typing import Literal, Protocol


class Verdict(BaseModel):
    status: Literal["PASS", "FAIL", "ERROR", "UNCERTAIN"]
    score: float          # 0.0 - 1.0
    reason: str           # 人话结论，进报告
    evidence: list[str]   # 证据引用：事件 seq、命令输出、文件路径、diff 摘要


class VerificationInput(BaseModel):
    case: EvalCase
    run: EvalRun
    terminal_state: str
    final_result: dict | None          # run.completed 的 result（含归一化 usage）
    events: list[EventEnvelope]        # canonical 事件流
    baseline_snapshot: WorkspaceRef    # 用例起点（版本快照）
    workspace_snapshot: WorkspaceRef   # 跑后状态（版本快照）


class Verifier(Protocol):
    id: str
    scope: Literal["cross-runner", "runner-specific"]
    status: Literal["active", "deprecated"]

    async def verify(self, input: VerificationInput) -> Verdict: ...
```

判定语义：

| status | 含义 | 谁触发 |
| --- | --- | --- |
| `PASS` | 用例达成预期 | Verifier 判定 |
| `FAIL` | 用例未达成预期 | Verifier 判定 |
| `ERROR` | 评估基础设施问题，不算用例失败 | 执行异常、验收命令写错、canonical 数据不合法 |
| `UNCERTAIN` | 无法判定（如 judge 拒答、数据缺失） | Verifier 显式返回 |

`ERROR` 与 `FAIL` 必须严格区分：前者表示"评估坏了"，后者表示"agent 变差了"。

## 3. 评估目录（启用 / 弃用）

| Verifier | scope | status | 判定方式 | 配置字段 |
| --- | --- | --- | --- | --- |
| `test_command` | cross-runner | ✅ active | 跑后 workspace 执行验收命令，按退出码/输出判定 | `command`、`timeout_seconds`、`expect`（可选） |
| `artifact_diff` | cross-runner | ✅ active | 跑后产物与 golden 版本 diff | `golden_version`、`paths`、`allow_extra` |
| `output_rules` | cross-runner | ✅ active | 最终结果内容规则 | `contains` / `not_contains` / `schema` |
| `terminal_state` | cross-runner | ✅ active | 终态断言（默认启用） | `expect`（默认 `completed`） |
| `tool_failure_rate` | cross-runner | ✅ active | `tool.result.success=false` 占比 | `max_rate`、`min_calls` |
| `duplicate_calls` | cross-runner | ✅ active | 按 call_id 去重后仍重复的调用数 | `max_duplicates` |
| `convergence` | cross-runner | ✅ active | 无 `run.failed` 到达终态、错误事件数 | `max_error_events` |
| `interaction_count` | cross-runner | ✅ active* | 人工交互次数（描述性，不进总分） | `max_count`（仅报告参考） |
| `cost_usage` | cross-runner | ✅ active | 归一化 tokens + 时长 + 价格表估算 | `max_tokens`、`max_cost`、`price_table` |
| `safety_policy` | cross-runner | ✅ active | 未授权工具零调用、副作用工具均经审批 | `policy`（复用 tool_policy） |
| `sandbox_boundary` | cross-runner | ✅ active | 网络/写盘越界零发生（运行时层记录） | — |
| `stability` | cross-runner | ✅ active | 同指纹重复 N 次，通过率/方差 | `repeat`、`min_pass_rate` |
| `llm_judge` | cross-runner | ✅ active（Phase 2） | 只喂 canonical 证据包按 rubric 打分 | `rubric`、`judge_model`、`min_score` |
| `trajectory_step_analysis` | runner-specific | ❌ deprecated | 基于 Trae 特有 trajectory 逐步分析 | `step_breakdown` |
| `step_count` | runner-specific | ❌ deprecated | 步数/收敛步数评分 | `max_steps` |
| `tool_name_stats` | runner-specific | ❌ deprecated | 工具名级调用统计评分 | `per_tool_limits` |
| `cache_token_cross_model` | runner-specific | ❌ deprecated | 跨模型 cache token 细节对比 | — |

> `interaction_count` 标 *：`interaction.requested` 是平台事件，但触发频率受 runner 工具
> 批处理策略影响，因此只作为描述性指标展示，不参与总分与门禁。

### 弃用规则

- deprecated verifier 不进入：CI 门禁、聚合报告、同/跨指纹对比。
- 需要调试时显式传 `allow_deprecated: true`，且结果只标注"单指纹诊断"，不进主报告。
- 新 runner 接入时，若 canonical 数据使某个 deprecated verifier 变得可用，可提出升级为
  active（走 ADR 评审），不能默认放开。

## 4. 数据集与用例格式（YAML）

```yaml
version: 1
name: code-task-regression
description: 代码任务回归集
labels:
  tenant_id: t-1
  project_id: eval-code
workspace:
  id: 6f9c2d3a-4b5c-4d6e-8f70-9a1b2c3d4e5f
  baseline_version: 9741211be4aa4135925b2c98f2c53c50
cases:
  - id: case-001
    task: 实现 add(a, b) 并让 tests/test_add.py 通过
    tags: [python]
    verifiers:
      - type: test_command
        command: "pytest -q tests/test_add.py"
        timeout_seconds: 120
      - type: safety_policy
        policy: { allowed_tools: [bash, str_replace_based_edit_tool, task_done] }
      - type: cost_usage
        max_tokens: 20000
  - id: case-002
    task: 重构 util.py 保持测试全绿
    tags: [python, refactor]
    verifiers:
      - type: test_command
        command: "pytest -q"
        timeout_seconds: 180
      - type: artifact_diff
        golden_version: 9741211be4aa4135925b2c98f2c53c50
        paths: [util.py]
```

不指定 `verifiers` 时的默认值：`terminal_state` + `convergence` + `output_rules`（空规则）+
`safety_policy`（沿用会话 tool_policy）。

## 5. EvalRun 配置与 runner 指纹

```yaml
dataset_id: <uuid>
runner:
  mode: deterministic | trae
  version: 0.1.0
  tool_versions_hash: <tool_versions_hash>   # 复用 checkpoint 契约
model:
  name: claude-sonnet-4-20250514
  provider: anthropic
prompt_hash: <sha256>                        # prompt 文件或注入内容哈希
skills: [review, docs-writing]
tool_policy: { allowed_tools: [...], approval_required_tools: [...] }
concurrency:
  max_concurrent_cases: 4
  repeat: 1
idempotency_key: <key>
```

**runner 指纹** = `mode + version + tool_versions_hash + model + prompt_hash + skills +
tool_policy` 的规范化哈希。对比规则：

- 同指纹：全部维度可比，出对比报告（回归/改进/指标 delta）。
- 跨指纹：仅 `test_command` / `artifact_diff` / `terminal_state` / `output_rules` 可比；
  报告顶部标注"runner/模型不同，过程、成本、交互维度不可比"。

## 6. 执行生命周期与结果合成

1. `EvalRun` 启动 → 为每个用例克隆基线版本为独立 Workspace（单写者租约互斥、可并行）；
2. 创建 Session/Conversation 走现有执行链路（确定性或 Trae），事件与 usage 正常落库；
3. 终态后对 workspace 打跑后快照；
4. 依次执行启用的 Verifier，任一返回 `FAIL`/`ERROR` 即短路（可通过 `all_verifiers` 覆盖）；
5. 合成 verdict：全部 `PASS` → `PASS`；存在 `FAIL` → `FAIL`；存在 `ERROR` 无 `FAIL` →
   `ERROR`；其余 → `UNCERTAIN`；总分 = 各维度加权分（数据集可配权重）；
6. 生成报告（运行总览 → 用例详情 → 轨迹时间线）与 JSON/JSONL 导出。

`ERROR` 用例进入报告时归入"评估基础设施问题"分区，不计入通过率分母（避免污染结论），
但会触发告警，防止"评估坏了还在假装跑评估"。

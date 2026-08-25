# 评估层落地试验方案（Phase 1）

目标：在不接真实模型、不写 DB 表的前提下，验证评估层最小闭环能否成立，并暴露
设计缺陷，为正式落地（ADR-004）提供依据。

## 1. 试验验证什么

- 数据集 → 用例 → 评估运行 → Verifier → 报告 → 对比的完整链路可用；
- Workspace 版本快照克隆（`create_from_version`）能为每个用例提供独立、可复现的基线；
- 只改变基线内容（不改用例、不改代码）时，verdict 从 PASS 翻转为 FAIL，compare 能检出
  恰好一个 regression；
- 未知 Verifier / 执行异常被判定为 `ERROR`，且不计入通过率分母。

## 2. 试验边界（不验证什么）

- 不接真实模型：case runner 是进程内确定性 Session Runner，只证明框架机制；
- 不落 DB：数据集/运行存内存，正式落地再迁移 PostgreSQL；
- 不评估 agent 行为质量：确定性 runner 不会改 workspace，`test_command` 验证的是
  基线内容的通过/失败，而不是 agent 产出。

## 3. 代码位置（当前均未提交）

| 内容 | 位置 |
| --- | --- |
| Workspace 快照克隆 | `src/agentsupport/adapters/workspace/providers.py`（`create_from_version`） |
| 评估领域模型 | `src/agentsupport/evaluation/domain.py` |
| Verifier 实现 | `src/agentsupport/evaluation/verifiers.py` |
| 评估编排 | `src/agentsupport/evaluation/service.py` |
| 试验装配 | `tests/e2e/evaluation/helpers.py`（`build_test_eval_service`，确定性 runner 仅测试使用） |
| 生产装配 | `src/agentsupport/bootstrap/container.py`（`build_eval_service`，走配置的执行后端） |
| 试验 API | `src/agentsupport/serving/http/routes/eval.py`（`/eval/*`） |
| 试验测试 | `tests/e2e/evaluation/test_eval_trial.py` |

## 4. 运行方式

```powershell
.venv\Scripts\python.exe -m pytest tests/e2e/evaluation/test_eval_trial.py -v -p no:cacheprovider
```

手工 HTTP 演练（启动 API 后）：

```powershell
.venv\Scripts\python.exe -m uvicorn agentsupport.main:app --port 8000
```

```text
POST /eval/datasets         {name, workspace_id, baseline_version}
POST /eval/datasets/{id}/cases   {task, verifiers:[{type:"test_command",params:{command}}]}
POST /eval/runs             {dataset_id}
GET  /eval/runs/{id}/report
GET  /eval/runs/{id}/compare?baseline={baseline_run_id}
```

## 5. 验收标准

- `test_good_baseline_passes_and_broken_baseline_regresses`：good 基线 PASS、
  broken 基线 FAIL、compare 报 1 个 regression；
- `test_eval_http_contract`：数据集幂等重放、用例创建、运行、报告、404 路径全通过；
- `test_unknown_verifier_is_error_not_fail`：未知 verifier → `ERROR`，不计入通过率；
- 全量测试无回归、ruff 无告警。

## 6. 试验结论后的下一步

- 通过：按 ADR-004 正式落地——PostgreSQL 表（`eval_datasets` / `eval_dataset_cases` /
  `eval_runs` / `eval_run_cases`）、`/eval` 进 OpenAPI 覆盖门禁与 API 参考、Temporal
  编排、接入真实 runner 的"评估就绪"契约门禁；
- 不通过：按暴露的问题修订设计（如 verifier 合成规则、基线隔离方式）后再试。

# Evaluation 层第 1 节处理项：验证链路记录

日期：2026-08-24
范围：评估层第 1 节全部 10 项（并行限流、连接池复用、N+1、upsert、幂等持久化、
compare 按 case_id、表结构补强、分页、后台执行、资源回收）。
基线：改动前 `pytest tests/unit tests/contract tests/e2e/evaluation tests/e2e/devtools` = 166 passed。

## 验证命令（可复跑）

```powershell
# 静态检查
.venv\Scripts\python.exe -m ruff check src tests alembic devtools

# 评估层专项
.venv\Scripts\python.exe -m pytest tests\unit\evaluation tests\e2e\evaluation -q -p no:cacheprovider

# 全量本地回归
.venv\Scripts\python.exe -m pytest tests\unit tests\contract tests\e2e\evaluation tests\e2e\devtools -q -p no:cacheprovider

# 迁移 SQL 生成校验
.venv\Scripts\python.exe -m alembic upgrade head --sql

# 集成测试收集校验（运行需 Temporal/Postgres，本机未启动）
.venv\Scripts\python.exe -m pytest tests\integration\temporal tests\integration\persistence --collect-only -q
```

## 逐项改动与验证证据

### 1. case 串行 → 并行（限流）
- 改动：`EvalService._execute_run` 用 `asyncio.Semaphore(case_concurrency)` +
  `asyncio.gather` 并发执行 case，`case_concurrency` 构造参数默认 4；结果按 case
  顺序重排（gather 保序）；单 case 失败仍走 per-case ERROR（P0-1 语义不变）。
- 验证：`test_cases_run_with_bounded_concurrency`（4 个 case、并发上限 2），断言
  实际并行（max_active >= 2）且被限流（max_active <= 2），run completed、结果数 4。

### 2. 三套数据库连接池 → 复用
- 改动：`SqlAlchemyEvalStore` 接受外部 `engine`；`build_eval_service(config,
  agentsupport=...)` 复用主 service 及其 repository engine；`create_app` 传入
  `selected_service`；测试 helper 同步共享 engine。
- 验证：`test_build_eval_service_reuses_agentsupport_engine` 断言
  `eval_service.agentsupport is service` 且 `eval_service.store.engine is
  service.repository.engine`（postgres 模式）。

### 3. 列表接口 N+1 → 批量加载
- 改动：`SqlAlchemyEvalStore.list_datasets/list_runs` 先查父行，再以
  `IN (...)` 批量取回子行后分组，查询次数与数据量无关。
- 验证：`test_list_queries_are_batched_not_n_plus_one` 用 SQLAlchemy event 统计，
  5 个 dataset 只发 2 条 SELECT（datasets + cases 各 1 条），且每个 dataset 的
  cases 完整返回。

### 4. save_run 全量删插 → 按 case_id upsert
- 改动：`save_run` 按 `(run_id, case_id)` 更新已有行、插入新增行、删除消失行，
  行 ID 保持稳定。
- 验证：`test_save_run_upserts_stable_rows_and_prunes_removed` 断言同一 case 两次
  保存后行 ID 不变、verdict 原地更新、新增 case 插入、移除 case 删除。

### 5. 幂等键进程内存 → 持久化
- 改动：`EvalService._idempotent` 在 repository 可用时走
  `find_idempotent/remember_idempotent`（平台 IdempotencyKeyRow 表），内存 dict
  仅作 memory 模式 fallback；`persist` 先于记录写入，保证重放可解析资源。
- 验证：`test_dataset_idempotency_survives_restart`（同 key 重放返回同一 dataset，
  不同 payload 返回 409）、`test_run_idempotency_survives_restart`（新 service 重放
  返回同一 run）。

### 6. compare 按 task → 按 case_id
- 改动：`compare` 优先按 `case_id` 匹配，task 文本仅作老数据 fallback。
- 验证：`test_compare_matches_by_case_id_not_task_text`（task 文本改变但 case_id
  不变仍检出 regression，delta 带 baseline_case_id）。

### 7. 表结构补强
- 改动：`eval_run_cases.case_id` 加索引；`eval_runs.status`、
  `eval_run_cases.verdict` 加 CHECK 约束（模型 + 初始迁移同步）。外键按项目全局
  无 FK 的约定不新增，ADR-004 记录该决策。
- 验证：`test_run_cases_index_and_check_constraints`（索引存在、非法 verdict/status
  插入抛 IntegrityError）；`alembic upgrade head --sql` 输出含
  `CREATE INDEX ix_eval_run_cases_case_id` 与两个 CHECK 约束。

### 8. 列表分页
- 改动：`EvalStore` 协议、`InMemoryEvalStore`、`SqlAlchemyEvalStore`、
  `EvalService` 及 `/eval/datasets`、`/eval/runs` 路由均支持 `limit`（1-500，
  默认 100）与 `offset`。
- 验证：`test_list_pagination_sql_and_memory`（SQL 与内存两种 store 的
  limit/offset 切片正确）。

### 9. POST /eval/runs 长请求 → 后台执行
- 改动：`EvalService.start_run` 建 run 后 `asyncio.create_task` 后台执行并立即
  返回；路由改为 202；同步语义保留在 `run_dataset`（测试与内部调用继续用）。
- 验证：`test_eval_http_contract` 断言 POST 返回 202 + status running，轮询
  GET 直到 completed，summary/report 正确；原有直接 await 的
  `run_dataset` 测试全部保持通过。

### 10. 资源回收（workspace 克隆）
- 改动：`LocalWorkspaceStorageDriver.delete_workspace` 删除工作区目录；
  `_run_case` 用 try/finally 在 case 结束后清理克隆（verifier 跑完后才删，
  不影响 test_command）；无 delete 能力的 driver 自动跳过。
- 验证：`test_case_workspace_clone_removed_on_failure`（执行失败后根目录只剩
  基线 workspace 与 .versions）、`test_cleanup_workspace_removes_clone`。

## 证据汇总（2026-08-24 实测输出）

```text
ruff check src tests alembic devtools
  -> All checks passed!

pytest tests\unit\evaluation tests\e2e\evaluation -q
  -> 19 passed

pytest tests\unit tests\contract tests\e2e\evaluation tests\e2e\devtools -q
  -> 177 passed

pytest tests\integration\temporal tests\integration\persistence --collect-only -q
  -> 17 tests collected

alembic upgrade head --sql | Select-String "ix_eval_run_cases"
  -> CREATE INDEX ix_eval_run_cases_case_id ON eval_run_cases (case_id);
     CREATE INDEX ix_eval_run_cases_run_id ON eval_run_cases (run_id);
```

## 已知边界（本记录未覆盖）

- start_run 进程重启丢失：已由构造期 stale-run 恢复兜底（见"重启恢复"），
  配合幂等重放可用新 key 重跑；单实例假设已记录。
- temporal 硬终止（execution timeout）路径原判为"无法在快测试里模拟"，2026-08-25
  已用真实 Temporal 覆盖（见下方"硬终止覆盖"），边界关闭。

## 第 2/3 节落地（2026-08-24 追加）

### 2a workflow 硬失败不落 terminal
- 改动：`_wait_for_temporal_case` 捕获 `wait_for_run` 返回值；重载 conversation
  后校验 `run.state in TERMINAL_STATES`，非 terminal 抛
  `EVAL_WORKFLOW_FAILED`（502），由 per-case catch 转为 ERROR 结果，run 正常
  完成，不再基于半成品状态判 verdict。平台 conversation 状态不改，仅结果侧处理。
- 验证：`test_non_terminal_workflow_end_is_case_error_not_stuck`（fake 返回
  failed + conversation 停 RUNNING → case ERROR、reason 含 workflow 信息、
  run completed）、`test_terminal_failed_conversation_is_evaluated_normally`
  （workflow 已持久化 FAILED → outcome 正常反映 FAILED）。

### 2b case 超时默认值
- 改动：Settings 新增 `eval_case_timeout_seconds`（默认 1800，env
  `AGENTSUPPORT_EVAL_CASE_TIMEOUT_SECONDS`）；`build_eval_service` 与测试
  helper 从配置传入。
- 验证：`test_eval_case_timeout_comes_from_settings`（构造 123 → eval service
  拿到 123）。

### 2c human gate 边界
- v1：维持"超时 → cancel workflow → case ERROR"语义（2a/2b 已覆盖）；需要
  人工输入的 case 视为不可自动评估。
- v2 自动代答（`eval_auto_interaction` + `eval_auto_input`）不在本次范围，
  记录为后续独立小功能。

### 3a temporal 模式 eval 集成测试
- 新增 `tests/integration/evaluation/test_eval_temporal_integration.py`（3 个
  用例）：正常完成 → verdict PASS 且 outcome 来自完成态；core runtime 抛错 →
  workflow 持久化 run.failed → verdict FAIL、run 不卡 running；同 key 重放返回
  同一 run。复用 `test_temporal_execution.py` 的 fixture 模式（无 Temporal 时
  `pytest.skip`）。
- 实测（见下方"真实环境验证"）：3 passed in 11.63s（真实 Temporal dev server）。

### 3b 本机全栈可运行
- README 新增"本地全栈运行（Docker Compose）"章节；新增
  `devtools/start-stack.ps1`（检查 docker → compose up → 等待 /ready）与
  `devtools/stop-stack.ps1`。
- 实测：本机 docker 不可用，脚本未实跑；有 docker 的机器按 README 步骤执行
  冒烟（`Invoke-RestMethod http://127.0.0.1:8000/ready` 返回 ready）。

### 3c devtools console 的 postgres 验收流程
- 现状问题：原 postgres 步骤跑的 `test_repository.py` 全是 sqlite 测试，环境
  变量未被读取，未真正测 postgres。
- 改动：新增 `tests/integration/persistence/test_repository_postgres.py`
  （`RUN_POSTGRES_INTEGRATION_TESTS=1` 门控，覆盖 repository 往返、eval store
  upsert、case_id 索引与 CHECK 约束）；console `_accept` 的 postgres 步骤改为
  "先 `alembic upgrade head`，再跑门控测试"；devtools 测试断言同步更新
  （alembic 步骤先于 pytest、目标文件为 postgres 门控测试）。
- 实测：本机无 postgres → 3 个门控测试 `skipped`；有 compose postgres 时按
  测试文件头部命令运行。

## 证据汇总（追加后，2026-08-24 实测）

```text
ruff check src tests alembic devtools
  -> All checks passed!

pytest tests\unit tests\contract tests\e2e\evaluation tests\e2e\devtools -q
  -> 180 passed

pytest tests\e2e\devtools tests\integration\evaluation tests\integration\persistence -q
  -> 27 passed, 6 skipped   # 6 skipped = temporal eval 3 + postgres gated 3

pytest tests\integration\temporal tests\integration\persistence --collect-only -q
  -> 20 tests collected
```

## 真实环境验证（WSL2，2026-08-24 回填）

本机有 WSL2 Ubuntu + Docker 29.7.0；Docker Hub 直连被网络拦截（TLS 握手超时），
镜像加速器同样被拦截。实测方案：

- **Temporal dev server**：从 GitHub releases 下载 Temporal CLI 1.8.2
  （`temporal_cli_1.8.2_linux_amd64.tar.gz`），
  `temporal server start-dev --headless --port 7233` 常驻运行（Windows 侧
  `127.0.0.1:7233` 可达）。
- **PostgreSQL**：用本地已有的 `postgres:16` 镜像经 compose 启动
  （`agentsupport-postgres-1` healthy，`0.0.0.0:5432->5432`）。
- **API/网关**：用本地已有的 `agentsupport-api:latest` 与 `nginx:1.27-alpine`
  启动 `db-migrate` / `workspace-init` / `api` / `gateway`（`--no-deps` 跳过
  无法拉取的 temporal 镜像服务）。

### 实测结果

```text
# temporal 模式 eval 集成测试（真实 Temporal）
pytest tests\integration\evaluation -q
  -> 4 passed in 17.59s
     test_temporal_eval_completes_with_terminal_verdict
     test_temporal_eval_workflow_failure_is_failed_not_stuck
     test_temporal_eval_run_idempotent_replay
     test_temporal_eval_workflow_execution_timeout_is_error_not_stuck

# 迁移对真实 postgres
alembic upgrade head
  -> Running upgrade 20260814_0001 -> 20260817_0001, Drop the retired distributed execution tables.
     Running upgrade 20260817_0001 -> 20260819_0001, Add evaluation layer tables (ADR-004, phase 1).

# postgres 门控集成测试（RUN_POSTGRES_INTEGRATION_TESTS=1）
pytest tests\integration\persistence\test_repository_postgres.py -q
  -> 3 passed in 1.45s
     test_repository_round_trip
     test_eval_store_round_trip_and_upsert
     test_run_cases_index_and_check_constraints

# 全栈冒烟（容器内 /ready 与 Windows 侧经网关）
http://127.0.0.1:8000/ready
  -> {"status":"ready","execution_mode":"temporal","persistence_mode":"postgres","instance_id":"46703c945ab7:1"}
```

### 过程中发现并修复的问题

1. **`TemporalRunCoordinator.wait_for_run` 的 timeout 类型 bug**：`asyncio.wait_for`
   的 timeout 参数需要秒数，传入 `timedelta` 会抛
   `TypeError: '<=' not supported between instances of 'datetime.timedelta' and 'int'`，
   导致 eval 的 temporal 等待路径在真实环境下必挂（fake 单测没暴露）。已改为
   `timeout.total_seconds()`；这是集成测试 fail → pass 的关键修复。
2. **门控 postgres 测试自身两处修正**：`request_hash` 生成 128 字符超出
   `idempotency_keys.request_hash varchar(64)`（sqlite 不校验长度所以未暴露）；
   `append_event` 前需显式置 `conversation.run.state = RUNNING`（与既有 sqlite
   测试一致）。

### 硬终止覆盖（2026-08-25）

- 用 `workflow_timeout_seconds=5` 的协调器 + 永不返回的 `BlockingCoreRuntime`
  模拟 workflow 执行超时被强制终止：`_fail` activity 不会执行，conversation
  停在非 terminal 状态，eval 走 `EVAL_WORKFLOW_FAILED` → per-case ERROR，run
  completed 不卡 running。
- 验证：`test_temporal_eval_workflow_execution_timeout_is_error_not_stuck`
  通过（1 passed in 11.13s；全文件 4 passed in 17.59s）。

### 重启恢复（2026-08-25）

- `EvalService.__init__` 调用 `_recover_stale_runs()`：把 store 中遗留的
  `running` run 标记为 `failed` 并写入
  `summary.error = {"code": "EVAL_PROCESS_RESTARTED", ...}`，避免后台执行随
  进程退出后 run 永远卡在 running。
- 验证：`test_stale_running_run_recovered_as_failed_on_service_start`（running
  → failed + 原因码；completed 不受影响）。
- 限制：假设单实例拥有 store；多实例部署需先引入 run 心跳/所有权再启用恢复。

### human gate 自动代答（v2 落地，2026-08-25）

- 配置：`eval_auto_interaction`（默认 false）、`eval_auto_input`（默认
  `continue`）、`eval_auto_answer_limit`（默认 10），env 前缀
  `AGENTSUPPORT_`；`build_eval_service` 透传。
- 行为：`_wait_with_auto_input` 轮询 workflow `get_status` 与持久化
  conversation：`waiting` 时读取 `pending_interaction.interaction_id` 自动
  `submit_input`（同一 interaction 只代答一次，`eval-auto-{interaction_id}`
  幂等 key，上限 `auto_answer_limit` 防多轮死循环）；conversation 到
  terminal 或 workflow 查询失败时转 `wait_for_run` 收尾；整体仍受
  `case_timeout_seconds` 约束（超时 cancel → ERROR）。
- 单测（fake coordinator）：自动代答提交参数正确、同一 interaction 去重、
  达到上限后停止代答、auto 模式超时仍 cancel → ERROR。
- 集成（真实 Temporal + gated runner）：runner 请求 interaction → workflow
  停 `waiting` → auto 代答 → resume → COMPLETED → verdict PASS
  （`test_temporal_eval_auto_answers_human_gate`，7.58s）。
- 过程中修复潜伏 bug：workflow `submit_input` 信号参数 `value: object` 在
  temporalio 下反序列化失败（`Unserializable type: object`），改为 `Any`；
  现有测试只覆盖 approval 信号，input 信号首次被端到端触发即暴露。

### 遗留说明

- 冒烟用的 `agentsupport-api:latest` 镜像早于 evaluation 层构建，容器内 `/eval/*`
  路由不在该镜像中；重建镜像需能拉取基础依赖（当前网络对 Docker Hub 不通，
  pip 源可达性未验证）。
- Temporal dev server 与 compose 服务仍在运行；停止方式：
  `wsl -d Ubuntu -- pkill -f "temporal server"` 与
  `.\devtools\stop-stack.ps1`。

## 第 2/3 节补充落地（2026-08-25）

范围：2a 配置文档化、2c API 参考同步、2b README 评估用法、3b 覆盖率。
按用户指示：3a（CI）暂不做；3c（.pytest-tmp）已由用户清理，根因修复已生效
（basetemp 移出仓库）。

### 2a 配置项文档化
- `.env.example` 补 `AGENTSUPPORT_EVAL_CASE_TIMEOUT_SECONDS=1800`（含注释）；
- README.md / README.en.md 配置表各补一行；
- 新增 `tests/unit/agentsupport/test_settings_env.py`：env 读取（123）与默认值
  （1800）各一测。
- 验证：2 个新单测通过；全量 `182 passed`、ruff 全绿。

### 2c API 参考同步（修复文档-代码不一致）
- `docs/api/agentsupport-api.md`：17.5 改为 `202` + 后台执行 + 轮询终态（并补
  `404 EVAL_DATASET_NOT_FOUND` / `409 IDEMPOTENCY_CONFLICT`）；17.2 / 17.6 补
  `limit`（1-500，默认 100）/ `offset`（默认 0）。
- 验证：与 `serving/http/routes/eval.py` 逐条核对（202 status_code、Query 参数、
  `start_run` 后台语义）。

### 2b README 评估用法
- README.md 新增「评估（/eval）」小节：最小闭环请求序列、verifier 契约链接、
  超时配置说明、API 参考入口。
- 验证：文档链接目标存在。

### 3b 覆盖率
- `pyproject.toml`：dev extras 加 `pytest-cov>=5,<7`；新增
  `[tool.coverage.run] source=["agentsupport"], branch=true` 与
  `[tool.coverage.report] show_missing=true`（暂不设 fail_under，出报告为主）。
- 实测（含真实 Temporal 集成，2026-08-25）：

```text
pytest tests\unit\evaluation tests\e2e\evaluation tests\integration\evaluation
       --cov=agentsupport.evaluation --cov-report=term-missing
  -> 26 passed
     TOTAL 83%  (420 stmts)
     __init__ 100% | domain 98% | service 87% | store 93% | verifiers 61%
```

- 未覆盖集中在 verifiers 的 `test_command` 超时 / LLM-judge / convergence 分支与
  service 的幂等冲突等防御性分支；作为后续补测目标。

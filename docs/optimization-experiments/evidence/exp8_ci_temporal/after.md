# Experiment `exp8_ci_temporal` — phase `after`

- timestamp: `2026-08-25T05:40:00+00:00`
- git head: `307c773`
- command: `pytest tests/integration/temporal -q` (local, Temporal dev server on `127.0.0.1:7233`)

---

## 核实的问题

CI 的 integration job 只跑
`tests/integration/evaluation tests/integration/persistence/test_repository_postgres.py`，
没有包含 `tests/integration/temporal/*` —— 而 `docs/migration/v0.2-to-v0.3.md` 把
`tests/integration/temporal/` 定义为 Temporal 执行层的验收门槛。Temporal 是生产默认
执行模式，缺少自动回归网。

## 修复

`.github/workflows/ci.yml` 的 integration pytest 命令追加 `tests/integration/temporal`
（该 job 已安装 `temporalio/setup-temporal@v1`，提供 `localhost:7233`）。

```diff
 run: >-
   pytest tests/integration/evaluation
   tests/integration/persistence/test_repository_postgres.py
   tests/integration/temporal -q
```

## 本地验证（同一批测试在本机 Temporal 上全绿）

```text
pytest tests/integration/temporal -q
8 passed, 1 warning in 24.04s
```

覆盖：
- `test_temporal_execution.py`（service -> workflow -> activity -> repository 全链路）
- `test_dual_run_parity.py`（新旧执行面里程碑一致性）

## 附：验证过程中发现的既有迁移链缺陷（已修复）

为了让全新数据库能执行 `alembic upgrade head`（exp7 的迁移验证依赖它），发现并修复了
两个与本次优化无关的既有缺陷：

1. `20260728_0002` 在全新库上无条件读取 `execution_jobs`（已退役表，新库不存在）→
   `NoSuchTableError`；已加表存在性守卫。
2. `20260819_0001` 在全新库上无条件重建 eval 表/索引/检查约束，而 `20260728_0001` 的
   `Base.metadata.create_all` 已用当前 models 建出全部表 → `DuplicateTable`；已改为
   存在性守卫（与其它迁移一致）。

修复后全新库 `alembic upgrade head` 完整跑通 14 个迁移（证据见
`exp7_indexes/after.md` 的“Alembic migration verification”一节）。

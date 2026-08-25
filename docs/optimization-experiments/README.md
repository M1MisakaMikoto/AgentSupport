# AgentSupport 优化实验记录

本目录存放“先实验核实 → 修复 → 重跑验证”的完整证据链。每个实验都有一份
`before.md`（修复前基线）和 `after.md`（修复后结果），两个文件由同一脚本
生成，保证对比口径一致。

复现方式（PowerShell，仓库根目录）：

```powershell
$env:TEST_TMP=(New-Item -ItemType Directory -Force .pytest-tmp).FullName
$env:TEMP=$env:TEST_TMP; $env:TMP=$env:TEMP
python devtools/experiments/exp1_nplus1.py --phase before
python devtools/experiments/exp2_event_load.py --phase before
# ... 每个实验同理；--phase after 在修复后重跑
```

实验数据库：本机 PostgreSQL `agentsupport_exp`（脚本自动重建表结构），
迁移验证使用 `agentsupport_exp_mig`。

## 实验清单

| 实验 | 核实的问题 | 修复 | 证据 |
| --- | --- | --- | --- |
| exp1 | 列表/事件查询 N+1 与全表扫描 | repository 批量映射 + 会话级事件查询 | [exp1](./exp1_nplus1/) |
| exp2 | 事件缓存填充 O(E²) | 一次性批量补载 | [exp2](./exp2_event_load/) |
| exp3 | 事件发布 fire-and-forget 无追踪 | 追踪 + 异常日志 | [exp3](./exp3_publish_tasks/) |
| exp4 | 控制面超时后 Runner 孤儿执行 | 失败时主动 cancel Runner | [exp4](./exp4_runner_orphan/) |
| exp5 | checkpoint 全量事件重复存储 | 有界 recent_events 窗口 | [exp5](./exp5_checkpoint_growth/) |
| exp6 | outbox 指标硬编码 0 | repository 真实统计 | [exp6](./exp6_outbox_metrics/) |
| exp7 | 保留清理缺索引全表扫描 | 补索引 + Alembic 迁移 | [exp7](./exp7_indexes/) |
| exp8 | temporal 集成测试不在 CI | CI integration job 追加 temporal 目录 | [exp8](./exp8_ci_temporal/) |
| exp9 | SSE 流式路径同步 DB 阻塞事件循环 | 轮询读取移入 `asyncio.to_thread` | [exp9](./exp9_event_loop_blocking/) |
| exp10 | 请求体无长度上限导致 DB 膨胀 | task/metadata/交互值加大小上限（422） | [exp10](./exp10_request_size_limits/) |
| exp11 | K8s Runner Pod 无资源限制 | Pod 增加 requests/limits（默认 2CPU/2Gi） | [exp11](./exp11_k8s_pod_resources/) |
| exp12 | REST 路由同步 DB 阻塞事件循环 | 非 await handler 改 `def`（FastAPI 线程池） | [exp12](./exp12_rest_route_blocking/) |
| exp13 | SSE 空闲流无 keepalive | 空闲输出注释帧（可配间隔） | [exp13](./exp13_sse_keepalive/) |
| exp14 | 列表接口无分页（limit 被忽略） | 列表接口支持 `limit`/`offset`（可选、向后兼容） | [exp14](./exp14_list_pagination/) |
| exp15 | Redis pubsub 每次 wait 新建订阅且通知丢失 | 每通道复用订阅 + 排空确认帧 | [exp15](./exp15_redis_pubsub_churn/) |
| exp16 | Runner 选择忽略负载（load 字段未用） | `select_ready_runner` 按最低 load 选择 | [exp16](./exp16_runner_load_balancing/) |
| exp17 | Core-runtime 每次调用新建 httpx 客户端 | 复用单客户端 + 绝对 URL | [exp17](./exp17_httpx_client_reuse/) |
| exp18 | 内存事件流 lost-wakeup（通知丢失） | `stream()` 锁内重查后再等待 | [exp18](./exp18_event_store_lost_wakeup/) |
| exp19 | SSE 忽略 `Last-Event-ID` 断线重放 | 流式路由支持 `Last-Event-ID` 续传 | [exp19](./exp19_sse_last_event_id/) |
| exp20 | temporal 模式协调指标恒 0 | `metrics()` 从 PostgreSQL 真实统计 | [exp20](./exp20_metrics_temporal_truth/) |

## 口径说明

- 每个实验的 `before.md` 在修复前代码上生成，`after.md` 在修复后代码上重跑同一脚本；
  脚本参数 `--phase before|after` 控制输出目录。
- 实验数据库为本机 PostgreSQL 的 `agentsupport_exp`（脚本自动重建表结构），迁移验证
  使用 `agentsupport_exp_mig`；**实验脚本不要并行执行**（共用同一数据库会锁冲突）。
- exp7 的 before 阶段会先删除保留索引再 ANALYZE，模拟修复前 schema，保证前后对比
  只差“索引”本身。
- exp1 顺带证伪了一个假设：`list_conversations` 在同一事务内因 SQLAlchemy identity
  map 命中而不存在 N+1（查询计数为 1）；真正的问题在 `list_sessions` 的每行容器租约
  查询，以及 `session_events` / SSE 轮询路径。

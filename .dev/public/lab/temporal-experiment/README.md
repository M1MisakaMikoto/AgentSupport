# AgentSupport x Temporal 实验（.dev/lab/temporal-experiment）

本实验回答一个问题：**AgentSupport 现有 checkpoint 机制能做到的所有功能，
Temporal 是否都能做到，或能与 checkpoint 结合？**

结论（由 `run_all.py` 的 6 个实验逐项证明）：**全部覆盖**。
暂停/恢复、取消、幂等命令、租约/心跳过期、崩溃恢复都由 Temporal 原生承担；
"Trae 会话重建包"（ContextBundle 的后代）作为数据契约放在 workflow 状态里，
与 Temporal 结合使用。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `common.py` | 共享工具：Temporal 连接、磁盘副作用记录（用于证明"只执行一次"） |
| `exp01_pause_resume.py` | 暂停等人 + 恢复 + 幂等信号（对应 WAITING_INPUT/PAUSED/checkpoint） |
| `exp02_cancel.py` | 取消：workflow 级取消 + 信号取消（对应 RunCommand.cancel） |
| `exp03_idempotent_signals.py` | 幂等命令 + 提前投递的缓冲（对应 RunCommand 队列） |
| `exp04_crash_recovery.py` | Worker 崩溃恢复，已完成步骤不重跑（当前 checkpoint 做不到） |
| `exp05_checkpoint_combined.py` | checkpoint 结合：重建包存在 workflow 状态，重试拿到字节一致的数据 |
| `exp06_heartbeat_timeout.py` | 心跳超时判失败，替代租约/心跳过期 + reconciler 轮询 |
| `crash_defs.py` / `worker_proc.py` | exp04 的子进程 Worker |
| `run_all.py` | 一键跑全部实验 |
| `verify_setup.py` | 环境连通性冒烟测试 |
| `evidence/` | 每次运行留下的副作用证据（JSON） |

## 环境准备

1. Python 3.12 虚拟环境，安装 `temporalio`（本项目 `.venv` 已装 1.31.0）：
   ```powershell
   .\.venv\Scripts\python.exe -m pip install "temporalio>=1.7,<2"
   ```
2. 启动 Temporal dev server（两种方式任选）：
   - **Windows 原生（推荐）**：从
     <https://github.com/temporalio/cli/releases> 下载
     `temporal_cli_<ver>_windows_amd64.tar.gz` 解压后：
     ```powershell
     .\temporal.exe server start-dev --headless --port 7233
     ```
     如果 Windows 无法直连 GitHub，可在 WSL 里 `curl -L` 下载再拷到 Windows。
   - **Docker**：`docker run -p 7233:7233 temporalio/dev-server:latest`
     （本机 WSL 的镜像源不可用时改用上面的 CLI 方式）。
3. 冒烟测试：
   ```powershell
   .\.venv\Scripts\python.exe .dev\lab\temporal-experiment\experiments\verify_setup.py
   ```

## 运行全部实验

```powershell
.\.venv\Scripts\python.exe .dev\lab\temporal-experiment\experiments\run_all.py
```

期望输出：`6/6 experiments passed`。每个实验会在 `evidence/` 留下 JSON，
记录各 Activity 实际执行次数，作为"不重跑 / 只执行一次"的可审计证据。

## 实验含义速览

| 实验 | 证明的能力 | 现有机制 |
| --- | --- | --- |
| exp01 | 暂停 = workflow 状态，无需 checkpoint 落盘；恢复 = 信号；重复命令无效 | `create_checkpoint` + `save_claimed_checkpoint` + 恢复往返 |
| exp02 | 取消经心跳投递到 Activity；workflow 终止为 CANCELLED | `RunCommand.cancel` 命令队列 |
| exp03 | 信号带幂等键，重复投递只生效一次；提前投递被可靠缓冲 | RunCommand 的 `idempotency_key` + 状态机 |
| exp04 | Worker 崩溃后只重试在途 Activity，已完成步骤不重跑 | `JobState.RETRY` 全量重跑 |
| exp05 | 重建包（ContextBundle）存 workflow 状态，重试拿到一致数据 | checkpoint 表 + 哈希校验 |
| exp06 | 心跳超时即判失败，无需轮询 | `runner_heartbeat_timeout_seconds` + reconciler |

详细方案见同目录 [PLAN.md](PLAN.md)。

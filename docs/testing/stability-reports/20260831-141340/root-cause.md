# 根因分析（run 2 / run 3 生成失败）

## 现象

- 成功率 33.3%（1/3），run 2、run 3 的生成阶段均为 `generation run ended with FAILED`。
- 四轮对话全部 completed（12/12），失败全部集中在"总结成 Skill"的生成会话。
- 生成耗时 649-1206 秒，远高于正常水平（成功样例约 5 分钟）。

## 证据（归档轨迹）

run 2（3 次 attempt 轨迹）：
- `853e1af1`：`'utf-8' codec can't decode byte 0xb2`（读到非 UTF-8 文件）
- `ba6f8f28`：`D:\workspace\.agentsupport\skill-generation-8AABC5C5-...\events.jsonl does not exist`
- `11aaa8ed`：多个事件文件路径 `No such file or directory`

run 3（3 次 attempt 轨迹）：
- `ee65e1f5`：`tools are not authorized: ['bash']`（模型试图用 bash）
- `66c20293`：`silent mode sandbox: path outside workspace: .agentsupport\skill-generation-...\events.jsonl`（相对路径被沙箱拒绝）
- `a52eb118`：`JSON edit tool error: File does not exist: D:\workspace\events.jsonl`

所有 attempt 均 `Task execution exceeded maximum steps without completion`（24 步耗尽）。

## 根因

生成会话的事件文件路径在 **API 控制面与 runner 执行面之间不一致**：

1. `skill_generation_ops._write_event_file` 把 `events.jsonl` 写到 API 侧工作区
   （`workspace-data/<uuid>/.agentsupport/skill-generation-<id>/events.jsonl`），
   返回给 agent 的是**相对路径** `.agentsupport/skill-generation-<id>/events.jsonl`。
2. demo 直跑模式下 runner 工作区根是 `D:\workspace`（`workspace_ref="/workspace"`），
   与 API 侧工作区**没有挂载/同步**，agent 按相对路径或自行拼接的绝对路径都找不到文件。
3. 生成会话处于 SILENT 模式，runner 的 `_sandbox_problem` 把相对路径 resolve 到进程 cwd
   → 判定"outside workspace"拒绝。
4. 模型在"找不到文件"与"沙箱拒绝"之间反复试错，消耗完 24 步上限 → run FAILED →
   demo 层 3 次重试全部命中同一缺陷 → 整体失败。

成功的那次（run 1）属于模型偶然读到遗留文件或蒙对路径，属于运气而非链路可靠。

## 建议修复方向（待确认）

- A：事件文件写入 runner 工作区根（`D:\workspace\.agentsupport\...`）并传绝对路径，
  与 `workspace_ref` 完全一致（demo 最直接，但需考虑多租户隔离）。
- B：修正 `_sandbox_problem` 支持相对路径（相对 workspace 解析），同时保证事件文件
  真实存在于该 workspace。
- C：事件量可控时直接把事件内容放入 prompt（回归早期方案，有截断风险）。

修复后建议重跑同规模稳定性测试验证成功率。

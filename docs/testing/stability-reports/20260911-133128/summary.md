# Skill 触发稳定性

- 技能：`quarterly-margin-report`；每类任务重复 5 次
- 触发率：5/5 = 100%
- 触发率（仅完成轮）：100%（完成 5/5）
- 采用率（报告含 `MARGIN-V2`）：5/5 = 100%
- 误触发率（寒暄任务）：0/5 = 0%
- 时延：min 45.2s / median 48.2s / max 135.4s

| # | 类型 | 触发 | 采用（来源） | 时延(s) | 读取命令 |
| --- | --- | --- | --- | --- | --- |
| 1 | matching | True | True (tool:str_replace_based_edit_tool) | 51.3 | `cat /opt/agent-skills/e4905629-42e0-4d16-be82-c245377df168/quarterly-margin-repo` |
| 2 | matching | True | True (message) | 45.2 | `cat /opt/agent-skills/975e746b-e52d-49a5-adde-49cd8184db47/quarterly-margin-repo` |
| 3 | matching | True | True (tool:str_replace_based_edit_tool) | 48.2 | `cat /opt/agent-skills/0705e87a-99ce-4ef6-87d8-888c87589fea/quarterly-margin-repo` |
| 4 | matching | True | True (tool:str_replace_based_edit_tool) | 48.2 | `cat /opt/agent-skills/3ebe2039-4194-43d1-a5e6-c32121d05c05/quarterly-margin-repo` |
| 5 | matching | True | True (message) | 135.4 | `cat /opt/agent-skills/a5bd2a6c-769e-4a66-adf6-d40e5c6ce6a8/quarterly-margin-repo` |
| 6 | control | False | False (-) | 6.1 | `` |
| 7 | control | False | False (-) | 6.1 | `` |
| 8 | control | False | False (-) | 6.1 | `` |
| 9 | control | False | False (-) | 6.1 | `` |
| 10 | control | False | False (-) | 6.1 | `` |

- 完成/失败：5/0


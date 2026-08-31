# Skill 生成稳定性测试报告

- 时间：2026-08-31T15:02:32
- 任务组：固定 4 轮后台文档操作（excel/word/pdf + 总结）
- 运行次数：3

## 汇总

- 成功率：33.3%（1/3）
- 首试成功：0 / 重试后成功：1
- 唯一 skill_id：1 / frontmatter 合法：1

## 耗时（秒）

| 阶段 | min | median | p95 | max | n |
| --- | --- | --- | --- | --- | --- |
| round_seconds | 9.0 | 19.6 | 45.8 | 54.1 | 12 |
| generation_seconds | 649.3 | 805.7 | 1165.9 | 1205.9 | 3 |

## 失败明细

- run 2: generation run ended with FAILED
- run 3: generation run ended with FAILED

## 每次运行明细（摘要）

| run | 状态 | attempt | skill_id | 轮数 | 总耗时(秒) |
| --- | --- | --- | --- | --- | --- |
| 1 | completed | 2 | doc-tool-create-verify | 4 | 775.6 |
| 2 | failed | None | None | 4 | 883.8 |
| 3 | failed | None | None | 4 | 1271.9 |
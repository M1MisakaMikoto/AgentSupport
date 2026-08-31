# Skill 生成稳定性测试报告

- 时间：2026-08-31T15:32:36
- 任务组：固定 4 轮后台文档操作（excel/word/pdf + 总结）
- 运行次数：3

## 汇总

- 成功率：100.0%（3/3）
- 首试成功：3 / 重试后成功：0
- 唯一 skill_id：3 / frontmatter 合法：3

## 耗时（秒）

| 阶段 | min | median | p95 | max | n |
| --- | --- | --- | --- | --- | --- |
| round_seconds | 9.0 | 21.1 | 34.5 | 36.1 | 12 |
| generation_seconds | 57.1 | 84.1 | 270.9 | 291.6 | 3 |

## 失败明细

无

## 每次运行明细（摘要）

| run | 状态 | attempt | skill_id | 轮数 | 总耗时(秒) |
| --- | --- | --- | --- | --- | --- |
| 1 | completed | 1 | workspace-doc-create-readback | 4 | 165.2 |
| 2 | completed | 1 | document-file-operations | 4 | 369.7 |
| 3 | completed | 1 | document-file-readback-verification | 4 | 141.2 |
# E2E Case：多轮交互生成 Skill（后台文档操作场景）

## 目标

验证 AgentSupport skill 生成演示的完整链路：用户与 agent 进行**多轮交互**完成一组后台
文档操作任务，点击"总结成 Skill"后系统把整段对话沉淀为标准 SKILL.md 并入库，且演示页
正确展示 skill 信息与文件路径。由 Playwright 驱动浏览器自动化执行并断言。

## 前置条件

- 已启动演示栈（`demo/start-demo.ps1`）：API `:8000`、runner `:8080`、演示页 `:8900`。
- 已安装 `pytest-playwright` 与 Chromium（`python -m playwright install chromium`）。
- 本机可访问 TRAE 模型 API（`TRAE_API_KEY` 配置在 `.env`）。

## Case 步骤

四轮任务均使用文档工具（`excel_edit_tool` / `word_edit_tool` / `pdf_tool`），禁止 bash；
每轮完成后 agent 调用 `task_done`，再进行下一轮。

| 轮次 | 任务 | 预期工具调用 | 断言点 |
| --- | --- | --- | --- |
| R1 创建用量周报 | 用 `excel_edit_tool` 创建 `tenant-usage-weekly.xlsx`：Sheet `W2026-35`，表头 `租户/会话数/输入Token/输出Token/成功会话`，3 行租户数据 | create + read 回读 | 文件存在；read 表头与 3 行正确 |
| R2 创建并审核 Word | 用 `word_edit_tool` 创建 `skill-review.docx`（标题 + 2 条记录，首条"待审核"），再把首条状态改为"已通过"并追加审核意见 | create + replace + append + read | 状态"已通过"、审核意见存在 |
| R3 创建评估摘要 PDF | 用 `pdf_tool` 创建 `eval-run-summary.pdf`（标题"评估运行摘要"，要点：数据集/用例数/通过率）并回读核对 | create + read | 中文文本可提取、要点齐全 |
| R4 提炼规则 | 把三轮经验整理成 3 条可复用规则（工具选择、回读核对、边界注意），直接回复 | task_done | 轮次 completed |

随后点击"总结成 Skill"：

1. 系统生成 SKILL.md 草稿并自动审核通过、发布入库。
2. 演示页出现"✓ 总结完成，已入库"，展示 `skill_id`、文件路径与完整 SKILL.md 内容。
3. 断言 `skills/tenants/t-demo/<skill_id>/SKILL.md` 存在，frontmatter 含 `name` 与 `description`。

## 自动化实现

脚本：`tests/e2e/demo/test_skill_generation_demo.py`（Python + pytest-playwright）。

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/e2e/demo/test_skill_generation_demo.py -v
```

环境变量：

- `DEMO_BASE_URL`：演示页地址，默认 `http://127.0.0.1:8900`。
- `RUN_ACTIVATION=1`：额外执行"总结后激活用例"（with-skill / without-skill 两组会话），
  耗时较长，默认关闭。

驱动方式：先通过页面 API 发起 `/api/start`（演示页首次"发送"依赖已建会话，脚本显式 start），
随后按真实用户路径在输入框逐轮下发任务、点击"发送"，通过 `/api/state` 轮询每轮
`completed`；最后点击"总结成 Skill"，轮询 `summary.status == completed`，并断言页面渲染
与入库文件。

## 已知边界

- 本 case 刻意避开 Office COM 转换（docx→pdf），保证自动化在无交互桌面环境可稳定通过；
  COM 转换由样例四/五与手动演示覆盖。
- 每轮真实模型调用耗时约 1-3 分钟，整条 case 约 5-10 分钟；`/api/round` 上限 30 轮。
- 首次"发送"前必须已创建会话：演示页当前未自动 start，脚本通过 `/api/start` 显式处理。

# 后台业务文档操作样例库

面向 AgentSupport 管理后台业务的文档操作测试样例，可直接用于：

- **演示**：把"任务文案"粘贴给 demo 会话，agent 用文档工具完成创建/修改/转换。
- **自动化验证**：`tests/unit/runner/test_backend_doc_samples.py` 用文档工具在种子文件副本上执行关键步骤并读回断言。
- **激活用例**：把种子文件复制进 demo 工作区（`D:\workspace`），让启用 skill 的新会话修改并核验。

生成种子文件：

```powershell
.venv\Scripts\python.exe demo\samples\seed_backend_samples.py
.venv\Scripts\python.exe demo\samples\seed_it_network_access_sample.py
```

默认输出到 `demo/samples/seed/`，可用 `--out` 指定目录。

## 样例一：租户用量周报（Excel）

- 业务背景：后台每周统计各租户会话数、Token 用量与成功率。
- 种子：`tenant-usage-weekly.xlsx`，Sheet `W2026-35`，表头
  `租户 / 会话数 / 输入Token / 输出Token / 成功会话 / 成功率%`，5 个租户明细行。
- 任务文案：
  > 你是后台运营专员。`tenant-usage-weekly.xlsx` 的用量周报缺少合计行，请用 excel_edit_tool 在最后一行下方追加合计行：会话数、输入Token、输出Token、成功会话求和；成功率 = 成功会话 ÷ 会话数 × 100（保留 1 位小数）。完成后回读核对并 task_done。禁止 bash。
- 期望：末行首列为"合计"，四列求和正确，成功率≈合计成功会话/合计会话数。
- 工具：`excel_edit_tool`（read / append_rows / read 回读）。

## 样例二：技能草稿审核清单（Word）

- 业务背景：后台 skill 草稿审核记录（名称、描述、状态、提交人）。
- 种子：`skill-review.docx`，标题"Skill 草稿审核清单"，3 条记录段落，首条状态为"待审核"。
- 任务文案：
  > 你是后台审核员。`skill-review.docx` 是技能草稿审核清单。请把第一条记录的状态由"待审核"改为"已通过"，并追加一行审核意见："审核通过，已安排入库。"完成后回读核对并 task_done。禁止 bash。
- 期望：读回包含"已通过"与审核意见，其余记录不变。
- 工具：`word_edit_tool`（read / replace / append / read 回读）。

## 样例三：评估运行摘要（PDF）

- 业务背景：后台评估运行报告（数据集、用例数、通过率、失败用例）。
- 种子：`eval-run-a.pdf` 与 `eval-run-b.pdf`，各含标题与要点段落。
- 任务文案：
  > 你是后台质量负责人。请用 pdf_tool 把两份评估运行摘要 `eval-run-a.pdf`、`eval-run-b.pdf` 合并为 `eval-runs-merged.pdf`，并用水印标记"DRAFT"。完成后读回核对并 task_done。禁止 bash。
- 期望：合并 PDF 包含两份摘要文本，且每页含水印 "DRAFT"。
- 工具：`pdf_tool`（merge / watermark / read）。

## 样例四：后台操作审计对账（Excel → PDF）

- 业务背景：后台操作审计事件（时间、操作人、操作类型、资源、结果）。
- 种子：`audit-events.xlsx`，Sheet `audit`，表头
  `时间 / 操作人 / 操作 / 资源 / 结果`，8 行审计事件。
- 任务文案：
  > 你是后台安全审计员。请用 document_convert_tool 把 `audit-events.xlsx` 转换为 `audit-events.pdf`，供归档。完成后 task_done 并回复输出路径。禁止 bash。
- 期望：输出 PDF 存在且非空。
- 工具：`document_convert_tool`（xlsx_to_pdf，依赖本机 Office COM，交互会话验证）。

## 样例五：月度后台运营报告（Word → PDF 全链路）

- 业务背景：后台月度运营报告（会话量、技能上线数、评估通过率、下月计划）。
- 种子：`monthly-ops-report.docx`，标题 + 4 段正文。
- 任务文案：
  > 你是后台运营负责人。请给 `monthly-ops-report.docx` 追加结论段："结论：本月各项指标达标，下月重点提升技能审核时效。"然后用 document_convert_tool 转换为 `monthly-ops-report.pdf`。完成后回读 docx 核对并 task_done。禁止 bash。
- 期望：docx 含结论段；转换出的 PDF 存在且非空（COM 部分交互会话验证）。
- 工具：`word_edit_tool`（append / read）+ `document_convert_tool`（docx_to_pdf）。

## 样例六：IT 网络异常访问记录分析（Excel → Word 分析报告）

- 业务背景：公司 IT 导出"网络异常访问记录"（xlsx），按用户给的口径过滤低风险内容，
  对剩余高优先级事件做分析并输出 Word 报告（报告样式由用户给定）。本样例用于在接入
  真实 IT 数据前验证 agent 的"读表 → 按口径过滤 → 结构化报告"能力。
- 种子：`abnormal-access-sample.xlsx`，Sheet `异常访问记录`，表头
  `序号 / 时间 / 源IP / 源归属 / 目标资产 / 目标端口 / 事件类型 / 行为摘要 / 请求数`，
  16 行明细：1–8 为低风险（应被过滤），9–16 为高优先级（应保留）。
- 过滤口径（模拟用户指导）：
  > 低风险可过滤：① UA 为已知搜索引擎/爬虫（Googlebot/Bingbot）且只访问公开页面；
  > ② 源为已登记的监控探针或白名单维护设备（192.168.200.5、10.20.0.8）；③ 仅端口/DNS
  > 探测且无任何成功认证或数据交互；④ 只命中 404 未触达敏感路径的 URL 探测。
  > 其余保留并按 SSH/RDP 爆破、敏感路径访问、横向移动、敏感文件下载、异常登录、
  > 弱口令/Webshell 特征归类。
- 报告样式（模拟用户指导）：
  > 用 word_edit_tool 创建 `网络异常访问分析报告.docx`：标题"网络异常访问分析报告（2026-09-01）"；
  > 依次为「1 分析范围与方法」（含过滤口径简述）、「2 结论概述」（保留 N 条需关注事件及类型分布）、
  > 「3 高优先事件清单」（每条一段：时间｜源IP｜目标资产｜事件类型｜理由｜建议动作）、
  > 「4 建议措施」。
- 任务文案（粘贴给 demo 会话，文件需已复制进该会话工作区）：
  > 你是公司 IT 安全分析员。`abnormal-access-sample.xlsx` 是本日网络异常访问记录。
  > 第一步：用 excel_edit_tool 的 `to_csv` 把它导出为同目录 `abnormal-access-sample.csv`
  > （UTF-8），确认导出成功。
  > 第二步：用只读 bash 检索该 csv（仅允许 `grep/rg/head/cat` 读取 csv，禁止写入、
  > 网络或其它命令），核对表结构与总行数，并按下面口径过滤低风险：① 已知搜索引擎爬虫
  > （Googlebot/Bingbot）且仅访问公开页面；② 白名单设备（192.168.200.5 监控探针、
  > 10.20.0.8 供应商维护）**且行为属于登记监控/维护**（定时探测、维护窗口内维护操作）；
  > 若白名单 IP 从事爆破、敏感文件下载、弱口令、横向移动等异常行为则必须保留；
  > ③ 仅探测且无成功认证/交互；④ 仅 404 未触达敏感路径（404 但命中 /admin 等敏感路径仍保留）。
  > 其余保留并按 SSH/RDP 爆破、敏感路径访问、横向移动、敏感文件下载、异常登录、
  > 弱口令/Webshell 特征归类。再用 word_edit_tool 创建报告 docx：标题"网络异常访问分析报告
  > （2026-09-01）"，含「分析范围与方法」「结论概述」「高优先事件清单（每条一段：
  > 时间｜源IP｜目标资产｜事件类型｜理由｜建议动作）」「建议措施」四节。完成后回读核对，
  > task_done 并列出 csv 与报告路径。
- 期望：过滤后保留 8 条（序号 9–16）；报告含四节结构、8 条事件及正确归类，不含低风险行；
  结论概述中的保留数量与类型分布正确。
- 工具：`excel_edit_tool`（read / to_csv）+ 只读 `bash`（grep/rg/head/cat）+ `word_edit_tool`
  （create / append / read 回读）。
- 提速路径（已验证 2026-09-09）：引导 agent「先 to_csv 导出 UTF-8，再用只读 bash 检索定位」，
  单轮即完成分析+报告（耗时约 108s，工具调用 excel_edit_tool×2 + bash×1 + word_edit_tool×1），
  产出与全量读表版一致（四节结构、8 条高优先、低风险不进入事件清单）。

## 验证断言一览

| 样例 | 自动化断言（pytest） | COM 依赖 |
| --- | --- | --- |
| 一 | 合计行存在、四列求和正确、成功率正确 | 无 |
| 二 | 状态改为"已通过"、审核意见存在 | 无 |
| 三 | 合并含两份文本、水印存在 | 无 |
| 四 | 种子生成与读回正确 | 转换需 Office |
| 五 | docx 追加与读回正确 | 转换需 Office |
| 六 | 16 行读回、低风险全过滤、报告含 8 条高优先事件与四节结构 | 无 |

COM 转换相关断言在非交互环境默认跳过（`AGENTSUPPORT_RUN_COM_TESTS=1` 时启用），与工具链主测试保持一致。

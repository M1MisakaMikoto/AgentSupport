# Agent Lab（项目实例）

本目录是 AgentSupport 项目的 agent 实验场：**agent 产出内容（实验、证据、门户 preview）记录在项目仓库**。

Lab 的**框架**（文档规范、词汇表、ADR、薄规则）位于 `.dev/private/lab/`，不在本目录重复维护。

## 快速开始

```powershell
# 方式一：双击打开门户（preview）
.\\.dev\\lab\\index.html

# 方式二：本地 HTTP 服务（推荐，markdown 链接可正常查看）
python -m http.server 8000 --directory .dev\lab
# 然后访问 http://localhost:8000
```

## 目录结构

```text
.dev/lab/
├─ index.html                 # 门户 preview（记录）
├─ README.md                  # 本文件
├─ agent-state-experiment/    # 实验（记录）
├─ confirmation/              # 需求确认与结论沉淀（记录）
├─ temporal-experiment/       # Temporal 工作流实验（记录）
├─ tool-approval-experiment/  # 工具审批与检查点恢复实验（记录）
└─ LICENSE                    # Temporal MIT（随实验记录）
```

框架（规范/词汇表/ADR）在 `.dev/private/lab/`，门户已用相对链接指向。

## 实验索引

| 实验 | 入口 | 内容 |
|---|---|---|
| Agent Core 状态依赖 | [README](agent-state-experiment/README.md) | 判断 Core 是否需要与会话整体绑定 |
| 需求确认 | [已确认事项](confirmation/accepted.md) | 已对齐的产品与架构决策沉淀 |
| Temporal 工作流 | [README](temporal-experiment/README.md) | 暂停/恢复、取消、幂等、崩溃恢复等验证 |
| 工具审批 | [报告](tool-approval-experiment/report.md) | 真实 Trae 审批、检查点与跨进程恢复 |

## 与 .dev/private 的分工

- `.dev/public/lab/`：agent 产出，**记录**（不被忽略，自动入库）。
- `.dev/private/`：服务于 coding agent 的内容（搬入的 skills、agent docs），**不记录**（由 sync.ps1 追加到 .gitignore）。

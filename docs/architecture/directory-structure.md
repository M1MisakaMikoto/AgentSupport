> 简体中文 | [English](directory-structure.en.md)

# AgentSupport 目录架构

AgentSupport 围绕三个边界组织：

- `agent_runner_contracts`：控制面与 Runner 之间交换的稳定数据。
- `agentsupport`：控制面领域、应用用例、适配器与进程。
- `session_runner`：执行面状态、工具、Trae/MCP 适配器与私有 HTTP API。

依赖方向为：

~~~text
serving / processes -> application -> domain + ports <- adapters

agentsupport -> agent_runner_contracts <- session_runner
~~~

`agentsupport.application` 不得导入适配器、serving 或 bootstrap 模块。具体实现只在
`agentsupport.bootstrap.container` 中选择。Session Runner 不得导入 `agentsupport`；跨进程模型
属于 `agent_runner_contracts`。

`agentsupport` 和 `session_runner` 根目录下的公共模块提供简洁的服务和进程入口。实现保留在各分层
包中。仅用于仓库开发的工具位于 wheel 之外的 `devtools/` 下。

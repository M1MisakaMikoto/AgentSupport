# Trae 内部工具审批接入实验

更新时间：2026-07-24

## 结论

Trae 的内置工具可以在统一边界拦截。源码中 `BaseAgent._tool_call_handler` 不直接执行工具，而是统一调用 `_tool_caller.parallel_tool_call()` 或 `_tool_caller.sequential_tool_call()`；因此平台可以用 `ApprovalExecutor/ToolGateway` 替换 `_tool_caller`，在实际工具执行前完成权限判断、审批等待和审计。

实验脚本：[`test_tool_gateway.py`](test_tool_gateway.py)

## 已验证

- 审批请求在工具副作用发生前产生；未批准时标记文件不存在。
- `APPROVE_ONCE` 允许工具继续执行。
- `REJECT` 阻止工具执行并返回失败结果。
- 工具调用的 `call_id/name/arguments/status` 可以序列化为审批检查点并恢复读取。
- `BaseAgent` 的串行和并行工具调用都经过同一个 `_tool_caller` 边界。

运行命令：

```powershell
python .dev\lab\tool-approval-experiment\test_tool_gateway.py
```

## 尚未验证和必要改造

1. Trae 当前 `BaseAgent._tool_call_handler` 会直接等待工具结果；审批超时不能自然转换为平台的 `WAITING_INPUT`，需要引入受控异常或显式 `ToolPending` 返回值，并由 `CoreRuntime` 将其转换为平台事件。
2. 当前 Trae 的执行上下文和 LLM 客户端消息历史主要保存在进程内。实验只验证了工具调用元数据检查点，没有验证杀死进程、销毁容器后重建完整消息历史并继续执行。
3. 首期只要求在平台主动建立的安全停顿点恢复：工具尚未执行时保存Conversation执行字段、完整动作/消息事件、待审批工具调用和恢复所需的ContextBundle。Trae适配器需要提供从该检查点恢复的接口。任意崩溃、OOM、宿主机重启或外部操作中断不要求精确恢复，按`LOST`或未完成状态处理。
4. 不能把 Trae 自身的 CLI stdin 作为审批协议；审批必须发生在平台 ToolGateway，所有内置工具和 MCP 工具都必须使用该执行器。

## 下一步方案

- `ToolGateway` 接口：`describe_tools()`、`authorize(call)`、`execute(call)`、`checkpoint(call)`、`restore(checkpoint)`。
- 短时审批：容器保持运行，`execute(call)` 挂起等待平台决定。
- 首期不提供用户手动暂停；只有工具审批会产生主动停顿点。容器保持运行时为`WAITING_INPUT`，超时后转为`PAUSED`并释放容器。
- 超时审批：在待执行工具尚未发生副作用的安全停顿点保存检查点、结束当前Core进程并释放容器；批准后由平台自动创建新容器，重建ContextBundle，并以平台事件中的待执行工具调用作为恢复输入，不要求用户额外发送消息。
- 拒绝审批：向Core注入失败的`ToolResult`，由Core决定重试、改用其他工具或结束任务。
- 批量ToolCall：如果同一模型响应中的任意工具需要审批，整批工具在执行前统一暂停；批准后按原批次策略执行，拒绝时整批不执行并为每个调用生成失败结果。
- 先用确定性假 Core 验证跨容器恢复，再接入真实 Trae 模型，避免把恢复正确性绑定到模型输出偶然性。

## 2026-07-24 补充验证

确定性跨进程检查点实验 [`test_checkpoint_recovery.py`](test_checkpoint_recovery.py) 通过：平台持有的 Context Bundle、待交互信息和事件序号可以序列化；新 Core 进程可以恢复并继续执行；恢复不会重复检查点前的工具副作用；上下文哈希被篡改时恢复会拒绝。

真实 Qwen/Trae 两进程恢复实验 [`test_real_trae_checkpoint_resume.py`](test_real_trae_checkpoint_resume.py) 通过：进程A在首个工具副作用前持久化 Context Bundle、pending ToolCall、next step 和上下文哈希后退出，目标文件不存在；进程B读取并校验检查点，创建新的Trae实例，执行挂起工具一次，并在2个后续步骤内成功完成任务。实验同时产生了一个反例：只恢复ToolResult而不恢复系统/任务上下文时，模型会丢失目标并重复尝试工具；因此检查点必须保存完整Context Bundle，不能只保存待审批ToolCall。

Docker 运行时实验使用 Docker Desktop 29.6.2 和 `python:3.12-slim` 通过：Workspace 以 `/workspace` 可写挂载，容器标签可携带 `session_id/workspace_id/lease_epoch`，宿主机可观察容器退出后产生的文件。显式处理 SIGTERM 的进程在宽限期内以退出码 0 结束；未处理 SIGTERM 的进程在宽限期后以退出码 137 结束，证明 RuntimeDriver 必须区分优雅退出确认和 SIGKILL 超时。

真实 Qwen/Trae CLI 已验证普通工具执行和 Workspace 写入，但 CLI 本身没有平台 ToolGateway 或用户输入协议。要实现审批和跨容器恢复，首期必须在 Session 容器内运行可注入 `ToolExecutor` 的 Trae 适配层，或明确维护一个受控 Trae Fork；不能仅依靠原始 `trae-cli run` 命令行参数实现平台审批。

真实 Qwen/Trae 源码模式审批实验 [`test_real_trae_approval.py`](test_real_trae_approval.py) 通过。`APPROVE_ONCE`路径在文件副作用发生前截获`str_replace_based_edit_tool`，随后创建并校验`REAL-TRAE-APPROVE_ONCE`，Trae 以3步成功结束；`REJECT`路径先后截获文件编辑和bash回退调用，两个调用都收到失败`ToolResult`，目标文件始终不存在。结果证明 `_tool_caller` 是真实模型运行中的统一审批边界，不只是静态源码推断。

真实 `trae-cli run --docker-image python:3.12-slim` 端到端实验未通过：Trae 0.1.0 在 Windows 首次 Docker 模式会调用 Unix `rm`，并尝试从错误的当前目录构建缺失的 `edit_tool_cli.py`，因此在启动容器前失败。没有留下实验容器或 Workspace 副作用。该结果不否定 Docker Driver，而是证明首期不能直接把现成 CLI 的 Docker 模式当作平台 RuntimeDriver；需要平台构建受控镜像，并在容器内使用固定的 Trae runner/适配器。

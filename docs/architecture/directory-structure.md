# AgentSupport Directory Architecture

AgentSupport is organized around three boundaries:

- `agent_runner_contracts`: stable data exchanged between the control plane and Runner.
- `agentsupport`: control-plane domain, application use cases, adapters and processes.
- `session_runner`: execution-plane state, tools, Trae/MCP adapters and private HTTP API.

The dependency direction is:

~~~text
serving / processes -> application -> domain + ports <- adapters

agentsupport -> agent_runner_contracts <- session_runner
~~~

`agentsupport.application` must not import adapters, serving or bootstrap modules. Concrete
implementations are selected only in `agentsupport.bootstrap.container`. The Session Runner
must not import `agentsupport`; cross-process models belong in `agent_runner_contracts`.

Public modules at the root of `agentsupport` and `session_runner` provide concise service and
process entry points. Implementations remain in their layered packages. Repository-only
development tools live outside the wheel under `devtools/`.

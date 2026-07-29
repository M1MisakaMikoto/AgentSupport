# AgentSupport Directory Architecture

AgentSupport is organized around three boundaries:

- `agent_runner_contracts`: stable data exchanged between the control plane and Runner.
- `agent_platform`: control-plane domain, application use cases, adapters and processes.
- `session_runner`: execution-plane state, tools, Trae/MCP adapters and private HTTP API.

The dependency direction is:

~~~text
serving / processes -> application -> domain + ports <- adapters

agent_platform -> agent_runner_contracts <- session_runner
~~~

`agent_platform.application` must not import adapters, serving or bootstrap modules. Concrete
implementations are selected only in `agent_platform.bootstrap.container`. The Session Runner
must not import `agent_platform`; cross-process models belong in `agent_runner_contracts`.

Compatibility modules at the root of `agent_platform` and `session_runner` preserve runtime and
service import paths during migration. Repository-only development tools live outside the wheel
under `devtools/`; new code should import the layered modules.

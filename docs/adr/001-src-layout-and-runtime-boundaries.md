# ADR-001: Src Layout And Runtime Boundaries

## Status

Accepted

## Decision

Use a `src/` package layout and treat the AgentSupport control plane, shared Runner contracts and
Session Runner as separate code boundaries. Keep dependency construction in
`agentsupport.bootstrap.container`.

## Consequences

Editable and wheel installs resolve the same package tree. Runner deployments do not depend on
AgentSupport internals. Root-level modules provide concise public imports while implementations
remain within the layered package structure.

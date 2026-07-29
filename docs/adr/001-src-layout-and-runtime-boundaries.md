# ADR-001: Src Layout And Runtime Boundaries

## Status

Accepted

## Decision

Use a `src/` package layout and treat Platform, shared Runner contracts and Session Runner as
separate code boundaries. Keep dependency construction in `agent_platform.bootstrap.container`.

## Consequences

Editable and wheel installs resolve the same package tree. Runner deployments no longer depend
on Platform internals. Existing import paths remain as compatibility facades until a separately
versioned removal decision is made.

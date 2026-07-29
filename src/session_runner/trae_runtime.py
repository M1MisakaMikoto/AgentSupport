"""Compatibility exports for the Trae execution adapter."""

from .adapters.trae import (
    AgentFactory,
    TraeExecutionAdapter,
    TraeRuntimeSettings,
    TraeToolGatewayBridge,
)

__all__ = [
    "AgentFactory",
    "TraeExecutionAdapter",
    "TraeRuntimeSettings",
    "TraeToolGatewayBridge",
]

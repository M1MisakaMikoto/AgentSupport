from .diagnostics import router as diagnostics_router
from .eval import router as eval_router
from .events import router as events_router
from .interactions import router as interactions_router
from .mcp_servers import router as mcp_servers_router
from .operations import router as operations_router
from .registrations import router as registrations_router
from .resources import router as resources_router
from .skill_generations import router as skill_generations_router
from .skills import router as skills_router

__all__ = [
    "diagnostics_router",
    "eval_router",
    "events_router",
    "interactions_router",
    "mcp_servers_router",
    "operations_router",
    "registrations_router",
    "resources_router",
    "skill_generations_router",
    "skills_router",
]


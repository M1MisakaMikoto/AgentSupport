from .events import router as events_router
from .interactions import router as interactions_router
from .operations import router as operations_router
from .registrations import router as registrations_router
from .resources import router as resources_router
from .skills import router as skills_router

__all__ = [
    "events_router",
    "interactions_router",
    "operations_router",
    "registrations_router",
    "resources_router",
    "skills_router",
]

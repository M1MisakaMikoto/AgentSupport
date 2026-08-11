"""Public entry point for SQLAlchemy schema initialization."""

from .adapters.persistence.sqlalchemy.models import (
    Base,
    ContainerLeaseRow,
    ConversationCheckpointRow,
    ConversationEventRow,
    ConversationRow,
    ExecutionJobRow,
    IdempotencyKeyRow,
    OutboxEventRow,
    RunCommandRow,
    RunnerEndpointRow,
    RunnerRegistrationRow,
    RuntimeOperationRow,
    RuntimeSlotRow,
    SessionRow,
    WorkspaceRow,
    WorkspaceWriteLeaseRow,
    create_schema,
)

__all__ = [
    "Base",
    "ContainerLeaseRow",
    "ConversationCheckpointRow",
    "ConversationEventRow",
    "ConversationRow",
    "ExecutionJobRow",
    "IdempotencyKeyRow",
    "OutboxEventRow",
    "RunCommandRow",
    "RunnerEndpointRow",
    "RunnerRegistrationRow",
    "RuntimeOperationRow",
    "RuntimeSlotRow",
    "SessionRow",
    "WorkspaceRow",
    "WorkspaceWriteLeaseRow",
    "create_schema",
]

if __name__ == "__main__":
    from .bootstrap.settings import settings

    create_schema(settings.database_url)

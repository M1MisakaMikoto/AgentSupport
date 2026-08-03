class RepositoryConflict(Exception):
    """Raised when a persisted optimistic-concurrency invariant is violated."""


class StaleClaim(RepositoryConflict):
    """Raised when a worker attempts to use an expired or fenced claim."""

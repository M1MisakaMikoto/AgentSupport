"""Public entry point for the AgentSupport HTTP application."""

from .serving.http.app import app, create_app

__all__ = ["app", "create_app"]

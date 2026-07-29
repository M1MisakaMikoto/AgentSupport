"""Compatibility entry point for the private Runner HTTP API."""

from .serving.http.app import app, create_runner_app

__all__ = ["app", "create_runner_app"]

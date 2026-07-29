"""Compatibility entry point for the platform HTTP application."""

from .serving.http.app import app, create_app

__all__ = ["app", "create_app"]

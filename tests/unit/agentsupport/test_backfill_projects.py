"""Backfill helpers must tolerate a globally unique ``default`` username."""

from sqlalchemy import create_engine, text

from agentsupport.adapters.persistence.sqlalchemy.models import (
    _default_username,
    backfill_projects,
)


def _schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE organizations "
                "(id VARCHAR(36) PRIMARY KEY, name VARCHAR(120))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id VARCHAR(36) PRIMARY KEY, "
                "organization_id VARCHAR(36), "
                "username VARCHAR(120) UNIQUE, "
                "created_at DATETIME"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE workspaces ("
                "id VARCHAR(36) PRIMARY KEY, "
                "organization_id VARCHAR(36), "
                "name VARCHAR(120)"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE projects ("
                "id VARCHAR(36) PRIMARY KEY, "
                "organization_id VARCHAR(36), "
                "user_id VARCHAR(36), "
                "workspace_id VARCHAR(36), "
                "name VARCHAR(120), "
                "preset_id VARCHAR(36), "
                "config TEXT, "
                "created_at DATETIME, "
                "updated_at DATETIME"
                ")"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id VARCHAR(36) PRIMARY KEY, "
                "workspace_id VARCHAR(36), "
                "project_id VARCHAR(36)"
                ")"
            )
        )


def test_default_username_falls_back_when_taken() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36), username VARCHAR(120) UNIQUE)"))
        connection.execute(text("INSERT INTO users VALUES ('u1', 'default')"))

        first = _default_username(connection, "org-bbbbbbbb")
        assert first == "default-org-bbbb"
        connection.execute(
            text("INSERT INTO users (id, username) VALUES ('u2', :name)"),
            {"name": first},
        )
        second = _default_username(connection, "org-cccccccc")
        assert second == "default-org-cccc"
        connection.execute(
            text("INSERT INTO users (id, username) VALUES ('u3', :name)"),
            {"name": second},
        )
        third = _default_username(connection, "org-dddddddd")
        assert third == "default-org-dddd"


def test_backfill_projects_tolerates_existing_default_user() -> None:
    engine = create_engine("sqlite://")
    _schema(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO organizations VALUES ('org-a', 'A')"))
        connection.execute(text("INSERT INTO organizations VALUES ('org-b', 'B')"))
        connection.execute(
            text(
                "INSERT INTO users VALUES ('u-default', 'org-a', 'default', "
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text("INSERT INTO workspaces VALUES ('ws-a', 'org-a', 'ws-a')")
        )
        connection.execute(
            text("INSERT INTO workspaces VALUES ('ws-b', 'org-b', 'ws-b')")
        )

    backfill_projects(engine)

    with engine.connect() as connection:
        users = connection.execute(
            text("SELECT username, organization_id FROM users")
        ).fetchall()
        assert any(
            user.organization_id == "org-b" and user.username.startswith("default-")
            for user in users
        )
        projects = connection.execute(text("SELECT workspace_id FROM projects")).fetchall()
        assert len(projects) == 2

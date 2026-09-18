"""Tenant preset upload, asynchronous build queue and runtime CLI policy."""

import json

import pytest
from _support import make_temporal_service as _make_service
from httpx import ASGITransport, AsyncClient

from agentsupport.api import create_app
from agentsupport.application.service import ServiceError
from agentsupport.domain import (
    BuildStatus,
    CliApp,
    TenantPreset,
    cli_policy_from_fields,
    resolve_preset_fields,
)


def _service(tmp_path, **overrides):
    return _make_service(
        tmp_path,
        workspace_root=tmp_path / "workspaces",
        skills_root=tmp_path / "skills",
        presets_root=tmp_path / "presets",
        **overrides,
    ).service


def _metadata(**overrides):
    payload = {
        "name": "Acme tooling",
        "description": "tenant CLI bundle",
        "cli_apps": [
            {
                "cli_id": "mytool",
                "entry": "mytool",
                "safe_prefixes": ["mytool list", "mytool show"],
                "env": ["MYTOOL_TOKEN"],
                "package": "mytool.tar.gz",
            }
        ],
        "env": ["MYTOOL_TOKEN"],
    }
    payload.update(overrides)
    return payload


def test_put_preset_stores_packages_and_enqueues_build(tmp_path):
    service = _service(tmp_path)
    result = service.put_tenant_preset(
        "acme",
        metadata=_metadata(skills=[]),
        files={"mytool.tar.gz": b"binary-payload"},
    )

    preset = result["preset"]
    assert preset["tenant_id"] == "acme"
    assert [app["cli_id"] for app in preset["cli_apps"]] == ["mytool"]
    assert result["content_hash"]

    stored = tmp_path / "presets" / "acme" / "mytool" / "mytool.tar.gz"
    assert stored.read_bytes() == b"binary-payload"
    assert (stored.parent / "package.sha256").is_file()

    build = result["build"]
    assert build["status"] == BuildStatus.PENDING.value
    assert build["content_hash"] == result["content_hash"]
    assert service.get_preset_build("acme", build["build_id"]).tenant_id == "acme"

    # Re-uploading the same content converges on the same hash.
    again = service.put_tenant_preset(
        "acme", metadata=_metadata(), files={"mytool.tar.gz": b"binary-payload"}
    )
    assert again["content_hash"] == result["content_hash"]


def test_preset_validation_rejects_bad_payloads(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ServiceError) as exc:
        service.put_tenant_preset(
            "bad/id", metadata=_metadata(), files={"mytool.tar.gz": b"x"}
        )
    assert exc.value.code == "TENANT_PRESET_INVALID"

    with pytest.raises(ServiceError) as exc:
        service.put_tenant_preset(
            "acme", metadata=_metadata(), files={}
        )
    assert exc.value.code == "TENANT_PRESET_PACKAGE_MISSING"

    duplicate = _metadata(
        cli_apps=[
            _metadata()["cli_apps"][0],
            {**_metadata()["cli_apps"][0], "package": "other.tar.gz"},
        ]
    )
    with pytest.raises(ServiceError) as exc:
        service.put_tenant_preset(
            "acme",
            metadata=duplicate,
            files={"mytool.tar.gz": b"x", "other.tar.gz": b"y"},
        )
    assert exc.value.code == "TENANT_PRESET_INVALID"

    with pytest.raises(ServiceError) as exc:
        service.put_tenant_preset(
            "acme",
            metadata=_metadata(cli_apps=[{"cli_id": "t", "entry": "t", "env": ["1BAD"]}]),
            files={"p": b"x"},
        )
    assert exc.value.code == "TENANT_PRESET_INVALID"

    with pytest.raises(ServiceError) as exc:
        service.put_tenant_preset(
            "acme",
            metadata=_metadata(
                cli_apps=[
                    {
                        "cli_id": "t",
                        "entry": "t",
                        "package": "p",
                        "daemon": {"command": "t serve", "port": 70000},
                    }
                ]
            ),
            files={"p": b"x"},
        )
    assert exc.value.code == "TENANT_PRESET_INVALID"


def test_daemon_declaration_is_stored_and_hashed(tmp_path):
    service = _service(tmp_path)
    with_daemon = service.put_tenant_preset(
        "acme",
        metadata=_metadata(
            cli_apps=[
                {
                    "cli_id": "mytool",
                    "entry": "mytool",
                    "package": "mytool.tar.gz",
                    "daemon": {"command": "mytool  serve   --port 9000", "port": 9000},
                }
            ]
        ),
        files={"mytool.tar.gz": b"payload"},
    )
    stored = with_daemon["preset"]["cli_apps"][0]["daemon"]
    assert stored == {"command": "mytool serve --port 9000", "port": 9000}

    claimed = service.claim_preset_build()
    assert claimed["cli_apps"][0]["daemon"]["port"] == 9000

    without = service.put_tenant_preset(
        "acme", metadata=_metadata(), files={"mytool.tar.gz": b"payload"}
    )
    assert without["content_hash"] != with_daemon["content_hash"]


def test_build_claim_complete_and_image_lookup(tmp_path):
    service = _service(tmp_path)
    result = service.put_tenant_preset(
        "acme", metadata=_metadata(), files={"mytool.tar.gz": b"payload"}
    )
    build_id = result["build"]["build_id"]

    assert service.latest_ready_image("acme") == "agentsupport-api"

    claimed = service.claim_preset_build()
    assert claimed is not None
    assert claimed["build"]["build_id"] == build_id
    assert claimed["image_tag"].startswith("agentsupport-runner:acme-")
    app = claimed["cli_apps"][0]
    assert app["cli_id"] == "mytool"
    assert app["package_b64"] == "cGF5bG9hZA=="
    assert service.claim_preset_build() is None

    ready = service.complete_preset_build(
        __import__("uuid").UUID(build_id),
        status=BuildStatus.READY,
        image_tag=claimed["image_tag"],
        log_tail="built",
    )
    assert ready.status is BuildStatus.READY
    assert service.latest_ready_image("acme") == claimed["image_tag"]

    with pytest.raises(ServiceError) as exc:
        service.complete_preset_build(
            __import__("uuid").UUID(build_id), status=BuildStatus.READY
        )
    assert exc.value.code == "PRESET_BUILD_INVALID"


def test_failed_build_keeps_previous_ready_image(tmp_path):
    service = _service(tmp_path)
    first = service.put_tenant_preset(
        "acme", metadata=_metadata(), files={"mytool.tar.gz": b"v1"}
    )
    claimed = service.claim_preset_build()
    service.complete_preset_build(
        __import__("uuid").UUID(claimed["build"]["build_id"]),
        status=BuildStatus.READY,
        image_tag=claimed["image_tag"],
    )
    assert service.latest_ready_image("acme") == claimed["image_tag"]

    second = service.put_tenant_preset(
        "acme", metadata=_metadata(), files={"mytool.tar.gz": b"v2"}
    )
    assert second["content_hash"] != first["content_hash"]
    failed = service.claim_preset_build()
    service.complete_preset_build(
        __import__("uuid").UUID(failed["build"]["build_id"]),
        status=BuildStatus.FAILED,
        error="docker build failed",
    )
    # The previous READY image still serves the tenant.
    assert service.latest_ready_image("acme") == claimed["image_tag"]


def test_cli_policy_falls_back_to_default_preset(tmp_path):
    service = _service(tmp_path)
    service.put_tenant_preset(
        "default",
        metadata=_metadata(
            cli_apps=[
                {
                    "cli_id": "pdftotext",
                    "entry": "pdftotext",
                    "safe_prefixes": ["pdftotext -"],
                    "package": "pdf.tar.gz",
                }
            ]
        ),
        files={"pdf.tar.gz": b"pdf"},
    )

    workspace = service.create_workspace("ws")
    tenant_session = service.create_session(workspace.id, tenant_id="acme")
    policy = service.cli_policy_for_session(tenant_session)
    assert policy["tenant_id"] == "acme"
    assert policy["allowed_safe_prefixes"] == ["pdftotext -"]
    assert policy["image"] == "agentsupport-api"

    default_session = service.create_session(workspace.id)
    assert service.cli_policy_for_session(default_session)["tenant_id"] == "default"

    service.put_tenant_preset(
        "acme",
        metadata=_metadata(),
        files={"mytool.tar.gz": b"payload"},
    )
    tenant_policy = service.cli_policy_for_session(tenant_session)
    assert tenant_policy["allowed_safe_prefixes"] == ["mytool list", "mytool show"]


def test_resolve_preset_fields_and_policy_shape():
    default = TenantPreset(
        tenant_id="default",
        cli_apps=[CliApp(cli_id="d", entry="d", package="d.tgz")],
        skills=["shared"],
    )
    tenant = TenantPreset(tenant_id="acme", skills=["acme-skill"])

    fields = resolve_preset_fields(tenant, default)
    assert fields["used_default"] == {"cli_apps": True, "skills": False, "env": True}
    assert [app.cli_id for app in fields["cli_apps"]] == ["d"]
    assert fields["skills"] == ["acme-skill"]
    assert cli_policy_from_fields(fields)["allowed_safe_prefixes"] == []


@pytest.mark.asyncio
async def test_tenant_preset_api_upload_and_status(tmp_path):
    service = _service(tmp_path, runner_manager_token="secret")
    app = create_app(service)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        uploaded = await client.put(
            "/tenants/acme/preset",
            data={"metadata": json.dumps(_metadata())},
            files={"files": ("mytool.tar.gz", b"payload", "application/gzip")},
        )
        assert uploaded.status_code == 200, uploaded.text
        body = uploaded.json()
        assert body["preset"]["tenant_id"] == "acme"
        build_id = body["build"]["build_id"]
        assert body["build"]["status"] == BuildStatus.PENDING.value

        fetched = await client.get("/tenants/acme/preset")
        assert fetched.status_code == 200
        assert fetched.json()["cli_apps"][0]["entry"] == "mytool"

        listed = await client.get("/tenants", params={"tenant_id": "acme"})
        assert [item["tenant_id"] for item in listed.json()] == ["acme"]

        pending = await client.get("/tenants/acme/preset/builds")
        assert [item["build_id"] for item in pending.json()] == [build_id]

        # Internal endpoints stay closed without the manager token.
        denied = await client.post("/internal/preset-builds/claim")
        assert denied.status_code == 401

        claimed = await client.post(
            "/internal/preset-builds/claim",
            headers={"X-Runner-Manager-Token": "secret"},
        )
        assert claimed.status_code == 200
        image_tag = claimed.json()["image_tag"]

        completed = await client.post(
            f"/internal/preset-builds/{build_id}/complete",
            json={"status": "READY", "image_tag": image_tag, "log_tail": "ok"},
            headers={"X-Runner-Manager-Token": "secret"},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "READY"

        fetched_build = await client.get(f"/tenants/acme/preset/builds/{build_id}")
        assert fetched_build.json()["image_tag"] == image_tag

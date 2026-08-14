"""Unit tests for model-endpoint connectivity diagnostics (runner side)."""

import datetime
import importlib
import queue
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient

from session_runner.diagnostics import _proxy_for, model_endpoint_config, probe_tls
from session_runner.server import create_runner_app

runner_http_app = importlib.import_module("session_runner.serving.http.app")


def _self_signed_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.local")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    cert_file.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))

    ports: queue.Queue[int] = queue.Queue()
    stop = threading.Event()

    def serve() -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(4)
            ports.put(sock.getsockname()[1])
            with context.wrap_socket(sock, server_side=True) as tls:
                while not stop.is_set():
                    try:
                        connection, _ = tls.accept()
                    except (ssl.SSLError, OSError):
                        continue
                    try:
                        connection.recv(4096)
                    finally:
                        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    port = ports.get(timeout=10)
    return port, thread, stop


def test_model_endpoint_config_reads_environment(monkeypatch):
    monkeypatch.setenv("TRAE_PROVIDER", "anthropic")
    monkeypatch.setenv("TRAE_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.setenv("TRAE_MODEL_BASE_URL", "https://api.example.test/anthropic")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    config = model_endpoint_config()

    assert config == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "base_url": "https://api.example.test/anthropic",
    }


def test_probe_tls_captures_untrusted_certificate(tmp_path):
    port, thread, stop = _self_signed_server(tmp_path)
    try:
        report = probe_tls("127.0.0.1", port, timeout=5)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert report["verified"] is False
    assert "certificate verify failed" in report["error"]
    certificate = report["certificate"]
    assert "test.local" in certificate["subject"]
    assert certificate["key_bits"] == 2048
    assert certificate["serial"]


def test_proxy_selection_respects_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "api.internal,.corp.local")

    assert _proxy_for("api.deepseek.com") == "http://proxy.local:3128"
    assert _proxy_for("api.internal") is None
    assert _proxy_for("svc.corp.local") is None


@pytest.mark.asyncio
async def test_runner_connectivity_endpoint_unconfigured(monkeypatch):
    monkeypatch.delenv("TRAE_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    app = create_runner_app(runner_mode="deterministic")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.get("/diagnostics/model-connectivity")

    assert response.status_code == 200
    assert response.json()["configured"] is False


@pytest.mark.asyncio
async def test_runner_connectivity_endpoint_configured(monkeypatch):
    monkeypatch.setenv("TRAE_MODEL_BASE_URL", "https://api.example.test/anthropic")
    monkeypatch.setenv("TRAE_PROVIDER", "anthropic")
    monkeypatch.setenv("TRAE_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.setattr(
        runner_http_app,
        "run_model_connectivity",
        lambda timeout=8.0: {
            "configured": True,
            "tls": {"verified": False, "error": "certificate verify failed"},
        },
    )
    app = create_runner_app(runner_mode="deterministic")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        response = await client.get("/diagnostics/model-connectivity")

    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "tls": {"verified": False, "error": "certificate verify failed"},
    }

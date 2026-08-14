"""Model-endpoint connectivity diagnostics for the private Session Runner.

The runner performs real TLS handshakes against the configured model provider
so operators can tell whether a failing Run is caused by the model connection
(DNS hijack, TLS interception, weak certificate) rather than by the agent
toolchain or skill configuration.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
from typing import Any
from urllib.parse import urlparse


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def model_endpoint_config() -> dict[str, str]:
    """Resolve the model provider configuration without exposing secrets."""

    return {
        "provider": _env("TRAE_PROVIDER") or _env("SESSION_RUNNER_PROVIDER") or "unknown",
        "model": _env("TRAE_MODEL"),
        "base_url": _env("TRAE_MODEL_BASE_URL") or _env("ANTHROPIC_BASE_URL"),
    }


def _matches_no_proxy(host: str, no_proxy: str) -> bool:
    host = host.lower()
    for entry in no_proxy.split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def _proxy_for(host: str) -> str | None:
    """Resolve the HTTPS proxy for a host from the environment (trust_env style)."""

    for key in ("HTTPS_PROXY", "https_proxy"):
        value = _env(key)
        if value:
            for no_proxy_key in ("NO_PROXY", "no_proxy"):
                if _matches_no_proxy(host, _env(no_proxy_key)):
                    return None
            return value
    return None


def _connect_tunnel(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    proxy_host = parsed.hostname or "127.0.0.1"
    proxy_port = parsed.port or 3128
    raw = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    raw.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = raw.recv(4096)
        if not chunk:
            break
        response += chunk
    if not response.startswith(b"HTTP/1.1 200"):
        raw.close()
        status = response.split(b"\r\n", 1)[0].decode(errors="replace")
        raise OSError(f"proxy CONNECT failed: {status}")
    return raw


def _open_socket(host: str, port: int, proxy: str | None, timeout: float) -> socket.socket:
    if proxy:
        return _connect_tunnel(proxy, host, port, timeout)
    return socket.create_connection((host, port), timeout=timeout)


def _handshake(
    host: str, port: int, proxy: str | None, timeout: float, *, verify: bool
) -> tuple[ssl.SSLSocket, bytes | None]:
    raw = _open_socket(host, port, proxy, timeout)
    try:
        context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        tls_socket = context.wrap_socket(raw, server_hostname=host)
        return tls_socket, tls_socket.getpeercert(binary_form=True)
    except Exception:
        with contextlib.suppress(OSError):
            raw.close()
        raise


def _leaf_info(certificate_der: bytes) -> dict[str, Any]:
    try:
        from cryptography import x509

        certificate = x509.load_der_x509_certificate(certificate_der)
        public_key = certificate.public_key()
        return {
            "subject": certificate.subject.rfc4514_string(),
            "issuer": certificate.issuer.rfc4514_string(),
            "key_bits": public_key.key_size,
            "key_type": type(public_key).__name__.removesuffix("PublicKey"),
            "serial": str(certificate.serial_number),
            "not_before": certificate.not_valid_before_utc.isoformat(),
            "not_after": certificate.not_valid_after_utc.isoformat(),
            "signature_algorithm": certificate.signature_algorithm_oid._name,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return {"error": str(exc)}


def probe_tls(host: str, port: int, *, timeout: float = 8.0) -> dict[str, Any]:
    """Perform a real TLS handshake and describe the peer certificate.

    Returns ``verified`` plus the leaf certificate details. When verification
    fails (for example ``EE certificate key too weak``), the certificate is
    still captured through a second unverified handshake so the report shows
    exactly what the network path presented.
    """

    result: dict[str, Any] = {"host": host, "port": port, "tls_version": None}
    proxy = _proxy_for(host)
    if proxy:
        result["proxy"] = proxy
    try:
        try:
            tls_socket, certificate_der = _handshake(host, port, proxy, timeout, verify=True)
            result["verified"] = True
            result["error"] = None
            result["tls_version"] = tls_socket.version()
            if certificate_der:
                result["certificate"] = _leaf_info(certificate_der)
            tls_socket.close()
        except ssl.SSLCertVerificationError as exc:
            result["verified"] = False
            result["error"] = str(exc)
            if "key too weak" in str(exc):
                result["hint"] = (
                    "证书密钥过弱（通常低于 2048 位）被 OpenSSL 拒绝；"
                    "这通常是网络层的 DNS 劫持或 TLS 审计/中间人证书，而非模型服务本身的问题。"
                )
            try:
                raw_socket, certificate_der = _handshake(host, port, proxy, timeout, verify=False)
                result["tls_version"] = raw_socket.version()
                if certificate_der:
                    result["certificate"] = _leaf_info(certificate_der)
                raw_socket.close()
            except Exception as exc2:  # noqa: BLE001 - best-effort unverified capture
                result["error"] = f"{result['error']}; raw handshake: {exc2}"
    except OSError as exc:
        result["verified"] = False
        result["error"] = f"connect failed: {exc}"
    return result


def run_model_connectivity(timeout: float = 8.0) -> dict[str, Any]:
    """Produce the full diagnostics report for the configured model endpoint."""

    config = model_endpoint_config()
    base_url = config["base_url"]
    if not base_url:
        return {
            "configured": False,
            "error": "TRAE_MODEL_BASE_URL / ANTHROPIC_BASE_URL 未配置",
            **config,
        }
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return {
            "configured": False,
            "error": f"invalid model base URL: {base_url}",
            **config,
        }
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        tls = probe_tls(host, port, timeout=timeout)
    else:
        tls = {"host": host, "port": port, "tls_version": "not-https"}
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
            tls["verified"] = True
            tls["error"] = None
        except OSError as exc:
            tls["verified"] = False
            tls["error"] = f"connect failed: {exc}"
    return {
        "configured": True,
        "base_url": base_url,
        "provider": config["provider"],
        "model": config["model"],
        "tls": tls,
    }

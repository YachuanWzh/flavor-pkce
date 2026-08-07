"""Outbound SSRF protection (P0-7): the gateway must refuse to proxy to
private/loopback/link-local/metadata addresses, even when the user controls
their upstream_url."""
import pytest

from gateway.ssrf import validate_upstream_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/v1/chat",
    "http://10.0.0.5/x",
    "http://172.16.0.1/x",
    "http://192.168.1.10/x",
    "http://169.254.169.254/latest/meta-data",  # cloud metadata
    "http://0.0.0.0:80/x",
    "http://[::1]:8080/x",
    "http://[fe80::1]/x",
    "http://localhost:9999/x",
    "http://203.0.113.0/x",                     # docs TEST-NET-3, non-routable
    "ftp://8.8.8.8/x",                          # non-http scheme
    "file:///etc/passwd",
    "not-a-url",
    "",
])
def test_private_and_invalid_urls_rejected(url):
    assert validate_upstream_url(url) is False, f"should reject {url}"


@pytest.mark.parametrize("url", [
    "http://8.8.8.8/v1/chat",       # public literal IP
    "https://1.1.1.1/",
])
def test_public_urls_accepted(url):
    assert validate_upstream_url(url) is True, f"should accept {url}"


def test_public_hostname_resolves_to_public_ip(monkeypatch):
    """A hostname resolving only to public addresses is allowed."""
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ],
    )
    assert validate_upstream_url("http://public.example.com/x") is True


def test_hostname_resolving_to_private_ip_rejected(monkeypatch):
    """A hostname that resolves to a private address is rejected (DNS rebinding
    defense: every resolved address must be public)."""
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
        ],
    )
    assert validate_upstream_url("http://evil.example.com/x") is False


def test_hostname_with_any_private_address_rejected(monkeypatch):
    """Mixed resolution (public + private) must be rejected."""
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0)),
        ],
    )
    assert validate_upstream_url("http://mixed.example.com/x") is False


def test_unresolvable_hostname_rejected(monkeypatch):
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: (_ for _ in ()).throw(socket.gaierror("nxdomain")),
    )
    assert validate_upstream_url("http://no-such-host.invalid/x") is False


def test_ip_like_hostname_that_resolves_to_private_rejected(monkeypatch):
    """A hostname that merely *looks* like an IP (127.0.0.1.anyhost.com) is a
    hostname; it must still be resolved and every address checked."""
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.50.10", 0)),
        ],
    )
    assert validate_upstream_url("http://127.0.0.1.anyhost.com/x") is False


# ---------------------------------------------------------------------------
# Operator allowlist: approved hosts bypass the SSRF check
# ---------------------------------------------------------------------------

def test_allowlisted_loopback_accepted():
    """An operator-approved loopback host passes even though it is private."""
    assert validate_upstream_url(
        "http://127.0.0.1:8080/v1/chat",
        allowlist=["127.0.0.1"],
    ) is True


def test_allowlisted_hostname_resolving_to_private_accepted(monkeypatch):
    """An approved hostname is exempt even when DNS resolves to a reserved
    range (e.g. a proxy fake-ip range like 198.18.0.0/15)."""
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 0)),
        ],
    )
    assert validate_upstream_url(
        "https://api.deepseek.com/anthropic",
        allowlist=["api.deepseek.com"],
    ) is True


def test_allowlist_matching_is_case_insensitive(monkeypatch):
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ],
    )
    assert validate_upstream_url(
        "http://LLM.Example.com/x",
        allowlist=["llm.example.com"],
    ) is True


def test_non_allowlisted_private_still_rejected(monkeypatch):
    """Hosts outside the allowlist keep the SSRF checks."""
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
        ],
    )
    assert validate_upstream_url(
        "http://evil.example.com/x",
        allowlist=["other.example.com"],
    ) is False


# ---------------------------------------------------------------------------
# Integration: proxy refuses a private upstream_url
# ---------------------------------------------------------------------------

def test_proxy_rejects_private_upstream(monkeypatch):
    """Even with valid auth, a private upstream_url must be blocked before any
    outbound connection."""
    import gateway.config as config
    import gateway.main as gm

    async def fake_routing(payload):
        return {
            "service_name": "evil",
            "upstream_url": "http://169.254.169.254/latest/meta-data",
            "upstream_api_key": "k",
            "upstream_auth_type": "bearer",
            "models": [],
        }, None

    monkeypatch.setattr(gm, "_resolve_user_routing", fake_routing)

    from fastapi.testclient import TestClient
    client = TestClient(gm.app)

    # Patch verify_jwt to accept any token
    monkeypatch.setattr(
        gm, "verify_jwt",
        lambda t: {"sub": "u1", "config_version": 1, "jti": "j1"},
    )

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer fake"},
    )
    assert resp.status_code == 400
    assert "ssrf" in resp.text.lower() or "not allowed" in resp.text.lower()

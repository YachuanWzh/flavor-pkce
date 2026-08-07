"""Outbound SSRF protection for the API Gateway (P0-7).

The gateway proxies to upstream URLs that originate from per-user LLM config
(which the user controls).  Before any outbound connection, ``validate_upstream_url``
rejects:

- non-``http(s)`` schemes,
- private / loopback / link-local / metadata / documentation address ranges
  (both literal IPs and DNS-resolved hostnames),
- hostnames that fail to resolve, or resolve to *any* disallowed address
  (defense against DNS-rebinding style tricks).

A hostname is only accepted when *every* resolved address is public.

Operator-approved hosts can bypass these checks via the ``allowlist``
argument: hosts listed there (hostnames or literal IPs, matched
case-insensitively) are trusted without resolution. This is the
operator-approval escape hatch for on-prem/private LLM endpoints or
environments whose DNS resolves to reserved ranges (e.g. a proxy
fake-ip range such as 198.18.0.0/15).
"""

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}


def _is_private_ip(ip_str: str) -> bool:
    """True when the address is not globally routable (SSRF target)."""
    try:
        addr = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return True  # unparsable -> treat as unsafe
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    # Cloud metadata / link-local IPv4 (169.254.0.0/16 incl. 169.254.169.254)
    if addr.version == 4 and addr in ipaddress.ip_network("169.254.0.0/16", strict=False):
        return True
    # IPv6 unique-local / link-local fallbacks
    if addr.version == 6:
        if addr in ipaddress.ip_network("fc00::/7", strict=False):
            return True
        if addr in ipaddress.ip_network("fe80::/10", strict=False):
            return True
    return False


def _host_has_allowed_ips(host: str) -> bool:
    """Resolve ``host``; True only when every address is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        if _is_private_ip(ip_str):
            return False
    return True


def validate_upstream_url(url: str, allowlist: Iterable[str] = ()) -> bool:
    """Validate an upstream URL for outbound proxying.

    Returns True when the URL is http(s) and its host is a public literal IP,
    resolves entirely to public addresses, or is explicitly listed in
    ``allowlist`` (operator-approved hosts bypass the checks).
    """
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in _ALLOWED_SCHEMES:
        return False
    host = parts.hostname
    if not host:
        return False

    allowed = {
        entry.strip().lower()
        for entry in allowlist
        if entry and entry.strip()
    }
    if host.lower() in allowed:
        return True

    # Literal IP: check the address directly.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # Hostname: every resolved address must be public.
        return _host_has_allowed_ips(host)

    return not _is_private_ip(host)

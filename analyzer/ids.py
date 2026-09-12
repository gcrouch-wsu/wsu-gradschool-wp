from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import urlsplit


WORDPRESS_ID_RE = re.compile(r"^[1-9]\d{0,19}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def wordpress_id(value) -> str | None:
    """Return a canonical WordPress ID, or None if the value is not a positive integer."""
    text = str(value or "").strip()
    return text if WORDPRESS_ID_RE.fullmatch(text) else None


def canonical_host(url: str) -> str:
    identity = site_identity(url)
    return identity[1] if identity else ""


def site_identity(url: str) -> tuple[str, str, int, str] | None:
    """Return scheme, hostname, port, and site path for WordPress installation binding."""
    candidate = (url or "").strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    path = (parsed.path or "/").rstrip("/") or "/"
    return scheme, host, port, path


def same_http_origin(left: str, right: str) -> bool:
    """True when two URLs share scheme, hostname, and port. Path may differ."""
    first = site_identity(left)
    second = site_identity(right)
    if not first or not second:
        return False
    return first[:3] == second[:3]


def rest_base_url_allowed(url: str) -> bool:
    """Application Passwords may travel only over HTTPS, or HTTP to loopback."""
    try:
        parsed = urlsplit((url or "").strip())
        host = (parsed.hostname or "").casefold()
    except ValueError:
        return False
    if not host:
        return False
    if parsed.scheme.lower() == "https":
        return True
    return parsed.scheme.lower() == "http" and host in LOOPBACK_HOSTS


def sites_are_same(export_urls: list[str], rest_url: str) -> bool:
    rest = site_identity(rest_url)
    if not rest:
        return False
    return any(site_identity(url) == rest for url in export_urls if url)


def is_loopback_address(value: str) -> bool:
    candidate = (value or "").strip().strip("[]").split("%", 1)[0]
    if candidate.casefold() == "localhost":
        return True
    try:
        address = ip_address(candidate)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)

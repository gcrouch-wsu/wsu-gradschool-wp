from __future__ import annotations

import re
from urllib.parse import urlsplit


WORDPRESS_ID_RE = re.compile(r"^[1-9]\d{0,19}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def wordpress_id(value) -> str | None:
    """Return a canonical WordPress ID, or None if the value is not a positive integer."""
    text = str(value or "").strip()
    return text if WORDPRESS_ID_RE.fullmatch(text) else None


def canonical_host(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        host = (urlsplit(candidate).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        return host[4:]
    return host


def rest_base_url_allowed(url: str) -> bool:
    """Application Passwords may travel only over HTTPS, or HTTP to loopback."""
    try:
        parsed = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if not host:
        return False
    if parsed.scheme.lower() == "https":
        return True
    return parsed.scheme.lower() == "http" and host in LOOPBACK_HOSTS


def sites_are_same(export_urls: list[str], rest_url: str) -> bool:
    rest_host = canonical_host(rest_url)
    if not rest_host:
        return False
    return any(canonical_host(url) == rest_host for url in export_urls if url)

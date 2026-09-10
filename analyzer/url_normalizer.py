from __future__ import annotations

import html
import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


IMAGE_VARIANT_RE = re.compile(r"(?:-\d+x\d+|-scaled|-rotated)(?=\.[^./]+$)", re.IGNORECASE)
WP_IDENTITY_QUERY_RE = re.compile(
    r"(?:^|&)(?:p|page_id|attachment_id|name|post_type)=",
    re.IGNORECASE,
)


def _hostname(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def normalize_url(value: str, base_url: str = "") -> str:
    """Return a comparison-safe HTTP(S) URL while retaining meaningful queries."""
    if not value:
        return ""

    candidate = html.unescape(value).strip().strip("\"'").replace("\\/", "/")
    if candidate.startswith("//"):
        scheme = urlsplit(base_url).scheme or "https"
        candidate = f"{scheme}:{candidate}"
    elif base_url:
        candidate = urljoin(base_url.rstrip("/") + "/", candidate)

    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        port = parsed.port
    except ValueError:
        return ""

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    path = quote(unquote(parsed.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/")

    return urlunsplit((scheme, host, path, parsed.query, ""))


def public_href(value: str) -> str:
    """Return an http(s) URL safe for server-rendered hyperlinks."""
    return normalize_url(value)


def url_keys(value: str, base_url: str = "") -> set[str]:
    """Produce same-site keys. Cross-host URLs never share a path-only key."""
    normalized = normalize_url(value, base_url)
    if not normalized:
        return set()

    keys = {normalized}
    parsed = urlsplit(normalized)
    if base_url and _hostname(normalized) != _hostname(normalize_url(base_url) or base_url):
        return keys

    path_key = parsed.path or "/"
    if parsed.query:
        keys.add(f"path:{path_key}?{parsed.query}")
        if not WP_IDENTITY_QUERY_RE.search(parsed.query):
            keys.add(f"path:{path_key}")
    else:
        keys.add(f"path:{path_key}")
    return keys


def media_variant_keys(value: str, base_url: str = "") -> set[str]:
    """Also map generated WordPress image sizes back to their original attachment URL."""
    keys = url_keys(value, base_url)
    normalized = normalize_url(value, base_url)
    if not normalized:
        return keys
    parsed = urlsplit(normalized)
    original_path = IMAGE_VARIANT_RE.sub("", parsed.path)
    if original_path != parsed.path:
        original = urlunsplit((parsed.scheme, parsed.netloc, original_path, parsed.query, ""))
        keys.update(url_keys(original, base_url))
    return keys

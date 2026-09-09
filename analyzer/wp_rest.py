from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .ids import rest_base_url_allowed, wordpress_id


REST_BASES = {
    "post": "posts",
    "page": "pages",
    "attachment": "media",
    "document": "document",
    "tribe_events": "events",
    "tribe_venue": "tribe_venue",
    "tribe_organizer": "tribe_organizer",
    "wsu_html_snippet": "wsu_html_snippet",
    "wsu_template": "wsu_template",
    "wsuwp_embed_code": "wsuwp_embed_code",
    "wp_block": "blocks",
}

BATCH_SIZE = 100
MAX_TRASH_BATCH = 25
HttpGet = Callable[[str, dict[str, str], int], tuple[int, Any]]
HttpDelete = Callable[[str, dict[str, str], int], tuple[int, Any]]


def wp_admin_edit_url(site_url: str, item_id: str) -> str:
    safe_id = wordpress_id(item_id)
    if not site_url or not safe_id:
        return ""
    return f"{site_url.rstrip('/')}/wp-admin/post.php?post={safe_id}&action=edit"


def wp_admin_trash_url(site_url: str, post_type: str) -> str:
    if not site_url:
        return ""
    base = site_url.rstrip("/")
    if post_type == "attachment":
        return f"{base}/wp-admin/upload.php?status=trash"
    return f"{base}/wp-admin/edit.php?post_status=trash&post_type={post_type or 'post'}"


def rest_base_for(post_type: str) -> str | None:
    return REST_BASES.get(post_type)


def safe_rest_base(value: str) -> str | None:
    return value if value in set(REST_BASES.values()) else None


def _resource_url(base_url: str, rest_base: str, item_id: str) -> str | None:
    safe_base = safe_rest_base(rest_base)
    safe_id = wordpress_id(item_id)
    if not safe_base or not safe_id:
        return None
    return f"{base_url.rstrip('/')}/wp-json/wp/v2/{safe_base}/{safe_id}"


def _read_json_response(raw: str, fallback: str) -> Any:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"message": raw[:300] or fallback}


def _http_request(
    url: str,
    headers: dict[str, str],
    timeout: int,
    method: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = dict(headers)
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            headers = {key.lower(): str(value) for key, value in response.headers.items()}
            return response.status, _read_json_response(raw, str(response.status)), headers
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = _read_json_response(raw, str(exc.reason))
        if not parsed:
            parsed = {"message": str(exc.reason)}
        return exc.code, parsed, {}
    except URLError as exc:
        return 0, {"message": str(exc.reason or exc)}, {}


def _unwrap_http(result) -> tuple[int, Any, dict[str, str]]:
    if isinstance(result, tuple) and len(result) == 3:
        return result[0], result[1], result[2] or {}
    status, payload = result
    return status, payload, {}


def _http_get(url: str, headers: dict[str, str], timeout: int) -> tuple[int, Any, dict[str, str]]:
    return _unwrap_http(_http_request(url, headers, timeout, "GET"))


def _http_delete(url: str, headers: dict[str, str], timeout: int) -> tuple[int, Any, dict[str, str]]:
    return _unwrap_http(_http_request(url, headers, timeout, "DELETE"))


class WordPressRestClient:
    """WordPress REST client using Application Passwords. Writes only move items to Trash."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        http_get: HttpGet | None = None,
        http_delete: HttpDelete | None = None,
        timeout: int = 45,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "wsu-gradschool-wp-audit/local",
        }
        if not rest_base_url_allowed(base_url):
            raise ValueError("WordPress REST must use HTTPS, or HTTP only on loopback.")
        self.http_get = http_get or _http_get
        self.http_delete = http_delete or _http_delete
        self.timeout = timeout

    def list_type(self, rest_base: str, ids: list[str]) -> tuple[int, Any]:
        safe_base = safe_rest_base(rest_base)
        safe_ids = [item_id for item_id in (wordpress_id(value) for value in ids) if item_id]
        if not safe_base or not safe_ids:
            return 400, {"message": "WordPress REST route or include list is invalid."}
        # WordPress expects one CSV include list. Repeated include= keys collapse
        # to the last ID in PHP, which made a 306-page check report only 4 found.
        query = {
            "include": ",".join(safe_ids),
            "per_page": str(min(len(safe_ids), BATCH_SIZE)),
            # `any` excludes trash and auto-draft in WordPress REST.
            "status": "any,trash",
            "context": "edit",
            "_fields": "id,status,link,modified,type",
        }
        collected: list[Any] = []
        page = 1
        while page <= 20:
            query["page"] = str(page)
            url = f"{self.base_url}/wp-json/wp/v2/{safe_base}?{urlencode(query)}"
            status, payload, headers = _unwrap_http(self.http_get(url, self.headers, self.timeout))
            if status != 200:
                return status, payload
            if not isinstance(payload, list):
                return 502, {"message": "WordPress REST returned an unexpected list payload."}
            collected.extend(payload)
            try:
                total_pages = int(headers.get("x-wp-totalpages") or 0)
            except ValueError:
                total_pages = 0
            if total_pages:
                if page >= total_pages:
                    break
            elif not payload or len(payload) < int(query["per_page"]):
                break
            elif len(collected) >= len(safe_ids):
                break
            page += 1
        else:
            return 502, {"message": "WordPress REST listing exceeded the pagination limit."}
        return 200, collected

    def get_item(self, rest_base: str, item_id: str) -> tuple[int, Any]:
        url = _resource_url(self.base_url, rest_base, item_id)
        if not url:
            return 400, {"message": "WordPress ID or REST route is invalid."}
        query = urlencode({"context": "edit", "_fields": "id,status,type,title,link,modified"})
        status, payload, _headers = _unwrap_http(self.http_get(f"{url}?{query}", self.headers, self.timeout))
        return status, payload

    def trash(self, rest_base: str, item_id: str) -> tuple[int, Any]:
        # WordPress Trash is DELETE without force. POST status=trash is rejected
        # on this site because The Events Calendar replaces the REST status enum.
        url = _resource_url(self.base_url, rest_base, item_id)
        if not url:
            return 400, {"message": "WordPress ID or REST route is invalid."}
        status, payload, _headers = _unwrap_http(self.http_delete(url, self.headers, self.timeout))
        return status, payload


def client_from_env(
    http_get: HttpGet | None = None,
    http_delete: HttpDelete | None = None,
) -> WordPressRestClient:
    return WordPressRestClient(
        base_url=os.environ["WP_REST_BASE_URL"],
        username=os.environ["WP_REST_USERNAME"],
        password=os.environ["WP_REST_APPLICATION_PASSWORD"],
        http_get=http_get,
        http_delete=http_delete,
    )


def _empty_live(item_id: str, rest_base: str | None, live_state: str, error: str = "") -> dict:
    return {
        "id": item_id,
        "rest_base": rest_base or "",
        "live_state": live_state,
        "live_found": live_state in {"found", "trashed"},
        "live_status": "",
        "live_link": "",
        "live_modified": "",
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def live_check_items(
    items: list[dict],
    client: WordPressRestClient,
    site_url: str = "",
) -> dict[str, dict]:
    """Compare exported records to live WordPress. Read-only."""
    results: dict[str, dict] = {}
    grouped: dict[str, list[str]] = {}
    for item in items:
        item_id = str(item["id"])
        rest_base = rest_base_for(item.get("type", ""))
        if not rest_base:
            results[item_id] = _empty_live(item_id, None, "not-in-rest")
            continue
        grouped.setdefault(rest_base, []).append(item_id)

    for rest_base, ids in grouped.items():
        found: dict[str, dict] = {}
        unsupported = False
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start:start + BATCH_SIZE]
            status, payload = client.list_type(rest_base, chunk)
            if status in {404, 501}:
                unsupported = True
                break
            if status in {401, 403}:
                message = payload.get("message", "WordPress refused authenticated REST access.") if isinstance(payload, dict) else "WordPress refused authenticated REST access."
                raise PermissionError(message)
            if status == 0 or status >= 400:
                message = payload.get("message", f"WordPress REST returned HTTP {status}.") if isinstance(payload, dict) else f"WordPress REST returned HTTP {status}."
                for item_id in chunk:
                    results[item_id] = _empty_live(item_id, rest_base, "error", str(message))
                continue
            if not isinstance(payload, list):
                for item_id in chunk:
                    results[item_id] = _empty_live(item_id, rest_base, "error", "WordPress REST returned an unexpected payload.")
                continue
            records = payload
            for record in records:
                found[str(record.get("id"))] = record
        if unsupported:
            for item_id in ids:
                results[item_id] = _empty_live(item_id, rest_base, "not-in-rest")
            continue
        for item_id in ids:
            if item_id in results:
                continue
            record = found.get(item_id)
            if not record:
                results[item_id] = _empty_live(item_id, rest_base, "missing")
                continue
            live_status = str(record.get("status") or "")
            live_state = "trashed" if live_status == "trash" else "found"
            live = _empty_live(item_id, rest_base, live_state)
            live["live_status"] = live_status
            live["live_link"] = str(record.get("link") or "")
            live["live_modified"] = str(record.get("modified") or "")
            if live_state == "trashed":
                post_type = str(record.get("type") or "")
                live["wp_admin_url"] = wp_admin_trash_url(site_url, post_type)
            results[item_id] = live
    return results


def _payload_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("code") or fallback)
    return fallback


def _media_trash_blocked(rest_base: str, message: str) -> bool:
    if rest_base != "media":
        return False
    lowered = message.lower()
    return any(
        hint in lowered
        for hint in ("force", "does not support trashing", "trash is not supported", "bypass trash")
    )


def trash_items(
    items: list[dict],
    client: WordPressRestClient,
    site_url: str = "",
) -> list[dict]:
    """Move exported records to WordPress Trash. Never force-deletes."""
    results: list[dict] = []
    for item in items:
        item_id = wordpress_id(item.get("id"))
        rest_base = rest_base_for(item.get("type", ""))
        title = str(item.get("title") or item.get("file_name") or f"ID {item.get('id')}")
        if not item_id:
            results.append({
                "id": str(item.get("id") or ""),
                "ok": False,
                "title": title,
                "error": "WordPress ID is not a canonical positive integer.",
            })
            continue
        if not rest_base:
            results.append({
                "id": item_id,
                "ok": False,
                "title": title,
                "error": "This content type is not available through WordPress REST, so it cannot be trashed from this app.",
            })
            continue
        preflight_status, current = client.get_item(rest_base, item_id)
        if preflight_status in {401, 403}:
            message = _payload_message(current, "WordPress refused authenticated REST access.")
            results.append({"id": item_id, "ok": False, "title": title, "error": message})
            break
        if preflight_status != 200 or not isinstance(current, dict) or str(current.get("id")) != item_id:
            results.append({
                "id": item_id,
                "ok": False,
                "title": title,
                "error": "Could not re-read this record from the configured WordPress site before Trash.",
            })
            continue
        live_type = str(current.get("type") or "")
        expected_type = str(item.get("type") or "")
        if live_type and expected_type and live_type != expected_type:
            results.append({
                "id": item_id,
                "ok": False,
                "title": title,
                "error": f"Live WordPress type {live_type} does not match the exported type {expected_type}.",
            })
            continue
        status, payload = client.trash(rest_base, item_id)
        if status in {401, 403}:
            message = _payload_message(payload, "WordPress refused authenticated REST access.")
            results.append({"id": item_id, "ok": False, "title": title, "error": message})
            break
        if status in {200, 201} and isinstance(payload, dict):
            live_status = str(payload.get("status") or "")
            if live_status != "trash":
                results.append({
                    "id": item_id,
                    "ok": False,
                    "title": title,
                    "error": f"WordPress returned status {live_status or 'unknown'} instead of trash.",
                })
                continue
            live_state = "trashed"
            live = _empty_live(item_id, rest_base, live_state)
            live["live_status"] = live_status
            live["live_link"] = str(payload.get("link") or "")
            live["live_modified"] = str(payload.get("modified") or "")
            post_type = str(payload.get("type") or item.get("type") or "")
            if live_state == "trashed":
                live["wp_admin_url"] = wp_admin_trash_url(site_url, post_type)
            results.append({"id": item_id, "ok": True, "title": title, "live": live, "error": ""})
            continue
        message = _payload_message(payload, f"WordPress REST returned HTTP {status}.")
        if _media_trash_blocked(rest_base, message):
            message = (
                "WordPress will not move this media item to Trash because media trash is disabled. "
                "This app will not permanently delete it."
            )
        results.append({"id": item_id, "ok": False, "title": title, "error": message})
    return results

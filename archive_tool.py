from __future__ import annotations

import base64
import csv
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analyzer import parse_wxr
from analyzer.ids import wordpress_id


DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 5 * 1024 * 1024 * 1024
ARCHIVE_SCHEMA_VERSION = 3
MAX_TEST_ASSETS = 100
DOWNLOAD_TIMEOUT_SECONDS = 45
ERROR_BODY_SNIFF_BYTES = 64 * 1024
FILE_WRITE_RETRIES = 6
LOGIN_PATH_RE = re.compile(r"/(login|wp-login\.php|signin|sign-in)/?$", re.IGNORECASE)
HTML_PREFIX_RE = re.compile(rb"^\s*(?:<\?xml[^>]*>\s*)?<!doctype\s+html|^\s*<html[\s>]", re.IGNORECASE)
DOCUMENT_EXTENSIONS = {
    ".csv", ".doc", ".docm", ".docx", ".dot", ".dotx", ".epub", ".ics",
    ".key", ".md", ".numbers", ".odf", ".ods", ".odt", ".pages", ".pdf",
    ".pot", ".potx", ".pps", ".ppsx", ".ppt", ".pptm", ".pptx", ".rtf",
    ".txt", ".vcf", ".xls", ".xlsb", ".xlsm", ".xlsx", ".xml", ".zip",
}
FILE_META_HINTS = ("attachment", "document", "download", "file", "media", "pdf")
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
ORIGINAL_IMAGE_RE = re.compile(
    r's:\d+:"original_image";s:\d+:"(?P<name>[^"]+)"', re.IGNORECASE
)
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class ArchiveError(Exception):
    """A download or planning failure with an operator-facing reason.

    ``result`` is the short HTTP/network outcome shown in reports (for example
    ``HTTP 403 Forbidden``); ``blocked`` marks refusals made by this tool's own
    host-approval policy rather than by the remote server.
    """

    def __init__(self, message: str, *, result: str = "", blocked: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.result = result
        self.blocked = blocked
        self.http_status = http_status


def _is_login_url(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(LOGIN_PATH_RE.search(parsed.path or "")) or "redirect_to=" in (parsed.query or "")


class RestrictedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.hops: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        host = (urlsplit(target).hostname or "").casefold()
        self.hops.append(target)
        if _is_login_url(target):
            raise ArchiveError(
                f"The server redirected to its sign-in page ({urlsplit(target).path}) instead of serving the file, "
                "so this URL is not publicly readable.",
                result=f"HTTP {code} redirect to sign-in page",
                http_status=code,
            )
        if host not in self.allowed_hosts:
            raise ArchiveError(
                f"Redirect to unapproved host {host or '(missing host)'} was refused.",
                result=f"HTTP {code} redirect refused",
                blocked=True,
                http_status=code,
            )
        _reject_private_destination(host)
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected and host != (urlsplit(req.full_url).hostname or "").casefold():
            # Credentials belong to the WordPress host only; never let a
            # redirect carry them to a CDN or any other host.
            redirected.remove_header("Authorization")
        return redirected


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_with_retry(temporary: Path, target: Path) -> None:
    """os.replace that tolerates the brief Windows locks placed by antivirus and readers."""
    for attempt in range(FILE_WRITE_RETRIES):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if attempt == FILE_WRITE_RETRIES - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    for attempt in range(FILE_WRITE_RETRIES):
        try:
            temporary.write_text(encoded, encoding="utf-8")
            break
        except PermissionError:
            if attempt == FILE_WRITE_RETRIES - 1:
                raise
            time.sleep(0.25 * (attempt + 1))
    _replace_with_retry(temporary, path)


def _csv_safe(value) -> str:
    if isinstance(value, list):
        text = "; ".join(str(part) for part in value)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_safe(row.get(field)) for field in fields})
    _replace_with_retry(temporary, path)


def write_archive_reports(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    record_fields = (
        "wordpress_id", "post_type", "title", "status", "mime_type", "modified_gmt",
        "document_url", "current_attachment_id", "resolution", "linked_attachment_ids",
        "asset_ids", "error",
    )
    asset_fields = (
        "asset_id", "record_ids", "role", "url", "source_host", "fallback_urls", "relative_path",
        "status", "attempts", "bytes", "sha256", "content_type", "response_url", "result", "error",
    )
    records = list(manifest.get("records", []))
    assets = list(manifest.get("assets", []))
    _write_csv(root / "records.csv", record_fields, records)
    _write_csv(root / "assets.csv", asset_fields, assets)
    failures = [
        {
            "kind": "record",
            "wordpress_id": record.get("wordpress_id"),
            "title_or_url": record.get("title"),
            "status": record.get("resolution"),
            "reason": record.get("error"),
        }
        for record in records if not record.get("asset_ids")
    ]
    failures.extend(
        {
            "kind": "asset",
            "wordpress_id": asset.get("record_ids"),
            "title_or_url": asset.get("url"),
            "status": asset.get("status"),
            "reason": asset.get("error"),
        }
        for asset in assets if asset.get("status") in {"failed", "blocked"}
    )
    _write_csv(
        root / "failures.csv",
        ("kind", "wordpress_id", "title_or_url", "status", "reason"),
        failures,
    )
    summary = manifest.get("summary", {})
    summary_lines = [
        "WordPress Media Archive",
        f"Status: {manifest.get('status', 'unknown')}",
        f"Site: {manifest.get('site', {}).get('site_url', '')}",
        f"Created: {manifest.get('created_at', '')}",
        f"Completed: {manifest.get('completed_at', '')}",
        f"Source export: {manifest.get('source_export', {}).get('filename', '')}",
        f"Source export SHA-256: {manifest.get('source_export', {}).get('sha256', '')}",
        f"Attachment records: {summary.get('attachment_records', 0)}",
        f"Document records: {summary.get('document_records', 0)}",
        f"Files planned: {summary.get('assets', 0)}",
        f"Files completed: {summary.get('completed_assets', 0)}",
        f"Files failed: {summary.get('failed_assets', 0)}",
        f"Files blocked: {summary.get('blocked_assets', 0)}",
        f"Unresolved records: {summary.get('unresolved_records', 0)}",
        "",
        "See records.csv, assets.csv, failures.csv, and manifest.json for details.",
    ]
    temporary = root / "SUMMARY.txt.tmp"
    temporary.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    _replace_with_retry(temporary, root / "SUMMARY.txt")


def safe_name(value: str, fallback: str = "file") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", unquote(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    stem = Path(cleaned).stem.casefold()
    if stem in WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > 180:
        suffix = Path(cleaned).suffix[:20]
        cleaned = cleaned[: 180 - len(suffix)].rstrip(" .") + suffix
    return cleaned


def archive_folder_name(site_url: str, stamp: datetime | None = None) -> str:
    host = (urlsplit(site_url).hostname or "wordpress-site").casefold()
    moment = stamp or datetime.now(timezone.utc)
    return f"{safe_name(host, 'wordpress-site')}-media-{moment.strftime('%Y%m%d-%H%M%S')}"


def _valid_http_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    return parsed.geturl()


def _document_urls(value: str) -> set[str]:
    results = set()
    for raw in URL_RE.findall(html.unescape(value or "")):
        candidate = raw.rstrip(".,;:!?)]}")
        valid = _valid_http_url(candidate)
        if valid and PurePosixPath(urlsplit(valid).path).suffix.casefold() in DOCUMENT_EXTENSIONS:
            results.add(valid)
    return results


def _wsu_document_url(value: str) -> str:
    """Return a WSU Document Revisions file route, including legacy extensionless routes."""
    valid = _valid_http_url(value)
    if not valid:
        return ""
    path = urlsplit(valid).path
    if PurePosixPath(path).suffix.casefold() in DOCUMENT_EXTENSIONS:
        return valid
    if re.fullmatch(r"/documents/\d{4}/\d{2}/[^/]+/?", path, re.IGNORECASE):
        return valid
    return ""


def _original_image_url(attachment_url: str, metadata_values: list[str]) -> str:
    for metadata in metadata_values:
        match = ORIGINAL_IMAGE_RE.search(metadata or "")
        if match:
            return urljoin(attachment_url, match.group("name"))
    return ""


def _asset_filename(url: str, role: str, mime_type: str = "") -> str:
    filename = PurePosixPath(urlsplit(url).path).name
    if not filename:
        extension = mimetypes.guess_extension(mime_type.split(";", 1)[0]) or ""
        filename = f"{role}{extension}"
    return safe_name(filename, role)


def _attached_file(item) -> str:
    """Return the relative uploads path WordPress stores in ``_wp_attached_file``."""
    value = (item.meta.get("_wp_attached_file") or [""])[0].strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value.split("/"):
        return ""
    return value


def _uploads_base_url(attachments) -> str:
    """Infer the public uploads base URL from attachments whose URL ends with their stored path.

    WordPress writes ``_wp_attached_file`` (``2014/12/name.pdf``) for every
    attachment, but plugins such as WP Document Revisions replace the exported
    ``attachment_url`` with a permalink that is served through PHP and may
    require sign-in. Combining the base seen on ordinary uploads with the
    stored path recovers the directly served file.
    """
    counts: dict[str, int] = {}
    for item in attachments:
        attached = _attached_file(item)
        url = _valid_http_url(item.attachment_url)
        if attached and url and url.endswith("/" + attached):
            base = url[: -len(attached)]
            counts[base] = counts.get(base, 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda base: (counts[base], base))


def _archive_asset_file_type(asset: dict) -> str:
    suffix = PurePosixPath(urlsplit(str(asset.get("url") or "")).path).suffix.casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    mime_type = str(asset.get("expected_mime_type") or "").split(";", 1)[0].strip().casefold()
    return mime_type or "(no extension)"


def select_archive_test_assets(manifest: dict) -> list[dict]:
    """Select bounded coverage across record class, file type, host, and asset role."""
    document_ids = {
        str(record.get("wordpress_id") or "")
        for record in manifest.get("records", [])
        if record.get("post_type") == "document"
    }
    record_status = {
        str(record.get("wordpress_id") or ""): str(record.get("status") or "")
        for record in manifest.get("records", [])
    }
    fallback_asset_ids = {
        str(asset_id)
        for record in manifest.get("records", [])
        if record.get("resolution") == "document-link-to-verify"
        for asset_id in record.get("asset_ids", [])
    }
    selected: dict[str, dict] = {}

    def category(asset: dict) -> str:
        return (
            "document"
            if any(str(record_id) in document_ids for record_id in asset.get("record_ids", []))
            else "media"
        )

    def record_statuses(asset: dict) -> list[str]:
        return sorted({
            record_status.get(str(record_id), "") for record_id in asset.get("record_ids", [])
        } - {""})

    def include(asset: dict, reason: str) -> None:
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            return
        if asset_id not in selected:
            sample = dict(asset)
            sample["record_ids"] = list(asset.get("record_ids", []))
            sample["category"] = category(asset)
            sample["file_type"] = _archive_asset_file_type(asset)
            sample["record_statuses"] = record_statuses(asset)
            sample["test_reasons"] = []
            selected[asset_id] = sample
        if reason not in selected[asset_id]["test_reasons"]:
            selected[asset_id]["test_reasons"].append(reason)

    assets = sorted(
        manifest.get("assets", []),
        key=lambda asset: (
            str(asset.get("source_host") or ""),
            str(asset.get("url") or ""),
            str(asset.get("asset_id") or ""),
        ),
    )
    covered_type_groups: set[tuple[str, str, str]] = set()
    covered_role_groups: set[tuple[str, str]] = set()
    covered_status_groups: set[tuple[str, str]] = set()
    for asset in assets:
        group = (category(asset), _archive_asset_file_type(asset), str(asset.get("source_host") or ""))
        if group not in covered_type_groups:
            include(asset, "record class + file type + source host")
            covered_type_groups.add(group)
        role_group = (str(asset.get("role") or ""), str(asset.get("source_host") or ""))
        if role_group not in covered_role_groups:
            include(asset, "asset role + source host")
            covered_role_groups.add(role_group)
    # Draft, pending, and private records are gated by their WordPress
    # permalink, not by the stored upload; sample each status so the test
    # proves non-public files are archived without signing in. Unverified
    # document links are considered last so a real file represents a status.
    for asset in sorted(assets, key=lambda asset: str(asset.get("asset_id") or "") in fallback_asset_ids):
        for status in record_statuses(asset):
            status_group = (category(asset), status)
            if status_group not in covered_status_groups:
                include(asset, f"record status {status}")
                covered_status_groups.add(status_group)
        if str(asset.get("asset_id") or "") in fallback_asset_ids:
            include(asset, "unverified WSU document link")

    if len(selected) > MAX_TEST_ASSETS:
        raise ArchiveError(
            f"The representative test would require {len(selected)} files, exceeding the {MAX_TEST_ASSETS}-file test limit."
        )
    return list(selected.values())


def build_archive_plan(wxr_path: Path, archive_dir: Path, source_filename: str = "") -> dict:
    wxr_path = wxr_path.resolve()
    archive_dir = archive_dir.resolve()
    archive_dir.mkdir(parents=True, exist_ok=False)
    with wxr_path.open("rb") as stream:
        export = parse_wxr(stream)

    attachments = {item.id: item for item in export.items if item.post_type == "attachment"}
    document_posts = [item for item in export.items if item.post_type == "document"]
    document_ids = {item.id for item in document_posts}
    child_attachments: dict[str, list[str]] = {}
    for attachment in attachments.values():
        if attachment.parent_id in document_ids:
            child_attachments.setdefault(attachment.parent_id, []).append(attachment.id)
    assets: list[dict] = []
    asset_by_url: dict[str, dict] = {}
    asset_by_id: dict[str, dict] = {}
    records: list[dict] = []
    record_by_id: dict[str, dict] = {}

    uploads_base_url = _uploads_base_url(attachments.values())

    def add_asset(
        owner_id: str,
        role: str,
        url: str,
        mime_type: str = "",
        fallback_urls: list[str] | None = None,
        filename_hint: str = "",
    ) -> str:
        valid_url = _valid_http_url(url)
        if not valid_url:
            return ""
        existing = asset_by_url.get(valid_url)
        if existing:
            if owner_id not in existing["record_ids"]:
                existing["record_ids"].append(owner_id)
            return existing["asset_id"]
        fallbacks = []
        for candidate in fallback_urls or []:
            valid_fallback = _valid_http_url(candidate)
            if valid_fallback and valid_fallback != valid_url and valid_fallback not in fallbacks:
                fallbacks.append(valid_fallback)
        asset_id = hashlib.sha256(valid_url.encode("utf-8")).hexdigest()[:20]
        filename = _asset_filename(filename_hint or valid_url, role, mime_type)
        relative_path = str(
            Path("files") / safe_name(owner_id, "unknown-id")
            / f"{safe_name(role)}-{asset_id[:8]}-{filename}"
        )
        asset = {
            "asset_id": asset_id,
            "record_ids": [owner_id],
            "role": role,
            "url": valid_url,
            "source_host": (urlsplit(valid_url).hostname or "").casefold(),
            "fallback_urls": fallbacks,
            "expected_mime_type": mime_type,
            "relative_path": relative_path,
            "status": "pending",
            "attempts": 0,
            "bytes": 0,
            "sha256": "",
            "content_type": "",
            "response_url": "",
            "result": "",
            "error": "",
        }
        assets.append(asset)
        asset_by_url[valid_url] = asset
        asset_by_id[asset_id] = asset
        return asset_id

    for item in attachments.values():
        record = {
            "wordpress_id": item.id,
            "post_type": item.post_type,
            "title": item.title,
            "status": item.status,
            "mime_type": item.mime_type,
            "modified_gmt": item.modified_gmt,
            "asset_ids": [],
            "resolution": "attachment-url" if item.attachment_url else "unresolved",
            "error": "" if item.attachment_url else "The WXR attachment record has no attachment URL.",
        }
        # Prefer the directly served upload when the exported URL is a
        # plugin permalink (for example a WP Document Revisions
        # ``-revision-N`` route that answers 403 unless signed in). The
        # exported URL stays as an authenticated fallback.
        attached = _attached_file(item)
        stored_url = urljoin(uploads_base_url, attached) if uploads_base_url and attached else ""
        primary_url = item.attachment_url
        fallback_urls: list[str] = []
        if stored_url and item.attachment_url and stored_url != item.attachment_url:
            record["resolution"] = "stored-upload-url"
            primary_url = stored_url
            fallback_urls = [item.attachment_url]
        primary_id = add_asset(
            item.id, "current-original", primary_url, item.mime_type,
            fallback_urls=fallback_urls, filename_hint=item.attachment_url,
        )
        if primary_id:
            record["asset_ids"].append(primary_id)
        original_url = _original_image_url(
            item.attachment_url, item.meta.get("_wp_attachment_metadata", [])
        ) if item.attachment_url else ""
        if original_url and original_url != item.attachment_url:
            original_id = add_asset(item.id, "pre-edit-original", original_url, item.mime_type)
            if original_id and original_id not in record["asset_ids"]:
                record["asset_ids"].append(original_id)
        records.append(record)
        record_by_id[item.id] = record

    for item in document_posts:
        record = {
            "wordpress_id": item.id,
            "post_type": item.post_type,
            "title": item.title,
            "status": item.status,
            "mime_type": item.mime_type,
            "modified_gmt": item.modified_gmt,
            "document_url": item.url,
            "current_attachment_id": "",
            "asset_ids": [],
            "linked_attachment_ids": [],
            "resolution": "unresolved",
            "error": "The document record did not expose an attachment ID or direct document URL in the WXR export.",
        }
        direct_urls: set[str] = set()
        used_document_link_fallback = False
        current_attachment_id = wordpress_id(item.content.strip())
        if current_attachment_id:
            record["current_attachment_id"] = current_attachment_id
        attachment_candidates = []
        if current_attachment_id in attachments:
            attachment_candidates.append(current_attachment_id)
        attachment_candidates.extend(child_attachments.get(item.id, []))
        for key, values in item.meta.items():
            key_has_file_hint = any(hint in key.casefold() for hint in FILE_META_HINTS)
            for value in values:
                direct_urls.update(_document_urls(value))
                possible_id = value.strip()
                if key_has_file_hint and possible_id in attachments:
                    attachment_candidates.append(possible_id)
        for attachment_id in attachment_candidates:
            if attachment_id in record["linked_attachment_ids"]:
                continue
            linked = record_by_id[attachment_id]
            record["linked_attachment_ids"].append(attachment_id)
            for asset_id in linked["asset_ids"]:
                if asset_id not in record["asset_ids"]:
                    record["asset_ids"].append(asset_id)
                asset = asset_by_id[asset_id]
                if item.id not in asset["record_ids"]:
                    asset["record_ids"].append(item.id)
        direct_urls.update(_document_urls(item.content))
        if not record["asset_ids"] and not direct_urls:
            fallback_url = _wsu_document_url(item.url)
            if fallback_url:
                direct_urls.add(fallback_url)
                used_document_link_fallback = True
        for url in sorted(direct_urls):
            asset_id = add_asset(item.id, "document-file", url)
            if asset_id and asset_id not in record["asset_ids"]:
                record["asset_ids"].append(asset_id)
        if record["asset_ids"]:
            record["resolution"] = "attachment-reference" if record["linked_attachment_ids"] else "direct-url"
            if used_document_link_fallback and not record["linked_attachment_ids"]:
                record["resolution"] = "document-link-to-verify"
            if record["linked_attachment_ids"] and direct_urls:
                record["resolution"] = "attachment-reference-and-direct-url"
            record["error"] = ""
        records.append(record)

    source_hosts = sorted(
        {asset["source_host"] for asset in assets if asset["source_host"]}
        | {
            (urlsplit(url).hostname or "").casefold()
            for asset in assets for url in asset.get("fallback_urls", [])
            if urlsplit(url).hostname
        }
    )
    unresolved = [record for record in records if not record["asset_ids"]]
    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "created_at": utc_now(),
        "started_at": "",
        "completed_at": "",
        "status": "planned",
        "site": {
            "title": export.site_title,
            "site_url": export.site_url,
            "home_url": export.home_url,
        },
        "source_export": {
            "filename": source_filename or wxr_path.name,
            "sha256": file_sha256(wxr_path),
        },
        "summary": {
            "attachment_records": len(attachments),
            "document_records": len(document_posts),
            "document_links_to_verify": sum(
                record.get("resolution") == "document-link-to-verify" for record in records
            ),
            "stored_upload_urls": sum(
                record.get("resolution") == "stored-upload-url" for record in records
            ),
            "uploads_base_url": uploads_base_url,
            "assets": len(assets),
            "unresolved_records": len(unresolved),
            "completed_assets": 0,
            "failed_assets": 0,
            "blocked_assets": 0,
        },
        "source_hosts": source_hosts,
        "approved_hosts": [],
        "warnings": list(export.warnings),
        "records": records,
        "assets": assets,
    }
    write_json_atomic(archive_dir / "manifest.json", manifest)
    write_archive_reports(archive_dir, manifest)
    return manifest


def _reject_private_destination(host: str) -> None:
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise ArchiveError(f"Could not resolve source host {host}: {exc}") from exc
    if any(
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
        for address in addresses
    ):
        raise ArchiveError(f"Private or local network destination {host} was refused.")


def _destination(root: Path, relative_path: str) -> Path:
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ArchiveError("An unsafe archive destination path was refused.")
    return target


def _html_page_summary(body: bytes) -> str:
    """Return the title and first sentence of an HTML error page for reporting."""
    text = body.decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip() if title_match else ""
    body_text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))).strip()
    if title and body_text.startswith(title):
        body_text = body_text[len(title):].strip()
    snippet = body_text[:160].strip()
    parts = [part for part in (title, snippet) if part]
    return " — ".join(parts)


def _describe_http_error(exc: HTTPError, source_host: str, wordpress_host: str, credentials_sent: bool) -> str:
    reason = f"HTTP {exc.code} {exc.reason}".strip()
    try:
        body = exc.read(ERROR_BODY_SNIFF_BYTES)
    except (OSError, ValueError):
        body = b""
    content_type = (exc.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    detail = _html_page_summary(body) if content_type == "text/html" or HTML_PREFIX_RE.match(body or b"") else ""
    message = reason
    if detail:
        message += f". The server answered with an HTML page: {detail}"
    if exc.code in {401, 403}:
        if source_host == wordpress_host and credentials_sent:
            message += ". The WordPress site rejected the supplied Application Password for this file."
        elif source_host == wordpress_host:
            message += (
                ". The WordPress host requires sign-in for this URL; retry with a WordPress username "
                "and Application Password if the file should be archived."
            )
        else:
            message += f". The host {source_host} refused the request; this URL is not publicly served."
    elif exc.code == 404:
        message += ". The file no longer exists at this URL."
    return message


def _fetch_candidate(
    asset: dict,
    url: str,
    root: Path,
    allowed_hosts: set[str],
    wordpress_host: str,
    username: str,
    application_password: str,
) -> None:
    """Stream one URL into the asset's destination and verify it, or raise ArchiveError."""
    source_host = (urlsplit(url).hostname or "").casefold()
    if source_host not in allowed_hosts:
        raise ArchiveError(
            f"Source host {source_host or '(missing host)'} was not approved.",
            result="not attempted: host not approved",
            blocked=True,
        )
    _reject_private_destination(source_host)
    headers = {"User-Agent": "WSU-WordPress-Media-Archive/1.0", "Accept": "*/*"}
    credentials_sent = bool(username and application_password and source_host == wordpress_host)
    if credentials_sent:
        credentials = base64.b64encode(f"{username}:{application_password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"
    redirect_handler = RestrictedRedirectHandler(allowed_hosts)
    opener = build_opener(redirect_handler)
    request = Request(url, headers=headers, method="GET")
    target = _destination(root, asset["relative_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200) or 200)
            final_url = response.geturl()
            final_host = (urlsplit(final_url).hostname or "").casefold()
            result = f"HTTP {status} {getattr(response, 'reason', '') or 'OK'}".strip()
            if final_host not in allowed_hosts:
                raise ArchiveError(
                    f"Final response host {final_host or '(missing host)'} was not approved.",
                    result=result, blocked=True, http_status=status,
                )
            length_header = response.headers.get("Content-Length")
            expected_length = int(length_header) if length_header and length_header.isdigit() else None
            if expected_length is not None and expected_length > MAX_ARCHIVE_FILE_BYTES:
                raise ArchiveError("The file exceeds the 5 GB per-file archive limit.", result=result, http_status=status)
            if expected_length is not None and shutil.disk_usage(root).free < expected_length + DOWNLOAD_CHUNK_BYTES:
                raise ArchiveError(
                    "The archive destination does not have enough free space for this file.",
                    result=result, http_status=status,
                )
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            expected_type = str(asset.get("expected_mime_type") or "").casefold()
            html_expected = expected_type == "text/html" or Path(target).suffix.casefold() in {".htm", ".html"}
            if content_type == "text/html" and not html_expected:
                raise ArchiveError(
                    "The URL returned an HTML page (Content-Type text/html) instead of the expected media or document.",
                    result=result, http_status=status,
                )
            with temporary.open("wb") as output:
                first_chunk = True
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    if first_chunk and not html_expected and HTML_PREFIX_RE.match(chunk[:512]):
                        raise ArchiveError(
                            "The response body is an HTML page (it begins with an HTML document tag) rather than the expected file.",
                            result=result, http_status=status,
                        )
                    first_chunk = False
                    total += len(chunk)
                    if total > MAX_ARCHIVE_FILE_BYTES:
                        raise ArchiveError("The file exceeds the 5 GB per-file archive limit.", result=result, http_status=status)
                    output.write(chunk)
                    digest.update(chunk)
            if expected_length is not None and total != expected_length:
                raise ArchiveError(
                    f"The response ended at {total} bytes; {expected_length} bytes were expected.",
                    result=result, http_status=status,
                )
            if total == 0:
                raise ArchiveError("The file response was empty (0 bytes).", result=result, http_status=status)
            _replace_with_retry(temporary, target)
            if not target.is_file() or target.stat().st_size != total:
                raise ArchiveError(
                    f"The downloaded file could not be verified at {target}.", result=result, http_status=status,
                )
            asset.update({
                "status": "completed",
                "bytes": total,
                "sha256": digest.hexdigest(),
                "content_type": content_type,
                "response_url": final_url,
                "downloaded_url": url,
                "http_status": status,
                "result": result,
                "error": "",
                "completed_at": utc_now(),
            })
    except HTTPError as exc:
        temporary.unlink(missing_ok=True)
        raise ArchiveError(
            _describe_http_error(exc, source_host, wordpress_host, credentials_sent),
            result=f"HTTP {exc.code} {exc.reason}".strip(),
            http_status=exc.code,
        ) from exc
    except ArchiveError:
        temporary.unlink(missing_ok=True)
        raise
    except (TimeoutError, socket.timeout) as exc:
        temporary.unlink(missing_ok=True)
        raise ArchiveError(
            f"The download timed out after {DOWNLOAD_TIMEOUT_SECONDS} seconds without a complete response.",
            result="network timeout",
        ) from exc
    except URLError as exc:
        temporary.unlink(missing_ok=True)
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise ArchiveError(
                f"The connection timed out after {DOWNLOAD_TIMEOUT_SECONDS} seconds.", result="network timeout",
            ) from exc
        raise ArchiveError(f"Network error: {reason}", result="network error") from exc
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ArchiveError(f"Local file error: {exc}", result="local file error") from exc


def _download_asset(
    asset: dict,
    root: Path,
    allowed_hosts: set[str],
    wordpress_host: str,
    username: str,
    application_password: str,
) -> None:
    """Download the asset from its primary URL, then any fallback URL, and record every attempt."""
    candidates = [asset["url"]]
    for fallback in asset.get("fallback_urls", []) or []:
        if fallback and fallback not in candidates:
            candidates.append(fallback)
    attempts: list[dict] = []
    asset["attempted_urls"] = attempts
    failures: list[ArchiveError] = []
    for url in candidates:
        try:
            _fetch_candidate(asset, url, root, allowed_hosts, wordpress_host, username, application_password)
        except ArchiveError as exc:
            attempts.append({"url": url, "result": exc.result or "failed", "error": str(exc)})
            failures.append(exc)
            continue
        attempts.append({"url": url, "result": asset.get("result", ""), "error": ""})
        return
    primary = failures[0]
    if len(failures) == 1:
        asset["http_status"] = primary.http_status
        asset["result"] = primary.result
        raise primary
    summary = "; ".join(
        f"{urlsplit(attempt['url']).hostname}{urlsplit(attempt['url']).path}: {attempt['error']}"
        for attempt in attempts
    )
    combined = ArchiveError(
        f"Every source URL failed. {summary}",
        result=" / ".join(attempt["result"] for attempt in attempts),
        blocked=all(failure.blocked for failure in failures),
        http_status=primary.http_status,
    )
    asset["http_status"] = primary.http_status
    asset["result"] = combined.result
    raise combined


def archive_test_from_manifest(
    manifest_path: Path,
    approved_hosts: set[str],
    username: str = "",
    application_password: str = "",
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    """Download a representative sample without changing the full archive manifest."""
    manifest_path = manifest_path.resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hosts = {str(host).casefold() for host in manifest.get("source_hosts", [])}
    allowed_hosts = {str(host).strip().casefold() for host in approved_hosts if str(host).strip()}
    if not allowed_hosts or not allowed_hosts.issubset(source_hosts):
        raise ArchiveError("Choose one or more source hosts shown in the archive plan.")

    samples = select_archive_test_assets(manifest)
    if not samples:
        raise ArchiveError("The archive plan has no files available for a test download.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    wordpress_host = (urlsplit(manifest.get("site", {}).get("site_url", "")).hostname or "").casefold()
    for asset in samples:
        filename = Path(str(asset.get("relative_path") or "sample-file")).name
        asset["relative_path"] = str(
            Path("test-downloads") / run_id / safe_name(asset["category"])
            / safe_name(asset["file_type"], "unknown-type")
            / f"{asset['asset_id'][:8]}-{filename}"
        )
        _reset_asset_progress(asset)

    unapproved_hosts = sorted(
        {str(asset.get("source_host") or "") for asset in samples} - allowed_hosts - {""}
    )
    result = {
        "schema_version": 2,
        "run_id": run_id,
        "created_at": utc_now(),
        "completed_at": "",
        "status": "running",
        "approved_hosts": sorted(allowed_hosts),
        "unapproved_sample_hosts": unapproved_hosts,
        "download_root": str(root / "test-downloads" / run_id),
        "summary": {
            "selected_assets": len(samples),
            "completed_assets": 0,
            "failed_assets": 0,
            "blocked_assets": 0,
            "categories": sorted({asset["category"] for asset in samples}),
            "file_types": sorted({asset["file_type"] for asset in samples}),
            "source_hosts": sorted({str(asset.get("source_host") or "") for asset in samples}),
            "record_statuses": sorted({
                status for asset in samples for status in asset.get("record_statuses", [])
            }),
        },
        "assets": samples,
    }
    result_path = root / "test-results.json"
    write_json_atomic(result_path, result)

    def finalize(status_override: str = "", error: str = "") -> None:
        completed = sum(asset.get("status") == "completed" for asset in samples)
        failed = sum(asset.get("status") == "failed" for asset in samples)
        blocked = sum(asset.get("status") == "blocked" for asset in samples)
        for asset in samples:
            if asset.get("status") in {"pending", "downloading"}:
                asset["status"] = "not-tested"
                asset["error"] = asset.get("error") or "The test stopped before this file was attempted."
        result["summary"].update({
            "completed_assets": completed,
            "failed_assets": failed,
            "blocked_assets": blocked,
            "not_tested_assets": sum(asset.get("status") == "not-tested" for asset in samples),
            "http_results": sorted({str(asset.get("result") or "") for asset in samples} - {""}),
        })
        result["completed_at"] = utc_now()
        result["error"] = error
        result["status"] = status_override or ("passed" if completed == len(samples) else "failed")
        write_json_atomic(result_path, result)
        _write_csv(
            root / "test-assets.csv",
            (
                "asset_id", "category", "file_type", "record_statuses", "test_reasons", "record_ids",
                "role", "url", "source_host", "fallback_urls", "downloaded_url", "relative_path", "status",
                "http_status", "result", "bytes", "sha256", "content_type", "response_url", "error",
            ),
            samples,
        )

    try:
        for index, asset in enumerate(samples, start=1):
            asset["attempts"] = 1
            asset["status"] = "downloading"
            write_json_atomic(result_path, result)
            if progress:
                progress(
                    index - 1, len(samples),
                    f"Testing {asset['category']} {asset['file_type']} from {asset.get('source_host') or 'unknown host'}",
                )
            try:
                _download_asset(
                    asset, root, allowed_hosts, wordpress_host, username, application_password
                )
            except ArchiveError as exc:
                asset["status"] = "blocked" if exc.blocked else "failed"
                asset["error"] = str(exc)
                if "unverified WSU document link" in asset.get("test_reasons", []):
                    asset["error"] += (
                        " This document record exported no attachment, so the URL was only a candidate; "
                        "the record stays unresolved in the complete archive unless the file is served."
                    )
                asset["result"] = exc.result or asset.get("result") or "failed"
                asset["http_status"] = exc.http_status
                asset["completed_at"] = utc_now()
            write_json_atomic(result_path, result)
            if progress:
                progress(index, len(samples), f"Tested {index} of {len(samples)} representative files")
    except BaseException as exc:
        # Persist whatever was learned before re-raising, so a crash still
        # leaves an inspectable test-results.json and test-assets.csv.
        finalize(status_override="error", error=f"{type(exc).__name__}: {exc}")
        raise
    finalize()
    return result


def _reset_asset_progress(asset: dict) -> None:
    asset.update({
        "status": "pending",
        "attempts": 0,
        "bytes": 0,
        "sha256": "",
        "content_type": "",
        "response_url": "",
        "downloaded_url": "",
        "http_status": None,
        "result": "",
        "attempted_urls": [],
        "error": "",
    })


def archive_from_manifest(
    manifest_path: Path,
    approved_hosts: set[str],
    username: str = "",
    application_password: str = "",
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    manifest_path = manifest_path.resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hosts = {str(host).casefold() for host in manifest.get("source_hosts", [])}
    allowed_hosts = {str(host).strip().casefold() for host in approved_hosts if str(host).strip()}
    if not allowed_hosts or not allowed_hosts.issubset(source_hosts):
        raise ArchiveError("Choose one or more source hosts shown in the archive plan.")
    wordpress_host = (urlsplit(manifest.get("site", {}).get("site_url", "")).hostname or "").casefold()
    manifest["approved_hosts"] = sorted(allowed_hosts)
    manifest["status"] = "running"
    manifest["started_at"] = manifest.get("started_at") or utc_now()
    write_json_atomic(manifest_path, manifest)
    assets = manifest.get("assets", [])
    total_assets = len(assets)

    for index, asset in enumerate(assets, start=1):
        target = _destination(root, asset["relative_path"])
        if asset.get("status") == "completed" and target.exists():
            if asset.get("sha256") and file_sha256(target) == asset["sha256"]:
                if progress:
                    progress(index, total_assets, f"Verified existing {target.name}")
                continue
        asset["attempts"] = int(asset.get("attempts") or 0) + 1
        asset["status"] = "downloading"
        asset["error"] = ""
        write_json_atomic(manifest_path, manifest)
        if progress:
            progress(index - 1, total_assets, f"Downloading {Path(asset['relative_path']).name}")
        try:
            _download_asset(
                asset, root, allowed_hosts, wordpress_host, username, application_password
            )
        except ArchiveError as exc:
            asset["status"] = "blocked" if exc.blocked else "failed"
            asset["error"] = str(exc)
            asset["result"] = exc.result or asset.get("result") or "failed"
            asset["http_status"] = exc.http_status
            asset["completed_at"] = utc_now()
        write_json_atomic(manifest_path, manifest)
        if progress:
            progress(index, total_assets, f"Processed {index} of {total_assets} files")

    completed = sum(asset.get("status") == "completed" for asset in assets)
    failed = sum(asset.get("status") == "failed" for asset in assets)
    blocked = sum(asset.get("status") == "blocked" for asset in assets)
    unresolved = sum(not record.get("asset_ids") for record in manifest.get("records", []))
    manifest["summary"].update({
        "completed_assets": completed,
        "failed_assets": failed,
        "blocked_assets": blocked,
        "unresolved_records": unresolved,
    })
    manifest["completed_at"] = utc_now()
    manifest["status"] = "completed" if completed == total_assets and not unresolved else "incomplete"
    write_json_atomic(manifest_path, manifest)
    write_archive_reports(root, manifest)
    return manifest

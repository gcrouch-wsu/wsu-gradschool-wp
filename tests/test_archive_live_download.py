"""Real-network downloader tests.

These run archive_tool against a live local HTTP server, so they fail if the
production download path never writes a file. ``127.0.0.1`` plays the
WordPress host and ``127.0.0.2`` plays the CDN so credential scoping and
host approval can be observed per host.
"""
from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import archive_tool
import pytest
from archive_tool import build_archive_plan


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"
WP_403_PAGE = (
    b"<!DOCTYPE html>\n<html lang=\"en-US\"><head><title>WordPress &rsaquo; Error</title>"
    b"<style>body{}</style></head><body><p>You are not authorized to access that file.</p></body></html>"
)


class _ArchiveTestHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str | None]] = []

    def log_message(self, *_args):
        return None

    def _send(self, status, body=b"", content_type="application/octet-stream", headers=None, length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        type(self).seen.append((host, self.path, self.headers.get("Authorization")))
        path = self.path.split("?", 1)[0]
        if path == "/uploads/2014/12/stored.pdf":
            return self._send(200, PDF_BYTES, "binary/octet-stream")
        if path == "/uploads/2014/06/photo.jpg":
            return self._send(200, JPEG_BYTES, "binary/octet-stream")
        if path == "/documents/2014/12/policy-revision-1.pdf":
            if self.headers.get("Authorization"):
                return self._send(200, PDF_BYTES, "application/pdf")
            return self._send(403, WP_403_PAGE, "text/html; charset=UTF-8")
        if path == "/documents/2023/09/legacy":
            return self._send(302, b"", "text/html", {"Location": "/login?redirect_to=/documents/2023/09/legacy"})
        if path == "/login":
            return self._send(200, b"<!doctype html><html><body>Sign in</body></html>", "text/html")
        if path == "/hop.pdf":
            return self._send(302, b"", "text/html", {"Location": "https://evil.example/hop.pdf"})
        if path == "/html-as.pdf":
            return self._send(200, b"<!DOCTYPE html><html><body>Not found</body></html>", "application/pdf")
        if path == "/empty.pdf":
            return self._send(200, b"", "application/pdf")
        if path == "/truncated.pdf":
            return self._send(200, PDF_BYTES[:10], "application/pdf", length=len(PDF_BYTES))
        return self._send(404, b"<html><body>Not Found</body></html>", "text/html")


class _QuietBindServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind reverse-resolves the bound address, which
        # stalls for seconds on secondary loopback addresses under Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


@pytest.fixture
def live_server(monkeypatch):
    """Two loopback listeners so the WordPress host and CDN host are distinct."""
    _ArchiveTestHandler.seen = []
    servers = []
    for address in ("127.0.0.1", "127.0.0.2"):
        server = _QuietBindServer((address, 0), _ArchiveTestHandler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(server)
    # Loopback is refused in production; the live-server tests need it.
    monkeypatch.setattr(archive_tool, "_reject_private_destination", lambda _host: None)
    wp_port, cdn_port = (server.server_address[1] for server in servers)
    yield {
        "wp": f"http://127.0.0.1:{wp_port}",
        "cdn": f"http://127.0.0.2:{cdn_port}",
        "seen": _ArchiveTestHandler.seen,
        "hosts": {"127.0.0.1", "127.0.0.2"},
    }
    for server in servers:
        server.shutdown()
        server.server_close()


def _asset(url, relative_path="files/1/current-original-sample.pdf", **extra):
    asset = {
        "asset_id": "abc123",
        "url": url,
        "relative_path": relative_path,
        "expected_mime_type": "application/pdf",
        "fallback_urls": [],
    }
    asset.update(extra)
    return asset


def test_real_downloader_writes_verified_file_from_cdn_without_credentials(tmp_path, live_server):
    asset = _asset(f"{live_server['cdn']}/uploads/2014/12/stored.pdf")
    archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "user", "app-pass")

    saved = tmp_path / asset["relative_path"]
    assert saved.is_file() and saved.stat().st_size == len(PDF_BYTES) > 0
    assert saved.read_bytes() == PDF_BYTES
    assert asset["status"] == "completed"
    assert asset["sha256"] == archive_tool.file_sha256(saved)
    assert asset["http_status"] == 200 and asset["result"].startswith("HTTP 200")
    assert asset["downloaded_url"] == asset["url"]
    assert not list(tmp_path.rglob("*.part"))
    # The CDN host never receives the WordPress credentials.
    assert live_server["seen"] == [("127.0.0.2", "/uploads/2014/12/stored.pdf", None)]


def test_real_downloader_reports_403_from_wordpress_host_and_suggests_credentials(tmp_path, live_server):
    asset = _asset(f"{live_server['wp']}/documents/2014/12/policy-revision-1.pdf")
    with pytest.raises(archive_tool.ArchiveError) as failure:
        archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "", "")

    message = str(failure.value)
    assert failure.value.http_status == 403
    assert failure.value.result == "HTTP 403 Forbidden"
    assert not failure.value.blocked
    assert "WordPress › Error" in message
    assert "You are not authorized to access that file." in message
    assert "Application Password" in message
    assert not (tmp_path / asset["relative_path"]).exists()
    assert asset["attempted_urls"][0]["result"] == "HTTP 403 Forbidden"


def test_real_downloader_sends_credentials_only_to_wordpress_host(tmp_path, live_server):
    asset = _asset(f"{live_server['wp']}/documents/2014/12/policy-revision-1.pdf")
    archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "user", "app-pass")

    assert (tmp_path / asset["relative_path"]).read_bytes() == PDF_BYTES
    assert asset["status"] == "completed"
    host, path, authorization = live_server["seen"][-1]
    assert (host, path) == ("127.0.0.1", "/documents/2014/12/policy-revision-1.pdf")
    assert authorization.startswith("Basic ")
    assert "app-pass" not in authorization
    assert "app-pass" not in json.dumps(asset) and "user" not in json.dumps(asset)


def test_real_downloader_falls_back_to_revision_permalink_with_credentials(tmp_path, live_server):
    asset = _asset(
        f"{live_server['cdn']}/uploads/2014/12/missing.pdf",
        fallback_urls=[f"{live_server['wp']}/documents/2014/12/policy-revision-1.pdf"],
    )
    archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "user", "app-pass")

    assert asset["status"] == "completed"
    assert asset["downloaded_url"].endswith("/documents/2014/12/policy-revision-1.pdf")
    assert [attempt["result"] for attempt in asset["attempted_urls"]] == ["HTTP 404 Not Found", "HTTP 200 OK"]
    assert (tmp_path / asset["relative_path"]).read_bytes() == PDF_BYTES
    assert [(host, auth is not None) for host, _path, auth in live_server["seen"]] == [
        ("127.0.0.2", False), ("127.0.0.1", True),
    ]


def test_real_downloader_reports_every_failed_candidate(tmp_path, live_server):
    asset = _asset(
        f"{live_server['cdn']}/uploads/2014/12/missing.pdf",
        fallback_urls=[f"{live_server['wp']}/documents/2014/12/policy-revision-1.pdf"],
    )
    with pytest.raises(archive_tool.ArchiveError, match="Every source URL failed") as failure:
        archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "", "")
    assert failure.value.result == "HTTP 404 Not Found / HTTP 403 Forbidden"
    assert failure.value.http_status == 404
    assert "missing.pdf: HTTP 404" in str(failure.value)
    assert "policy-revision-1.pdf: HTTP 403" in str(failure.value)
    assert not (tmp_path / asset["relative_path"]).exists()


def test_real_downloader_reports_redirect_to_login_page(tmp_path, live_server):
    asset = _asset(f"{live_server['wp']}/documents/2023/09/legacy", expected_mime_type="")
    with pytest.raises(archive_tool.ArchiveError, match="sign-in page") as failure:
        archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "", "")
    assert failure.value.result == "HTTP 302 redirect to sign-in page"
    assert not failure.value.blocked
    # The login page itself was never requested.
    assert [path for _host, path, _auth in live_server["seen"]] == ["/documents/2023/09/legacy"]
    assert not (tmp_path / asset["relative_path"]).exists()


def test_real_downloader_refuses_redirect_to_unapproved_host(tmp_path, live_server):
    asset = _asset(f"{live_server['wp']}/hop.pdf")
    with pytest.raises(archive_tool.ArchiveError, match="unapproved host evil.example") as failure:
        archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "user", "app-pass")
    assert failure.value.blocked
    assert failure.value.result == "HTTP 302 redirect refused"
    assert not (tmp_path / asset["relative_path"]).exists()


def test_real_downloader_blocks_unapproved_source_host_before_any_request(tmp_path, live_server):
    asset = _asset(f"{live_server['cdn']}/uploads/2014/12/stored.pdf")
    with pytest.raises(archive_tool.ArchiveError, match="not approved") as failure:
        archive_tool._download_asset(asset, tmp_path, {"127.0.0.1"}, "127.0.0.1", "", "")
    assert failure.value.blocked
    assert live_server["seen"] == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/html-as.pdf", "HTML page"),
        ("/empty.pdf", "empty"),
        ("/truncated.pdf", "bytes were expected"),
    ],
)
def test_real_downloader_rejects_html_empty_and_truncated_bodies(tmp_path, live_server, path, expected):
    asset = _asset(f"{live_server['cdn']}{path}")
    with pytest.raises(archive_tool.ArchiveError, match=expected):
        archive_tool._download_asset(asset, tmp_path, live_server["hosts"], "127.0.0.1", "", "")
    assert not (tmp_path / asset["relative_path"]).exists()
    assert not list(tmp_path.rglob("*.part"))


def _blank_progress() -> dict:
    return {
        "status": "pending", "attempts": 0, "bytes": 0, "sha256": "", "content_type": "",
        "response_url": "", "result": "", "error": "",
    }


def _live_manifest(tmp_path, live_server, hosts):
    """Write a schema-current manifest pointing at the live server."""
    site_url = live_server["wp"]
    assets = [
        {
            "asset_id": "cdnpdf01", "record_ids": ["1", "4"], "role": "current-original",
            "url": f"{live_server['cdn']}/uploads/2014/12/stored.pdf", "source_host": "127.0.0.2",
            "fallback_urls": [f"{live_server['wp']}/documents/2014/12/policy-revision-1.pdf"],
            "expected_mime_type": "application/pdf",
            "relative_path": "files/1/current-original-cdnpdf01-policy-revision-1.pdf",
            **_blank_progress(),
        },
        {
            "asset_id": "cdnjpg01", "record_ids": ["2"], "role": "current-original",
            "url": f"{live_server['cdn']}/uploads/2014/06/photo.jpg", "source_host": "127.0.0.2",
            "fallback_urls": [], "expected_mime_type": "image/jpeg",
            "relative_path": "files/2/current-original-cdnjpg01-photo.jpg",
            **_blank_progress(),
        },
        {
            "asset_id": "legacy01", "record_ids": ["3"], "role": "document-file",
            "url": f"{live_server['wp']}/documents/2023/09/legacy", "source_host": "127.0.0.1",
            "fallback_urls": [], "expected_mime_type": "",
            "relative_path": "files/3/document-file-legacy01-legacy",
            **_blank_progress(),
        },
    ]
    manifest = {
        "schema_version": archive_tool.ARCHIVE_SCHEMA_VERSION,
        "created_at": archive_tool.utc_now(), "started_at": "", "completed_at": "", "status": "planned",
        "site": {"title": "Live", "site_url": site_url, "home_url": site_url},
        "source_export": {"filename": "live.xml", "sha256": "0" * 64},
        "summary": {
            "attachment_records": 2, "document_records": 2, "document_links_to_verify": 1,
            "assets": 3, "unresolved_records": 0, "completed_assets": 0, "failed_assets": 0,
            "blocked_assets": 0,
        },
        "source_hosts": sorted(hosts),
        "approved_hosts": [],
        "warnings": [],
        "records": [
            {"wordpress_id": "1", "post_type": "attachment", "resolution": "stored-upload-url", "asset_ids": ["cdnpdf01"]},
            {"wordpress_id": "2", "post_type": "attachment", "resolution": "attachment-url", "asset_ids": ["cdnjpg01"]},
            {"wordpress_id": "3", "post_type": "document", "resolution": "document-link-to-verify", "asset_ids": ["legacy01"]},
            {"wordpress_id": "4", "post_type": "document", "resolution": "attachment-reference", "asset_ids": ["cdnpdf01"]},
        ],
        "assets": assets,
    }
    root = tmp_path / "archive"
    root.mkdir()
    archive_tool.write_json_atomic(root / "manifest.json", manifest)
    return root / "manifest.json"


def test_representative_test_downloads_real_files_and_leaves_manifest_untouched(tmp_path, live_server):
    manifest_path = _live_manifest(tmp_path, live_server, live_server["hosts"])
    before = manifest_path.read_bytes()

    result = archive_tool.archive_test_from_manifest(manifest_path, live_server["hosts"])

    assert result["status"] == "failed"
    assert result["summary"]["completed_assets"] == 2
    assert result["summary"]["failed_assets"] == 1
    by_id = {asset["asset_id"]: asset for asset in result["assets"]}
    for asset_id in ("cdnpdf01", "cdnjpg01"):
        saved = tmp_path / "archive" / by_id[asset_id]["relative_path"]
        assert saved.is_file() and saved.stat().st_size > 0
        assert by_id[asset_id]["sha256"] == archive_tool.file_sha256(saved)
        assert saved.parent.parent.parent.name == result["run_id"]
        assert saved.parent.parent.parent.parent.name == "test-downloads"
    assert by_id["cdnpdf01"]["category"] == "document" and by_id["cdnjpg01"]["category"] == "media"
    legacy = by_id["legacy01"]
    assert legacy["status"] == "failed"
    assert legacy["result"] == "HTTP 302 redirect to sign-in page"
    assert legacy["http_status"] == 302
    assert legacy["category"] == "document" and legacy["file_type"] == "(no extension)"
    # The full-archive plan is byte-for-byte unchanged and no files landed under files/.
    assert manifest_path.read_bytes() == before
    assert not (tmp_path / "archive" / "files").exists()
    csv_text = (tmp_path / "archive" / "test-assets.csv").read_text(encoding="utf-8-sig")
    assert "http_status" in csv_text and "HTTP 302 redirect to sign-in page" in csv_text


def test_representative_test_persists_useful_results_when_every_download_fails(tmp_path, live_server):
    manifest_path = _live_manifest(tmp_path, live_server, live_server["hosts"])
    # Approve only the WordPress host: CDN samples are blocked, the legacy link fails.
    result = archive_tool.archive_test_from_manifest(manifest_path, {"127.0.0.1"})

    assert result["status"] == "failed"
    assert result["summary"]["completed_assets"] == 0
    assert result["summary"]["blocked_assets"] == 1
    assert result["summary"]["failed_assets"] == 2
    assert result["unapproved_sample_hosts"] == ["127.0.0.2"]
    assert result["approved_hosts"] == ["127.0.0.1"]
    saved = json.loads((tmp_path / "archive" / "test-results.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    for asset in saved["assets"]:
        assert asset["status"] in {"blocked", "failed"}
        assert asset["error"] and asset["result"]
        assert asset["category"] in {"document", "media"}
        assert asset["source_host"] and asset["url"]
    download_root = Path(result["download_root"])
    assert not any(path.is_file() for path in download_root.rglob("*"))
    rows = (tmp_path / "archive" / "test-assets.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(rows) == 4
    # A fallback on an approved host is still attempted (without credentials it
    # answers 403, which counts as failed rather than blocked); the photo has no
    # fallback so it is blocked before any request.
    assert sorted(path for _host, path, _auth in live_server["seen"]) == [
        "/documents/2014/12/policy-revision-1.pdf", "/documents/2023/09/legacy",
    ]
    by_id = {asset["asset_id"]: asset for asset in saved["assets"]}
    assert by_id["cdnjpg01"]["status"] == "blocked"
    assert by_id["cdnpdf01"]["status"] == "failed"
    assert by_id["cdnpdf01"]["result"] == "not attempted: host not approved / HTTP 403 Forbidden"


def test_representative_test_can_be_retried_and_persists_results_on_crash(tmp_path, live_server, monkeypatch):
    manifest_path = _live_manifest(tmp_path, live_server, live_server["hosts"])
    first = archive_tool.archive_test_from_manifest(manifest_path, live_server["hosts"])

    calls = {"count": 0}
    real_download = archive_tool._download_asset

    def crash_on_second(asset, *args):
        calls["count"] += 1
        if calls["count"] == 2:
            raise PermissionError(13, "Permission denied", "test-results.json.tmp")
        return real_download(asset, *args)

    monkeypatch.setattr(archive_tool, "_download_asset", crash_on_second)
    with pytest.raises(PermissionError):
        archive_tool.archive_test_from_manifest(manifest_path, live_server["hosts"])
    crashed = json.loads((tmp_path / "archive" / "test-results.json").read_text(encoding="utf-8"))
    assert crashed["status"] == "error"
    assert crashed["run_id"] != first["run_id"]
    assert "Permission denied" in crashed["error"]
    statuses = [asset["status"] for asset in crashed["assets"]]
    assert statuses.count("not-tested") == 2
    assert statuses[0] in {"completed", "failed"}
    assert all(asset["error"] for asset in crashed["assets"] if asset["status"] != "completed")
    assert (tmp_path / "archive" / "test-assets.csv").exists()

    monkeypatch.setattr(archive_tool, "_download_asset", real_download)
    retried = archive_tool.archive_test_from_manifest(manifest_path, live_server["hosts"])
    assert retried["run_id"] not in {first["run_id"], crashed["run_id"]}
    assert retried["summary"]["completed_assets"] == 2
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "planned"


def test_json_writer_retries_transient_windows_permission_errors(tmp_path, monkeypatch):
    attempts = {"count": 0}
    real_replace = archive_tool.os.replace

    def flaky_replace(source, target):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(13, "Permission denied", str(source))
        return real_replace(source, target)

    monkeypatch.setattr(archive_tool.os, "replace", flaky_replace)
    monkeypatch.setattr(archive_tool.time, "sleep", lambda _seconds: None)
    archive_tool.write_json_atomic(tmp_path / "state.json", {"ok": True})
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {"ok": True}
    assert attempts["count"] == 3


def test_archive_plan_prefers_stored_upload_for_document_revision_permalinks(tmp_path):
    insertion = b"""  <item>
    <title>Ordinary upload</title><link>https://example.test/?attachment_id=30</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>30</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>inherit</wp:status>
    <wp:post_type>attachment</wp:post_type><wp:post_parent>0</wp:post_parent>
    <wp:post_mime_type>image/jpeg</wp:post_mime_type>
    <wp:attachment_url>https://cdn.test/wp-site/uploads/sites/3/2026/01/photo.jpg</wp:attachment_url>
    <wp:postmeta><wp:meta_key>_wp_attached_file</wp:meta_key><wp:meta_value>2026/01/photo.jpg</wp:meta_value></wp:postmeta>
  </item>
  <item>
    <title>Revision file</title><link></link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>20</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>inherit</wp:status>
    <wp:post_type>attachment</wp:post_type><wp:post_parent>21</wp:post_parent>
    <wp:post_mime_type>application/pdf</wp:post_mime_type>
    <wp:attachment_url>https://example.test/documents/2026/01/policy-revision-1.pdf</wp:attachment_url>
    <wp:postmeta><wp:meta_key>_wp_attached_file</wp:meta_key><wp:meta_value>2026/01/0ccb3e42290c6376d62b7e0d16512a94.pdf</wp:meta_value></wp:postmeta>
  </item>
  <item>
    <title>Policy document</title><link>https://example.test/documents/2026/01/policy.pdf</link>
    <dc:creator>editor</dc:creator><content:encoded>20</content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>21</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>document</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
"""
    wxr = tmp_path / "stored-upload.xml"
    wxr.write_bytes(FIXTURE.read_bytes().replace(b"</channel></rss>", insertion + b"</channel></rss>"))

    manifest = build_archive_plan(wxr, tmp_path / "archive")
    revision = next(record for record in manifest["records"] if record["wordpress_id"] == "20")
    asset = next(asset for asset in manifest["assets"] if "20" in asset["record_ids"])

    assert manifest["summary"]["uploads_base_url"] == "https://cdn.test/wp-site/uploads/sites/3/"
    assert manifest["summary"]["stored_upload_urls"] == 1
    assert revision["resolution"] == "stored-upload-url"
    assert asset["url"] == "https://cdn.test/wp-site/uploads/sites/3/2026/01/0ccb3e42290c6376d62b7e0d16512a94.pdf"
    assert asset["source_host"] == "cdn.test"
    assert asset["fallback_urls"] == ["https://example.test/documents/2026/01/policy-revision-1.pdf"]
    assert asset["relative_path"].endswith("policy-revision-1.pdf")
    assert "21" in asset["record_ids"]
    assert manifest["source_hosts"] == ["cdn.test", "example.test"]
    ordinary = next(asset for asset in manifest["assets"] if "30" in asset["record_ids"])
    assert ordinary["url"] == "https://cdn.test/wp-site/uploads/sites/3/2026/01/photo.jpg"
    assert ordinary["fallback_urls"] == []
    sample = archive_tool.select_archive_test_assets(manifest)
    assert any(asset["category"] == "document" and asset["file_type"] == ".pdf" for asset in sample)


def test_uploads_base_url_ignores_unsafe_attached_paths():
    class Item:
        def __init__(self, url, attached):
            self.attachment_url = url
            self.meta = {"_wp_attached_file": [attached]}

    assert archive_tool._uploads_base_url([
        Item("https://cdn.test/uploads/2026/01/a.jpg", "2026/01/a.jpg"),
        Item("https://cdn.test/uploads/2026/01/b.jpg", "2026/01/b.jpg"),
        Item("https://other.test/uploads/2026/01/c.jpg", "2026/01/c.jpg"),
    ]) == "https://cdn.test/uploads/"
    assert archive_tool._uploads_base_url([]) == ""
    assert archive_tool._attached_file(Item("", "../../etc/passwd")) == ""
    assert archive_tool._attached_file(Item("", "/absolute.pdf")) == ""


def test_representative_selector_samples_private_and_pending_records():
    manifest = {
        "records": [
            {"wordpress_id": "10", "post_type": "document", "status": "publish", "resolution": "attachment-reference", "asset_ids": ["pub"]},
            {"wordpress_id": "11", "post_type": "document", "status": "private", "resolution": "attachment-reference", "asset_ids": ["priv"]},
            {"wordpress_id": "12", "post_type": "document", "status": "pending", "resolution": "attachment-reference", "asset_ids": ["pend"]},
            {"wordpress_id": "13", "post_type": "document", "status": "draft", "resolution": "attachment-reference", "asset_ids": ["draft"]},
            {"wordpress_id": "20", "post_type": "attachment", "status": "inherit", "resolution": "stored-upload-url", "asset_ids": ["pub"]},
        ],
        "assets": [
            {"asset_id": "pub", "record_ids": ["20", "10"], "role": "current-original", "url": "https://cdn.test/u/a.pdf", "source_host": "cdn.test"},
            {"asset_id": "priv", "record_ids": ["11"], "role": "current-original", "url": "https://cdn.test/u/b.pdf", "source_host": "cdn.test"},
            {"asset_id": "pend", "record_ids": ["12"], "role": "current-original", "url": "https://cdn.test/u/c.pdf", "source_host": "cdn.test"},
            {"asset_id": "draft", "record_ids": ["13"], "role": "current-original", "url": "https://cdn.test/u/d.pdf", "source_host": "cdn.test"},
        ],
    }
    selected = {asset["asset_id"]: asset for asset in archive_tool.select_archive_test_assets(manifest)}
    # Same category/type/host/role, so only status coverage can pull these in.
    assert set(selected) == {"pub", "priv", "pend", "draft"}
    assert selected["priv"]["record_statuses"] == ["private"]
    assert "record status private" in selected["priv"]["test_reasons"]
    assert "record status draft" in selected["draft"]["test_reasons"]
    assert selected["pub"]["record_statuses"] == ["inherit", "publish"]

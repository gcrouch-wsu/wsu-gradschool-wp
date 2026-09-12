from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import archive_tool
import pytest
from archive_tool import archive_from_manifest, build_archive_plan


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"


def test_archive_plan_includes_attachment_originals_and_source_hosts(tmp_path):
    target = tmp_path / "archive"
    manifest = build_archive_plan(FIXTURE, target)

    assert manifest["status"] == "planned"
    assert manifest["schema_version"] == archive_tool.ARCHIVE_SCHEMA_VERSION
    assert manifest["summary"]["attachment_records"] == 1
    assert manifest["summary"]["document_records"] == 0
    assert manifest["summary"]["assets"] == 1
    assert manifest["summary"]["unresolved_records"] == 0
    assert manifest["source_hosts"] == ["cdn.test"]
    assert manifest["assets"][0]["url"] == "https://cdn.test/photo.jpg"
    assert manifest["assets"][0]["role"] == "current-original"
    assert (target / "manifest.json").exists()
    assert (target / "SUMMARY.txt").exists()
    assert (target / "records.csv").exists()
    assert (target / "assets.csv").exists()
    assert (target / "failures.csv").exists()


def test_archive_plan_links_custom_document_and_pre_edit_original(tmp_path):
    insertion = b"""  <item>
    <title>Edited photo</title><link>https://example.test/?attachment_id=10</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>10</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>inherit</wp:status>
    <wp:post_type>attachment</wp:post_type><wp:post_parent>0</wp:post_parent>
    <wp:post_mime_type>image/jpeg</wp:post_mime_type>
    <wp:attachment_url>https://example.test/uploads/photo-scaled.jpg</wp:attachment_url>
    <wp:postmeta><wp:meta_key>_wp_attachment_metadata</wp:meta_key><wp:meta_value>a:1:{s:14:&quot;original_image&quot;;s:9:&quot;photo.jpg&quot;;}</wp:meta_value></wp:postmeta>
  </item>
  <item>
    <title>Policy document</title><link>https://example.test/document/policy</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>11</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>document</wp:post_type><wp:post_parent>0</wp:post_parent>
    <wp:postmeta><wp:meta_key>document_attachment_id</wp:meta_key><wp:meta_value>10</wp:meta_value></wp:postmeta>
  </item>
"""
    wxr = tmp_path / "document.xml"
    wxr.write_bytes(FIXTURE.read_bytes().replace(b"</channel></rss>", insertion + b"</channel></rss>"))

    manifest = build_archive_plan(wxr, tmp_path / "archive")
    document = next(record for record in manifest["records"] if record["wordpress_id"] == "11")
    edited = next(record for record in manifest["records"] if record["wordpress_id"] == "10")

    assert document["resolution"] == "attachment-reference"
    assert document["linked_attachment_ids"] == ["10"]
    assert document["asset_ids"] == edited["asset_ids"]
    assert {asset["role"] for asset in manifest["assets"] if "10" in asset["record_ids"]} == {
        "current-original", "pre-edit-original",
    }
    assert any(asset["url"].endswith("/uploads/photo.jpg") for asset in manifest["assets"])


def test_archive_plan_links_wsu_document_revision_parent_and_content_id(tmp_path):
    insertion = b"""  <item>
    <title>Revision file</title><link></link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>20</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>inherit</wp:status>
    <wp:post_type>attachment</wp:post_type><wp:post_parent>21</wp:post_parent>
    <wp:post_mime_type>application/pdf</wp:post_mime_type>
    <wp:attachment_url>https://example.test/documents/2026/01/policy-revision-1.pdf</wp:attachment_url>
  </item>
  <item>
    <title>Policy document</title><link>https://example.test/documents/2026/01/policy.pdf</link>
    <dc:creator>editor</dc:creator><content:encoded>20</content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>21</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>private</wp:status>
    <wp:post_type>document</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
"""
    wxr = tmp_path / "wsu-document.xml"
    wxr.write_bytes(FIXTURE.read_bytes().replace(b"</channel></rss>", insertion + b"</channel></rss>"))

    manifest = build_archive_plan(wxr, tmp_path / "archive")
    document = next(record for record in manifest["records"] if record["wordpress_id"] == "21")
    revision = next(record for record in manifest["records"] if record["wordpress_id"] == "20")

    assert document["current_attachment_id"] == "20"
    assert document["linked_attachment_ids"] == ["20"]
    assert document["asset_ids"] == revision["asset_ids"]
    assert document["resolution"] == "attachment-reference"
    assert not any(asset["url"].endswith("/policy.pdf") for asset in manifest["assets"])


def test_archive_plan_uses_wsu_document_route_only_when_attachment_is_absent(tmp_path):
    insertion = b"""  <item>
    <title>Legacy document</title><link>https://example.test/documents/2023/09/legacy-document</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>21</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>private</wp:status>
    <wp:post_type>document</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
"""
    wxr = tmp_path / "legacy-document.xml"
    wxr.write_bytes(FIXTURE.read_bytes().replace(b"</channel></rss>", insertion + b"</channel></rss>"))

    manifest = build_archive_plan(wxr, tmp_path / "archive")
    document = next(record for record in manifest["records"] if record["wordpress_id"] == "21")
    asset = next(asset for asset in manifest["assets"] if "21" in asset["record_ids"])

    assert document["resolution"] == "document-link-to-verify"
    assert asset["url"] == "https://example.test/documents/2023/09/legacy-document"
    assert manifest["summary"]["document_links_to_verify"] == 1


def test_archive_is_resumable_and_only_completes_verified_plan(tmp_path, monkeypatch):
    target = tmp_path / "archive"
    build_archive_plan(FIXTURE, target)

    def fake_download(asset, root, allowed_hosts, wordpress_host, username, application_password):
        assert allowed_hosts == {"cdn.test"}
        assert not username and not application_password
        destination = root / asset["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"archived-file")
        asset.update({
            "status": "completed",
            "bytes": len(b"archived-file"),
            "sha256": archive_tool.file_sha256(destination),
            "content_type": "image/jpeg",
            "response_url": asset["url"],
            "error": "",
            "completed_at": archive_tool.utc_now(),
        })

    monkeypatch.setattr(archive_tool, "_download_asset", fake_download)
    completed = archive_from_manifest(target / "manifest.json", {"cdn.test"})
    assert completed["status"] == "completed"
    assert completed["summary"]["completed_assets"] == 1

    def fail_if_downloaded(*_args, **_kwargs):
        raise AssertionError("A verified completed file should be reused on retry.")

    monkeypatch.setattr(archive_tool, "_download_asset", fail_if_downloaded)
    resumed = archive_from_manifest(target / "manifest.json", {"cdn.test"})
    assert resumed["status"] == "completed"


def test_unresolved_document_prevents_completed_status(tmp_path, monkeypatch):
    insertion = b"""  <item>
    <title>Unresolved policy</title><link>https://example.test/document/policy</link>
    <dc:creator>editor</dc:creator><content:encoded></content:encoded><excerpt:encoded></excerpt:encoded>
    <wp:post_id>11</wp:post_id><wp:post_date>2026-01-01 00:00:00</wp:post_date>
    <wp:post_modified>2026-01-02 00:00:00</wp:post_modified><wp:status>publish</wp:status>
    <wp:post_type>document</wp:post_type><wp:post_parent>0</wp:post_parent>
  </item>
"""
    wxr = tmp_path / "unresolved.xml"
    wxr.write_bytes(FIXTURE.read_bytes().replace(b"</channel></rss>", insertion + b"</channel></rss>"))
    target = tmp_path / "archive"
    build_archive_plan(wxr, target)

    def fake_download(asset, root, *_args):
        destination = root / asset["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"archived-file")
        asset.update({"status": "completed", "sha256": archive_tool.file_sha256(destination)})

    monkeypatch.setattr(archive_tool, "_download_asset", fake_download)
    result = archive_from_manifest(target / "manifest.json", {"cdn.test"})
    assert result["status"] == "incomplete"
    assert result["summary"]["unresolved_records"] == 1


def test_manifest_does_not_store_archive_credentials(tmp_path, monkeypatch):
    target = tmp_path / "archive"
    build_archive_plan(FIXTURE, target)

    def fake_download(asset, root, _hosts, _wordpress_host, username, application_password):
        assert username == "archive-user"
        assert application_password == "secret application password"
        destination = root / asset["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"archived-file")
        asset.update({"status": "completed", "sha256": archive_tool.file_sha256(destination)})

    monkeypatch.setattr(archive_tool, "_download_asset", fake_download)
    archive_from_manifest(
        target / "manifest.json", {"cdn.test"},
        username="archive-user", application_password="secret application password",
    )
    saved = (target / "manifest.json").read_text(encoding="utf-8")
    assert "archive-user" not in saved
    assert "secret application password" not in saved
    assert json.loads(saved)["status"] == "completed"


def test_private_network_download_destinations_are_refused():
    with pytest.raises(archive_tool.ArchiveError, match="Private or local network"):
        archive_tool._reject_private_destination("127.0.0.1")


def test_html_escaped_document_url_is_extracted_without_truncation():
    urls = archive_tool._document_urls(
        "a:1:{s:3:&quot;url&quot;;s:40:&quot;https://files.example.test/report.pdf&quot;;}"
    )
    assert urls == {"https://files.example.test/report.pdf"}


def test_archive_csv_cells_are_safe_for_spreadsheets():
    assert archive_tool._csv_safe("=HYPERLINK(\"https://attacker.example\")").startswith("'=")
    assert archive_tool._csv_safe("ordinary title") == "ordinary title"


def test_representative_selector_covers_types_hosts_roles_and_all_fallbacks():
    manifest = {
        "records": [
            {"wordpress_id": "20", "post_type": "document", "resolution": "attachment-reference", "asset_ids": ["d1", "d2"]},
            {"wordpress_id": "30", "post_type": "document", "resolution": "document-link-to-verify", "asset_ids": ["fallback"]},
            {"wordpress_id": "40", "post_type": "attachment", "resolution": "attachment-url", "asset_ids": ["m1", "m2"]},
        ],
        "assets": [
            {"asset_id": "d1", "record_ids": ["20"], "role": "current-original", "url": "https://wp.test/a.pdf", "source_host": "wp.test"},
            {"asset_id": "d2", "record_ids": ["20"], "role": "current-original", "url": "https://wp.test/b.pdf", "source_host": "wp.test"},
            {"asset_id": "fallback", "record_ids": ["30"], "role": "document-file", "url": "https://wp.test/documents/2023/09/legacy", "source_host": "wp.test"},
            {"asset_id": "m1", "record_ids": ["40"], "role": "current-original", "url": "https://cdn.test/photo.jpg", "source_host": "cdn.test"},
            {"asset_id": "m2", "record_ids": ["40"], "role": "pre-edit-original", "url": "https://cdn.test/photo-original.jpg", "source_host": "cdn.test"},
        ],
    }

    selected = archive_tool.select_archive_test_assets(manifest)
    selected_ids = {asset["asset_id"] for asset in selected}

    assert "d1" in selected_ids or "d2" in selected_ids
    assert not {"d1", "d2"}.issubset(selected_ids)
    assert {"fallback", "m1", "m2"}.issubset(selected_ids)
    assert {asset["category"] for asset in selected} == {"document", "media"}
    assert {asset["file_type"] for asset in selected} == {".pdf", ".jpg", "(no extension)"}
    assert {asset["source_host"] for asset in selected} == {"wp.test", "cdn.test"}


def test_representative_download_does_not_change_full_archive(tmp_path, monkeypatch):
    target = tmp_path / "archive"
    build_archive_plan(FIXTURE, target)

    def fake_download(asset, root, allowed_hosts, wordpress_host, username, application_password):
        assert allowed_hosts == {"cdn.test"}
        assert wordpress_host == "example.test"
        assert username == "archive-user"
        assert application_password == "application-password"
        destination = root / asset["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"representative-file")
        asset.update({
            "status": "completed",
            "bytes": len(b"representative-file"),
            "sha256": archive_tool.file_sha256(destination),
            "content_type": "image/jpeg",
            "response_url": asset["url"],
            "error": "",
            "completed_at": archive_tool.utc_now(),
        })

    monkeypatch.setattr(archive_tool, "_download_asset", fake_download)
    result = archive_tool.archive_test_from_manifest(
        target / "manifest.json", {"cdn.test"},
        username="archive-user", application_password="application-password",
    )
    full_manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    saved_test = (target / "test-results.json").read_text(encoding="utf-8")

    assert result["status"] == "passed"
    assert result["summary"]["selected_assets"] == 1
    assert full_manifest["status"] == "planned"
    assert full_manifest["summary"]["completed_assets"] == 0
    assert full_manifest["assets"][0]["status"] == "pending"
    assert not (target / full_manifest["assets"][0]["relative_path"]).exists()
    assert (target / result["assets"][0]["relative_path"]).exists()
    assert (target / "test-assets.csv").exists()
    assert "archive-user" not in saved_test
    assert "application-password" not in saved_test


def test_downloader_streams_and_verifies_file_without_saving_credentials(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse:
        headers = {"Content-Length": "13", "Content-Type": "application/pdf"}

        def __init__(self):
            self.body = BytesIO(b"archived-file")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            return self.body.read(size)

        def geturl(self):
            return "https://example.test/report.pdf"

    class FakeOpener:
        def open(self, request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(archive_tool, "build_opener", lambda *_args: FakeOpener())
    monkeypatch.setattr(archive_tool, "_reject_private_destination", lambda _host: None)
    asset = {
        "url": "https://example.test/report.pdf",
        "relative_path": "files/10/current-original-report.pdf",
        "expected_mime_type": "application/pdf",
    }
    archive_tool._download_asset(
        asset, tmp_path, {"example.test"}, "example.test", "archive-user", "application-password"
    )

    saved = tmp_path / asset["relative_path"]
    assert saved.read_bytes() == b"archived-file"
    assert asset["status"] == "completed"
    assert asset["bytes"] == 13
    assert asset["sha256"] == archive_tool.file_sha256(saved)
    assert captured["authorization"].startswith("Basic ")
    assert "application-password" not in captured["authorization"]
    assert captured["timeout"] == 45


def test_downloader_rejects_html_error_page_for_document(tmp_path, monkeypatch):
    class HtmlResponse:
        headers = {"Content-Length": "18", "Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.test/report.pdf"

    class FakeOpener:
        def open(self, _request, timeout):
            assert timeout == 45
            return HtmlResponse()

    monkeypatch.setattr(archive_tool, "build_opener", lambda *_args: FakeOpener())
    monkeypatch.setattr(archive_tool, "_reject_private_destination", lambda _host: None)
    asset = {
        "url": "https://example.test/report.pdf",
        "relative_path": "files/10/current-original-report.pdf",
        "expected_mime_type": "application/pdf",
    }
    with pytest.raises(archive_tool.ArchiveError, match="HTML page"):
        archive_tool._download_asset(asset, tmp_path, {"example.test"}, "example.test", "", "")
    assert not (tmp_path / asset["relative_path"]).exists()

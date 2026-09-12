"""Dashboard behaviour for representative test results: host pre-checks,
per-sample failure reporting, and safe retry after a failed or crashed test."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import archive_app


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"


def test_preferred_hosts_prechecks_site_and_sibling_institutional_hosts():
    manifest = {
        "site": {"site_url": "https://gradschool.wsu.edu", "home_url": "https://wsuwp.gradschool.wsu.edu/"},
        "source_hosts": ["gradschool.wsu.edu", "wpcdn.web.wsu.edu", "cdn.vendor.example", "wsuwp.gradschool.wsu.edu"],
    }
    assert archive_app.preferred_hosts(manifest) == {
        "gradschool.wsu.edu", "wsuwp.gradschool.wsu.edu", "wpcdn.web.wsu.edu",
    }
    assert archive_app.preferred_hosts(None) == set()


def test_test_outcome_message_explains_unapproved_hosts_and_auth_failures():
    result = {
        "status": "failed",
        "summary": {"selected_assets": 31, "completed_assets": 0, "failed_assets": 17, "blocked_assets": 14},
        "unapproved_sample_hosts": ["wpcdn.web.wsu.edu"],
        "assets": [{"http_status": 403}, {"http_status": None}],
    }
    message = archive_app.test_outcome_message(result)
    assert message.startswith("Representative test failed: 0 of 31 files downloaded, 17 failed, 14 blocked.")
    assert "wpcdn.web.wsu.edu was not approved" in message
    assert "Application Password" in message
    passed = archive_app.test_outcome_message(
        {"status": "passed", "summary": {"selected_assets": 3, "completed_assets": 3}}
    )
    assert passed == "Representative test passed. All 3 selected files were downloaded and verified."


def _planned_client(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(archive_app, "DATA_ROOT", data_root)
    monkeypatch.setattr(archive_app, "UPLOAD_ROOT", data_root / "uploads")
    monkeypatch.setattr(archive_app, "JOB_ROOT", data_root / "jobs")
    monkeypatch.setattr(archive_app, "SESSION_KEY_PATH", data_root / "session.key")

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(archive_app.threading, "Thread", ImmediateThread)
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as sess:
        csrf = sess["archive_csrf"]
    planned = client.post(
        "/inspect",
        data={
            "csrf": csrf,
            "output_root": str(tmp_path / "archives"),
            "wxr_file": (BytesIO(FIXTURE.read_bytes()), "fresh-export.xml"),
        },
        content_type="multipart/form-data",
    )
    job_id = parse_qs(urlsplit(planned.headers["Location"]).query)["job"][0]
    return client, csrf, job_id


def test_dashboard_shows_each_failed_sample_with_host_url_result_and_reason(tmp_path, monkeypatch):
    client, csrf, job_id = _planned_client(tmp_path, monkeypatch)

    def failing_test(manifest_path, approved_hosts, **_kwargs):
        result = {
            "status": "failed",
            "run_id": "20260912-155352-373948",
            "approved_hosts": sorted(approved_hosts),
            "unapproved_sample_hosts": ["wpcdn.web.wsu.edu"],
            "download_root": str(manifest_path.parent / "test-downloads" / "20260912-155352-373948"),
            "summary": {
                "selected_assets": 3, "completed_assets": 1, "failed_assets": 1, "blocked_assets": 1,
                "file_types": [".jpg", ".pdf", "(no extension)"],
            },
            "assets": [
                {
                    "asset_id": "a1", "category": "document", "file_type": ".pdf",
                    "source_host": "gradschool.wsu.edu",
                    "url": "https://gradschool.wsu.edu/documents/2014/12/form-revision-1.pdf",
                    "fallback_urls": [], "status": "failed", "http_status": 403,
                    "result": "HTTP 403 Forbidden",
                    "error": (
                        "HTTP 403 Forbidden. The server answered with an HTML page: "
                        "WordPress › Error — You are not authorized to access that file."
                    ),
                },
                {
                    "asset_id": "a2", "category": "media", "file_type": ".jpg", "source_host": "wpcdn.web.wsu.edu",
                    "url": "https://wpcdn.web.wsu.edu/uploads/photo.jpg", "fallback_urls": [],
                    "status": "blocked", "http_status": None, "result": "not attempted: host not approved",
                    "error": "Source host wpcdn.web.wsu.edu was not approved.",
                },
                {
                    "asset_id": "a3", "category": "media", "file_type": ".jpg", "source_host": "cdn.test",
                    "url": "https://cdn.test/photo.jpg", "fallback_urls": [], "status": "completed",
                    "http_status": 200, "result": "HTTP 200 OK", "bytes": 9227, "content_type": "image/jpeg",
                    "relative_path": "test-downloads/20260912-155352-373948/media/jpg/a3-photo.jpg",
                    "error": "",
                },
            ],
        }
        archive_app._write_json(manifest_path.parent / "test-results.json", result)
        return result

    monkeypatch.setattr(archive_app, "archive_test_from_manifest", failing_test)
    client.post(f"/test/{job_id}", data={"csrf": csrf, "approved_host": "cdn.test"})
    page = client.get(f"/?job={job_id}")
    job = archive_app.load_job(job_id)

    assert job["test_status"] == "failed"
    assert job["approved_hosts"] == ["cdn.test"]
    assert job["test_run_id"] == "20260912-155352-373948"
    body = page.data.decode("utf-8")
    assert "Representative test: Failed" in body
    assert "1 of 3 files downloaded, 1 failed, 1 blocked" in body
    assert "Host not approved" in body and "wpcdn.web.wsu.edu" in body
    assert "HTTP / network result" in body
    assert "HTTP 403 Forbidden" in body
    assert "https://gradschool.wsu.edu/documents/2014/12/form-revision-1.pdf" in body
    assert "You are not authorized to access that file." in body
    assert "not attempted: host not approved" in body
    assert "Downloaded samples (1)" in body
    assert "test-downloads" in body
    # The approved host stays checked for the retry, and the retry button is available.
    assert 'value="cdn.test" checked' in body
    assert "Test representative sample (1 file)" in body


def test_failed_or_errored_representative_test_can_be_retried(tmp_path, monkeypatch):
    client, csrf, job_id = _planned_client(tmp_path, monkeypatch)
    calls = {"count": 0}

    def flaky_test(manifest_path, approved_hosts, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(13, "Permission denied", "test-results.json.tmp")
        result = {
            "status": "passed", "run_id": f"run-{calls['count']}", "approved_hosts": sorted(approved_hosts),
            "unapproved_sample_hosts": [], "download_root": "",
            "summary": {
                "selected_assets": 1, "completed_assets": 1, "failed_assets": 0, "blocked_assets": 0,
                "file_types": [".jpg"],
            },
            "assets": [],
        }
        archive_app._write_json(manifest_path.parent / "test-results.json", result)
        return result

    monkeypatch.setattr(archive_app, "archive_test_from_manifest", flaky_test)
    client.post(f"/test/{job_id}", data={"csrf": csrf, "approved_host": "cdn.test"})
    errored = archive_app.load_job(job_id)
    error_page = client.get(f"/?job={job_id}").data.decode("utf-8")
    assert errored["test_status"] == "error"
    assert "PermissionError: [Errno 13] Permission denied" in errored["test_message"]
    assert "Run the test again to retry" in error_page
    assert job_id not in archive_app.active_jobs

    client.post(f"/test/{job_id}", data={"csrf": csrf, "approved_host": "cdn.test"})
    retried = archive_app.load_job(job_id)
    assert calls["count"] == 2
    assert retried["test_status"] == "passed"
    assert retried["status"] == "planned"
    assert retried["approved_hosts"] == ["cdn.test"]


def test_recent_plans_drop_records_whose_archive_folder_was_deleted(tmp_path, monkeypatch):
    client, _csrf, job_id = _planned_client(tmp_path, monkeypatch)
    job = archive_app.load_job(job_id)
    assert [row["id"] for row in archive_app.recent_jobs()] == [job_id]

    stale = dict(job, id="0" * 32, created_at="2026-01-01T00:00:00+00:00",
                 manifest_path=str(tmp_path / "gone" / "manifest.json"), output_dir=str(tmp_path / "gone"))
    archive_app.save_job(stale)
    assert archive_app._job_path(stale["id"]).exists()

    page = client.get("/")
    assert [row["id"] for row in archive_app.recent_jobs()] == [job_id]
    assert not archive_app._job_path(stale["id"]).exists()
    assert str(tmp_path / "gone").encode() not in page.data

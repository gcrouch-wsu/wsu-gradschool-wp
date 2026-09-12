"""Upload status/recovery and live-check coverage for the review dashboard."""
from __future__ import annotations

from app import create_app
from tests.test_analyzer import WXR
from tests.test_app import _client_with_report


def _start_upload(client):
    started = client.post(
        "/api/upload-session",
        json={"filename": "test.xml", "size": len(WXR), "total_chunks": 1},
    )
    assert started.status_code == 200
    return started.get_json()["upload_id"]


def test_upload_status_reports_pending_chunks_then_analyzed():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.post("/api/upload-status").get_json() == {"pending": False, "analyzed": False}

    _start_upload(client)
    status = client.post("/api/upload-status").get_json()
    assert status["pending"] is True and status["analyzed"] is False
    assert status["stored_chunks"] == 0 and status["total_chunks"] == 1

    client.post("/api/upload-chunk/0", data=WXR, headers={"Content-Type": "application/octet-stream"})
    assert client.post("/api/upload-status").get_json()["stored_chunks"] == 1
    assert client.post("/api/complete-upload").status_code == 200
    # Once adopted there is nothing pending.
    assert client.post("/api/upload-status").get_json() == {"pending": False, "analyzed": False}


def test_lost_analysis_response_is_recovered_without_reanalyzing(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    store = app.extensions["audit_store"]
    upload_id = _start_upload(client)
    client.post("/api/upload-chunk/0", data=WXR, headers={"Content-Type": "application/octet-stream"})

    # Simulate the server finishing analysis while the HTTP response was lost:
    # the report is stored under the upload id but the session never learned.
    from io import BytesIO
    from analyzer import analyze_export, parse_wxr
    report = analyze_export(parse_wxr(BytesIO(WXR)), set())
    report["analysis_seconds"] = 1.5
    store.save_report(upload_id, report, "test.xml", owner_email="", expires_at=None)
    with client.session_transaction() as sess:
        assert sess.get("audit_id") != upload_id

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("A finished analysis must be adopted, not repeated.")

    monkeypatch.setattr("app.analyze_export", must_not_run)
    status = client.post("/api/upload-status").get_json()
    assert status["analyzed"] is True and status["records"] == 5 and status["redirect"] == "/"
    with client.session_transaction() as sess:
        assert sess["audit_id"] == upload_id
        assert "pending_upload" not in sess
    assert store.get_chunk(upload_id, 0) is None
    assert client.get("/api/items?group=media").get_json()["total"] == 1

    # Calling complete-upload again after adoption reports the missing session cleanly.
    assert client.post("/api/complete-upload").status_code == 400


def test_complete_upload_adopts_finished_report_instead_of_reanalyzing(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    store = app.extensions["audit_store"]
    upload_id = _start_upload(client)
    client.post("/api/upload-chunk/0", data=WXR, headers={"Content-Type": "application/octet-stream"})
    from io import BytesIO
    from analyzer import analyze_export, parse_wxr
    report = analyze_export(parse_wxr(BytesIO(WXR)), set())
    report["analysis_seconds"] = 2.0
    store.save_report(upload_id, report, "test.xml", owner_email="", expires_at=None)
    monkeypatch.setattr("app.analyze_export", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("re-analyzed")))

    completed = client.post("/api/complete-upload")
    assert completed.status_code == 200
    payload = completed.get_json()
    assert payload["recovered"] is True and payload["records"] == 5


def test_items_payload_reports_live_coverage_and_resets_for_new_export(monkeypatch):
    def fake_get(url, _headers, _timeout):
        if "/pages?" in url:
            return 200, [{
                "id": 1, "status": "trash", "link": "https://example.test/landing",
                "modified": "2026-01-02T00:00:00", "type": "page", "title": {"rendered": "Landing page"},
            }], {"x-wp-total": "1", "x-wp-totalpages": "1"}
        return 200, [], {"x-wp-total": "0", "x-wp-totalpages": "0"}

    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")
    monkeypatch.setattr("analyzer.wp_rest._http_get", fake_get)

    client = _client_with_report()
    before = client.get("/api/items?group=pages").get_json()["live_summary"]
    assert before["total"] == 3 and before["checked"] == 0 and before["unchecked"] == 3
    # The trashed filter is empty until a live check runs, even though the record is in Trash.
    assert client.get("/api/items?group=pages&live=trashed").get_json()["total"] == 0

    ids = client.get("/api/items/ids?group=pages").get_json()["ids"]
    checked = client.post("/api/live-check", json={"ids": ids}).get_json()
    assert checked["checked"] == 3 and checked["trashed"] == 1 and checked["missing"] == 2

    after = client.get("/api/items?group=pages").get_json()["live_summary"]
    assert after == {"total": 3, "checked": 3, "unchecked": 0, "found": 0, "trashed": 1, "missing": 2, "not-in-rest": 0, "error": 0}
    assert client.get("/api/items?group=pages&live=trashed").get_json()["total"] == 1

    # Analyzing a replacement export starts a new audit: live states are reset.
    _start_upload(client)
    client.post("/api/upload-chunk/0", data=WXR, headers={"Content-Type": "application/octet-stream"})
    assert client.post("/api/complete-upload").status_code == 200
    reset = client.get("/api/items?group=pages").get_json()["live_summary"]
    assert reset["checked"] == 0
    assert client.get("/api/items?group=pages&live=trashed").get_json()["total"] == 0


def test_live_check_caps_ids_per_request(monkeypatch):
    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")
    client = _client_with_report()
    too_many = client.post("/api/live-check", json={"ids": [str(i) for i in range(1, 502)]})
    assert too_many.status_code == 400
    assert "at most 500" in too_many.get_json()["error"]

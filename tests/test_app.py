import json
from io import BytesIO

from werkzeug.security import generate_password_hash

from app import create_app
from tests.test_analyzer import WXR


def _write_csrf(client):
    with client.session_transaction() as sess:
        return sess["write_csrf"]


def _client_with_report():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.post(
        "/",
        data={"export_file": (BytesIO(WXR), "test.xml")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    return client


def test_paginated_group_api_and_lazy_detail():
    client = _client_with_report()
    listing = client.get("/api/items?group=pages&page_size=25").get_json()
    assert listing["total"] == 3
    assert listing["items"][0]["group"] == "pages"
    assert listing["items"][0]["live_state"] == "unchecked"
    assert "/wp-admin/post.php?post=" in listing["items"][0]["wp_admin_url"]
    ids = client.get("/api/items/ids?group=pages").get_json()
    assert ids["total"] == 3
    assert ids["ids"] == [item["id"] for item in listing["items"]]
    detail = client.get("/api/items/3").get_json()
    assert detail["author_name"] == "Greg Crouch"
    assert detail["classification"] == "unreferenced"
    assert detail["underlying_classification"] == "unreferenced"
    assert detail["expected_development"] is True


def test_review_decision_and_filtered_export():
    client = _client_with_report()
    saved = client.post("/api/items/3/decision", json={"decision": "keep"})
    assert saved.status_code == 200
    listing = client.get("/api/items?group=pages&decision=keep").get_json()
    assert listing["total"] == 1
    exported = client.get("/api/export.csv?group=pages&decision=keep")
    assert exported.status_code == 200
    assert b"Development page" in exported.data
    assert b"author_email" in exported.data
    full_json = client.get("/api/export.json?group=pages&decision=keep").get_json()
    assert full_json[0]["reasons"]
    assert "evidence" in full_json[0]


def test_review_decisions_use_simple_outcomes_and_normalize_legacy_values():
    client = _client_with_report()
    assert client.post("/api/items/3/decision", json={"decision": "candidate"}).status_code == 400
    app = client.application
    store = app.extensions["audit_store"]
    with client.session_transaction() as sess:
        audit_id = sess["audit_id"]
    state = store.load_state(audit_id)
    store.save_decisions(audit_id, {"3": "candidate", "2": "expected"}, expected_rev=state["decision_rev"])
    assert client.get("/api/items/3").get_json()["review_decision"] == "verify"
    assert client.get("/api/items/2").get_json()["review_decision"] == "keep"
    facets = client.get("/api/items?group=pages").get_json()["facets"]["decisions"]
    assert facets == ["approved", "keep", "unreviewed", "verify"]


def test_chunked_upload_survives_separate_requests_and_can_be_removed():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    started = client.post(
        "/api/upload-session",
        json={"filename": "test.xml", "size": len(WXR), "total_chunks": 1},
    )
    assert started.status_code == 200
    uploaded = client.post(
        "/api/upload-chunk/0", data=WXR,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 200
    completed = client.post("/api/complete-upload")
    assert completed.status_code == 200
    assert completed.get_json()["records"] == 5
    assert client.get("/api/items?group=media").get_json()["total"] == 1
    assert client.delete("/api/audit").status_code == 200
    assert client.get("/api/items").status_code == 404


def test_new_upload_session_deletes_previous_pending_chunks():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    store = app.extensions["audit_store"]
    first = client.post(
        "/api/upload-session",
        json={"filename": "test.xml", "size": len(WXR), "total_chunks": 1},
    )
    first_id = first.get_json()["upload_id"]
    client.post(
        "/api/upload-chunk/0", data=WXR,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert store.get_chunk(first_id, 0) == WXR
    client.post(
        "/api/upload-session",
        json={"filename": "again.xml", "size": len(WXR), "total_chunks": 1},
    )
    assert store.get_chunk(first_id, 0) is None


def test_stale_upload_chunks_expire():
    from audit_store import AuditStore

    store = AuditStore()
    store.put_chunk("abandoned", 0, b"stale")
    store._chunk_times[("abandoned", 0)] = 0
    assert store.get_chunk("abandoned", 0) is None


def test_live_check_is_disabled_during_tests_by_default():
    client = _client_with_report()
    response = client.post("/api/live-check", json={"group": "pages"})
    assert response.status_code == 403


def test_live_check_merges_read_only_wordpress_status(monkeypatch):
    def fake_get(url, _headers, _timeout):
        if "/pages?" in url:
            return 200, [{
                "id": 1,
                "status": "publish",
                "link": "https://example.test/landing",
                "modified": "2026-01-02T00:00:00",
                "type": "page",
                "title": {"rendered": "Landing page"},
            }], {"x-wp-total": "1", "x-wp-totalpages": "1"}
        return 200, [], {"x-wp-total": "0", "x-wp-totalpages": "0"}

    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")
    monkeypatch.setattr("analyzer.wp_rest._http_get", fake_get)

    client = _client_with_report()
    checked = client.post("/api/live-check", json={"group": "pages"})
    assert checked.status_code == 200
    payload = checked.get_json()
    assert payload["checked"] == 3
    assert payload["found"] == 1
    assert payload["missing"] == 2

    listing = client.get("/api/items?group=pages&live=found").get_json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == "1"
    assert listing["items"][0]["live_status"] == "publish"
    assert listing["items"][0]["live_title"] == "Landing page"
    assert listing["items"][0]["live_snapshot_ready"] is True
    assert listing["items"][0]["live_identity_matches"] is True
    by_id = client.post("/api/live-check", json={"ids": ["1"]})
    assert by_id.status_code == 200
    assert by_id.get_json()["checked"] == 1
    assert by_id.get_json()["found"] == 1
    exported = client.get("/api/export.csv?group=pages&live=missing")
    assert exported.status_code == 200
    assert b"wp_admin_url" in exported.data
    assert b"Development page" in exported.data


def test_work_queue_includes_unreferenced_and_missing_live_records():
    client = _client_with_report()
    marked = client.post("/api/items/1/decision", json={"decision": "approved"})
    assert marked.status_code == 200
    queue = client.get("/api/items?queue=1").get_json()
    ids = {item["id"] for item in queue["items"]}
    assert "1" in ids
    assert "3" in ids
    assert queue["total"] >= 2
    assert queue["queue"] is True


def test_trash_is_disabled_during_tests_by_default():
    client = _client_with_report()
    response = client.post("/api/trash", json={"ids": ["3"], "confirm": "trash"})
    assert response.status_code == 403


def test_confirmed_trash_updates_live_state(monkeypatch):
    deleted = []

    def fake_get(url, _headers, _timeout):
        if "/pages?" in url:
            return 200, [
                {"id": 1, "status": "publish", "type": "page", "title": {"rendered": "Landing page"}, "link": "https://example.test/landing", "modified": "2026-01-02T00:00:00"},
                {"id": 2, "status": "publish", "type": "page", "title": {"rendered": "Child page"}, "link": "https://example.test/child", "modified": "2026-01-02T00:00:00"},
                {"id": 3, "status": "publish", "type": "page", "title": {"rendered": "Development page"}, "link": "https://example.test/dev", "modified": "2026-01-02T00:00:00"},
            ], {"x-wp-total": "3", "x-wp-totalpages": "1"}
        return 200, {
            "id": 3,
            "status": "publish",
            "type": "page",
            "title": {"rendered": "Development page"},
            "link": "https://example.test/dev",
            "modified": "2026-01-02T00:00:00",
        }

    def fake_delete(url, _headers, _timeout):
        deleted.append(url)
        return 200, {
            "id": 3,
            "status": "trash",
            "type": "page",
            "link": "https://example.test/dev",
            "modified": "2026-01-02T00:00:00",
        }

    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")
    monkeypatch.setattr("analyzer.wp_rest._http_get", fake_get)
    monkeypatch.setattr("analyzer.wp_rest._http_delete", fake_delete)

    client = _client_with_report()
    refused = client.post("/api/trash", json={"ids": ["3"]})
    assert refused.status_code == 403
    missing_confirm = client.post("/api/trash", json={"ids": ["3"], "csrf": _write_csrf(client)})
    assert missing_confirm.status_code == 400
    blocked = client.post("/api/trash", json={"ids": ["3"], "confirm": "trash", "csrf": _write_csrf(client)})
    assert blocked.status_code == 400
    assert "fresh live check" in blocked.get_json()["error"]
    assert client.post("/api/live-check", json={"group": "pages"}).status_code == 200
    unapproved = client.post("/api/trash", json={"ids": ["3"], "confirm": "trash", "csrf": _write_csrf(client)})
    assert unapproved.status_code == 400
    assert "approved to delete" in unapproved.get_json()["error"]
    assert client.post("/api/items/3/decision", json={"decision": "approved"}).status_code == 200
    trashed = client.post("/api/trash", json={"ids": ["3"], "confirm": "trash", "csrf": _write_csrf(client)})
    assert trashed.status_code == 200
    payload = trashed.get_json()
    assert payload["trashed"] == 1
    assert deleted[0].endswith("/wp-json/wp/v2/pages/3")
    assert "force" not in deleted[0]
    listing = client.get("/api/items?group=pages&live=trashed").get_json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == "3"
    assert listing["items"][0]["can_trash"] is False
    hostile = client.post("/api/trash", json={"ids": ["3?force=true"], "confirm": "trash", "csrf": _write_csrf(client)})
    assert hostile.status_code == 400


def test_bulk_trash_preflight_allows_selection_without_prior_approval(monkeypatch):
    deleted = []

    def fake_live_check(items, _client, site_url):
        assert site_url.rstrip("/") == "https://example.test"
        return {
            item["id"]: {
                "live_state": "found",
                "live_found": True,
                "live_status": "publish",
                "live_type": item["type"],
                "live_title": item["title"],
                "live_link": item["url"],
                "live_modified": "2026-01-02T00:00:00",
                "checked_at": "2026-09-11T12:00:00+00:00",
            }
            for item in items
        }

    def fake_trash(items, _client, site_url, expected_live):
        assert site_url.rstrip("/") == "https://example.test"
        assert all(expected_live[item["id"]]["live_state"] == "found" for item in items)
        deleted.extend(item["id"] for item in items)
        return [
            {
                "id": item["id"],
                "title": item["title"],
                "ok": True,
                "live": {
                    "live_state": "trashed",
                    "live_found": True,
                    "live_status": "trash",
                    "live_type": item["type"],
                    "live_title": item["title"],
                    "live_modified": "2026-01-02T00:00:00",
                    "checked_at": "2026-09-11T12:00:01+00:00",
                },
            }
            for item in items
        ]

    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")
    monkeypatch.setattr("app.live_check_items", fake_live_check)
    monkeypatch.setattr("app.trash_items", fake_trash)

    client = _client_with_report()
    row = client.get("/api/items?group=pages").get_json()["items"][0]
    assert row["review_decision"] == "unreviewed"
    assert row["can_trash"] is False
    assert row["selectable_for_trash"] is True

    csrf = _write_csrf(client)
    preflight = client.post("/api/trash/preflight", json={"ids": [row["id"]], "csrf": csrf})
    assert preflight.status_code == 200
    reviewed = preflight.get_json()
    assert [item["id"] for item in reviewed["ready"]] == [row["id"]]
    assert reviewed["blocked"] == []
    assert reviewed["preflight_token"]

    trashed = client.post("/api/trash", json={
        "ids": [row["id"]],
        "confirm": "trash",
        "csrf": csrf,
        "preflight_token": reviewed["preflight_token"],
    })
    assert trashed.status_code == 200
    assert deleted == [row["id"]]


def test_trash_refuses_export_from_a_different_site(monkeypatch):
    def fail_delete(url, _headers, _timeout):
        raise AssertionError("Trash must not run against a different WordPress site.")

    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://gradschool.wsu.edu")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")
    monkeypatch.setattr("analyzer.wp_rest._http_delete", fail_delete)

    client = _client_with_report()
    response = client.post(
        "/api/trash",
        json={"ids": ["3"], "confirm": "trash", "csrf": _write_csrf(client)},
    )
    assert response.status_code == 409
    assert "not from the WordPress site" in response.get_json()["error"]


def test_live_check_does_not_use_home_url_to_override_wordpress_site_url(monkeypatch):
    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")

    wxr = WXR.replace(
        b"<link>https://example.test</link>",
        b"<link>https://example.test</link><wp:site_url>https://different.test</wp:site_url>",
        1,
    )
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.post(
        "/",
        data={"export_file": (BytesIO(wxr), "test.xml")},
        content_type="multipart/form-data",
    ).status_code == 200
    response = client.post("/api/live-check", json={"ids": ["3"]})
    assert response.status_code == 409


def test_trash_rejects_non_loopback_clients_and_null_origin(monkeypatch):
    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "gcrouch")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "not-a-real-password")

    client = _client_with_report()
    csrf = _write_csrf(client)
    remote = client.post(
        "/api/trash",
        json={"ids": ["3"], "confirm": "trash", "csrf": csrf},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    assert remote.status_code == 403
    origin = client.post(
        "/api/trash",
        json={"ids": ["3"], "confirm": "trash", "csrf": csrf},
        headers={"Origin": "null"},
    )
    assert origin.status_code == 403


def test_delete_audit_removes_pending_upload_chunks():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    store = app.extensions["audit_store"]
    client.post(
        "/",
        data={"export_file": (BytesIO(WXR), "test.xml")},
        content_type="multipart/form-data",
    )
    started = client.post(
        "/api/upload-session",
        json={"filename": "again.xml", "size": len(WXR), "total_chunks": 1},
    )
    pending_id = started.get_json()["upload_id"]
    client.post(
        "/api/upload-chunk/0", data=WXR,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert store.get_chunk(pending_id, 0) == WXR
    assert client.delete("/api/audit").status_code == 200
    assert store.get_chunk(pending_id, 0) is None


def test_decision_write_conflicts_when_revision_changes():
    from audit_store import AuditStore, RevisionConflict

    store = AuditStore()
    store.save_decisions("a", {"3": "keep"})
    store.save_decisions("a", {"3": "approved"}, expected_rev=1)
    try:
        store.save_decisions("a", {"3": "candidate"}, expected_rev=1)
        raise AssertionError("Stale revision must conflict.")
    except RevisionConflict as exc:
        assert exc.current_rev == 2


def test_configured_users_must_sign_in_and_send_csrf(monkeypatch):
    monkeypatch.setenv("APP_AUTH_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("APP_AUTH_USERS_JSON", json.dumps({
        "Reviewer@wsu.edu": generate_password_hash("correct horse battery staple"),
    }))
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    assert client.get("/").status_code == 302
    assert client.get("/api/items").status_code == 401
    assert client.get("/login").status_code == 200
    csrf = _write_csrf(client)
    refused = client.post("/login", data={
        "csrf": csrf, "email": "reviewer@wsu.edu", "password": "wrong",
    })
    assert refused.status_code == 401
    accepted = client.post("/login", data={
        "csrf": csrf, "email": "REVIEWER@WSU.EDU", "password": "correct horse battery staple",
    })
    assert accepted.status_code == 302
    with client.session_transaction() as sess:
        assert sess["user_email"] == "reviewer@wsu.edu"
    assert client.get("/").status_code == 200
    assert client.post(
        "/api/upload-session",
        json={"filename": "test.xml", "size": len(WXR), "total_chunks": 1},
    ).status_code == 403
    new_csrf = _write_csrf(client)
    assert client.post(
        "/api/upload-session",
        json={"filename": "test.xml", "size": len(WXR), "total_chunks": 1},
        headers={"X-CSRF-Token": new_csrf},
    ).status_code == 200


def test_authenticated_users_cannot_open_another_users_audit(monkeypatch):
    monkeypatch.setenv("APP_AUTH_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("APP_AUTH_USERS_JSON", json.dumps({
        "first@wsu.edu": generate_password_hash("first password"),
        "second@wsu.edu": generate_password_hash("second password"),
    }))
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    client.get("/login")
    assert client.post("/login", data={
        "csrf": _write_csrf(client), "email": "first@wsu.edu", "password": "first password",
    }).status_code == 302
    uploaded = client.post(
        "/",
        data={"csrf": _write_csrf(client), "export_file": (BytesIO(WXR), "first.xml")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    with client.session_transaction() as sess:
        first_audit_id = sess["audit_id"]
    assert client.post("/logout", data={"csrf": _write_csrf(client)}).status_code == 302

    client.get("/login")
    assert client.post("/login", data={
        "csrf": _write_csrf(client), "email": "second@wsu.edu", "password": "second password",
    }).status_code == 302
    with client.session_transaction() as sess:
        sess["audit_id"] = first_audit_id
    assert client.get("/api/items").status_code == 404


def test_vercel_trash_requires_production_and_explicit_remote_enable(monkeypatch):
    from local_env import rest_enabled, rest_write_enabled

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "audit-user")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "application-password")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_REMOTE_WRITE_ENABLED", "1")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    assert rest_enabled() is True
    assert rest_write_enabled() is False
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert rest_write_enabled() is True
    monkeypatch.setenv("WP_REST_REMOTE_WRITE_ENABLED", "0")
    assert rest_write_enabled() is False


def test_write_enabled_dashboard_renders_bulk_selection_controls(monkeypatch):
    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "audit-user")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "application-password")

    client = _client_with_report()
    page = client.get("/")
    assert page.status_code == 200
    assert b'id="select-page"' in page.data
    assert b'id="bulk-trash"' in page.data
    assert b"Review selected for Trash" in page.data
    assert b'"rest_write_enabled": true' in page.data


def test_vercel_trash_requires_authenticated_same_origin_request(monkeypatch):
    monkeypatch.setenv("APP_AUTH_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("APP_AUTH_USERS_JSON", json.dumps({
        "reviewer@wsu.edu": generate_password_hash("review password"),
    }))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-only-secret-key-that-is-long-enough")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "audit-user")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "application-password")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_REMOTE_WRITE_ENABLED", "1")

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    client.get("/login")
    assert client.post("/login", data={
        "csrf": _write_csrf(client), "email": "reviewer@wsu.edu", "password": "review password",
    }).status_code == 302
    csrf = _write_csrf(client)
    body = {"ids": ["3"], "confirm": "trash", "csrf": csrf}

    assert client.post(
        "/api/trash", json=body,
        headers={"X-CSRF-Token": csrf, "Origin": "https://hostile.example"},
    ).status_code == 403
    assert client.post(
        "/api/trash", json=body,
        headers={"X-CSRF-Token": csrf, "Origin": "https://localhost", "Sec-Fetch-Site": "cross-site"},
    ).status_code == 403
    assert client.post(
        "/api/trash", json=body,
        headers={"X-CSRF-Token": csrf, "Origin": "https://localhost", "Sec-Fetch-Site": "same-origin"},
    ).status_code == 404


def test_successful_trash_writes_limited_action_metadata(monkeypatch):
    recorded = []

    def capture_action(_event_id, event):
        recorded.append(event)

    def fake_get(url, _headers, _timeout):
        if "/pages?" in url:
            return 200, [{
                "id": 3, "status": "publish", "type": "page",
                "title": {"rendered": "Development page"},
                "link": "https://example.test/dev", "modified": "2026-01-02T00:00:00",
            }], {"x-wp-total": "1", "x-wp-totalpages": "1"}
        return 200, {
            "id": 3, "status": "publish", "type": "page",
            "title": {"rendered": "Development page"},
            "link": "https://example.test/dev", "modified": "2026-01-02T00:00:00",
        }

    def fake_delete(_url, _headers, _timeout):
        return 200, {"id": 3, "status": "trash", "type": "page"}

    monkeypatch.setenv("WP_REST_ALLOW_IN_TESTS", "1")
    monkeypatch.setenv("WP_REST_ENABLED", "1")
    monkeypatch.setenv("WP_REST_WRITE_ENABLED", "1")
    monkeypatch.setenv("WP_REST_BASE_URL", "https://example.test")
    monkeypatch.setenv("WP_REST_USERNAME", "audit-user")
    monkeypatch.setenv("WP_REST_APPLICATION_PASSWORD", "application-password")
    monkeypatch.setattr("analyzer.wp_rest._http_get", fake_get)
    monkeypatch.setattr("analyzer.wp_rest._http_delete", fake_delete)

    client = _client_with_report()
    store = client.application.extensions["audit_store"]
    monkeypatch.setattr(store, "record_action", capture_action)
    assert client.post("/api/live-check", json={"ids": ["3"]}).status_code == 200
    assert client.post("/api/items/3/decision", json={"decision": "approved"}).status_code == 200
    assert client.post(
        "/api/trash",
        json={"ids": ["3"], "confirm": "trash", "csrf": _write_csrf(client)},
    ).status_code == 200
    assert len(recorded) == 1
    assert recorded[0]["wordpress_id"] == "3"
    assert recorded[0]["ok"] is True
    assert "title" not in recorded[0]
    assert "application-password" not in json.dumps(recorded[0])

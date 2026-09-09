from io import BytesIO

from app import create_app
from tests.test_analyzer import WXR


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
    detail = client.get("/api/items/3").get_json()
    assert detail["author_name"] == "Greg Crouch"
    assert detail["underlying_classification"] == "unreferenced"


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
    assert completed.get_json()["records"] == 4
    assert client.get("/api/items?group=media").get_json()["total"] == 1
    assert client.delete("/api/audit").status_code == 200
    assert client.get("/api/items").status_code == 404

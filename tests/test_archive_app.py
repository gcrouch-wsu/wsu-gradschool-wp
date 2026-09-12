from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import archive_app


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xml"


def test_local_archive_dashboard_inspects_export_and_removes_temporary_xml(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(archive_app, "DATA_ROOT", data_root)
    monkeypatch.setattr(archive_app, "UPLOAD_ROOT", data_root / "uploads")
    monkeypatch.setattr(archive_app, "JOB_ROOT", data_root / "jobs")
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert b"WordPress Media Archive" in page.data
    with client.session_transaction() as sess:
        csrf = sess["archive_csrf"]

    response = client.post(
        "/inspect",
        data={
            "csrf": csrf,
            "output_root": str(tmp_path / "archives"),
            "wxr_file": (BytesIO(FIXTURE.read_bytes()), "fresh-export.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Review and run the archive" in response.data
    assert b"cdn.test" in response.data
    assert not list((data_root / "uploads").glob("*.xml"))
    manifests = list((tmp_path / "archives").glob("*/manifest.json"))
    assert len(manifests) == 1
    assert archive_app._read_json(manifests[0])["source_export"]["filename"] == "fresh-export.xml"


def test_local_archive_dashboard_rejects_non_loopback_requests():
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.10"})
    assert response.status_code == 403


def test_local_archive_dashboard_rejects_post_without_session_csrf():
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.post(
        "/inspect",
        data={"csrf": "not-the-session-token"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403


def test_browser_origin_rewrite_does_not_block_valid_local_form():
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as sess:
        csrf = sess["archive_csrf"]
    response = client.post(
        "/inspect",
        data={"csrf": csrf},
        headers={"Origin": "https://managed-browser.invalid"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("?error=Choose+a+WordPress+WXR+XML+export.")


def test_local_archive_dashboard_accepts_ipv4_mapped_loopback(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(archive_app, "DATA_ROOT", data_root)
    monkeypatch.setattr(archive_app, "SESSION_KEY_PATH", data_root / "session.key")
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    response = app.test_client().get("/", environ_base={"REMOTE_ADDR": "::ffff:127.0.0.1"})
    assert response.status_code == 200


def test_local_archive_dashboard_accepts_browser_origin_headers(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(archive_app, "DATA_ROOT", data_root)
    monkeypatch.setattr(archive_app, "UPLOAD_ROOT", data_root / "uploads")
    monkeypatch.setattr(archive_app, "JOB_ROOT", data_root / "jobs")
    monkeypatch.setattr(archive_app, "SESSION_KEY_PATH", data_root / "session.key")
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as sess:
        csrf = sess["archive_csrf"]
    response = client.post(
        "/inspect",
        data={
            "csrf": csrf,
            "output_root": str(tmp_path / "archives"),
            "wxr_file": (BytesIO(FIXTURE.read_bytes()), "fresh-export.xml"),
        },
        content_type="multipart/form-data",
        headers={
            "Origin": "http://127.0.0.1:5055",
            "Referer": "http://127.0.0.1:5055/",
        },
    )
    assert response.status_code == 302


def test_browser_can_launch_planned_archive_without_second_security_failure(tmp_path, monkeypatch):
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

    def complete_archive(manifest_path, approved_hosts, **_kwargs):
        manifest = archive_app._read_json(manifest_path)
        manifest["status"] = "completed"
        manifest["summary"]["completed_assets"] = manifest["summary"]["assets"]
        return manifest

    monkeypatch.setattr(archive_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(archive_app, "archive_from_manifest", complete_archive)
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as sess:
        csrf = sess["archive_csrf"]
    headers = {
        "Origin": "http://127.0.0.1:5055",
        "Referer": "http://127.0.0.1:5055/",
    }
    planned = client.post(
        "/inspect",
        data={
            "csrf": csrf,
            "output_root": str(tmp_path / "archives"),
            "wxr_file": (BytesIO(FIXTURE.read_bytes()), "fresh-export.xml"),
        },
        content_type="multipart/form-data",
        headers=headers,
    )
    job_id = parse_qs(urlsplit(planned.headers["Location"]).query)["job"][0]

    started = client.post(
        f"/start/{job_id}",
        data={"csrf": csrf, "approved_host": "cdn.test"},
        headers=headers,
    )

    assert started.status_code == 302
    assert archive_app.load_job(job_id)["status"] == "completed"


def test_browser_can_run_representative_test_without_starting_full_archive(tmp_path, monkeypatch):
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

    def pass_test(manifest_path, approved_hosts, **_kwargs):
        assert approved_hosts == {"cdn.test"}
        result = {
            "status": "passed",
            "summary": {
                "selected_assets": 1,
                "completed_assets": 1,
                "failed_assets": 0,
                "blocked_assets": 0,
                "file_types": [".jpg"],
            },
            "assets": [],
        }
        archive_app._write_json(manifest_path.parent / "test-results.json", result)
        return result

    monkeypatch.setattr(archive_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(archive_app, "archive_test_from_manifest", pass_test)
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
    plan_page = client.get(f"/?job={job_id}")

    tested = client.post(
        f"/test/{job_id}",
        data={"csrf": csrf, "approved_host": "cdn.test"},
    )
    result_page = client.get(f"/?job={job_id}")
    job = archive_app.load_job(job_id)
    manifest = archive_app._read_json(Path(job["manifest_path"]))

    assert b"Test representative sample (1 file)" in plan_page.data
    assert b"media-owned samples 1; document-owned samples 0" in plan_page.data
    assert b".jpg" in plan_page.data
    assert b"cdn.test" in plan_page.data
    assert tested.status_code == 302
    assert job["test_status"] == "passed"
    assert job["status"] == "planned"
    assert job["approved_hosts"] == ["cdn.test"]
    assert manifest["status"] == "planned"
    assert b"Representative test: Passed" in result_page.data
    assert b"Start complete archive" in result_page.data


def test_outdated_archive_plan_cannot_be_started(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(archive_app, "DATA_ROOT", data_root)
    monkeypatch.setattr(archive_app, "UPLOAD_ROOT", data_root / "uploads")
    monkeypatch.setattr(archive_app, "JOB_ROOT", data_root / "jobs")
    monkeypatch.setattr(archive_app, "SESSION_KEY_PATH", data_root / "session.key")
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
    job = archive_app.load_job(job_id)
    manifest_path = Path(job["manifest_path"])
    manifest = archive_app._read_json(manifest_path)
    manifest["schema_version"] = 1
    archive_app._write_json(manifest_path, manifest)

    page = client.get(f"/?job={job_id}")
    started = client.post(
        f"/start/{job_id}",
        data={"csrf": csrf, "approved_host": "cdn.test"},
    )

    assert b"Fresh inspection required" in page.data
    assert b"Inspect export again to continue" in page.data
    assert started.status_code == 302
    assert "older+planner" in started.headers["Location"]
    assert archive_app.load_job(job_id)["status"] == "planned"


def test_local_archive_session_survives_launcher_restart(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(archive_app, "DATA_ROOT", data_root)
    monkeypatch.setattr(archive_app, "SESSION_KEY_PATH", data_root / "session.key")
    first_app = archive_app.create_archive_app()
    first_app.config.update(TESTING=True)
    first_client = first_app.test_client()
    first_client.get("/")
    with first_client.session_transaction() as sess:
        csrf = sess["archive_csrf"]
    session_cookie = first_client.get_cookie("session")

    second_app = archive_app.create_archive_app()
    second_app.config.update(TESTING=True)
    second_client = second_app.test_client()
    second_client.set_cookie("session", session_cookie.value)
    response = second_client.post("/inspect", data={"csrf": csrf})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("?error=Choose+a+WordPress+WXR+XML+export.")


def test_local_archive_expired_form_has_actionable_error():
    app = archive_app.create_archive_app()
    app.config.update(TESTING=True)
    response = app.test_client().post("/inspect", data={"csrf": "expired"})
    assert response.status_code == 403
    assert b"form expired" in response.data
    assert b"Reload the archive dashboard" in response.data

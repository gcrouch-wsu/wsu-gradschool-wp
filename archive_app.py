from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from defusedxml.common import DefusedXmlException
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from markupsafe import escape
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from analyzer.ids import is_loopback_address
from archive_tool import (
    ARCHIVE_SCHEMA_VERSION,
    ArchiveError,
    archive_folder_name,
    archive_from_manifest,
    archive_test_from_manifest,
    build_archive_plan,
    select_archive_test_assets,
    utc_now,
)


REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = REPO_ROOT / ".archive-data"
UPLOAD_ROOT = DATA_ROOT / "uploads"
JOB_ROOT = DATA_ROOT / "jobs"
SESSION_KEY_PATH = DATA_ROOT / "session.key"
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
jobs_lock = threading.RLock()
active_jobs: set[str] = set()


def local_session_secret() -> str:
    """Keep local form sessions valid when the launcher is restarted."""
    SESSION_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = SESSION_KEY_PATH.read_text(encoding="ascii").strip()
    except OSError:
        existing = ""
    if len(existing) >= 64:
        return existing
    value = secrets.token_hex(32)
    try:
        descriptor = os.open(SESSION_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        existing = SESSION_KEY_PATH.read_text(encoding="ascii").strip()
        if len(existing) < 64:
            raise RuntimeError("The local archive session key is invalid.")
        return existing
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(value)
    return value


def default_output_root() -> Path:
    documents = Path.home() / "Documents"
    return documents / "WordPress Media Archives"


def _job_path(job_id: str) -> Path:
    return JOB_ROOT / f"{job_id}.json"


def _read_json(path: Path) -> dict | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_job(job_id: str) -> dict | None:
    if not job_id or not job_id.isalnum():
        return None
    return _read_json(_job_path(job_id))


def save_job(job: dict) -> None:
    with jobs_lock:
        _write_json(_job_path(job["id"]), job)


def recent_jobs() -> list[dict]:
    """List saved plans whose archive folder still exists; drop records for deleted folders."""
    if not JOB_ROOT.exists():
        return []
    rows = []
    for path in JOB_ROOT.glob("*.json"):
        row = _read_json(path)
        if not row:
            continue
        if not Path(str(row.get("manifest_path") or "")).is_file():
            path.unlink(missing_ok=True)
            continue
        rows.append(row)
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)[:8]


def _registrable_domain(host: str) -> str:
    labels = host.casefold().strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.casefold()


def preferred_hosts(manifest: dict | None) -> set[str]:
    """Hosts pre-checked for approval: the site itself plus its sibling hosts (e.g. the WSU CDN).

    Approval stays explicit — every host is a visible checkbox — but a media
    host under the same institutional domain as the WordPress site is the
    expected download source rather than a surprise.
    """
    site = (manifest or {}).get("site", {})
    site_hosts = {
        (urlsplit(str(site.get(key) or "")).hostname or "").casefold()
        for key in ("site_url", "home_url")
    } - {""}
    site_domains = {_registrable_domain(host) for host in site_hosts}
    siblings = {
        str(host).casefold()
        for host in (manifest or {}).get("source_hosts", [])
        if _registrable_domain(str(host)) in site_domains
    }
    return site_hosts | siblings


def test_outcome_message(result: dict) -> str:
    """Summarize a representative test in one operator-facing sentence."""
    summary = result.get("summary", {})
    selected = int(summary.get("selected_assets", 0))
    completed = int(summary.get("completed_assets", 0))
    failed = int(summary.get("failed_assets", 0))
    blocked = int(summary.get("blocked_assets", 0))
    if result.get("status") == "passed":
        return f"Representative test passed. All {selected} selected files were downloaded and verified."
    parts = [f"Representative test failed: {completed} of {selected} files downloaded"]
    if failed:
        parts.append(f"{failed} failed")
    if blocked:
        parts.append(f"{blocked} blocked")
    message = ", ".join(parts) + "."
    unapproved = [str(host) for host in result.get("unapproved_sample_hosts", []) if host]
    if unapproved:
        message += (
            f" Host{'s' if len(unapproved) > 1 else ''} {', '.join(unapproved)} "
            f"{'were' if len(unapproved) > 1 else 'was'} not approved, so those samples were not requested; "
            "check the host above and run the test again."
        )
    statuses = {int(asset.get("http_status") or 0) for asset in result.get("assets", [])}
    if statuses & {401, 403}:
        message += (
            " Some files answered 401/403; if they belong on the WordPress host, retry with a WordPress "
            "username and Application Password."
        )
    return message + " Review each failed sample below before starting the complete archive."


def safe_output_root(raw_value: str) -> Path:
    value = Path(raw_value.strip()).expanduser()
    if not value.is_absolute():
        raise ValueError("Enter an absolute archive destination, such as a shared drive or Documents folder.")
    resolved = value.resolve()
    if resolved.parent == resolved:
        raise ValueError("Choose a folder rather than the root of a drive.")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("The archive destination is not a folder.")
    return resolved


def create_archive_app() -> Flask:
    app = Flask(__name__, static_folder="public", static_url_path="/static")
    app.secret_key = local_session_secret()
    app.jinja_env.globals["archive_schema_version"] = ARCHIVE_SCHEMA_VERSION
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )

    def csrf_token() -> str:
        token = session.get("archive_csrf")
        if not token:
            token = secrets.token_hex(32)
            session["archive_csrf"] = token
        return token

    @app.before_request
    def protect_loopback():
        csrf_token()
        if not is_loopback_address(request.remote_addr or ""):
            abort(403, description="This archive tool only accepts requests from this computer.")
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        supplied = str(request.form.get("csrf") or request.headers.get("X-CSRF-Token") or "")
        if not secrets.compare_digest(supplied, str(session.get("archive_csrf") or "")):
            abort(403, description="This form expired after the archive tool restarted. Reload the page and try again.")
        # Loopback-only access, a signed SameSite session, and the per-session
        # token provide the local CSRF boundary. Origin/Referer are deliberately
        # not required because privacy tools and managed browsers can rewrite
        # or suppress them on multipart file submissions.
        return None

    @app.errorhandler(403)
    def local_forbidden(error):
        return (
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Archive request blocked</title>"
            "<body style=\"font:16px system-ui;max-width:48rem;margin:4rem auto;padding:0 1rem\">"
            "<h1>Archive request blocked</h1>"
            f"<p>{escape(error.description)}</p>"
            "<p><a href=\"/\">Reload the archive dashboard</a></p></body></html>",
            403,
        )

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        return render_template(
            "archive.html",
            csrf=csrf_token(),
            error="The WXR export is larger than the 250 MB local archive limit.",
            output_root=str(default_output_root()),
            job=None,
            manifest=None,
            test_result=None,
            test_sample_count=0,
            test_sample_summary=None,
            preferred_hosts=set(),
            recent=recent_jobs(),
        ), 413

    @app.get("/")
    def index():
        job = load_job(request.args.get("job", ""))
        if job and job.get("status") == "running" and job["id"] not in active_jobs:
            job.update({
                "status": "incomplete",
                "updated_at": utc_now(),
                "message": "The previous local process stopped before finishing. Retry to verify completed files and resume.",
            })
            save_job(job)
        if job and job.get("test_status") == "running" and job["id"] not in active_jobs:
            job.update({
                "test_status": "interrupted",
                "updated_at": utc_now(),
                "test_message": "The previous test process stopped before finishing. Run the representative test again.",
            })
            save_job(job)
        manifest = _read_json(Path(job["manifest_path"])) if job and job.get("manifest_path") else None
        test_result = (
            _read_json(Path(job["test_results_path"]))
            if job and job.get("test_results_path") else None
        )
        try:
            test_sample = select_archive_test_assets(manifest) if manifest else []
        except ArchiveError:
            test_sample = []
        test_sample_summary = {
            "document": sum(asset.get("category") == "document" for asset in test_sample),
            "media": sum(asset.get("category") == "media" for asset in test_sample),
            "file_types": sorted({str(asset.get("file_type") or "") for asset in test_sample}),
            "source_hosts": sorted({str(asset.get("source_host") or "") for asset in test_sample}),
            "record_statuses": sorted({
                status for asset in test_sample for status in asset.get("record_statuses", [])
            }),
        }
        return render_template(
            "archive.html",
            csrf=csrf_token(),
            error=request.args.get("error", ""),
            output_root=str(default_output_root()),
            job=job,
            manifest=manifest,
            test_result=test_result,
            test_sample_count=len(test_sample),
            test_sample_summary=test_sample_summary,
            preferred_hosts=preferred_hosts(manifest),
            recent=recent_jobs(),
        )

    @app.post("/inspect")
    def inspect_export():
        uploaded = request.files.get("wxr_file")
        if not uploaded or not uploaded.filename:
            return redirect(url_for("index", error="Choose a WordPress WXR XML export."))
        filename = secure_filename(uploaded.filename)
        if not filename.casefold().endswith(".xml"):
            return redirect(url_for("index", error="The selected file must be a WordPress XML export."))
        try:
            output_root = safe_output_root(request.form.get("output_root", ""))
            output_root.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            return redirect(url_for("index", error=str(exc)))

        job_id = uuid4().hex
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        upload_path = UPLOAD_ROOT / f"{job_id}.xml"
        archive_dir = output_root / f"pending-{job_id[:8]}"
        uploaded.save(upload_path)
        try:
            manifest = build_archive_plan(upload_path, archive_dir, source_filename=filename)
            final_dir = output_root / archive_folder_name(manifest.get("site", {}).get("site_url", ""))
            if final_dir.exists():
                final_dir = output_root / f"{final_dir.name}-{job_id[:6]}"
            archive_dir.rename(final_dir)
            manifest_path = final_dir / "manifest.json"
            manifest = _read_json(manifest_path) or manifest
            job = {
                "id": job_id,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "planned",
                "message": "Review the source hosts and unresolved records, then start the archive.",
                "manifest_path": str(manifest_path),
                "output_dir": str(final_dir),
                "completed": 0,
                "total": int(manifest.get("summary", {}).get("assets", 0)),
            }
            save_job(job)
        except (DefusedXmlException, ArchiveError, OSError, ValueError) as exc:
            if archive_dir.exists():
                try:
                    archive_dir.rmdir()
                except OSError:
                    pass
            return redirect(url_for("index", error=f"The export could not be inspected: {exc}"))
        finally:
            upload_path.unlink(missing_ok=True)
        return redirect(url_for("index", job=job_id))

    def run_archive(job_id: str, approved_hosts: set[str], username: str, application_password: str) -> None:
        job = load_job(job_id)
        if not job:
            return

        def progress(completed: int, total: int, message: str) -> None:
            current = load_job(job_id) or job
            current.update({
                "status": "running",
                "updated_at": utc_now(),
                "completed": completed,
                "total": total,
                "message": message,
            })
            save_job(current)

        try:
            result = archive_from_manifest(
                Path(job["manifest_path"]),
                approved_hosts,
                username=username,
                application_password=application_password,
                progress=progress,
            )
            summary = result.get("summary", {})
            job = load_job(job_id) or job
            job.update({
                "status": result.get("status", "incomplete"),
                "updated_at": utc_now(),
                "completed": int(summary.get("completed_assets", 0)),
                "total": int(summary.get("assets", 0)),
                "message": (
                    "Archive completed and every planned file was verified."
                    if result.get("status") == "completed"
                    else "Archive is incomplete. Review unresolved records and failed or blocked files, then retry."
                ),
            })
            save_job(job)
        except Exception as exc:
            job = load_job(job_id) or job
            job.update({
                "status": "error",
                "updated_at": utc_now(),
                "message": f"Archive stopped: {exc}",
            })
            save_job(job)
        finally:
            with jobs_lock:
                active_jobs.discard(job_id)

    def run_archive_test(job_id: str, approved_hosts: set[str], username: str, application_password: str) -> None:
        job = load_job(job_id)
        if not job:
            return

        def progress(completed: int, total: int, message: str) -> None:
            current = load_job(job_id) or job
            current.update({
                "test_status": "running",
                "updated_at": utc_now(),
                "test_completed": completed,
                "test_total": total,
                "test_message": message,
            })
            save_job(current)

        try:
            result = archive_test_from_manifest(
                Path(job["manifest_path"]),
                approved_hosts,
                username=username,
                application_password=application_password,
                progress=progress,
            )
            summary = result.get("summary", {})
            job = load_job(job_id) or job
            job.update({
                "test_status": result.get("status", "failed"),
                "updated_at": utc_now(),
                "test_completed": int(summary.get("completed_assets", 0)),
                "test_total": int(summary.get("selected_assets", 0)),
                "test_results_path": str(Path(job["output_dir"]) / "test-results.json"),
                "test_run_id": result.get("run_id", ""),
                "test_message": test_outcome_message(result),
            })
            save_job(job)
        except Exception as exc:
            job = load_job(job_id) or job
            job.update({
                "test_status": "error",
                "updated_at": utc_now(),
                "test_results_path": str(Path(job["output_dir"]) / "test-results.json"),
                "test_message": (
                    f"Representative test stopped before finishing: {type(exc).__name__}: {exc}. "
                    "Results recorded so far are in test-results.json. Run the test again to retry."
                ),
            })
            save_job(job)
        finally:
            with jobs_lock:
                active_jobs.discard(job_id)

    @app.post("/test/<job_id>")
    def test_archive(job_id: str):
        job = load_job(job_id)
        if not job:
            abort(404)
        manifest = _read_json(Path(job["manifest_path"])) or {}
        if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            return redirect(url_for(
                "index", job=job_id,
                error="This archive plan was created by an older planner. Inspect the original WXR export again before testing files.",
            ))
        try:
            sample_count = len(select_archive_test_assets(manifest))
        except ArchiveError as exc:
            return redirect(url_for("index", job=job_id, error=str(exc)))
        source_hosts = {str(host).casefold() for host in manifest.get("source_hosts", [])}
        approved_hosts = {
            str(host).strip().casefold() for host in request.form.getlist("approved_host")
            if str(host).strip()
        }
        if not approved_hosts or not approved_hosts.issubset(source_hosts):
            return redirect(url_for("index", job=job_id, error="Approve at least one listed source host."))
        username = (request.form.get("wp_username") or "").strip()
        application_password = (request.form.get("wp_application_password") or "").strip()
        with jobs_lock:
            if job_id in active_jobs or job.get("status") == "running":
                return redirect(url_for("index", job=job_id))
            active_jobs.add(job_id)
            job.update({
                "test_status": "running",
                "updated_at": utc_now(),
                "test_completed": 0,
                "test_total": sample_count,
                "test_results_path": str(Path(job["output_dir"]) / "test-results.json"),
                "approved_hosts": sorted(approved_hosts),
                "test_message": "Starting the representative archive test.",
            })
            save_job(job)
        thread = threading.Thread(
            target=run_archive_test,
            args=(job_id, approved_hosts, username, application_password),
            daemon=True,
        )
        thread.start()
        return redirect(url_for("index", job=job_id))

    @app.post("/start/<job_id>")
    def start_archive(job_id: str):
        job = load_job(job_id)
        if not job:
            abort(404)
        manifest = _read_json(Path(job["manifest_path"])) or {}
        if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            return redirect(url_for(
                "index", job=job_id,
                error="This archive plan was created by an older planner. Inspect the original WXR export again before downloading files.",
            ))
        source_hosts = {str(host).casefold() for host in manifest.get("source_hosts", [])}
        approved_hosts = {
            str(host).strip().casefold() for host in request.form.getlist("approved_host")
            if str(host).strip()
        }
        if not approved_hosts or not approved_hosts.issubset(source_hosts):
            return redirect(url_for("index", job=job_id, error="Approve at least one listed source host."))
        username = (request.form.get("wp_username") or "").strip()
        application_password = (request.form.get("wp_application_password") or "").strip()
        with jobs_lock:
            if job_id in active_jobs or job.get("status") == "running":
                return redirect(url_for("index", job=job_id))
            active_jobs.add(job_id)
            job.update({
                "status": "running",
                "updated_at": utc_now(),
                "message": "Starting the local archive.",
                "approved_hosts": sorted(approved_hosts),
            })
            save_job(job)
        thread = threading.Thread(
            target=run_archive,
            args=(job_id, approved_hosts, username, application_password),
            daemon=True,
        )
        thread.start()
        return redirect(url_for("index", job=job_id))

    @app.get("/status/<job_id>")
    def archive_status(job_id: str):
        job = load_job(job_id)
        if not job:
            return jsonify({"error": "Archive job not found."}), 404
        manifest = _read_json(Path(job["manifest_path"])) or {}
        return jsonify({"job": job, "summary": manifest.get("summary", {})})

    @app.post("/open/<job_id>")
    def open_archive_folder(job_id: str):
        job = load_job(job_id)
        if not job:
            abort(404)
        target = Path(job["output_dir"]).resolve()
        if not target.is_dir():
            abort(404)
        if os.name == "nt":
            os.startfile(str(target))
        return redirect(url_for("index", job=job_id))

    return app


app = create_archive_app()

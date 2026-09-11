from __future__ import annotations

import csv
import io
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from defusedxml.common import DefusedXmlException
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from analyzer import analyze_export, parse_wxr
from analyzer.ids import LOOPBACK_HOSTS, is_loopback_address, sites_are_same, wordpress_id
from analyzer.wp_rest import MAX_TRASH_BATCH, client_from_env, live_check_items, rest_base_for, trash_items
from app_auth import load_users, make_dummy_hash, verify_credentials
from audit_store import AuditStore, RevisionConflict
from local_env import load_local_env, rest_enabled, rest_write_enabled


MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_PAGE_SIZE = 200
UPLOAD_CHUNK_BYTES = 3 * 1024 * 1024
MAX_UPLOAD_CHUNKS = (MAX_UPLOAD_BYTES + UPLOAD_CHUNK_BYTES - 1) // UPLOAD_CHUNK_BYTES
AUDIT_TTL_SECONDS = 48 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5
REVIEW_DECISIONS = {"unreviewed", "keep", "verify", "approved"}
LEGACY_REVIEW_DECISIONS = {"expected": "keep", "candidate": "verify"}
FINDING_PRIORITY = {
    "unreferenced": 0,
    "unreferenced-media": 1,
    "disconnected": 2,
    "needs-verification": 3,
    "non-public": 4,
    "linked": 5,
}
QUEUE_FINDINGS = {"unreferenced", "unreferenced-media", "disconnected"}
QUEUE_LIVE_STATES = {"missing", "trashed"}


def _expected_author_aliases() -> set[str]:
    if "WP_EXPECTED_DEVELOPMENT_AUTHORS" in os.environ:
        configured = os.environ.get("WP_EXPECTED_DEVELOPMENT_AUTHORS", "")
        return {alias.strip() for alias in configured.split(",") if alias.strip()}
    return {"greg crouch", "gcrouch"}


def _integer_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(request.args.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


def _spreadsheet_safe(value) -> str:
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


def create_app() -> Flask:
    load_local_env()
    app = Flask(__name__, static_folder="public", static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    if os.environ.get("VERCEL") and not os.environ.get("FLASK_SECRET_KEY"):
        raise RuntimeError("FLASK_SECRET_KEY is required on Vercel.")
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    app_users = load_users(os.environ.get("APP_AUTH_USERS_JSON", ""))
    dummy_password_hash = make_dummy_hash()
    app.extensions["app_users"] = app_users
    audit_store = AuditStore()
    app.extensions["audit_store"] = audit_store

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if os.environ.get("VERCEL"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        return response

    def write_csrf_token() -> str:
        token = session.get("write_csrf")
        if not token:
            token = secrets.token_hex(32)
            session["write_csrf"] = token
        return token

    def auth_enforced() -> bool:
        return not (
            app.config.get("TESTING")
            and os.environ.get("APP_AUTH_ALLOW_IN_TESTS", "") != "1"
        )

    def current_user_email() -> str:
        email = str(session.get("user_email") or "").casefold()
        return email if email in app_users else ""

    def request_csrf_token() -> str:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
        if supplied is None and request.is_json:
            supplied = (request.get_json(silent=True) or {}).get("csrf")
        return str(supplied or "")

    def authentication_response(status: int = 401):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Sign in is required."}), status
        return redirect(url_for("login"))

    @app.before_request
    def protect_request():
        write_csrf_token()
        if request.endpoint == "static":
            return None
        if request.endpoint == "login":
            if request.method == "POST" and not secrets.compare_digest(
                request_csrf_token(), str(session.get("write_csrf") or "")
            ):
                return render_template(
                    "login.html", csrf=write_csrf_token(), error="Refresh the page and sign in again.",
                    configuration_error="" if app_users else "No application users are configured.",
                ), 403
            return None
        if not auth_enforced():
            return None
        if not app_users:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Application sign-in is not configured."}), 503
            return redirect(url_for("login"))
        if not current_user_email():
            return authentication_response()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not secrets.compare_digest(
            request_csrf_token(), str(session.get("write_csrf") or "")
        ):
            return jsonify({"error": "Refresh the page and try again."}), 403
        return None

    def write_request_allowed() -> bool:
        origin = request.headers.get("Origin")
        if os.environ.get("VERCEL"):
            if not current_user_email() or not origin:
                return False
            try:
                parsed_origin = urlsplit(origin)
            except ValueError:
                return False
            if parsed_origin.scheme.casefold() != "https" or parsed_origin.netloc.casefold() != request.host.casefold():
                return False
            fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().casefold()
            if fetch_site and fetch_site != "same-origin":
                return False
            return True
        if not is_loopback_address(request.remote_addr or ""):
            return False
        if origin is not None:
            if origin.strip().casefold() in {"", "null"}:
                return False
            try:
                origin_host = (urlsplit(origin).hostname or "").casefold()
            except ValueError:
                return False
            if origin_host and origin_host not in LOOPBACK_HOSTS:
                return False
        referer = request.headers.get("Referer")
        if referer:
            try:
                referer_host = (urlsplit(referer).hostname or "").casefold()
            except ValueError:
                return False
            if referer_host and referer_host not in LOOPBACK_HOSTS:
                return False
        return True

    def rest_available() -> bool:
        return rest_enabled(testing=bool(app.config.get("TESTING")))

    def write_available() -> bool:
        return rest_write_enabled(testing=bool(app.config.get("TESTING")))

    def rest_context():
        return {
            "rest_enabled": rest_available(),
            "rest_write_enabled": write_available(),
            "write_csrf": write_csrf_token(),
        }

    def latest_state():
        audit_id = session.get("audit_id")
        state = audit_store.load_state(audit_id)
        if not state:
            return None
        if auth_enforced() and state.get("owner_email") != current_user_email():
            session.pop("audit_id", None)
            return None
        if state.get("expires_at") and float(state["expires_at"]) < time.time():
            audit_store.delete_audit(audit_id)
            session.pop("audit_id", None)
            return None
        return state

    def export_site_url(site: dict) -> str:
        """Use the WordPress installation URL; fall back to home only when absent."""
        return str(site.get("site_url") or site.get("home_url") or "")

    def export_matches_rest(site: dict, rest_url: str) -> bool:
        site_url = export_site_url(site)
        return bool(site_url and sites_are_same([site_url], rest_url))

    def live_snapshot_ready(row: dict, record: dict) -> bool:
        return bool(
            rest_base_for(row.get("type", ""))
            and record.get("live_state") == "found"
            and record.get("checked_at")
            and record.get("live_status")
            and record.get("live_modified")
            and record.get("live_type") == row.get("type")
        )

    def live_identity_matches(row: dict, record: dict) -> bool:
        live_title = str(record.get("live_title") or "").strip()
        export_title = str(row.get("title") or "").strip()
        return bool(
            live_snapshot_ready(row, record)
            and live_title
            and export_title
            and live_title.casefold() == export_title.casefold()
        )

    def record_trash_results(audit_id: str, site_url: str, items: list[dict], results: list[dict]) -> None:
        by_id = {str(item.get("id") or ""): item for item in items}
        attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for result in results:
            item = by_id.get(str(result.get("id") or ""), {})
            event = {
                "attempted_at": attempted_at,
                "user_email": current_user_email(),
                "audit_id": audit_id,
                "site_url": site_url,
                "wordpress_id": str(result.get("id") or ""),
                "post_type": str(item.get("type") or ""),
                "export_status": str(item.get("status") or ""),
                "ok": bool(result.get("ok")),
                "error": str(result.get("error") or ""),
            }
            try:
                audit_store.record_action(uuid4().hex, event)
            except Exception:
                app.logger.exception("WordPress Trash action could not be recorded")

    def analyze_and_store(stream, filename: str, audit_id: str) -> dict:
        started = perf_counter()
        export = parse_wxr(stream)
        report = analyze_export(export, _expected_author_aliases())
        report["analysis_seconds"] = round(perf_counter() - started, 2)
        audit_store.save_report(
            audit_id, report, filename,
            owner_email=current_user_email(),
            expires_at=time.time() + AUDIT_TTL_SECONDS,
        )
        session["audit_id"] = audit_id
        return report

    def filtered_rows(report: dict) -> list[dict]:
        rows = report["items"]
        group = request.args.get("group", "")
        queue_mode = request.args.get("queue", "").strip().lower() in {"1", "true", "yes"}
        classification = request.args.get("classification", "")
        status = request.args.get("status", "")
        author = request.args.get("author", "")
        subtype = request.args.get("subtype", "")
        file_type = request.args.get("file_type", "")
        taxonomy = request.args.get("taxonomy", "")
        term = request.args.get("term", "")
        decision = request.args.get("decision", "")
        live_filter = request.args.get("live", "")
        query = request.args.get("q", "").strip().casefold()
        current_state = latest_state()
        decisions = current_state["decisions"] if current_state else {}
        live = current_state["live"] if current_state else {}

        result = []
        for row in rows:
            if group and row["group"] != group:
                continue
            live_state = (live.get(row["id"]) or {}).get("live_state") or "unchecked"
            row_decision = LEGACY_REVIEW_DECISIONS.get(
                decisions.get(row["id"], "unreviewed"),
                decisions.get(row["id"], "unreviewed"),
            )
            findings = {row["classification"], row.get("underlying_classification") or row["classification"]}
            if queue_mode and (
                findings.isdisjoint(QUEUE_FINDINGS)
                and live_state not in QUEUE_LIVE_STATES
                and row_decision != "approved"
            ):
                continue
            if classification == "expected-development":
                if not row.get("expected_development"):
                    continue
            elif classification and row["classification"] != classification:
                continue
            if status and row["status"] != status:
                continue
            if author and (row["author_name"] or row["author_login"] or "Unknown") != author:
                continue
            if subtype and row["content_class"] != subtype:
                continue
            if file_type and (row.get("file_extension") or "<none>").lower() != file_type:
                continue
            if taxonomy and taxonomy not in row["taxonomy_terms"]:
                continue
            assigned = row["taxonomy_terms"].get(taxonomy, [])
            slugs = row.get("taxonomy_slugs", {}).get(taxonomy, [])
            if term and term not in assigned and term not in slugs:
                continue
            if decision and row_decision != decision:
                continue
            if live_filter and live_state != live_filter:
                continue
            if query:
                searchable = " ".join(
                    str(value)
                    for value in (
                        row["id"], row["title"], row["slug"], row["url"],
                        row["file_name"], row["author_name"], row["author_login"],
                        row["mime_type"], row["categories"], row["tags"], row["taxonomies"],
                        row.get("taxonomy_slugs"),
                    )
                ).casefold()
                if query not in searchable:
                    continue
            result.append(row)

        sort = request.args.get("sort", "priority")
        if sort == "modified-desc":
            result.sort(key=lambda row: (row["modified"] or "", row["title"].casefold()), reverse=True)
        elif sort == "modified-asc":
            result.sort(key=lambda row: (row["modified"] or "", row["title"].casefold()))
        elif sort == "title":
            result.sort(key=lambda row: (row["title"].casefold(), row["id"]))
        elif sort == "author":
            result.sort(key=lambda row: ((row["author_name"] or row["author_login"]).casefold(), row["title"].casefold()))
        else:
            result.sort(key=lambda row: (FINDING_PRIORITY.get(row["classification"], 99), row["title"].casefold()))
        return result

    def list_payload(row: dict, decisions: dict[str, str], live: dict[str, dict]) -> dict:
        fields = (
            "id", "group", "group_label", "type", "content_class", "title", "url",
            "wp_admin_url", "file_name", "file_size", "width", "height", "mime_type", "file_extension",
            "author_name", "author_login", "status", "created", "modified", "categories",
            "tags", "taxonomy_terms", "taxonomy_slugs", "classification", "underlying_classification",
            "confidence", "inbound_strong", "inbound_possible", "inbound_structural",
            "outbound", "expected_development", "derivative_count",
        )
        payload = {field: row.get(field) for field in fields}
        stored_decision = decisions.get(row["id"], "unreviewed")
        payload["review_decision"] = LEGACY_REVIEW_DECISIONS.get(stored_decision, stored_decision)
        record = live.get(row["id"]) or {}
        payload["live_state"] = record.get("live_state") or "unchecked"
        payload["live_status"] = record.get("live_status") or ""
        payload["live_type"] = record.get("live_type") or ""
        payload["live_title"] = record.get("live_title") or ""
        payload["live_link"] = record.get("live_link") or ""
        payload["live_modified"] = record.get("live_modified") or ""
        payload["live_found"] = bool(record.get("live_found"))
        payload["live_checked_at"] = record.get("checked_at") or ""
        payload["live_snapshot_ready"] = live_snapshot_ready(row, record)
        payload["live_identity_matches"] = live_identity_matches(row, record)
        payload["can_trash"] = bool(
            payload["live_identity_matches"]
            and payload["review_decision"] == "approved"
        )
        if record.get("wp_admin_url"):
            payload["wp_admin_url"] = record["wp_admin_url"]
        return payload

    def merge_live(row: dict, decisions: dict[str, str], live: dict[str, dict]) -> dict:
        exported = dict(row)
        stored_decision = decisions.get(row["id"], "unreviewed")
        exported["review_decision"] = LEGACY_REVIEW_DECISIONS.get(stored_decision, stored_decision)
        record = live.get(row["id"]) or {}
        exported["live_state"] = record.get("live_state") or "unchecked"
        exported["live_status"] = record.get("live_status") or ""
        exported["live_type"] = record.get("live_type") or ""
        exported["live_title"] = record.get("live_title") or ""
        exported["live_link"] = record.get("live_link") or ""
        exported["live_modified"] = record.get("live_modified") or ""
        exported["live_found"] = bool(record.get("live_found"))
        exported["live_error"] = record.get("error") or ""
        exported["live_checked_at"] = record.get("checked_at") or ""
        exported["live_snapshot_ready"] = live_snapshot_ready(row, record)
        exported["live_identity_matches"] = live_identity_matches(row, record)
        exported["can_trash"] = bool(
            exported["live_identity_matches"]
            and exported["review_decision"] == "approved"
        )
        if record.get("wp_admin_url"):
            exported["wp_admin_url"] = record["wp_admin_url"]
        return exported

    def template_kwargs(**extra):
        payload = {
            "storage_mode": audit_store.mode,
            "chunk_size": UPLOAD_CHUNK_BYTES,
            "current_user_email": current_user_email(),
            **rest_context(),
        }
        payload.update(extra)
        return payload

    @app.route("/login", methods=["GET", "POST"])
    def login():
        configuration_error = "" if app_users else "No application users are configured."
        if request.method == "GET":
            if current_user_email():
                return redirect(url_for("index"))
            return render_template(
                "login.html", csrf=write_csrf_token(), error="", configuration_error=configuration_error,
            ), 503 if configuration_error else 200

        now = time.time()
        window_started = float(session.get("login_window_started") or now)
        failures = int(session.get("login_failures") or 0)
        if now - window_started > LOGIN_WINDOW_SECONDS:
            window_started, failures = now, 0
        if failures >= MAX_LOGIN_FAILURES:
            return render_template(
                "login.html", csrf=write_csrf_token(),
                error="Too many sign-in attempts. Wait 15 minutes and try again.",
                configuration_error=configuration_error,
            ), 429
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        authenticated_email = verify_credentials(app_users, email, password, dummy_password_hash)
        if not authenticated_email:
            session["login_window_started"] = window_started
            session["login_failures"] = failures + 1
            return render_template(
                "login.html", csrf=write_csrf_token(),
                error="The email address or password was not accepted.",
                configuration_error=configuration_error,
            ), 401
        session.clear()
        session.permanent = True
        session["user_email"] = authenticated_email
        session["authenticated_at"] = int(now)
        write_csrf_token()
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "GET":
            state = latest_state()
            return render_template(
                "index.html",
                **template_kwargs(
                    report=state["report"] if state else None,
                    error=None,
                    filename=state["filename"] if state else None,
                ),
            )

        uploaded = request.files.get("export_file")
        if uploaded is None or not uploaded.filename:
            return render_template(
                "index.html",
                **template_kwargs(
                    report=None,
                    error="Choose a WordPress WXR (.xml) export before starting the analysis.",
                    filename=None,
                ),
            ), 400
        if not uploaded.filename.lower().endswith(".xml"):
            return render_template(
                "index.html",
                **template_kwargs(
                    report=None,
                    error="The selected file must be a WordPress XML export.",
                    filename=uploaded.filename,
                ),
            ), 400

        try:
            previous_id = session.get("audit_id")
            audit_id = uuid4().hex
            report = analyze_and_store(uploaded.stream, uploaded.filename, audit_id)
            if previous_id and previous_id != audit_id:
                audit_store.delete_audit(previous_id)
            return render_template(
                "index.html",
                **template_kwargs(report=report, error=None, filename=uploaded.filename),
            )
        except (DefusedXmlException, ValueError) as exc:
            message = f"The export could not be analyzed: {exc}"
        except Exception:
            app.logger.exception("Unexpected WXR analysis failure")
            message = "The export could not be analyzed because of an unexpected parsing error."
        return render_template(
            "index.html",
            **template_kwargs(report=None, error=message, filename=uploaded.filename),
        ), 400

    @app.post("/api/upload-session")
    def api_upload_session():
        data = request.get_json(silent=True) or {}
        filename = str(data.get("filename", "")).strip()
        try:
            size = int(data.get("size", 0))
            total_chunks = int(data.get("total_chunks", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid upload metadata."}), 400
        expected_chunks = (size + UPLOAD_CHUNK_BYTES - 1) // UPLOAD_CHUNK_BYTES if size else 0
        if not filename.lower().endswith(".xml"):
            return jsonify({"error": "Choose a WordPress XML export."}), 400
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            return jsonify({"error": "The export must be between 1 byte and 250 MB."}), 400
        if total_chunks != expected_chunks or total_chunks > MAX_UPLOAD_CHUNKS:
            return jsonify({"error": "Invalid upload chunk count."}), 400
        previous_pending = session.get("pending_upload") or {}
        if previous_pending.get("id"):
            try:
                audit_store.delete_chunks(previous_pending["id"], previous_pending.get("total_chunks", 0))
            except Exception:
                app.logger.exception("Abandoned upload chunks could not be removed")
        upload_id = uuid4().hex
        session["pending_upload"] = {
            "id": upload_id, "filename": filename, "size": size,
            "total_chunks": total_chunks, "previous_audit_id": session.get("audit_id"),
            "owner_email": current_user_email(),
        }
        return jsonify({"upload_id": upload_id, "chunk_size": UPLOAD_CHUNK_BYTES})

    @app.post("/api/upload-chunk/<int:index>")
    def api_upload_chunk(index: int):
        pending = session.get("pending_upload") or {}
        if (
            not pending
            or (auth_enforced() and pending.get("owner_email") != current_user_email())
            or index < 0
            or index >= pending.get("total_chunks", 0)
        ):
            return jsonify({"error": "Upload session is missing or the chunk index is invalid."}), 400
        payload = request.get_data(cache=False)
        if not payload or len(payload) > UPLOAD_CHUNK_BYTES:
            return jsonify({"error": "The upload chunk is empty or too large."}), 400
        if index < pending["total_chunks"] - 1 and len(payload) != UPLOAD_CHUNK_BYTES:
            return jsonify({"error": "An intermediate upload chunk has the wrong size."}), 400
        audit_store.put_chunk(pending["id"], index, payload)
        return jsonify({"received": index, "bytes": len(payload)})

    @app.post("/api/complete-upload")
    def api_complete_upload():
        pending = session.get("pending_upload") or {}
        if not pending or (auth_enforced() and pending.get("owner_email") != current_user_email()):
            return jsonify({"error": "Upload session is missing or expired."}), 400
        upload_id = pending["id"]
        try:
            assembled = BytesIO()
            for index in range(pending["total_chunks"]):
                chunk = audit_store.get_chunk(upload_id, index)
                if chunk is None:
                    return jsonify({"error": f"Upload chunk {index + 1} is missing."}), 400
                assembled.write(chunk)
            if assembled.tell() != pending["size"]:
                return jsonify({"error": "The assembled upload size does not match the selected file."}), 400
            assembled.seek(0)
            report = analyze_and_store(assembled, pending["filename"], upload_id)
            previous_id = pending.get("previous_audit_id")
            if previous_id and previous_id != upload_id:
                audit_store.delete_audit(previous_id)
            session.pop("pending_upload", None)
            return jsonify({
                "ok": True, "redirect": "/", "records": report["stats"]["reportable_items"],
                "analysis_seconds": report["analysis_seconds"],
            })
        except (DefusedXmlException, ValueError) as exc:
            return jsonify({"error": f"The export could not be analyzed: {exc}"}), 400
        except Exception:
            app.logger.exception("Unexpected chunked WXR analysis failure")
            return jsonify({"error": "The export could not be analyzed because of an unexpected error."}), 500
        finally:
            try:
                audit_store.delete_chunks(upload_id, pending["total_chunks"])
            except Exception:
                app.logger.exception("Temporary upload chunks could not be removed")

    @app.delete("/api/audit")
    def api_delete_audit():
        audit_id = session.get("audit_id")
        if audit_id and auth_enforced() and latest_state() is None:
            return jsonify({"error": "This audit is unavailable."}), 404
        pending = session.get("pending_upload") or {}
        if pending.get("id"):
            try:
                audit_store.delete_chunks(pending["id"], pending.get("total_chunks", 0))
            except Exception:
                app.logger.exception("Pending upload chunks could not be removed")
        audit_store.delete_audit(audit_id)
        session.pop("audit_id", None)
        session.pop("pending_upload", None)
        return jsonify({"ok": True})

    @app.get("/api/items")
    def api_items():
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        report = state["report"]
        all_group_rows = [
            row for row in report["items"]
            if not request.args.get("group") or row["group"] == request.args.get("group")
        ]
        rows = filtered_rows(report)
        page = _integer_arg("page", 1, 1, 1_000_000)
        page_size = _integer_arg("page_size", 50, 10, MAX_PAGE_SIZE)
        start = (page - 1) * page_size
        decisions = state["decisions"]
        live = state["live"]
        facets = {
            "authors": sorted({row["author_name"] or row["author_login"] or "Unknown" for row in all_group_rows}, key=str.casefold),
            "statuses": sorted({row["status"] or "unknown" for row in all_group_rows}),
            "classifications": sorted(
                {row["classification"] for row in all_group_rows}
                | ({"expected-development"} if any(row.get("expected_development") for row in all_group_rows) else set())
            ),
            "subtypes": sorted({row["content_class"] for row in all_group_rows}),
            "file_types": sorted({(row.get("file_extension") or "<none>").lower() for row in all_group_rows}),
            "decisions": sorted(REVIEW_DECISIONS),
            "live": ["unchecked", "found", "trashed", "missing", "not-in-rest", "error"],
        }
        return jsonify(
            {
                "items": [list_payload(row, decisions, live) for row in rows[start:start + page_size]],
                "total": len(rows),
                "page": page,
                "page_size": page_size,
                "page_count": max(1, (len(rows) + page_size - 1) // page_size),
                "facets": facets,
                "rest_enabled": rest_available(),
                "rest_write_enabled": write_available(),
                "queue": request.args.get("queue", "").strip().lower() in {"1", "true", "yes"},
            }
        )

    @app.get("/api/items/ids")
    def api_item_ids():
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        rows = filtered_rows(state["report"])
        return jsonify({"ids": [row["id"] for row in rows], "total": len(rows)})

    @app.get("/api/items/<item_id>")
    def api_item(item_id: str):
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        row = next((row for row in state["report"]["items"] if row["id"] == item_id), None)
        if row is None:
            return jsonify({"error": "Item not found."}), 404
        payload = merge_live(row, state["decisions"], state["live"])
        return jsonify(payload)

    @app.get("/api/taxonomies/<group_id>")
    def api_taxonomies(group_id: str):
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        return jsonify({"group": group_id, "taxonomies": state["report"]["taxonomies_by_group"].get(group_id, [])})

    @app.post("/api/items/<item_id>/decision")
    def api_decision(item_id: str):
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        if not any(row["id"] == item_id for row in state["report"]["items"]):
            return jsonify({"error": "Item not found."}), 404
        data = request.get_json(silent=True) or {}
        decision = data.get("decision", "")
        if decision not in REVIEW_DECISIONS:
            return jsonify({"error": "Invalid review decision."}), 400
        if decision == "unreviewed":
            state["decisions"].pop(item_id, None)
        else:
            state["decisions"][item_id] = decision
        try:
            audit_store.save_decisions(
                session["audit_id"], state["decisions"], expected_rev=state.get("decision_rev")
            )
        except RevisionConflict:
            return jsonify({"error": "Another review update was saved first. Reload and try again."}), 409
        return jsonify({"id": item_id, "decision": decision})

    @app.post("/api/live-check")
    def api_live_check():
        if not rest_available():
            return jsonify({"error": "WordPress REST is not configured."}), 403
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        data = request.get_json(silent=True) or {}
        group = str(data.get("group") or request.args.get("group") or "").strip()
        ids_supplied = "ids" in data
        raw_ids = data.get("ids")
        if ids_supplied:
            if not isinstance(raw_ids, list) or not raw_ids:
                return jsonify({"error": "Choose one or more valid WordPress records to check."}), 400
            parsed_ids = [wordpress_id(value) for value in raw_ids]
            if any(item_id is None for item_id in parsed_ids):
                return jsonify({"error": "Each live-check ID must be a positive WordPress ID."}), 400
            wanted = set(parsed_ids)
        else:
            wanted = set()
            if not group:
                return jsonify({"error": "Choose a content group or one or more records to check."}), 400
        site = state["report"].get("site") or {}
        rest_url = os.environ.get("WP_REST_BASE_URL", "")
        if not export_matches_rest(site, rest_url):
            return jsonify({"error": "This export is not from the WordPress site configured for REST."}), 409
        items = [
            row for row in state["report"]["items"]
            if (not group or row["group"] == group) and (not wanted or row["id"] in wanted)
        ]
        try:
            site_url = export_site_url(site)
            results = live_check_items(items, client_from_env(), site_url=site_url)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception:
            app.logger.exception("WordPress REST live check failed")
            return jsonify({"error": "The WordPress live check failed."}), 500
        live = dict(state["live"])
        live.update(results)
        try:
            audit_store.save_live(session["audit_id"], live, expected_rev=state.get("live_rev"))
        except RevisionConflict:
            return jsonify({"error": "Another live check was saved first. Reload and try again."}), 409
        counts = {
            "checked": len(results),
            "found": sum(item["live_state"] == "found" for item in results.values()),
            "trashed": sum(item["live_state"] == "trashed" for item in results.values()),
            "missing": sum(item["live_state"] == "missing" for item in results.values()),
            "not_in_rest": sum(item["live_state"] == "not-in-rest" for item in results.values()),
            "error": sum(item["live_state"] == "error" for item in results.values()),
        }
        return jsonify({"ok": True, "group": group, **counts})

    @app.post("/api/trash")
    def api_trash():
        if not write_available():
            return jsonify({"error": "WordPress Trash is not enabled for this environment."}), 403
        if not write_request_allowed():
            return jsonify({"error": "WordPress Trash requires an authenticated same-origin request."}), 403
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        data = request.get_json(silent=True) or {}
        if str(data.get("csrf") or "") != session.get("write_csrf"):
            return jsonify({"error": "Refresh the page and confirm Trash again."}), 403
        if str(data.get("confirm") or "") != "trash":
            return jsonify({"error": "Confirm moving these records to WordPress Trash."}), 400
        raw_ids = data.get("ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"error": "Choose one or more records to move to Trash."}), 400
        ids = []
        for raw_id in raw_ids:
            safe_id = wordpress_id(raw_id)
            if not safe_id:
                return jsonify({"error": "Each record ID must be a positive WordPress ID."}), 400
            ids.append(safe_id)
        if not ids:
            return jsonify({"error": "Choose one or more records to move to Trash."}), 400
        if len(ids) > MAX_TRASH_BATCH:
            return jsonify({"error": f"Move at most {MAX_TRASH_BATCH} records to Trash at a time."}), 400
        by_id = {row["id"]: row for row in state["report"]["items"]}
        missing = [item_id for item_id in ids if item_id not in by_id]
        if missing:
            return jsonify({"error": f"Unknown record ID: {missing[0]}."}), 404
        site = state["report"].get("site") or {}
        site_url = export_site_url(site)
        rest_url = os.environ.get("WP_REST_BASE_URL", "")
        if not export_matches_rest(site, rest_url):
            return jsonify({"error": "This export is not from the WordPress site configured for REST writes."}), 409
        not_ready = [
            item_id for item_id in ids
            if not live_identity_matches(by_id[item_id], state["live"].get(item_id) or {})
            or LEGACY_REVIEW_DECISIONS.get(
                state["decisions"].get(item_id, "unreviewed"),
                state["decisions"].get(item_id, "unreviewed"),
            ) != "approved"
        ]
        if not_ready:
            return jsonify({
                "error": (
                    f"Record {not_ready[0]} needs a complete fresh live check that matches the export and must be "
                    "approved to delete before Trash."
                )
            }), 400
        unsupported = [item_id for item_id in ids if not rest_base_for(by_id[item_id].get("type", ""))]
        if unsupported:
            return jsonify({"error": f"Record {unsupported[0]} cannot be trashed through WordPress REST."}), 400
        items = [by_id[item_id] for item_id in ids]
        try:
            results = trash_items(
                items,
                client_from_env(),
                site_url=site_url,
                expected_live=state["live"],
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception:
            app.logger.exception("WordPress REST trash failed")
            return jsonify({"error": "Moving records to WordPress Trash failed."}), 500
        record_trash_results(session["audit_id"], site_url, items, results)
        live = dict(state["live"])
        for result in results:
            if result.get("ok") and result.get("live"):
                live[result["id"]] = result["live"]
        try:
            audit_store.save_live(session["audit_id"], live, expected_rev=state.get("live_rev"))
        except RevisionConflict:
            latest = audit_store.load_state(session["audit_id"]) or state
            merged = dict(latest.get("live") or {})
            for result in results:
                if result.get("ok") and result.get("live"):
                    merged[result["id"]] = result["live"]
            try:
                audit_store.save_live(session["audit_id"], merged, expected_rev=latest.get("live_rev"))
            except RevisionConflict:
                app.logger.exception("Live overlay could not be saved after WordPress Trash")
        if not any(result["ok"] for result in results):
            return jsonify({
                "error": results[0]["error"] if results else "Moving records to WordPress Trash failed.",
                "trashed": 0,
                "failed": len(results),
                "results": results,
            }), 400
        return jsonify({
            "ok": all(result["ok"] for result in results),
            "trashed": sum(result["ok"] for result in results),
            "failed": sum(not result["ok"] for result in results),
            "results": results,
        })

    @app.get("/api/export.<format_name>")
    def api_export(format_name: str):
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        rows = filtered_rows(state["report"])
        decisions = state["decisions"]
        live = state["live"]
        if format_name == "json":
            def generate_json():
                yield "[\n"
                for index, row in enumerate(rows):
                    if index:
                        yield ",\n"
                    yield json.dumps(merge_live(row, decisions, live), ensure_ascii=False, separators=(",", ":"))
                yield "\n]\n"

            return Response(
                generate_json(),
                mimetype="application/json",
                headers={"Content-Disposition": "attachment; filename=wordpress-content-audit.json"},
            )
        if format_name != "csv":
            return jsonify({"error": "Unsupported export format."}), 404

        fields = (
            "id", "group_label", "type", "content_class", "title", "url", "wp_admin_url",
            "file_name", "mime_type", "file_extension", "file_size", "width", "height", "stored_path",
            "alt_text", "caption", "derivative_count", "status", "author_name", "author_login",
            "author_email", "created", "created_gmt", "modified", "modified_gmt", "parent_id",
            "categories", "tags", "taxonomies", "taxonomy_terms", "meta_keys",
            "classification", "underlying_classification", "confidence", "reasons", "recommendation",
            "inbound_strong", "inbound_possible", "inbound_structural", "outbound",
            "review_decision", "live_state", "live_status", "live_type", "live_title",
            "live_link", "live_modified", "live_checked_at", "live_snapshot_ready", "live_identity_matches",
        )
        def generate_csv():
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            yield "\ufeff" + output.getvalue()
            for row in rows:
                output.seek(0)
                output.truncate(0)
                writer.writerow({field: _spreadsheet_safe(merge_live(row, decisions, live).get(field)) for field in fields})
                yield output.getvalue()

        return Response(
            generate_csv(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=wordpress-content-audit.csv"},
        )

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return render_template(
            "index.html",
            **template_kwargs(
                report=None,
                error="The selected export is larger than the 250 MB upload limit.",
                filename=None,
            ),
        ), 413

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"},
        use_reloader=False,
    )

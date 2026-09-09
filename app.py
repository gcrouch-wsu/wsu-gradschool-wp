from __future__ import annotations

import csv
import io
import json
import os
import secrets
from io import BytesIO
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from defusedxml.common import DefusedXmlException
from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from analyzer import analyze_export, parse_wxr
from analyzer.ids import LOOPBACK_HOSTS, sites_are_same, wordpress_id
from analyzer.wp_rest import MAX_TRASH_BATCH, client_from_env, live_check_items, rest_base_for, trash_items
from audit_store import AuditStore
from local_env import load_local_env, rest_enabled, rest_write_enabled


MAX_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_PAGE_SIZE = 200
UPLOAD_CHUNK_BYTES = 3 * 1024 * 1024
MAX_UPLOAD_CHUNKS = (MAX_UPLOAD_BYTES + UPLOAD_CHUNK_BYTES - 1) // UPLOAD_CHUNK_BYTES
REVIEW_DECISIONS = {"unreviewed", "keep", "expected", "verify", "candidate", "approved"}
FINDING_PRIORITY = {
    "unreferenced": 0,
    "unreferenced-media": 1,
    "disconnected": 2,
    "needs-verification": 3,
    "expected-development": 4,
    "non-public": 5,
    "linked": 6,
}
QUEUE_FINDINGS = {"unreferenced", "unreferenced-media", "disconnected"}
QUEUE_LIVE_STATES = {"missing", "trashed"}


def _expected_author_aliases() -> set[str]:
    configured = os.environ.get("WP_EXPECTED_DEVELOPMENT_AUTHORS", "greg crouch,gcrouch")
    return {alias.strip() for alias in configured.split(",") if alias.strip()}


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
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


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
    )
    audit_store = AuditStore()
    app.extensions["audit_store"] = audit_store

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        return response

    def write_request_allowed() -> bool:
        if app.config.get("TESTING"):
            return True
        host = (request.host or "").split(":")[0].casefold()
        if host not in LOOPBACK_HOSTS:
            return False
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if not origin:
            return True
        try:
            origin_host = (urlsplit(origin).hostname or "").casefold()
        except ValueError:
            return False
        return not origin_host or origin_host in LOOPBACK_HOSTS

    def rest_available() -> bool:
        return rest_enabled(testing=bool(app.config.get("TESTING")))

    def write_available() -> bool:
        return rest_write_enabled(testing=bool(app.config.get("TESTING")))

    def rest_context():
        return {
            "rest_enabled": rest_available(),
            "rest_write_enabled": write_available(),
        }

    def latest_state():
        return audit_store.load_state(session.get("audit_id"))

    def analyze_and_store(stream, filename: str, audit_id: str) -> dict:
        started = perf_counter()
        export = parse_wxr(stream)
        report = analyze_export(export, _expected_author_aliases())
        report["analysis_seconds"] = round(perf_counter() - started, 2)
        audit_store.save_report(audit_id, report, filename)
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
            row_decision = decisions.get(row["id"], "unreviewed")
            findings = {row["classification"], row.get("underlying_classification") or row["classification"]}
            if queue_mode and (
                findings.isdisjoint(QUEUE_FINDINGS)
                and live_state not in QUEUE_LIVE_STATES
                and row_decision not in {"candidate", "approved"}
            ):
                continue
            if classification and row["classification"] != classification:
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
            if term and term not in row["taxonomy_terms"].get(taxonomy, []):
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
            "tags", "taxonomy_terms", "classification", "underlying_classification",
            "confidence", "inbound_strong", "inbound_possible", "inbound_structural",
            "outbound", "expected_development", "derivative_count",
        )
        payload = {field: row.get(field) for field in fields}
        payload["review_decision"] = decisions.get(row["id"], "unreviewed")
        record = live.get(row["id"]) or {}
        payload["live_state"] = record.get("live_state") or "unchecked"
        payload["live_status"] = record.get("live_status") or ""
        payload["live_link"] = record.get("live_link") or ""
        payload["live_found"] = bool(record.get("live_found"))
        payload["can_trash"] = bool(
            rest_base_for(row.get("type", ""))
            and payload["live_state"] not in {"trashed", "not-in-rest"}
        )
        if record.get("wp_admin_url"):
            payload["wp_admin_url"] = record["wp_admin_url"]
        return payload

    def merge_live(row: dict, decisions: dict[str, str], live: dict[str, dict]) -> dict:
        exported = dict(row)
        exported["review_decision"] = decisions.get(row["id"], "unreviewed")
        record = live.get(row["id"]) or {}
        exported["live_state"] = record.get("live_state") or "unchecked"
        exported["live_status"] = record.get("live_status") or ""
        exported["live_link"] = record.get("live_link") or ""
        exported["live_found"] = bool(record.get("live_found"))
        exported["live_error"] = record.get("error") or ""
        exported["live_checked_at"] = record.get("checked_at") or ""
        exported["can_trash"] = bool(
            rest_base_for(row.get("type", ""))
            and exported["live_state"] not in {"trashed", "not-in-rest"}
        )
        if record.get("wp_admin_url"):
            exported["wp_admin_url"] = record["wp_admin_url"]
        return exported

    def template_kwargs(**extra):
        payload = {
            "storage_mode": audit_store.mode,
            "chunk_size": UPLOAD_CHUNK_BYTES,
            **rest_context(),
        }
        payload.update(extra)
        return payload

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
            audit_id = uuid4().hex
            report = analyze_and_store(uploaded.stream, uploaded.filename, audit_id)
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
        upload_id = uuid4().hex
        session["pending_upload"] = {
            "id": upload_id, "filename": filename, "size": size,
            "total_chunks": total_chunks, "previous_audit_id": session.get("audit_id"),
        }
        return jsonify({"upload_id": upload_id, "chunk_size": UPLOAD_CHUNK_BYTES})

    @app.post("/api/upload-chunk/<int:index>")
    def api_upload_chunk(index: int):
        pending = session.get("pending_upload") or {}
        if not pending or index < 0 or index >= pending.get("total_chunks", 0):
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
        if not pending:
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
        audit_store.delete_audit(session.get("audit_id"))
        session.clear()
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
            "classifications": sorted({row["classification"] for row in all_group_rows}),
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
        audit_store.save_decisions(session["audit_id"], state["decisions"])
        return jsonify({"id": item_id, "decision": decision})

    @app.post("/api/live-check")
    def api_live_check():
        if not rest_available():
            return jsonify({"error": "Local WordPress REST is not configured."}), 403
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        data = request.get_json(silent=True) or {}
        group = str(data.get("group") or request.args.get("group") or "").strip()
        items = [
            row for row in state["report"]["items"]
            if not group or row["group"] == group
        ]
        try:
            site = state["report"].get("site") or {}
            site_url = site.get("site_url") or site.get("home_url") or ""
            results = live_check_items(items, client_from_env(), site_url=site_url)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception:
            app.logger.exception("WordPress REST live check failed")
            return jsonify({"error": "The WordPress live check failed."}), 500
        live = dict(state["live"])
        live.update(results)
        audit_store.save_live(session["audit_id"], live)
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
            return jsonify({"error": "Local WordPress Trash is not enabled. Set WP_REST_WRITE_ENABLED=1 in .env.local."}), 403
        if not write_request_allowed():
            return jsonify({"error": "WordPress Trash is only available from this machine."}), 403
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        data = request.get_json(silent=True) or {}
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
        already = [
            item_id for item_id in ids
            if (state["live"].get(item_id) or {}).get("live_state") == "trashed"
        ]
        if already:
            return jsonify({"error": f"Record {already[0]} is already in WordPress Trash."}), 400
        unsupported = [item_id for item_id in ids if not rest_base_for(by_id[item_id].get("type", ""))]
        if unsupported:
            return jsonify({"error": f"Record {unsupported[0]} cannot be trashed through WordPress REST."}), 400
        items = [by_id[item_id] for item_id in ids]
        site = state["report"].get("site") or {}
        site_url = site.get("site_url") or site.get("home_url") or ""
        rest_url = os.environ.get("WP_REST_BASE_URL", "")
        if not sites_are_same([site.get("site_url") or "", site.get("home_url") or ""], rest_url):
            return jsonify({"error": "This export is not from the WordPress site configured for local REST writes."}), 409
        try:
            results = trash_items(items, client_from_env(), site_url=site_url)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception:
            app.logger.exception("WordPress REST trash failed")
            return jsonify({"error": "Moving records to WordPress Trash failed."}), 500
        live = dict(state["live"])
        for result in results:
            if result.get("ok") and result.get("live"):
                live[result["id"]] = result["live"]
        audit_store.save_live(session["audit_id"], live)
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
            "review_decision", "live_state", "live_status", "live_link", "live_checked_at",
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

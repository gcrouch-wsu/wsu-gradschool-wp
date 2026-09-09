from __future__ import annotations

import csv
import io
import json
import os
import secrets
from io import BytesIO
from time import perf_counter
from uuid import uuid4

from defusedxml.common import DefusedXmlException
from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from analyzer import analyze_export, parse_wxr
from audit_store import AuditStore


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
    app = Flask(__name__, static_folder="public", static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.secret_key = (
        os.environ.get("FLASK_SECRET_KEY")
        or os.environ.get("BLOB_READ_WRITE_TOKEN")
        or secrets.token_hex(32)
    )
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
        return response

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
        classification = request.args.get("classification", "")
        status = request.args.get("status", "")
        author = request.args.get("author", "")
        subtype = request.args.get("subtype", "")
        taxonomy = request.args.get("taxonomy", "")
        term = request.args.get("term", "")
        decision = request.args.get("decision", "")
        query = request.args.get("q", "").strip().casefold()
        current_state = latest_state()
        decisions = current_state["decisions"] if current_state else {}

        result = []
        for row in rows:
            if group and row["group"] != group:
                continue
            if classification and row["classification"] != classification:
                continue
            if status and row["status"] != status:
                continue
            if author and (row["author_name"] or row["author_login"] or "Unknown") != author:
                continue
            if subtype and row["content_class"] != subtype:
                continue
            if taxonomy and taxonomy not in row["taxonomy_terms"]:
                continue
            if term and term not in row["taxonomy_terms"].get(taxonomy, []):
                continue
            row_decision = decisions.get(row["id"], "unreviewed")
            if decision and row_decision != decision:
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

    def list_payload(row: dict, decisions: dict[str, str]) -> dict:
        fields = (
            "id", "group", "group_label", "type", "content_class", "title", "url",
            "file_name", "file_size", "width", "height", "mime_type", "file_extension",
            "author_name", "author_login", "status", "created", "modified", "categories",
            "tags", "taxonomy_terms", "classification", "underlying_classification",
            "confidence", "inbound_strong", "inbound_possible", "inbound_structural",
            "outbound", "expected_development", "derivative_count",
        )
        payload = {field: row[field] for field in fields}
        payload["review_decision"] = decisions.get(row["id"], "unreviewed")
        return payload

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "GET":
            state = latest_state()
            return render_template(
                "index.html",
                report=state["report"] if state else None,
                error=None,
                filename=state["filename"] if state else None,
                storage_mode=audit_store.mode,
                chunk_size=UPLOAD_CHUNK_BYTES,
            )

        uploaded = request.files.get("export_file")
        if uploaded is None or not uploaded.filename:
            return render_template(
                "index.html", report=None,
                error="Choose a WordPress WXR (.xml) export before starting the analysis.",
                filename=None,
                storage_mode=audit_store.mode, chunk_size=UPLOAD_CHUNK_BYTES,
            ), 400
        if not uploaded.filename.lower().endswith(".xml"):
            return render_template(
                "index.html", report=None,
                error="The selected file must be a WordPress XML export.",
                filename=uploaded.filename,
                storage_mode=audit_store.mode, chunk_size=UPLOAD_CHUNK_BYTES,
            ), 400

        try:
            audit_id = uuid4().hex
            report = analyze_and_store(uploaded.stream, uploaded.filename, audit_id)
            return render_template(
                "index.html", report=report, error=None, filename=uploaded.filename,
                storage_mode=audit_store.mode, chunk_size=UPLOAD_CHUNK_BYTES,
            )
        except (DefusedXmlException, ValueError) as exc:
            message = f"The export could not be analyzed: {exc}"
        except Exception:
            app.logger.exception("Unexpected WXR analysis failure")
            message = "The export could not be analyzed because of an unexpected parsing error."
        return render_template(
            "index.html", report=None, error=message, filename=uploaded.filename,
            storage_mode=audit_store.mode, chunk_size=UPLOAD_CHUNK_BYTES,
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
        facets = {
            "authors": sorted({row["author_name"] or row["author_login"] or "Unknown" for row in all_group_rows}, key=str.casefold),
            "statuses": sorted({row["status"] or "unknown" for row in all_group_rows}),
            "classifications": sorted({row["classification"] for row in all_group_rows}),
            "subtypes": sorted({row["content_class"] for row in all_group_rows}),
            "decisions": sorted(REVIEW_DECISIONS),
        }
        return jsonify(
            {
                "items": [list_payload(row, decisions) for row in rows[start:start + page_size]],
                "total": len(rows),
                "page": page,
                "page_size": page_size,
                "page_count": max(1, (len(rows) + page_size - 1) // page_size),
                "facets": facets,
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
        payload = dict(row)
        payload["review_decision"] = state["decisions"].get(item_id, "unreviewed")
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

    @app.get("/api/export.<format_name>")
    def api_export(format_name: str):
        state = latest_state()
        if not state:
            return jsonify({"error": "No export has been analyzed."}), 404
        rows = filtered_rows(state["report"])
        decisions = state["decisions"]
        if format_name == "json":
            payload = []
            for row in rows:
                exported_row = dict(row)
                exported_row["review_decision"] = decisions.get(row["id"], "unreviewed")
                payload.append(exported_row)
            return Response(
                json.dumps(payload, ensure_ascii=False, indent=2),
                mimetype="application/json",
                headers={"Content-Disposition": "attachment; filename=wordpress-content-audit.json"},
            )
        if format_name != "csv":
            return jsonify({"error": "Unsupported export format."}), 404

        payload = []
        for row in rows:
            exported_row = dict(row)
            exported_row["review_decision"] = decisions.get(row["id"], "unreviewed")
            payload.append(exported_row)
        fields = (
            "id", "group_label", "type", "content_class", "title", "url", "file_name",
            "mime_type", "file_extension", "file_size", "width", "height", "stored_path",
            "alt_text", "caption", "derivative_count", "status", "author_name", "author_login",
            "author_email", "created", "created_gmt", "modified", "modified_gmt", "parent_id",
            "categories", "tags", "taxonomies", "taxonomy_terms", "meta_keys",
            "classification", "underlying_classification", "confidence", "reasons", "recommendation",
            "inbound_strong", "inbound_possible", "inbound_structural", "outbound",
            "review_decision",
        )
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in payload:
            writer.writerow({field: _spreadsheet_safe(row.get(field)) for field in fields})
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=wordpress-content-audit.csv"},
        )

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return render_template(
            "index.html", report=None,
            error="The selected export is larger than the 250 MB upload limit.", filename=None,
            storage_mode=audit_store.mode, chunk_size=UPLOAD_CHUNK_BYTES,
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

"""CSV Structure Mapper routes: analyze/clean/dedupe/validate an uploaded
CSV against LeadFlow's canonical lead schema, manage saved mapping
templates, and serve the combined "All Leads" view over every export."""

import csv
import io
import logging
import os

from flask import Blueprint, jsonify, request, send_file

import pipeline as pl

bp = Blueprint("csv_mapper", __name__, url_prefix="/api/csv-mapper")


@bp.route("/analyze", methods=["POST"])
def csv_mapper_analyze():
    """
    Standalone personal utility — no relation to the lead-gen pipeline.
    Parses an uploaded CSV, suggests a mapping of its columns onto LeadFlow's
    own canonical lead structure, and returns everything the client needs to
    render a preview + editable mapping table. Stateless: nothing is written
    to disk and no run is created.
    """
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "no file uploaded"}), 400
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Unsupported file format. Please upload a .csv file"}), 400

    try:
        stream = io.StringIO(file.read().decode("utf-8"), newline="")
        reader = csv.DictReader(stream)
        rows = list(reader)
        headers = reader.fieldnames or []
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 400

    if not headers:
        return jsonify({"error": "No columns found in file"}), 400

    sample_row = rows[0] if rows else {}
    result = pl.suggest_csv_column_mapping(headers, sample_row)

    return jsonify({
        "headers": headers,
        "rows": rows,
        "mapping": result["mapping"],
        "ai_used_for": result["ai_used_for"],
        # An ordered [key, label] list, not an object — jsonify() sorts dict
        # keys alphabetically, which would scramble the canonical column order.
        "canonical_fields": [[k, v[0]] for k, v in pl._CSV_MAPPER_FIELDS.items()],
    })


@bp.route("/process", methods=["POST"])
def csv_mapper_process():
    """
    Cleans, validates, and deduplicates rows already parsed by
    /api/csv-mapper/analyze — the client sends them back along with the
    current (possibly user-edited) mapping and settings, avoiding a second
    file upload. Also persists the resulting normalized CSV under
    ./output/csv_mapper/. Stateless otherwise: no run is created.
    """
    body = request.get_json(silent=True) or {}
    rows = body.get("rows") or []
    mapping = body.get("mapping") or {}
    settings = body.get("settings") or {}

    if not rows or not mapping:
        return jsonify({"error": "rows and mapping are required"}), 400

    result = pl.process_csv_mapper_rows(rows, mapping, settings)
    return jsonify(result)


@bp.route("/templates", methods=["GET"])
def csv_mapper_list_templates():
    return jsonify(pl.list_csv_mapping_templates())


@bp.route("/templates", methods=["POST"])
def csv_mapper_save_template():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "template name is required"}), 400
    pl.save_csv_mapping_template(name, body.get("mapping") or {}, body.get("settings") or {})
    return jsonify({"ok": True})


@bp.route("/templates/<name>", methods=["DELETE"])
def csv_mapper_delete_template(name):
    pl.delete_csv_mapping_template(name)
    return jsonify({"ok": True})


@bp.route("/downloads")
def csv_mapper_list_downloads():
    """Lists normalized CSVs previously saved by /api/csv-mapper/process, newest first."""
    files = []
    dir_path = os.path.join("output", "csv_mapper")
    if os.path.exists(dir_path):
        for name in os.listdir(dir_path):
            if not (name.startswith("normalized_") and name.endswith(".csv")):
                continue
            filepath = os.path.join(dir_path, name)
            parts = name.replace("normalized_", "").replace(".csv", "").split("_")
            timestamp = ""
            if len(parts) >= 2 and len(parts[0]) == 8 and len(parts[1]) == 6:
                d, t = parts[0], parts[1]
                timestamp = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"
            size_bytes = os.path.getsize(filepath)
            try:
                # csv.reader (not a raw line count) — some columns like Biz
                # Description contain embedded newlines inside quoted fields,
                # which would otherwise inflate a naive line count.
                with open(filepath, "r", encoding="utf-8", newline="") as f:
                    row_count = max(sum(1 for _ in csv.reader(f)) - 1, 0)
            except Exception:
                row_count = 0
            files.append({
                "filename": name,
                "timestamp": timestamp or "Unknown",
                "size": f"{size_bytes / 1024:.1f} KB",
                "rows": row_count,
            })
    files.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify(files)


@bp.route("/leads")
def csv_mapper_all_leads():
    """
    Combines every row from every saved normalized CSV in output/csv_mapper/
    into one list — a CRM-style view over everything the CSV Mapper has ever
    exported. Rows are de-duplicated by Email Id (falling back to LinkedIn
    URL), keeping the row from the most recently-saved file, since re-running
    Clean & Validate on the same source CSV without deleting the old export
    would otherwise double-count identical leads.
    """
    dir_path = os.path.join("output", "csv_mapper")
    headers: list = []
    seen_headers = set()
    combined = []
    filenames = []

    if os.path.exists(dir_path):
        filenames = sorted(n for n in os.listdir(dir_path) if n.startswith("normalized_") and n.endswith(".csv"))
        for name in filenames:
            path = os.path.join(dir_path, name)
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for h in reader.fieldnames or []:
                        if h not in seen_headers:
                            seen_headers.add(h)
                            headers.append(h)
                    for row in reader:
                        row["_source_file"] = name
                        combined.append(row)
            except Exception as e:
                logging.warning("CSV mapper leads aggregation: failed to read %s: %s", name, e)

    deduped: dict = {}
    order = []
    for i, row in enumerate(combined):
        key = (row.get("Email Id") or "").strip().lower() or (row.get("LinkedIn URL") or "").strip().lower() or f"__row_{i}"
        if key not in deduped:
            order.append(key)
        deduped[key] = row

    return jsonify({
        "headers": headers + ["_source_file"],
        "rows": [deduped[k] for k in order],
        "total_files": len(filenames),
        "total_raw_rows": len(combined),
    })


@bp.route("/download/<filename>")
def csv_mapper_download_file(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400
    path = os.path.join("output", "csv_mapper", filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True)


@bp.route("/preview/<filename>")
def csv_mapper_preview_file(filename):
    """Returns the first 50 rows of a saved normalized CSV so it can be
    viewed inline in the Saved Downloads list without downloading it."""
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400
    path = os.path.join("output", "csv_mapper", filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            rows = []
            for i, row in enumerate(reader):
                if i >= 50:
                    break
                rows.append(row)
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {str(e)}"}), 400
    return jsonify({"headers": headers, "rows": rows})

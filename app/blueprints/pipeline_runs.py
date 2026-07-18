"""Routes that start, stream, and read back pipeline runs. Run state lives
in the shared RunRegistry (app.extensions["run_registry"]), not a bare
module-level global, so it can be safely accessed from any blueprint/thread."""

import json
import queue
import threading
import uuid

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

import pipeline as pl

from ..pipeline_runner import run_pipeline_imported_thread, run_pipeline_thread

bp = Blueprint("pipeline_runs", __name__, url_prefix="/api")


def _registry():
    return current_app.extensions["run_registry"]


@bp.route("/run", methods=["POST"])
def start_run():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    inquiry = (data.get("inquiry") or "").strip()
    if not inquiry:
        return jsonify({"error": "inquiry is required"}), 400

    try:
        target = int(data.get("target", 25))
        max_pages = int(data.get("max_pages", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "target/max_pages must be numbers"}), 400

    enable_apollo     = bool(data.get("enable_apollo", True))
    enable_apify      = bool(data.get("enable_apify", True))
    enable_explorium  = bool(data.get("enable_explorium", False))
    explorium_api_key = data.get("explorium_api_key")
    verifier_provider = str(data.get("verifier_provider", "custom"))
    icp               = data.get("icp")
    profile           = str(data.get("profile", "balanced"))
    apify_actor       = data.get("apify_actor") or None

    run_id = uuid.uuid4().hex
    registry = _registry()
    registry.create(run_id, inquiry, queue.Queue())

    thread = threading.Thread(
        target=run_pipeline_thread,
        kwargs={
            "run_registry": registry,
            "run_id": run_id,
            "inquiry": inquiry,
            "target": target,
            "max_pages": max_pages,
            "enable_apollo": enable_apollo,
            "enable_apify": enable_apify,
            "enable_explorium": enable_explorium,
            "explorium_api_key": explorium_api_key,
            "verifier_provider": verifier_provider,
            "icp": icp,
            "profile": profile,
            "apify_actor": apify_actor,
        },
        daemon=True,
    )
    thread.start()

    return jsonify({"run_id": run_id})


@bp.route("/run-custom", methods=["POST"])
def start_custom_run():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    icp = data.get("icp")
    if not icp:
        return jsonify({"error": "icp object is required"}), 400

    try:
        target = int(data.get("target", 25))
        max_pages = int(data.get("max_pages", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "target/max_pages must be numbers"}), 400

    enable_apollo     = bool(data.get("enable_apollo", True))
    enable_apify      = bool(data.get("enable_apify", True))
    enable_explorium  = bool(data.get("enable_explorium", False))
    explorium_api_key = data.get("explorium_api_key")
    verifier_provider = str(data.get("verifier_provider", "custom"))
    profile           = str(data.get("profile", "balanced"))
    apify_actor       = data.get("apify_actor") or None

    run_id = uuid.uuid4().hex
    registry = _registry()
    registry.create(run_id, "Custom ICP Run", queue.Queue())

    thread = threading.Thread(
        target=run_pipeline_thread,
        kwargs={
            "run_registry": registry,
            "run_id": run_id,
            "inquiry": "Custom ICP Run",
            "target": target,
            "max_pages": max_pages,
            "enable_apollo": enable_apollo,
            "enable_apify": enable_apify,
            "enable_explorium": enable_explorium,
            "explorium_api_key": explorium_api_key,
            "verifier_provider": verifier_provider,
            "icp": icp,
            "profile": profile,
            "apify_actor": apify_actor,
        },
        daemon=True,
    )
    thread.start()

    return jsonify({"run_id": run_id})


@bp.route("/run-imported", methods=["POST"])
def start_imported_run():
    inquiry = request.form.get("inquiry", "").strip()
    try:
        target = int(request.form.get("target", 25))
    except (TypeError, ValueError):
        return jsonify({"error": "target must be a number"}), 400
    verifier_provider = request.form.get("verifier_provider", "custom")
    if not inquiry:
        return jsonify({"error": "inquiry is required"}), 400

    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "no selected file"}), 400

    # Parse JSON or CSV
    raw_leads = []
    filename = file.filename.lower()

    try:
        if filename.endswith(".json"):
            content = file.read().decode("utf-8")
            data = json.loads(content)
            if not isinstance(data, list):
                if isinstance(data, dict) and "data" in data:
                    data = data["data"]
                else:
                    data = [data]

            for item in data:
                lead = {
                    "name":         str(item.get("name") or item.get("fullName") or "").strip(),
                    "title":        str(item.get("jobTitle") or item.get("title") or item.get("headline") or "").strip(),
                    "company":      str(item.get("companyName") or item.get("company") or item.get("organization") or "").strip(),
                    "email":        str(item.get("email") or item.get("emailAddress") or "").strip().lower(),
                    "linkedin_url": str(item.get("linkedinUrl") or item.get("linkedInUrl") or item.get("url") or "").strip(),
                    "company_domain": pl.normalize_domain(str(item.get("companyDomain") or item.get("company_domain") or item.get("website") or item.get("domain") or "")),
                    "source":       "imported_json",
                }
                # Preserve all other keys from the imported JSON item
                for k, v in item.items():
                    normalized_k = k.lower().replace(" ", "_")
                    if normalized_k not in ["name", "fullname", "jobtitle", "title", "headline", "companyname", "company", "organization", "email", "emailaddress", "linkedinurl", "linkedin_url", "url", "companydomain", "company_domain", "website", "domain"]:
                        lead[f"imported_{normalized_k}"] = v
                raw_leads.append(lead)
        elif filename.endswith(".csv"):
            import csv
            import io
            stream = io.StringIO(file.read().decode("utf-8"), newline="")
            reader = csv.DictReader(stream)
            for row in reader:
                normalized_row = {k.lower().replace(" ", "_"): v for k, v in row.items()}
                lead = {
                    "name":         str(normalized_row.get("name") or normalized_row.get("fullname") or "").strip(),
                    "title":        str(normalized_row.get("jobtitle") or normalized_row.get("title") or normalized_row.get("headline") or "").strip(),
                    "company":      str(normalized_row.get("companyname") or normalized_row.get("company") or normalized_row.get("organization") or "").strip(),
                    "email":        str(normalized_row.get("email") or normalized_row.get("emailaddress") or "").strip().lower(),
                    "linkedin_url": str(normalized_row.get("linkedinurl") or normalized_row.get("linkedin_url") or normalized_row.get("url") or "").strip(),
                    "company_domain": pl.normalize_domain(normalized_row.get("company_domain") or normalized_row.get("companydomain") or normalized_row.get("website") or normalized_row.get("domain") or ""),
                    "source":       "imported_csv",
                }
                # Preserve all other columns from the CSV row
                for original_k, v in row.items():
                    k = original_k.lower().replace(" ", "_")
                    if k not in ["name", "fullname", "jobtitle", "title", "headline", "companyname", "company", "organization", "email", "emailaddress", "linkedinurl", "linkedin_url", "url", "company_domain", "companydomain", "website", "domain"]:
                        lead[f"imported_{k}"] = v
                raw_leads.append(lead)
        else:
            return jsonify({"error": "Unsupported file format. Please upload .json or .csv"}), 400

    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 400

    if not raw_leads:
        return jsonify({"error": "No leads found in file"}), 400

    run_id = uuid.uuid4().hex
    registry = _registry()
    registry.create(run_id, inquiry, queue.Queue())

    thread = threading.Thread(
        target=run_pipeline_imported_thread,
        args=(registry, run_id, inquiry, raw_leads, target, verifier_provider),
        daemon=True,
    )
    thread.start()

    return jsonify({"run_id": run_id})


@bp.route("/stream/<run_id>")
def stream(run_id: str):
    registry = _registry()
    if run_id not in registry:
        return jsonify({"error": "run not found"}), 404

    q: queue.Queue = registry.get(run_id)["queue"]

    def generate():
        while True:
            try:
                msg = q.get(timeout=25)
            except queue.Empty:
                # The queue is drained exactly once (one None sentinel at
                # the end of the run). A second connection to this route
                # for an already-finished run — a browser reconnect, a
                # refresh, a duplicate tab — used to find an empty queue
                # and block here forever, permanently parking the request
                # thread. If the run has already finished, stop waiting;
                # otherwise this is a legitimate long-running stage, so
                # send an SSE comment to keep the connection alive and
                # keep waiting.
                run = registry.get(run_id)
                if run is None or run["status"] in ("complete", "error"):
                    break
                yield ": keep-alive\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@bp.route("/results/<run_id>")
def results(run_id: str):
    run = _registry().get(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    if run["status"] != "complete":
        return jsonify({"status": run["status"], "error": run.get("error")}), 202
    return jsonify(run["results"])


@bp.route("/runs")
def list_runs():
    return jsonify([
        {"id": r["id"], "status": r["status"], "inquiry": r["inquiry"]}
        for r in _registry().list()
    ])

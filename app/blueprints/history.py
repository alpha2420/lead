"""Historical pipeline-run listing and CSV download routes (Datasets tab)."""

import csv
import json
import os

from flask import Blueprint, jsonify, send_file

bp = Blueprint("history", __name__, url_prefix="/api")


@bp.route("/history")
def list_history():
    files = []
    if os.path.exists("output"):
        for name in os.listdir("output"):
            if name.startswith("leads_sample_") and name.endswith(".csv"):
                parts = name.replace("leads_sample_", "").replace(".csv", "").split("_")
                timestamp = ""
                if len(parts) >= 2:
                    date_str, time_str = parts[0], parts[1]
                    if len(date_str) == 8 and len(time_str) == 6:
                        timestamp = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]} {time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"

                # Check for companion metadata file
                inquiry = "Historical Scraped Run"
                timestamp_str = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
                meta_path = os.path.join("output", f"metadata_{timestamp_str}.json")

                filepath = os.path.join("output", name)
                size_bytes = os.path.getsize(filepath)
                lead_count = 0

                verified_pct = None
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                            inquiry = meta.get("inquiry", "Historical Scraped Run")
                            run_stats = meta.get("stats", {}) or {}
                            lead_count = run_stats.get("sample", 0)
                            if meta.get("timestamp"):
                                timestamp = meta.get("timestamp")
                            raw = run_stats.get("raw", 0) or 0
                            if raw:
                                verified_pct = round(run_stats.get("verified", 0) / raw * 100, 1)
                    except Exception:
                        pass
                else:
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            lead_count = len(f.readlines()) - 1
                    except Exception:
                        pass

                files.append({
                    "filename": name,
                    "timestamp": timestamp or "Unknown",
                    "inquiry": inquiry,
                    "size": f"{size_bytes / 1024:.1f} KB",
                    "leads": lead_count if lead_count >= 0 else 0,
                    "verified_pct": verified_pct,
                })
    files.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify(files)


@bp.route("/history/aggregate")
def aggregate_stats():
    """All-time totals across every completed run, for the dashboard Home
    page. Reads output/metadata_*.json directly (the durable per-run
    record save_run_metadata() writes) rather than the in-memory
    RunRegistry (GET /api/runs) — that registry is wiped on every server
    restart, so a "recent activity" or "all-time totals" view built on it
    would go blank right after a restart, exactly when a user is most
    likely to want their history. Degrades gracefully: an empty output/
    directory returns all-zero totals rather than erroring, and runtime
    averaging only counts runs that actually have runtime_seconds (a
    field added after this endpoint — older metadata predates it)."""
    total_leads = total_verified = total_deduped = 0
    total_invalid = total_catchall = 0
    runtime_sum = 0.0
    runtime_count = 0
    run_count = 0
    disk_bytes = 0
    runs = []
    latest_timestamp = ""
    latest_sample = []

    if os.path.exists("output"):
        for name in os.listdir("output"):
            if not (name.startswith("metadata_") and name.endswith(".json")):
                continue
            meta_path = os.path.join("output", name)
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue

            stats = meta.get("stats", {}) or {}
            raw = stats.get("raw", 0) or 0
            verified = stats.get("verified", 0) or 0

            run_count += 1
            total_leads += raw
            total_verified += verified
            total_deduped += stats.get("deduped", 0) or 0
            total_invalid += stats.get("invalid", 0) or 0
            total_catchall += stats.get("catchall", 0) or 0
            disk_bytes += os.path.getsize(meta_path)

            runtime = stats.get("runtime_seconds")
            if isinstance(runtime, (int, float)):
                runtime_sum += runtime
                runtime_count += 1

            timestamp = meta.get("timestamp", "Unknown")
            runs.append({
                "filename": meta.get("csv_filename", ""),
                "inquiry": meta.get("inquiry", "Historical Scraped Run"),
                "timestamp": timestamp,
                "leads": stats.get("sample", 0) or 0,
                "verified_pct": round(verified / raw * 100, 1) if raw else 0.0,
            })

            if timestamp > latest_timestamp:
                latest_timestamp = timestamp
                latest_sample = meta.get("sample", []) or []

    runs.sort(key=lambda r: r["timestamp"], reverse=True)

    return jsonify({
        "run_count": run_count,
        "total_leads": total_leads,
        "total_verified": total_verified,
        "total_duplicates": max(total_leads - total_deduped, 0),
        "success_rate": round(total_verified / total_leads * 100, 1) if total_leads else 0.0,
        "avg_runtime_seconds": round(runtime_sum / runtime_count, 1) if runtime_count else None,
        "runtime_sample_size": runtime_count,
        "outcome_totals": {
            "valid": total_verified,
            "invalid": total_invalid,
            "catchall": total_catchall,
        },
        "disk_size_kb": round(disk_bytes / 1024, 1),
        "recent_runs": runs[:5],
        "recent_sample": latest_sample[:8],
    })


@bp.route("/history/<filename>")
def get_history_detail(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400

    parts = filename.replace("leads_sample_", "").replace(".csv", "").split("_")
    timestamp_str = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
    meta_path = os.path.join("output", f"metadata_{timestamp_str}.json")

    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": f"failed to read metadata: {str(e)}"}), 500

    # Fallback: Read CSV directly if metadata is missing
    csv_path = os.path.join("output", filename)
    if not os.path.exists(csv_path):
        return jsonify({"error": "file not found"}), 404

    leads = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                leads.append({
                    "name": row.get("Name", ""),
                    "title": row.get("Title", ""),
                    "company": row.get("Company", ""),
                    "location": row.get("Location", ""),
                    "email": row.get("Email", ""),
                    "linkedin_url": row.get("LinkedIn URL", ""),
                    "status": row.get("Verification Status", ""),
                    "score": float(row.get("Confidence Score", 0)) if row.get("Confidence Score") else 0.0,
                    "source": row.get("Source", "")
                })
    except Exception as e:
        return jsonify({"error": f"failed to parse CSV: {str(e)}"}), 500

    # Build standard response structure
    return jsonify({
        "inquiry": "Historical Scraped Run",
        "timestamp": "Unknown",
        "csv_filename": filename,
        "stats": {
            "raw": len(leads),
            "deduped": len(leads),
            "verified": len(leads),
            "sample": len(leads),
            "invalid": 0,
            "catchall": 0,
            "bounce_rate": 0.0,
            "pages": 1
        },
        "icp": {
            "icp_summary": "Historical dataset loaded from CSV — no ICP metadata available.",
            "industry_intelligence": {},
            "geography_intelligence": {},
            "technology_intelligence": {},
            "buying_committee_intelligence": {},
            "company_intelligence": {},
            "market_intelligence": {},
            "intent_intelligence": {},
            "search_intelligence": {},
            "lead_scoring": [],
            "data_sources": [],
            "confidence": {},
        },
        "sample": leads
    })


@bp.route("/download/<filename>")
def download_file(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400
    path = os.path.join("output", filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True)

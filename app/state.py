"""In-memory registry of pipeline runs, shared across blueprints via
`app.extensions["run_registry"]` instead of a bare module-level global."""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class RunRegistry:
    """Thread-safe store of in-flight/completed pipeline runs, keyed by run_id."""

    def __init__(self):
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, run_id: str, inquiry: str, q) -> dict:
        run = {
            "id": run_id,
            "inquiry": inquiry,
            "status": "pending",
            "queue": q,
            "results": None,
            "error": None,
        }
        with self._lock:
            self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._runs.values())

    def __contains__(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs


def save_run_metadata(run, csv_path, icp):
    try:
        timestamp_str = os.path.basename(csv_path).replace("leads_sample_", "").replace(".csv", "")
        meta_path = os.path.join("./output", f"metadata_{timestamp_str}.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "inquiry": run.get("inquiry", "Imported File Run"),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "csv_filename": os.path.basename(csv_path),
                "stats": run["results"]["stats"],
                "icp": icp,
                "sample": run["results"]["sample"]
            }, f, indent=2)
    except Exception as e:
        logger.error("Failed to write run metadata: %s", e)

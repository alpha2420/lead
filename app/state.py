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
        self.prune()
        run = {
            "id": run_id,
            "inquiry": inquiry,
            "status": "pending",
            "queue": q,
            "results": None,
            "error": None,
            "created_at": time.time(),
            "cancel_requested": False,
        }
        with self._lock:
            self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(run_id)

    def request_cancel(self, run_id: str) -> bool:
        """Marks a run for cooperative cancellation. Python threads can't be
        killed outright, so this is advisory: the pipeline thread checks
        is_cancel_requested() at its own checkpoints (between scrape pages,
        before slow stages) and exits cleanly there — it doesn't take effect
        mid-instruction. Returns False if the run doesn't exist."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            run["cancel_requested"] = True
            return True

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            return bool(run and run.get("cancel_requested"))

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._runs.values())

    def prune(self, max_age_hours: float = 24) -> None:
        """Drops finished (complete/error) runs older than max_age_hours.
        Their CSV/metadata already live on disk (save_run_metadata), so this
        only discards the in-memory bookkeeping entry — never a run that's
        still pending/running, regardless of age. Called opportunistically
        on every new run, since this registry was previously never pruned
        at all (unbounded memory growth over the server's lifetime)."""
        cutoff = time.time() - max_age_hours * 3600
        with self._lock:
            stale = [
                rid for rid, run in self._runs.items()
                if run.get("status") in ("complete", "error") and run.get("created_at", 0) < cutoff
            ]
            for rid in stale:
                del self._runs[rid]

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

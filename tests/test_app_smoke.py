"""Smoke tests for the Flask route layer (app/ package).

This is the first route-level test coverage this project has had — until
now only pipeline.py's internals were tested. Not exhaustive: one
happy-path and/or one bad-input check per route, enough to catch a route
wired to the wrong blueprint or a broken import during refactors.
"""

import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(["gemini", "apify", "zerobounce"]).issubset(data.keys())


def test_run_requires_inquiry(client):
    resp = client.post("/api/run", json={})
    assert resp.status_code == 400


def test_run_rejects_non_object_body(client):
    resp = client.post("/api/run", data="null", content_type="application/json")
    assert resp.status_code == 400


def test_run_rejects_non_numeric_target(client):
    resp = client.post("/api/run", json={"inquiry": "test", "target": "not-a-number"})
    assert resp.status_code == 400


def test_run_starts_with_inquiry(client, monkeypatch):
    # Avoid spawning the real pipeline thread / hitting live paid APIs —
    # this is a route-wiring smoke test, not an end-to-end pipeline test.
    import app.blueprints.pipeline_runs as routes_mod

    class FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(routes_mod.threading, "Thread", FakeThread)
    resp = client.post("/api/run", json={"inquiry": "test inquiry"})
    assert resp.status_code == 200
    assert "run_id" in resp.get_json()


def test_run_custom_requires_icp(client):
    resp = client.post("/api/run-custom", json={})
    assert resp.status_code == 400


def test_run_imported_requires_inquiry(client):
    resp = client.post("/api/run-imported", data={"target": "5"})
    assert resp.status_code == 400


def test_run_imported_requires_file(client):
    resp = client.post("/api/run-imported", data={"inquiry": "test", "target": "5"})
    assert resp.status_code == 400


def test_stream_unknown_run_404s(client):
    resp = client.get("/api/stream/doesnotexist")
    assert resp.status_code == 404


def test_results_unknown_run_404s(client):
    resp = client.get("/api/results/doesnotexist")
    assert resp.status_code == 404


def test_runs_list(client):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_generate_icp_requires_inquiry(client):
    resp = client.post("/api/generate-icp", json={})
    assert resp.status_code == 400


def test_generate_buyer_icp_requires_description(client):
    resp = client.post("/api/generate-buyer-icp", json={})
    assert resp.status_code == 400


def test_chat_icp_requires_message(client):
    resp = client.post("/api/chat-icp", json={})
    assert resp.status_code == 400


def test_csv_mapper_analyze_requires_file(client):
    resp = client.post("/api/csv-mapper/analyze")
    assert resp.status_code == 400


def test_csv_mapper_process_requires_rows(client):
    resp = client.post("/api/csv-mapper/process", json={})
    assert resp.status_code == 400


def test_csv_mapper_templates_list(client):
    resp = client.get("/api/csv-mapper/templates")
    assert resp.status_code == 200


def test_csv_mapper_save_template_requires_name(client):
    resp = client.post("/api/csv-mapper/templates", json={})
    assert resp.status_code == 400


def test_csv_mapper_delete_template(client):
    resp = client.delete("/api/csv-mapper/templates/does-not-exist")
    assert resp.status_code == 200


def test_csv_mapper_downloads_list(client):
    resp = client.get("/api/csv-mapper/downloads")
    assert resp.status_code == 200


def test_csv_mapper_leads(client):
    resp = client.get("/api/csv-mapper/leads")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "headers" in data and "rows" in data


def test_csv_mapper_download_rejects_path_traversal(client):
    resp = client.get("/api/csv-mapper/download/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_csv_mapper_preview_missing_file_404s(client):
    resp = client.get("/api/csv-mapper/preview/does-not-exist.csv")
    assert resp.status_code == 404


def test_history_list(client):
    resp = client.get("/api/history")
    assert resp.status_code == 200


def test_history_detail_rejects_path_traversal(client):
    # A decoded "/" in the path segment doesn't match the <filename> route
    # converter at all, so this 404s at routing rather than reaching the
    # view's explicit ".." check — both outcomes mean the traversal is blocked.
    resp = client.get("/api/history/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_download_rejects_path_traversal(client):
    resp = client.get("/api/download/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_history_aggregate_smoke(client):
    # Matches this file's existing minimal style for routes that read the
    # real output/ dir — a real 200 + the expected top-level keys, not a
    # controlled fixture. See test_history_aggregate_sums_across_runs below
    # for the actual aggregation-math coverage.
    resp = client.get("/api/history/aggregate")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set([
        "run_count", "total_leads", "total_verified", "total_duplicates",
        "success_rate", "avg_runtime_seconds", "outcome_totals",
        "recent_runs", "recent_sample",
    ]).issubset(data.keys())


def test_history_aggregate_empty_output_dir(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no "output" dir at all here
    resp = client.get("/api/history/aggregate")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["run_count"] == 0
    assert data["total_leads"] == 0
    assert data["success_rate"] == 0.0
    assert data["avg_runtime_seconds"] is None
    assert data["recent_runs"] == []
    assert data["recent_sample"] == []


def test_history_aggregate_sums_across_runs(client, tmp_path, monkeypatch):
    import json as json_module

    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    # Older run — predates runtime_seconds entirely (must not crash the average).
    (output_dir / "metadata_20240101_100000.json").write_text(json_module.dumps({
        "inquiry": "Old run", "timestamp": "2024-01-01 10:00:00",
        "csv_filename": "leads_sample_20240101_100000.csv",
        "stats": {"raw": 10, "verified": 5, "deduped": 8, "invalid": 2, "catchall": 1, "sample": 5},
        "sample": [{"name": "Old Lead", "status": "valid"}],
    }))
    # Newer run — has runtime_seconds.
    (output_dir / "metadata_20240102_100000.json").write_text(json_module.dumps({
        "inquiry": "New run", "timestamp": "2024-01-02 10:00:00",
        "csv_filename": "leads_sample_20240102_100000.csv",
        "stats": {
            "raw": 20, "verified": 15, "deduped": 18, "invalid": 3, "catchall": 2,
            "sample": 15, "runtime_seconds": 42.0,
        },
        "sample": [{"name": "New Lead", "status": "valid"}],
    }))

    resp = client.get("/api/history/aggregate")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["run_count"] == 2
    assert data["total_leads"] == 30       # 10 + 20
    assert data["total_verified"] == 20    # 5 + 15
    assert data["total_duplicates"] == 4   # (10-8) + (20-18)
    assert data["success_rate"] == round(20 / 30 * 100, 1)
    # Only the newer run has runtime_seconds — average is over that 1 run alone.
    assert data["avg_runtime_seconds"] == 42.0
    assert data["runtime_sample_size"] == 1
    assert data["outcome_totals"] == {"valid": 20, "invalid": 5, "catchall": 3}
    # Most recent run first, and its own sample used for the leads preview.
    assert data["recent_runs"][0]["inquiry"] == "New run"
    assert data["recent_sample"] == [{"name": "New Lead", "status": "valid"}]

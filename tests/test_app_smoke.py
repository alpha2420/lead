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
    assert set(["gemini", "apollo", "apify", "explorium", "zerobounce"]).issubset(data.keys())


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


def test_estimate_coverage_requires_icp(client):
    resp = client.post("/api/estimate-coverage", json={})
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

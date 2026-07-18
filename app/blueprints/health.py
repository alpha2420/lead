"""Provider API health-check route — pings Gemini/Apollo/Apify/Explorium and
the active email verifier, returning live status/latency for the dashboard's
API Status indicator."""

import os
import time

import google.genai as genai
import requests
from flask import Blueprint, jsonify

bp = Blueprint("health", __name__, url_prefix="/api")

GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
APOLLO_API_KEY     = os.getenv("APOLLO_API_KEY")
APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN")
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY")
APIFY_ACTOR_ID     = os.getenv("APIFY_ACTOR_ID", "")


@bp.route("/health")
def api_health():
    """
    Pings all 4 APIs and returns live status, latency, and metadata.
    Each result: { status: "ok"|"error"|"unconfigured", message: str, latency_ms: int }
    """
    results = {}

    # ── Gemini ────────────────────────────────────────────────────────────────
    if not GEMINI_API_KEY:
        results["gemini"] = {"status": "unconfigured", "message": "GEMINI_API_KEY not set", "latency_ms": 0}
    else:
        t0 = time.time()
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            models = list(client.models.list())
            results["gemini"] = {
                "status":     "ok",
                "message":    f"{len(models)} models available",
                "latency_ms": round((time.time() - t0) * 1000),
            }
        except Exception as e:
            results["gemini"] = {"status": "error", "message": str(e)[:120], "latency_ms": round((time.time() - t0) * 1000)}

    # ── Apollo ────────────────────────────────────────────────────────────────
    if not APOLLO_API_KEY:
        results["apollo"] = {"status": "unconfigured", "message": "APOLLO_API_KEY not set", "latency_ms": 0}
    else:
        t0 = time.time()
        try:
            # Apollo requires api_key in the POST body (not as a query param).
            # Use a minimal people search (per_page=1) to validate the key —
            # a 200 or 422 means authenticated; 401/403 means invalid key.
            resp = requests.post(
                "https://api.apollo.io/v1/mixed_people/search",
                json={"api_key": APOLLO_API_KEY, "per_page": 1},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            latency = round((time.time() - t0) * 1000)
            if resp.status_code == 200:
                data    = resp.json()
                total   = data.get("pagination", {}).get("total_entries", "?")
                results["apollo"] = {"status": "ok", "message": f"Authenticated — {total} leads in index", "latency_ms": latency}
            elif resp.status_code in (401, 403):
                results["apollo"] = {"status": "error", "message": "Invalid API key (401/403)", "latency_ms": latency}
            elif resp.status_code == 422:
                # 422 = key accepted but request needs more params — key is valid
                results["apollo"] = {"status": "ok", "message": "Authenticated (key valid)", "latency_ms": latency}
            else:
                results["apollo"] = {"status": "error", "message": f"HTTP {resp.status_code}", "latency_ms": latency}
        except Exception as e:
            results["apollo"] = {"status": "error", "message": str(e)[:120], "latency_ms": round((time.time() - t0) * 1000)}

    # ── Apify ─────────────────────────────────────────────────────────────────
    if not APIFY_API_TOKEN:
        results["apify"] = {"status": "unconfigured", "message": "APIFY_API_TOKEN not set", "latency_ms": 0}
    else:
        t0 = time.time()
        try:
            resp = requests.get(
                "https://api.apify.com/v2/users/me",
                params={"token": APIFY_API_TOKEN},
                timeout=10,
            )
            latency = round((time.time() - t0) * 1000)
            if resp.status_code == 200:
                data     = resp.json().get("data", {})
                username = data.get("username", "")
                plan     = data.get("plan", {}).get("id", "unknown")
                results["apify"] = {
                    "status":     "ok",
                    "message":    f"{username} · Plan: {plan} · Actor: {APIFY_ACTOR_ID}",
                    "latency_ms": latency,
                }
            elif resp.status_code in (401, 403):
                results["apify"] = {"status": "error", "message": "Invalid API token", "latency_ms": latency}
            else:
                results["apify"] = {"status": "error", "message": f"HTTP {resp.status_code}", "latency_ms": latency}
        except Exception as e:
            results["apify"] = {"status": "error", "message": str(e)[:120], "latency_ms": round((time.time() - t0) * 1000)}

    # ── Explorium ─────────────────────────────────────────────────────────────
    explorium_key = os.getenv("EXPLORIUM_API_KEY")
    if not explorium_key:
        results["explorium"] = {"status": "unconfigured", "message": "EXPLORIUM_API_KEY not set", "latency_ms": 0}
    else:
        t0 = time.time()
        try:
            resp = requests.get(
                "https://api.explorium.ai/v1/credits",
                headers={"api_key": explorium_key, "accept": "application/json"},
                timeout=10,
            )
            latency = round((time.time() - t0) * 1000)
            if resp.status_code == 200:
                rem = resp.json().get("remaining_credits", "?")
                results["explorium"] = {"status": "ok", "message": f"Authenticated · {rem} credits remaining", "latency_ms": latency}
            elif resp.status_code in (401, 403):
                results["explorium"] = {"status": "error", "message": "Invalid API key (401/403)", "latency_ms": latency}
            else:
                results["explorium"] = {"status": "error", "message": f"HTTP {resp.status_code}", "latency_ms": latency}
        except Exception as e:
            results["explorium"] = {"status": "error", "message": str(e)[:120], "latency_ms": round((time.time() - t0) * 1000)}

    # ── Email Verifier (ZeroBounce or Custom) ──────────────────────────────────
    provider = os.getenv("EMAIL_VERIFIER_PROVIDER", "custom").lower().strip()
    if provider == "custom":
        results["zerobounce"] = {
            "status": "ok",
            "message": "Custom DNS/SMTP verifier active (free)",
            "latency_ms": 0,
            "name": "Custom Verifier"
        }
    else:
        if not ZEROBOUNCE_API_KEY:
            results["zerobounce"] = {"status": "unconfigured", "message": "ZEROBOUNCE_API_KEY not set", "latency_ms": 0, "name": "ZeroBounce"}
        else:
            t0 = time.time()
            try:
                resp = requests.get(
                    "https://api.zerobounce.net/v2/getcredits",
                    params={"api_key": ZEROBOUNCE_API_KEY},
                    timeout=10,
                )
                latency = round((time.time() - t0) * 1000)
                if resp.status_code == 200:
                    credits = resp.json().get("Credits", "?")
                    results["zerobounce"] = {
                        "status":     "ok" if int(credits or 0) > 0 else "warning",
                        "message":    f"{credits} credits remaining",
                        "latency_ms": latency,
                        "name":       "ZeroBounce"
                    }
                elif resp.status_code in (401, 403):
                    results["zerobounce"] = {"status": "error", "message": "Invalid API key", "latency_ms": latency, "name": "ZeroBounce"}
                else:
                    results["zerobounce"] = {"status": "error", "message": f"HTTP {resp.status_code}", "latency_ms": latency, "name": "ZeroBounce"}
            except Exception as e:
                results["zerobounce"] = {"status": "error", "message": str(e)[:120], "latency_ms": round((time.time() - t0) * 1000), "name": "ZeroBounce"}

    return jsonify(results)

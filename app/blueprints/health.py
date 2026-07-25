"""Provider API health-check route — pings Gemini/Apify and the active
email verifier, returning live status/latency for the dashboard's API
Status indicator."""

import os
import re
import smtplib
import time

import google.genai as genai
import requests
from flask import Blueprint, jsonify

bp = Blueprint("health", __name__, url_prefix="/api")

GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN")
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY")
APIFY_ACTOR_ID     = os.getenv("APIFY_ACTOR_ID", "")

# Apify/ZeroBounce pass their key as a URL query param; `requests`
# connection/timeout exceptions commonly stringify the full request URL
# (e.g. "Max retries exceeded with url: /v2/users/me?token=..."), so a
# transient network failure here used to echo the live key straight back
# in this route's JSON response. Strip any api_key/token/key query value
# out of an exception message before it's ever returned to a client.
_SECRET_QUERY_RE = re.compile(r'([?&](?:api_key|apikey|token|key)=)[^&\s]+', re.IGNORECASE)


def _safe_error(e: Exception) -> str:
    return _SECRET_QUERY_RE.sub(r'\1***', str(e))[:120]


@bp.route("/health")
def api_health():
    """
    Pings Gemini + Apify and returns live status, latency, and metadata.
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
            results["gemini"] = {"status": "error", "message": _safe_error(e), "latency_ms": round((time.time() - t0) * 1000)}

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
            results["apify"] = {"status": "error", "message": _safe_error(e), "latency_ms": round((time.time() - t0) * 1000)}

    # ── Email Verifier (Custom, Gmail Bounce Check, or ZeroBounce) ──────────────
    provider = os.getenv("EMAIL_VERIFIER_PROVIDER", "gmail_bounce").lower().strip()
    if provider == "custom":
        results["zerobounce"] = {
            "status": "ok",
            "message": "Custom DNS/SMTP verifier active (free)",
            "latency_ms": 0,
            "name": "Custom Verifier"
        }
    elif provider == "gmail_bounce":
        gmail_sender   = os.getenv("GMAIL_SENDER_ADDRESS")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD")
        if not gmail_sender or not gmail_password:
            results["zerobounce"] = {
                "status": "unconfigured",
                "message": "GMAIL_SENDER_ADDRESS/GMAIL_APP_PASSWORD not set",
                "latency_ms": 0,
                "name": "Gmail Bounce Check",
            }
        else:
            # Login-only check — never sends an email, just confirms the app
            # password actually authenticates before a real run relies on it.
            t0 = time.time()
            try:
                with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as smtp_conn:
                    smtp_conn.starttls()
                    smtp_conn.login(gmail_sender, gmail_password)
                results["zerobounce"] = {
                    "status": "ok",
                    "message": f"SMTP login OK — {gmail_sender}",
                    "latency_ms": round((time.time() - t0) * 1000),
                    "name": "Gmail Bounce Check",
                }
            except smtplib.SMTPAuthenticationError:
                results["zerobounce"] = {
                    "status": "error",
                    "message": "SMTP authentication failed — check the Gmail App Password",
                    "latency_ms": round((time.time() - t0) * 1000),
                    "name": "Gmail Bounce Check",
                }
            except Exception as e:
                results["zerobounce"] = {
                    "status": "error", "message": _safe_error(e),
                    "latency_ms": round((time.time() - t0) * 1000), "name": "Gmail Bounce Check",
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
                results["zerobounce"] = {"status": "error", "message": _safe_error(e), "latency_ms": round((time.time() - t0) * 1000), "name": "ZeroBounce"}

    return jsonify(results)

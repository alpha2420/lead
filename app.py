"""
Flask Dashboard Server for the Lead Generation Pipeline
Provides real-time pipeline progress via Server-Sent Events (SSE)
"""

import os
import uuid
import time
import queue
import logging
import threading
import json
import requests
import csv
import io
import google.genai as genai
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file
from dotenv import load_dotenv

import pipeline as pl

load_dotenv(override=True)

# Pull keys (already loaded by pipeline, just read them here for health checks)
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
APOLLO_API_KEY     = os.getenv("APOLLO_API_KEY")
APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN")
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY")
APIFY_ACTOR_ID     = os.getenv("APIFY_ACTOR_ID", "")

app = Flask(__name__)

# ─────────────────────────────────────────────
# In-memory run store  {run_id -> run_dict}
# ─────────────────────────────────────────────
runs: dict[str, dict] = {}


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
        app.logger.error("Failed to write run metadata: %s", e)


# ─────────────────────────────────────────────
# Custom logging handler that pushes log records
# into a per-run queue for SSE streaming
# ─────────────────────────────────────────────
class RunQueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord):
        self.q.put({
            "type":    "log",
            "level":   record.levelname,   # DEBUG / INFO / WARNING / ERROR
            "message": self.format(record),
        })


# ─────────────────────────────────────────────
# Background thread — runs the full pipeline
# ─────────────────────────────────────────────
def run_pipeline_thread(run_id: str, inquiry: str, target: int = 25, max_pages: int = 10,
                        enable_apollo: bool = True, enable_apify: bool = True,
                        enable_explorium: bool = False, explorium_api_key: str = None,
                        verifier_provider: str = "custom", icp: dict = None,
                        profile: str = "balanced", apify_actor: str = None):
    run = runs[run_id]
    q: queue.Queue = run["queue"]

    # Attach our custom handler to the pipeline module's logger
    handler = RunQueueHandler(q)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pl.log.addHandler(handler)

    def push_stage(stage: int, label: str):
        q.put({"type": "stage", "stage": stage, "label": label})

    def push_stats(**kwargs):
        q.put({"type": "stats", **kwargs})

    def push_progress(collected: int, target: int, page: int):
        q.put({"type": "progress", "collected": collected, "target": target, "page": page})

    def push_stage_progress(stage: int, done: int, total: int):
        q.put({"type": "stage_progress", "stage": stage, "done": done, "total": total})

    try:
        run["status"] = "running"

        # ── Stage 1 ───────────────────────────────────────────────────────────
        if icp is None:
            push_stage(1, "Parsing Inquiry")
            icp = pl.parse_inquiry(inquiry)
        else:
            push_stage(1, "Using Custom ICP")
        q.put({"type": "icp", "data": icp})

        # ── Stage 2: Company Discovery ───────────────────────────────────────
        # Company-first search: find candidate companies matching the ICP's
        # firmographics before searching for any people at all. See
        # pl.discover_companies() — combines a real Apollo company-search
        # attempt (auto-falls back gracefully if the account's plan doesn't
        # support it), a free Apollo people-search preview, and a
        # Gemini+web-search fallback.
        push_stage(2, "Discovering Companies")
        discovery = pl.discover_companies(icp, target=target)
        q.put({"type": "companies_discovered", "data": discovery["counts"]})

        # ── Stage 3: Company Validation ──────────────────────────────────────
        # Two tiers: free rule-based screen on every candidate, then a
        # capped AI fact-check via web search on the top-ranked survivors
        # (see pl.validate_companies() / _SOURCE_PROFILES[profile]
        # ["max_ai_validate_companies"]). Companies beyond the cap stay
        # validated (rule-only), never silently dropped.
        push_stage(3, "Validating Companies")
        validation = pl.validate_companies(discovery["companies"], icp, profile=profile)
        validated_companies = validation["validated"]
        q.put({"type": "companies_validated", "data": validation["counts"]})

        # ── Pagination loop (Stages 4-8) ─────────────────────────────────────
        push_stage(4, "Searching Leads at Validated Companies")

        verified_pool:     list[dict] = []
        seen_emails:       set        = set()
        seen_fingerprints: set        = set()
        total_raw         = 0
        total_deduped     = 0
        apollo_total      = 0
        apify_total       = 0
        explorium_total   = 0
        invalid_count     = 0
        catchall_count    = 0
        page              = 1

        verified_high_conf_count = 0
        while verified_high_conf_count < target and page <= max_pages:
            # Scrape — via the Source Orchestrator (priority waterfall in
            # sequential-mode profiles, concurrent fan-out in parallel-mode
            # profiles; see pl.run_lead_sources())
            result = pl.run_lead_sources(
                icp, page, profile=profile,
                enable_apollo=enable_apollo, enable_apify=enable_apify, enable_explorium=enable_explorium,
                explorium_api_key=explorium_api_key, apify_actor_override=apify_actor,
                target=target, max_pages=max_pages,
                validated_companies=validated_companies,
            )
            raw_batch = result["leads"]
            apollo_total += result["counts"]["apollo"]
            apify_total  += result["counts"]["apify"]
            explorium_total += result["counts"]["explorium"]
            total_raw    += len(raw_batch)
            push_stats(raw=total_raw, apollo=apollo_total, apify=apify_total, explorium=explorium_total)

            if not raw_batch:
                pl.log.warning("Page %d returned 0 leads — stopping.", page)
                break

            # Dedupe (cumulative across pages)
            push_stage(5, f"Deduplication — page {page}")
            clean_batch, seen_emails, seen_fingerprints = pl.dedupe_leads(
                raw_batch, seen_emails, seen_fingerprints
            )
            total_deduped += len(clean_batch)
            push_stats(deduped=total_deduped)

            if not clean_batch:
                page += 1
                continue

            # LinkedIn cross-verify — company/title vs. LinkedIn, before other checks
            push_stage(6, f"LinkedIn Cross-Verify — page {page}")
            clean_batch = pl.linkedin_cross_verify_leads(
                clean_batch,
                on_progress=lambda done, total: push_stage_progress(6, done, total),
            )

            # Domain match — independent of email verification
            push_stage(7, f"Domain Match — page {page}")
            clean_batch = pl.domain_match_leads(clean_batch)

            # Verify
            push_stage(8, f"Email Verification — page {page}")
            verified_batch = pl.verify_emails(
                clean_batch, provider=verifier_provider,
                on_progress=lambda done, total: push_stage_progress(8, done, total),
            )

            newly = [l for l in verified_batch if l.get("_confidence_score", 0) >= 95.0]
            verified_pool.extend(verified_batch)
            verified_high_conf_count += len(newly)

            invalid_count  += sum(1 for l in verified_batch if l.get("_verification_status") == "invalid")
            catchall_count += sum(1 for l in verified_batch if l.get("_verification_status") == "catch-all")
            push_stats(
                verified=verified_high_conf_count,
                invalid=invalid_count,
                catchall=catchall_count,
            )
            push_progress(verified_high_conf_count, target, page)

            if verified_high_conf_count >= target:
                break
            page += 1

        # ── Stage 9: Composite Scoring + Sample Selection ────────────────────
        push_stage(9, "Composite Scoring & Sample Selection")
        verified_pool = pl.compute_composite_scores(verified_pool, icp)
        sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=None)
        push_stats(sample=len(sample))

        # ── Stage 10: Claim Verification ─────────────────────────────────────
        push_stage(10, "Verifying Claims")
        sample = pl.verify_claims_for_leads(
            sample, icp, on_progress=lambda done, total: push_stage_progress(10, done, total),
        )
        sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

        # ── Stage 11: Organization Enrichment ────────────────────────────────
        push_stage(11, "Enriching Organizations")
        sample = pl.enrich_organizations_for_leads(
            sample, on_progress=lambda done, total: push_stage_progress(11, done, total),
        )

        # ── Stage 12: Exporting CSV ────────────────────────────────────────────
        push_stage(12, "Exporting CSV")
        os.makedirs("./output", exist_ok=True)
        csv_path, report = pl.export_csv(
            sample=sample,
            all_leads_raw=total_raw,
            all_leads_deduped=total_deduped,
            all_leads_verified=len(verified_pool),
            output_dir="./output",
        )

        bounce_rate = round(
            sum(1 for l in sample if l.get("_verification_status") == "invalid")
            / max(len(sample), 1) * 100, 1
        )

        # Serialise leads (preserve all keys for the UI, don't cut anything!)
        def clean_lead(l):
            item = {k: v for k, v in l.items() if not k.startswith("_")}
            item["status"] = l.get("_verification_status", "")
            # "score" is now the Stage 7 composite (email + LinkedIn + domain
            # match + ICP fit) so existing sort/filter/badge UI gets the
            # smarter ranking for free; raw signals are exposed too for the
            # drawer breakdown.
            item["score"] = l.get("_composite_score", l.get("_confidence_score", 0.0))
            item["email_confidence"] = l.get("_confidence_score", 0.0)
            item["domain_signal"] = l.get("_domain_signal", "UNKNOWN")
            item["linkedin_signal"] = l.get("_linkedin_signal", "NO_LINKEDIN")
            item["linkedin_company_match"] = l.get("_linkedin_company_match")
            item["linkedin_title_match"] = l.get("_linkedin_title_match")
            item["linkedin_current_employee"] = l.get("_linkedin_current_employee")
            item["claim_verification_signal"] = l.get("_claim_verification_signal")
            item["claim_verification_evidence"] = l.get("_claim_verification_evidence")
            if "location" not in item:
                item["location"] = l.get("location", "")
            return item

        run["results"] = {
            "sample":   [clean_lead(l) for l in sample],
            "csv_path": csv_path,
            "stats": {
                "raw":         total_raw,
                "apollo":      apollo_total,
                "apify":       apify_total,
                "explorium":   explorium_total,
                "deduped":     total_deduped,
                "verified":    len(verified_pool),
                "invalid":     invalid_count,
                "catchall":    catchall_count,
                "sample":      len(sample),
                "bounce_rate": bounce_rate,
                "pages":       page,
            },
            "icp": icp,
        }
        save_run_metadata(run, csv_path, icp)
        run["status"] = "complete"
        q.put({"type": "complete", "data": run["results"]})

    except Exception as exc:
        run["status"] = "error"
        run["error"]  = str(exc)
        q.put({"type": "error", "message": str(exc)})
        app.logger.exception("Pipeline error for run %s", run_id)

    finally:
        pl.log.removeHandler(handler)
        q.put(None)   # sentinel — tells the SSE generator to stop


def run_pipeline_imported_thread(run_id: str, inquiry: str, raw_leads: list[dict], target: int = 25, verifier_provider: str = "custom"):
    run = runs[run_id]
    q: queue.Queue = run["queue"]

    # Attach our custom handler to the pipeline module's logger
    handler = RunQueueHandler(q)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pl.log.addHandler(handler)

    def push_stage(stage: int, label: str):
        q.put({"type": "stage", "stage": stage, "label": label})

    def push_stats(**kwargs):
        q.put({"type": "stats", **kwargs})

    def push_stage_progress(stage: int, done: int, total: int):
        q.put({"type": "stage_progress", "stage": stage, "done": done, "total": total})

    try:
        run["status"] = "running"

        # ── Stage 1: Parse ───────────────────────────────────────────────────
        push_stage(1, "Parsing Inquiry")
        icp = pl.parse_inquiry(inquiry)
        q.put({"type": "icp", "data": icp})

        # ── Stages 2-3: Company Discovery/Validation (skipped — companies ────
        # already exist in the uploaded file). Fired instantly as no-ops
        # purely so this thread's stage numbering stays a single shared
        # contract with run_pipeline_thread's — both drive the same sidebar
        # by absolute stage number.
        push_stage(2, "Company Discovery (skipped — companies from import)")
        push_stage(3, "Company Validation (skipped — companies from import)")

        # ── Stage 4: Scrape (Skipped / Imported) ─────────────────────────────
        push_stage(4, "Importing Leads (Scraping Skipped)")
        pl.log.info("Processing %d raw leads uploaded from file...", len(raw_leads))
        push_stats(raw=len(raw_leads), apollo=0, apify=len(raw_leads))

        # ── Stage 5: Deduplicate ─────────────────────────────────────────────
        push_stage(5, "Deduplication & Cleaning")
        clean_batch, seen_emails, seen_fingerprints = pl.dedupe_leads(raw_leads)
        total_deduped = len(clean_batch)
        push_stats(deduped=total_deduped)

        if not clean_batch:
            raise ValueError("All imported leads were duplicates or empty.")

        # ── Stage 6: LinkedIn Cross-Verify ────────────────────────────────────
        push_stage(6, "LinkedIn Cross-Verify")
        clean_batch = pl.linkedin_cross_verify_leads(
            clean_batch,
            on_progress=lambda done, total: push_stage_progress(6, done, total),
        )

        # ── Stage 7: Domain Match ─────────────────────────────────────────────
        push_stage(7, "Domain Match")
        clean_batch = pl.domain_match_leads(clean_batch)

        # ── Stage 8: Verify ──────────────────────────────────────────────────
        push_stage(8, "Email Verification")
        verified_batch = pl.verify_emails(
            clean_batch, provider=verifier_provider,
            on_progress=lambda done, total: push_stage_progress(8, done, total),
        )
        verified_pool = verified_batch
        verified_high_conf = [l for l in verified_batch if l.get("_confidence_score", 0) >= 95.0]

        invalid_count  = sum(1 for l in verified_batch if l.get("_verification_status") == "invalid")
        catchall_count = sum(1 for l in verified_batch if l.get("_verification_status") == "catch-all")
        push_stats(
            verified=len(verified_high_conf),
            invalid=invalid_count,
            catchall=catchall_count,
        )

        # ── Stage 9: Composite Scoring + Select sample ───────────────────────
        push_stage(9, "Composite Scoring & Sample Selection")
        verified_pool = pl.compute_composite_scores(verified_pool, icp)
        sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=target)
        push_stats(sample=len(sample))

        # ── Stage 10: Claim Verification ─────────────────────────────────────
        push_stage(10, "Verifying Claims")
        sample = pl.verify_claims_for_leads(
            sample, icp, on_progress=lambda done, total: push_stage_progress(10, done, total),
        )
        sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

        # ── Stage 11: Organization Enrichment ────────────────────────────────
        push_stage(11, "Enriching Organizations")
        sample = pl.enrich_organizations_for_leads(
            sample, on_progress=lambda done, total: push_stage_progress(11, done, total),
        )

        # ── Stage 12: Exporting CSV ────────────────────────────────────────────
        push_stage(12, "Exporting CSV")
        os.makedirs("./output", exist_ok=True)
        csv_path, report = pl.export_csv(
            sample=sample,
            all_leads_raw=len(raw_leads),
            all_leads_deduped=total_deduped,
            all_leads_verified=len(verified_pool),
            output_dir="./output",
        )

        bounce_rate = round(
            sum(1 for l in sample if l.get("_verification_status") == "invalid")
            / max(len(sample), 1) * 100, 1
        )

        # Serialise leads (strip internal _ keys for the UI)
        def clean_lead(l):
            # Preserve every field the uploaded file/scraper actually had
            # (city, state, phone, employee_count, etc.) instead of the
            # narrow hardcoded list this used to return — that was silently
            # dropping columns even when the source data had them.
            item = {k: v for k, v in l.items() if not k.startswith("_")}
            item["status"] = l.get("_verification_status", "")
            item["score"] = l.get("_composite_score", l.get("_confidence_score", 0))
            item["email_confidence"] = l.get("_confidence_score", 0)
            item["domain_signal"] = l.get("_domain_signal", "UNKNOWN")
            item["linkedin_signal"] = l.get("_linkedin_signal", "NO_LINKEDIN")
            item["linkedin_company_match"] = l.get("_linkedin_company_match")
            item["linkedin_title_match"] = l.get("_linkedin_title_match")
            item["linkedin_current_employee"] = l.get("_linkedin_current_employee")
            item["claim_verification_signal"] = l.get("_claim_verification_signal")
            item["claim_verification_evidence"] = l.get("_claim_verification_evidence")
            return item

        run["results"] = {
            "sample":   [clean_lead(l) for l in sample],
            "csv_path": csv_path,
            "stats": {
                "raw":         len(raw_leads),
                "apollo":      0,
                "apify":       len(raw_leads),
                "deduped":     total_deduped,
                "verified":    len(verified_pool),
                "invalid":     invalid_count,
                "catchall":    catchall_count,
                "sample":      len(sample),
                "bounce_rate": bounce_rate,
                "pages":       1,
            },
            "icp": icp,
        }
        save_run_metadata(run, csv_path, icp)
        run["status"] = "complete"
        q.put({"type": "complete", "data": run["results"]})

    except Exception as exc:
        run["status"] = "error"
        run["error"]  = str(exc)
        q.put({"type": "error", "message": str(exc)})
        app.logger.exception("Pipeline error for run %s", run_id)

    finally:
        pl.log.removeHandler(handler)
        q.put(None)   # sentinel — tells the SSE generator to stop


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
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


@app.route("/api/run", methods=["POST"])
def start_run():
    data    = request.get_json(force=True)
    inquiry = (data.get("inquiry") or "").strip()
    if not inquiry:
        return jsonify({"error": "inquiry is required"}), 400

    target            = int(data.get("target", 25))
    max_pages         = int(data.get("max_pages", 10))
    enable_apollo     = bool(data.get("enable_apollo", True))
    enable_apify       = bool(data.get("enable_apify", True))
    enable_explorium  = bool(data.get("enable_explorium", False))
    explorium_api_key = data.get("explorium_api_key")
    verifier_provider = str(data.get("verifier_provider", "custom"))
    icp               = data.get("icp")
    profile           = str(data.get("profile", "balanced"))
    apify_actor       = data.get("apify_actor") or None

    run_id = uuid.uuid4().hex[:8]
    runs[run_id] = {
        "id":      run_id,
        "inquiry": inquiry,
        "status":  "pending",
        "queue":   queue.Queue(),
        "results": None,
        "error":   None,
    }

    thread = threading.Thread(
        target=run_pipeline_thread,
        kwargs={
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


@app.route("/api/generate-icp", methods=["POST"])
def generate_icp_only():
    data = request.get_json(force=True)
    inquiry = (data.get("inquiry") or "").strip()
    if not inquiry:
        return jsonify({"error": "inquiry is required"}), 400

    try:
        icp = pl.parse_inquiry(inquiry)
        return jsonify({"status": "success", "icp": icp})
    except Exception as e:
        app.logger.error("Failed to generate ICP: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/generate-buyer-icp", methods=["POST"])
def generate_buyer_icp():
    """
    Inverted ICP mode: given a description of a dataset/audience, returns
    who would BUY that dataset (not the audience itself). "icp" is directly
    usable by /api/run-custom to run a real pipeline search; "buyer_report"
    is the fuller 12-section brief for display/export only.
    """
    data = request.get_json(force=True)
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description is required"}), 400

    try:
        result = pl.parse_buyer_inquiry(description)
        return jsonify({
            "status": "success",
            "icp": result.get("icp"),
            "buyer_report": result.get("buyer_report"),
        })
    except Exception as e:
        app.logger.error("Failed to generate buyer ICP: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/estimate-coverage", methods=["POST"])
def estimate_coverage_route():
    data = request.get_json(force=True)
    icp = data.get("icp")
    if not icp:
        return jsonify({"error": "icp object is required"}), 400
    target = int(data.get("target", 25))

    try:
        estimate = pl.estimate_coverage(icp, target)
        return jsonify({"status": "success", "estimate": estimate})
    except Exception as e:
        app.logger.error("Failed to estimate coverage: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/chat-icp", methods=["POST"])
def chat_icp_route():
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    current_icp = data.get("icp")

    if not message:
        return jsonify({"error": "message is required"}), 400

    try:
        result = pl.chat_icp(message, history, current_icp)
        return jsonify({
            "status": "success",
            "chat_response": result.get("chat_response"),
            "suggested_replies": result.get("suggested_replies") or ["+ Founder", "+ SaaS", "+ United States"],
            "icp": result.get("icp")
        })
    except Exception as e:
        app.logger.error("Failed in chat_icp_route: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/run-custom", methods=["POST"])
def start_custom_run():
    data = request.get_json(force=True)
    icp = data.get("icp")
    if not icp:
        return jsonify({"error": "icp object is required"}), 400

    target            = int(data.get("target", 25))
    max_pages         = int(data.get("max_pages", 10))
    enable_apollo     = bool(data.get("enable_apollo", True))
    enable_apify       = bool(data.get("enable_apify", True))
    enable_explorium  = bool(data.get("enable_explorium", False))
    explorium_api_key = data.get("explorium_api_key")
    verifier_provider = str(data.get("verifier_provider", "custom"))
    profile           = str(data.get("profile", "balanced"))
    apify_actor       = data.get("apify_actor") or None

    run_id = uuid.uuid4().hex[:8]
    runs[run_id] = {
        "id":      run_id,
        "inquiry": "Custom ICP Run",
        "status":  "pending",
        "queue":   queue.Queue(),
        "results": None,
        "error":   None,
    }

    thread = threading.Thread(
        target=run_pipeline_thread,
        kwargs={
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


@app.route("/api/run-imported", methods=["POST"])
def start_imported_run():
    inquiry = request.form.get("inquiry", "").strip()
    target = int(request.form.get("target", 25))
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

    run_id = uuid.uuid4().hex[:8]
    runs[run_id] = {
        "id":      run_id,
        "inquiry": inquiry,
        "status":  "pending",
        "queue":   queue.Queue(),
        "results": None,
        "error":   None,
    }

    thread = threading.Thread(
        target=run_pipeline_imported_thread,
        args=(run_id, inquiry, raw_leads, target, verifier_provider),
        daemon=True,
    )
    thread.start()

    return jsonify({"run_id": run_id})


@app.route("/api/csv-mapper/analyze", methods=["POST"])
def csv_mapper_analyze():
    """
    Standalone personal utility — no relation to the lead-gen pipeline.
    Parses an uploaded CSV, suggests a mapping of its columns onto LeadFlow's
    own canonical lead structure, and returns everything the client needs to
    render a preview + editable mapping table. Stateless: nothing is written
    to disk and no `runs` entry is created.
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


@app.route("/api/csv-mapper/process", methods=["POST"])
def csv_mapper_process():
    """
    Cleans, validates, and deduplicates rows already parsed by
    /api/csv-mapper/analyze — the client sends them back along with the
    current (possibly user-edited) mapping and settings, avoiding a second
    file upload. Also persists the resulting normalized CSV under
    ./output/csv_mapper/. Stateless otherwise: no `runs` entry is created.
    """
    body = request.get_json(silent=True) or {}
    rows = body.get("rows") or []
    mapping = body.get("mapping") or {}
    settings = body.get("settings") or {}

    if not rows or not mapping:
        return jsonify({"error": "rows and mapping are required"}), 400

    result = pl.process_csv_mapper_rows(rows, mapping, settings)
    return jsonify(result)


@app.route("/api/csv-mapper/templates", methods=["GET"])
def csv_mapper_list_templates():
    return jsonify(pl.list_csv_mapping_templates())


@app.route("/api/csv-mapper/templates", methods=["POST"])
def csv_mapper_save_template():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "template name is required"}), 400
    pl.save_csv_mapping_template(name, body.get("mapping") or {}, body.get("settings") or {})
    return jsonify({"ok": True})


@app.route("/api/csv-mapper/templates/<name>", methods=["DELETE"])
def csv_mapper_delete_template(name):
    pl.delete_csv_mapping_template(name)
    return jsonify({"ok": True})


@app.route("/api/csv-mapper/downloads")
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


@app.route("/api/csv-mapper/leads")
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


@app.route("/api/csv-mapper/download/<filename>")
def csv_mapper_download_file(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400
    path = os.path.join("output", "csv_mapper", filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True)


@app.route("/api/csv-mapper/preview/<filename>")
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


@app.route("/api/stream/<run_id>")
def stream(run_id: str):
    if run_id not in runs:
        return jsonify({"error": "run not found"}), 404

    q: queue.Queue = runs[run_id]["queue"]

    def generate():
        while True:
            msg = q.get()
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


@app.route("/api/results/<run_id>")
def results(run_id: str):
    run = runs.get(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    if run["status"] != "complete":
        return jsonify({"status": run["status"], "error": run.get("error")}), 202
    return jsonify(run["results"])


@app.route("/api/runs")
def list_runs():
    return jsonify([
        {"id": r["id"], "status": r["status"], "inquiry": r["inquiry"]}
        for r in runs.values()
    ])


@app.route("/api/history")
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
                
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                            inquiry = meta.get("inquiry", "Historical Scraped Run")
                            lead_count = meta.get("stats", {}).get("sample", 0)
                            if meta.get("timestamp"):
                                timestamp = meta.get("timestamp")
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
                    "leads": lead_count if lead_count >= 0 else 0
                })
    files.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify(files)


@app.route("/api/history/<filename>")
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


@app.route("/api/download/<filename>")
def download_file(filename):
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "invalid filename"}), 400
    path = os.path.join("output", filename)
    if not os.path.exists(path):
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, threaded=True, port=5001)

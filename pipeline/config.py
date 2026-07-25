"""
Lead Generation Sample Delivery Pipeline
=========================================
Stages (matches app/pipeline_runner.py's push_stage() numbering — the
absolute stage numbers the frontend's sidebar renders):
  1. parse_inquiry                 → AI-powered ICP extraction via Gemini
  2. build_search_plan              → Search Planner: translates the ICP into a search
                                       strategy tailored to leads-finder's real constraints
                                       (fixed industry enum, literal keyword filters)
  3. discover_companies             → Gemini + Apify google-search-scraper company discovery
  4. validate_companies             → rule pass + capped AI fact-check on candidate companies
  5. scrape_apify                   → Apify (code_crafter/leads-finder) lead scraping
  6. dedupe_leads                   → Deduplication + standardization
  7. linkedin_cross_verify_leads    → LinkedIn current-employer/title check (Bright Data)
  8. domain_match_leads             → Email domain vs. company website
  9. verify_emails                  → DNS/SMTP waterfall, ZeroBounce, or gmail_bounce
  10. compute_composite_scores + select_sample → blended scoring + best-N selection
  11. verify_claims_for_leads       → dynamic technology/industry claim verification (final sample only)
  12. enrich_organizations_for_leads → Organization Enrichment backfill (final sample only; dormant — no provider configured)
  13. rerank_final_sample           → final Gemini holistic re-sort (reorder-only)
  14. export_csv                    → CSV output + summary report

This module owns process-wide setup (dotenv, logging, API key globals) —
every other submodule in this package imports `pipeline as pl` and reads
these through `pl.NAME` rather than importing the bare names directly, so
that `unittest.mock.patch("pipeline.NAME", ...)` continues to affect every
caller regardless of which submodule it lives in.
"""

import os
import logging
import contextvars
from typing import Optional

from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Tags the calling thread's pipeline activity with the run id driving it.
# Each pipeline run executes in its own dedicated background thread (see
# app/pipeline_runner.py), so a plain ContextVar set once at the top of that
# thread stays correctly isolated per run for its whole lifetime. Two
# consumers: (1) RunIdFilter (app/logging_utils.py) uses it to keep two
# concurrent runs' SSE log streams from leaking into each other — previously
# every run's RunQueueHandler was attached to this same module-level `log`,
# so a record from run A was broadcast to run B's handler too; (2) debug
# dump filenames below use it so concurrent runs don't race on the same file.
current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_run_id", default=None)


def set_current_run_id(run_id: Optional[str]) -> None:
    current_run_id.set(run_id)


def get_current_run_id() -> Optional[str]:
    return current_run_id.get()


def submit_with_context(executor, fn, *args, **kwargs):
    """ThreadPoolExecutor.submit() wrapper that propagates the calling
    thread's contextvars — specifically current_run_id — into the worker
    thread. concurrent.futures does NOT do this automatically (unlike
    asyncio, which does): a bare `executor.submit(fn, ...)` runs `fn` with
    a fresh, empty context, so `pl.log.*()` calls made inside a worker
    (email verification, LinkedIn cross-verify, org enrichment, and
    per-source scraping all fan out via ThreadPoolExecutor) see
    current_run_id as None. RunIdFilter (app/logging_utils.py) then drops
    those records instead of routing them to the run's SSE log stream —
    silently, no error, just missing log lines during exactly the stages
    that take the longest. Every ThreadPoolExecutor.submit() call in this
    package should go through this instead of calling submit() directly."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def _debug_dump_path(basename: str) -> str:
    """Per-run debug dump path (e.g. apollo_raw.json) so concurrent runs
    don't race on/overwrite each other's raw-response dumps — these can
    contain PII, so cross-run leakage here was a real bug, not just noise."""
    run_id = get_current_run_id()
    if run_id:
        stem, ext = os.path.splitext(basename)
        return f"./output/{stem}_{run_id}{ext}"
    return f"./output/{basename}"


# API keys loaded from .env
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN")
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY")
LINKEDIN_API_KEY   = os.getenv("LINKEDIN_API_KEY")
LINKEDIN_API_URL   = os.getenv("LINKEDIN_API_URL")

# Gmail account used by the "gmail_bounce" email verifier provider (sends a
# real email to each lead and watches for a real bounce — see
# pipeline/scoring.py::verify_emails_via_gmail_bounce()). GMAIL_APP_PASSWORD
# must be a 16-character Gmail App Password (myaccount.google.com/apppasswords),
# not the account's normal login password — Gmail requires 2FA + an app
# password for raw SMTP/IMAP login.
GMAIL_SENDER_ADDRESS = os.getenv("GMAIL_SENDER_ADDRESS")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD")

# Left defined (unset) purely so org_enrichment.py's `if not pl.APOLLO_API_KEY`
# guard keeps working — Apollo is not a supported platform in this codebase;
# Stage 9 (Organization Enrichment) is a permanent no-op without it.
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")

# Apify actor — this pipeline's sole lead-sourcing platform.
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "code_crafter/leads-finder")

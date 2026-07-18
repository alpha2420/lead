"""
Lead Generation Sample Delivery Pipeline
=========================================
Stages:
  1. parse_inquiry              → AI-powered ICP extraction via Gemini
  2. scrape_apollo/apify/explorium → multi-source lead scraping
  3. dedupe_leads                → Deduplication + standardization
  4. linkedin_cross_verify_leads → LinkedIn current-employer/title check (Bright Data)
  5. domain_match_leads          → Email domain vs. company website
  6. verify_emails                → DNS/SMTP waterfall or ZeroBounce
  7. compute_composite_scores + select_sample → blended scoring + best-N selection
  8. verify_claims_for_leads      → dynamic technology/industry claim verification (final sample only)
  9. enrich_organizations_for_leads → Apollo Organization Enrichment backfill (final sample only)
  10. export_csv                  → CSV output + summary report

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
APOLLO_API_KEY     = os.getenv("APOLLO_API_KEY")
APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN")
ZEROBOUNCE_API_KEY = os.getenv("ZEROBOUNCE_API_KEY")
EXPLORIUM_API_KEY  = os.getenv("EXPLORIUM_API_KEY")
LINKEDIN_API_KEY   = os.getenv("LINKEDIN_API_KEY")
LINKEDIN_API_URL   = os.getenv("LINKEDIN_API_URL")

# Apify actor ID — replace with your real actor slug, e.g. "apify/linkedin-profile-scraper"
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "REPLACE_WITH_YOUR_ACTOR_ID")

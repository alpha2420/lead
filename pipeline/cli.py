import os
import csv
import json
import time
import math
import logging
import hashlib
import re
import smtplib
import socket
import sqlite3
import difflib
import urllib.parse
import dns.resolver
import requests
import contextvars
from datetime import datetime, timedelta
from typing import Optional
import google.genai as genai
import pipeline as pl


def main(
    inquiry: str,
    output_dir: str = ".",
    target: int = 25,
    max_pages: int = 10,
):
    """
    Runs the full lead-generation pipeline end-to-end.

    Runs a single Apify (code_crafter/leads-finder) call on page 1 — it has
    no page param — repeating dedupe/verify on that one batch until
    `target` leads with ≥95% ZeroBounce confidence are collected, or
    `max_pages` is exhausted.

    Args:
        inquiry           : Raw customer inquiry text.
        output_dir        : Directory for CSV / report output.
        target            : Number of verified leads to collect (default 25).
        max_pages         : Safety cap on pagination attempts.
    """
    pl.log.info("═" * 55)
    pl.log.info("  LEAD GENERATION PIPELINE — STARTING (target: %d verified)", target)
    pl.log.info("═" * 55)

    # ── Stage 1: Parse inquiry ────────────────────────────────────────────────
    icp = pl.parse_inquiry(inquiry)

    # ── Pagination loop ───────────────────────────────────────────────────────
    verified_pool:     list[dict] = []    # accumulates qualified verified leads
    seen_emails:       set        = set() # cross-page dedup state
    seen_fingerprints: set        = set()
    seen_linkedin_ids: set        = set()
    seen_phones:       set        = set()
    total_raw     = 0
    total_deduped = 0
    page          = 1

    # NOTE: this CLI entry point predates the Source Orchestrator
    # (pl.run_lead_sources()) and is not wired to it — it's an unused mirror of
    # app.py::run_pipeline_thread()'s pagination loop, deliberately left as
    # a plain sequential call-everything-enabled loop rather than updated in
    # lockstep. app.py::run_pipeline_thread() is the canonical, orchestrated
    # version; this one is out of sync by design, not by oversight.
    while len(verified_pool) < target and page <= max_pages:
        pl.log.info(
            "── Page %d │ verified so far: %d/%d ─────────────────────────────",
            page, len(verified_pool), target,
        )

        # ── Stage 2: Scrape ───────────────────────────────────────────────────
        # Apify is expensive — run only on the first page (no page param).
        raw_batch  = pl.scrape_apify(icp, max_leads=50) if page == 1 else []
        total_raw += len(raw_batch)

        if not raw_batch:
            pl.log.warning("Page %d returned 0 leads — stopping pagination.", page)
            break

        # ── Stage 3: Dedupe (cumulative across pages) ─────────────────────────
        clean_batch, seen_emails, seen_fingerprints, seen_linkedin_ids, seen_phones = pl.dedupe_leads(
            raw_batch, seen_emails, seen_fingerprints, seen_linkedin_ids, seen_phones
        )
        total_deduped += len(clean_batch)

        if not clean_batch:
            pl.log.info("Page %d: all leads were duplicates — trying next page.", page)
            page += 1
            continue

        # ── Stage 4: LinkedIn cross-verify ────────────────────────────────────
        clean_batch = pl.linkedin_cross_verify_leads(clean_batch)

        # ── Stage 5: Domain match ─────────────────────────────────────────────
        clean_batch = pl.domain_match_leads(clean_batch)

        # ── Stage 6: Verify emails ────────────────────────────────────────────
        verified_batch = pl.verify_emails(clean_batch)

        # Only keep leads that cleared the 95% threshold
        newly_qualified = [
            l for l in verified_batch
            if l.get("_confidence_score", 0) >= 95.0
        ]
        verified_pool.extend(newly_qualified)

        pl.log.info(
            "Page %d complete: +%d verified │ pool: %d/%d",
            page, len(newly_qualified), len(verified_pool), target,
        )
        page += 1

    # ── Stage 7: Composite scoring + select final sample ─────────────────────
    verified_pool = pl.compute_composite_scores(verified_pool, icp)
    sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=target)

    if len(sample) < target:
        pl.log.warning(
            "⚠ Collected %d/%d verified leads after %d pages. "
            "Increase max_pages, broaden ICP, or upgrade the Apify plan.",
            len(sample), target, page - 1,
        )
    else:
        pl.log.info("✅ Target reached: %d verified leads collected.", len(sample))

    # ── Stage 8: Claim Verification ───────────────────────────────────────────
    sample = pl.verify_claims_for_leads(sample, icp)
    sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

    # ── Stage 9: Organization Enrichment ──────────────────────────────────────
    sample = pl.enrich_organizations_for_leads(sample)

    # ── Stage 10: Export ──────────────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    csv_path, report = pl.export_csv(
        sample=sample,
        all_leads_raw=total_raw,
        all_leads_deduped=total_deduped,
        all_leads_verified=len(verified_pool),
        output_dir=output_dir,
    )

    pl.log.info("═" * 55)
    pl.log.info("  PIPELINE COMPLETE — CSV: %s", csv_path)
    pl.log.info("═" * 55)

    return sample, csv_path, report


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # You can pass the inquiry as a command-line argument or hardcode it here for testing.
    if len(sys.argv) > 1:
        customer_inquiry = " ".join(sys.argv[1:])
    else:
        # Default test inquiry
        customer_inquiry = (
            "Hi, I'm looking for marketing directors and VP of marketing "
            "at B2B SaaS companies in the United States. "
            "Company size should be between 50 and 200 employees. "
            "Ideally they use Salesforce or HubSpot."
        )

    main(customer_inquiry, output_dir="./output")

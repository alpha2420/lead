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


_ORG_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "org_enrichment_cache.db")
_ORG_CACHE_TTL_DAYS = 30


def _org_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_ORG_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_ORG_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orgs (domain TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_org_enrichment(domain: str) -> Optional[dict]:
    """Returns the cached Apollo Organization Enrichment response if younger
    than the TTL, else None. Enrichment costs credits per domain, so a repeat
    lookup for the same company within 30 days reuses the cached result."""
    try:
        conn = _org_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM orgs WHERE domain = ?", (domain,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_ORG_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Org enrichment cache read failed for %s: %s", domain, e)
        return None


def _cache_org_enrichment(domain: str, data: dict) -> None:
    try:
        conn = _org_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO orgs (domain, response_json, fetched_at) VALUES (?, ?, ?)",
                (domain, json.dumps(data), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Org enrichment cache write failed for %s: %s", domain, e)


def _org_enrichment_domain(lead: dict) -> str:
    """Company domain to enrich against — the lead's own company_domain if
    present, else derived from the lead's email domain (same idiom used in
    pl.domain_match_signal())."""
    domain = pl.normalize_domain(lead.get("company_domain") or "")
    if domain:
        return domain
    email = lead.get("email") or ""
    if "@" in email:
        return pl.normalize_domain(email.split("@", 1)[1])
    return ""


def _backfill_org_fields(lead: dict, org: Optional[dict]) -> None:
    """Fills employee_count/market_cap/industry/biz_category/biz_description/
    technology from an Organization Enrichment lookup — only the fields the
    scraper left blank. Never overwrites data Apollo/Explorium already provided."""
    if not org:
        return
    for field in ("employee_count", "market_cap", "industry", "biz_category", "biz_description", "technology"):
        if not lead.get(field) and org.get(field):
            lead[field] = org[field]


def enrich_organization(domain: str) -> Optional[dict]:
    """Cache-first Apollo Organization Enrichment lookup for a single company
    domain. Returns None on any failure (no domain, no API key, no credits,
    network error, unknown domain) so callers degrade gracefully instead of
    breaking the pipeline — enrichment is a bonus, not a requirement."""
    if not domain:
        return None

    cached = pl._get_cached_org_enrichment(domain)
    if cached is not None:
        return cached

    if not pl.APOLLO_API_KEY:
        return None

    headers = {"X-Api-Key": pl.APOLLO_API_KEY}
    try:
        resp = requests.get(
            "https://api.apollo.io/v1/organizations/enrich",
            params={"domain": domain}, headers=headers, timeout=20,
        )
        if resp.status_code != 200:
            pl.log.warning("Apollo org enrichment failed for %s: HTTP %d — %s", domain, resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        org = data.get("organization") or {}
        if not org:
            return None
        result = {
            "employee_count":  pl._safe(org.get("estimated_num_employees")),
            "market_cap":      pl._safe(org.get("market_cap")),
            "industry":        pl._safe(org.get("industry")),
            "biz_category":    pl._join_list_str(org.get("keywords") or org.get("secondary_industries")),
            "biz_description": pl._safe(org.get("short_description")),
            "technology":      pl._join_list_str(org.get("technology_names")),
        }
        pl._cache_org_enrichment(domain, result)
        return result
    except Exception as e:
        pl.log.warning("Apollo org enrichment error for %s: %s", domain, e)
        return None


def enrich_organizations_for_leads(leads: list[dict], on_progress=None, max_workers: int = 5) -> list[dict]:
    """Stage 9 — backfills company profile fields on the final sample.
    Dedupes by company domain first since many leads in a sample often share
    an employer — one Organization Enrichment call then serves all of them,
    keeping Apollo credit usage proportionate to unique companies, not leads."""
    pl.log.info("Stage 9 — Enriching organizations for %d leads …", len(leads))

    needing: list[tuple[dict, str]] = []
    for lead in leads:
        already_has_all = all(
            lead.get(f) for f in ("biz_category", "biz_description", "technology", "employee_count", "market_cap", "industry")
        )
        if already_has_all:
            continue
        domain = _org_enrichment_domain(lead)
        if domain:
            needing.append((lead, domain))

    done = len(leads) - len(needing)
    total = len(leads)
    if on_progress:
        on_progress(done, total)

    unique_domains = pl._dedupe_list([d for _, d in needing])

    domain_results: dict = {}
    if unique_domains:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_domain = {pl.submit_with_context(executor, enrich_organization, d): d for d in unique_domains}
            for future in future_to_domain:
                domain = future_to_domain[future]
                try:
                    domain_results[domain] = future.result()
                except Exception as e:
                    pl.log.error("Unhandled error in org enrichment for %s: %s", domain, e)
                    domain_results[domain] = None

    for lead, domain in needing:
        _backfill_org_fields(lead, domain_results.get(domain))
        done += 1
        if on_progress:
            on_progress(done, total)

    return leads


# ─────────────────────────────────────────────
# Stage 10 — CSV Export + Summary Report
# ─────────────────────────────────────────────


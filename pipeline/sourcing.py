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


_DEFAULT_PROFILE = "balanced"

# Each profile bundles priority order + execution mode + per-source
# max_leads. "balanced" preserves today's exact pre-orchestrator limits
# (Apollo 25 / Apify 50 / Explorium 50) so it's provably behavior-preserving
# as the default.
_SOURCE_PROFILES = {
    "cost_conscious": {
        "priority_order": ["apollo", "apify", "explorium"],
        "execution_mode": "sequential",
        "source_limits": {
            "apollo": {"max_leads": 25},
            "apify": {"max_leads": 25},
            "explorium": {"max_leads": 25},
        },
        # How many rule-validated companies get the paid AI fact-check
        # (Company Validation Tier B) — see pl.validate_companies(). Companies
        # beyond this cap stay validated (rule-only), never dropped.
        "max_ai_validate_companies": 15,
    },
    "balanced": {
        "priority_order": ["apollo", "apify", "explorium"],
        "execution_mode": "sequential",
        "source_limits": {
            "apollo": {"max_leads": 25},
            "apify": {"max_leads": 50},
            "explorium": {"max_leads": 50},
        },
        "max_ai_validate_companies": 40,
    },
    "maximum_coverage": {
        "priority_order": ["apollo", "apify", "explorium"],
        "execution_mode": "parallel",
        "source_limits": {
            "apollo": {"max_leads": 25},
            "apify": {"max_leads": 50},
            "explorium": {"max_leads": 50},
        },
        "max_ai_validate_companies": 100,
    },
}

# Waterfall quota tuning (sequential mode only) — rough empirical proxy: the
# pipeline's own dedupe/LinkedIn/domain/email waterfall means roughly 1 in 3
# raw leads survives to >=95% confidence, so a page needs about 3x its
# verified-lead target in raw leads to likely satisfy it. Tunable; not
# measured from real run data yet.
_RAW_LEADS_PER_VERIFIED_LEAD = 3
# Floor so a tiny target/max_pages ratio (e.g. target=25, max_pages=25 -> 1
# verified lead/page) doesn't let a 2-lead response short-circuit everything.
_MIN_RAW_QUOTA_FLOOR = 10


def _active_sources_for_profile(
    profile_name: str, enable_apollo: bool, enable_apify: bool, enable_explorium: bool
) -> tuple[list[str], dict]:
    """Filters a profile's priority_order down to only user-enabled
    sources, preserving relative order. The enable_* flags remain the
    opt-in/opt-out layer — a profile never re-enables a source the user
    explicitly turned off."""
    profile = _SOURCE_PROFILES.get(profile_name, _SOURCE_PROFILES[_DEFAULT_PROFILE])
    enabled_map = {"apollo": enable_apollo, "apify": enable_apify, "explorium": enable_explorium}
    active = [s for s in profile["priority_order"] if enabled_map.get(s, False)]
    return active, profile


def _call_source(
    source: str, icp: dict, page: int, profile: dict, explorium_api_key: Optional[str],
    apify_actor_override: Optional[str] = None,
    validated_companies: Optional[list[dict]] = None,
) -> list[dict]:
    """Dispatches to the named source's adapter with that profile's configured
    max_leads. Never raises — each adapter already returns [] gracefully on
    any failure, so this is just a name -> function lookup.

    validated_companies: when People Discovery is running company-first
    (post Company Discovery/Validation), scopes Apollo by domain and Apify
    crawlerbros by company name directly in the request. Explorium has no
    such filter in its API at all, so its results get post-filtered
    afterward instead (pl._filter_leads_by_validated_companies) — applied here
    as a defense-in-depth backstop for every source, not just Explorium, in
    case a provider silently ignores its scoping filter."""
    max_leads = profile["source_limits"].get(source, {}).get("max_leads", 25)
    if source == "apollo":
        domains = [c["domain"] for c in (validated_companies or []) if c.get("domain")] or None
        leads = pl.scrape_apollo(icp, max_leads=max_leads, page=page, organization_domains=domains)
    elif source == "apify":
        names = [c["name"] for c in (validated_companies or []) if c.get("name")] or None
        leads = pl.scrape_apify(icp, max_leads=max_leads, actor_override=apify_actor_override, company_names=names)
    elif source == "explorium":
        leads = pl.scrape_explorium(icp, max_leads=max_leads, page=page, api_key=explorium_api_key)
    else:
        return []

    if validated_companies:
        leads = pl._filter_leads_by_validated_companies(leads, validated_companies)
    return leads


def run_lead_sources(
    icp: dict,
    page: int,
    profile: str = _DEFAULT_PROFILE,
    enable_apollo: bool = True,
    enable_apify: bool = True,
    enable_explorium: bool = False,
    explorium_api_key: Optional[str] = None,
    apify_actor_override: Optional[str] = None,
    target: int = 25,
    max_pages: int = 10,
    validated_companies: Optional[list[dict]] = None,
) -> dict:
    """
    Orchestrates one page's worth of source calls according to the named
    profile. Returns {"leads": [...], "counts": {"apollo": n, "apify": n,
    "explorium": n}}.

    Hard rule preserved regardless of profile: Apify never fires when
    page != 1 — its "expensive, run once" nature is inherent to the
    adapter (it has no page param), not a profile concern.

    apify_actor_override: a per-run choice of which Apify actor to use
    (e.g. from a UI selector), passed straight through to pl.scrape_apify() —
    takes priority over the pl.APIFY_ACTOR_ID env var for this run only.

    validated_companies: People Discovery stage output from
    pl.discover_and_validate_companies() — when supplied, this becomes a
    company-scoped search (each source's People Discovery variant) instead
    of an open ICP-wide search. None preserves the original people-first
    behavior exactly.
    """
    active, prof = _active_sources_for_profile(profile, enable_apollo, enable_apify, enable_explorium)
    if page != 1 and "apify" in active:
        active = [s for s in active if s != "apify"]

    counts = {"apollo": 0, "apify": 0, "explorium": 0}
    all_leads: list[dict] = []

    if prof["execution_mode"] == "sequential":
        # Waterfall: call sources in priority order, stop once this page's
        # quota is met so lower-priority sources get skipped entirely —
        # the actual cost-saving mechanism (directly motivated by the real
        # Apollo/Apify credit-exhaustion issues hit earlier this session).
        per_page_target = math.ceil(target / max(max_pages, 1))
        per_page_quota = max(per_page_target * _RAW_LEADS_PER_VERIFIED_LEAD, _MIN_RAW_QUOTA_FLOOR)
        for source in active:
            try:
                batch = _call_source(source, icp, page, prof, explorium_api_key, apify_actor_override, validated_companies)
            except Exception as e:
                # One source's malformed ICP field (e.g. a non-numeric
                # company_size/revenue value that slipped past coercion)
                # must not kill the whole run — skip this source for this
                # page and let the remaining sources/pages still contribute.
                pl.log.error("Source Orchestrator — %s raised during sequential execution: %s", source, e)
                batch = []
            counts[source] = len(batch)
            all_leads.extend(batch)
            if len(all_leads) >= per_page_quota:
                skipped = active[active.index(source) + 1:]
                if skipped:
                    pl.log.info(
                        "Source Orchestrator — page %d quota met (%d/%d raw leads) after '%s', skipping %s.",
                        page, len(all_leads), per_page_quota, source, skipped,
                    )
                break
    else:
        # Parallel: all active sources fire concurrently, no waterfall skip
        # is possible mid-round — priority_order only affects submission/
        # logging order here, not whether a source gets called.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(len(active), 1)) as executor:
            future_to_source = {
                executor.submit(
                    _call_source, s, icp, page, prof, explorium_api_key, apify_actor_override, validated_companies
                ): s for s in active
            }
            for future in future_to_source:
                source = future_to_source[future]
                try:
                    batch = future.result()
                except Exception as e:
                    pl.log.error("Source Orchestrator — %s raised during parallel execution: %s", source, e)
                    batch = []
                counts[source] = len(batch)
                all_leads.extend(batch)

    return {"leads": all_leads, "counts": counts}


# ─────────────────────────────────────────────
# Stage 2/3 — Company Discovery + Company Validation (company-first search)
# ─────────────────────────────────────────────
# Runs before run_lead_sources() when the caller wants a company-first
# search: find candidate companies matching the ICP's firmographics first,
# validate they actually fit, then scope People Discovery
# (run_lead_sources(..., validated_companies=...)) to exactly that list.
#
# No provider in this codebase has a working company-search API today —
# live-tested during design: Apollo's real company-search endpoint
# (mixed_companies/search) exists and is reachable but returns 422
# "insufficient credits" on the current plan; Explorium has no
# company/business endpoint at all; no Apify actor does structured
# ICP-driven company discovery. So discovery below combines whatever
# actually works today (a free Apollo people-search preview with titles
# omitted, plus a Gemini+web-search fallback) with a forward-compatible
# slot that auto-detects and uses the real Apollo endpoint the moment the
# account's plan supports it — zero code changes needed then.


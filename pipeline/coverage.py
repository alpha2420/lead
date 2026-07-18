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


_COVERAGE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "coverage_estimate_cache.db")
_COVERAGE_CACHE_TTL_DAYS = 3   # shorter than the 30-day convention used elsewhere —
                                # this is presented to the user as a live estimate,
                                # not a stable enrichment fact, and Apollo's index drifts.

_COVERAGE_NARROW_DIMENSIONS = ("technology", "size", "location", "exclusions")
_COVERAGE_NARROW_THRESHOLD = 3.0   # flag a dimension only if removing it would
                                     # roughly triple (or more) the match count
_COVERAGE_MAX_SUGGESTIONS_PER_TYPE = 2


def _apollo_search_probe(
    icp: dict, per_page: int = 100, extra_keyword_tags: Optional[list[str]] = None,
    omit_dimension: Optional[str] = None,
) -> dict:
    """
    Builds the exact same api_search payload pl.scrape_apollo() builds — so an
    estimate always reflects what a real run would actually search for —
    but stops immediately after the search call, never touching enrichment
    (pl.scrape_apollo's real cost is entirely in its per-candidate
    ThreadPoolExecutor enrichment step; this function never runs that).

    extra_keyword_tags: appended before sending — "what if we also searched
    for this suggested term" (see _generate_coverage_suggestions()).
    omit_dimension: one of _COVERAGE_NARROW_DIMENSIONS — strips that one
    payload key entirely before sending — "what if this filter didn't
    exist" (see _detect_narrow_filters()).

    Returns {"total_entries": int | None, "sample_people": list[dict]}.
    Missing key or any request failure -> {"total_entries": None,
    "sample_people": []}, matching every other adapter's graceful-
    degradation contract.
    """
    if not pl.APOLLO_API_KEY:
        return {"total_entries": None, "sample_people": []}

    headers = {"Content-Type": "application/json", "X-Api-Key": pl.APOLLO_API_KEY}

    person_titles = pl._bi_all_titles(icp)
    exclusions = pl._bi_negative_keywords(icp)
    keyword_tags = list(pl._build_apollo_keyword_tags(icp))
    if extra_keyword_tags:
        keyword_tags = pl._dedupe_list(keyword_tags + list(extra_keyword_tags))
    size_min, size_max = pl._bi_size_range(icp)

    payload = {
        "page": 1,
        "per_page": min(per_page, 100),   # live-tested: api_search 422s above 100
        "person_titles": person_titles,
        "organization_num_employees_ranges": pl._build_apollo_size_range(size_min, size_max),
        "person_locations": pl._clean_apollo_locations(pl._bi_all_locations(icp)),
        "q_organization_keyword_tags": keyword_tags,
    }
    if exclusions:
        payload["q_organization_not_search_list"] = exclusions

    if omit_dimension == "technology":
        payload.pop("q_organization_keyword_tags", None)
    elif omit_dimension == "size":
        payload.pop("organization_num_employees_ranges", None)
    elif omit_dimension == "location":
        payload.pop("person_locations", None)
    elif omit_dimension == "exclusions":
        payload.pop("q_organization_not_search_list", None)

    payload = {k: v for k, v in payload.items() if v not in [[], None, ""]}

    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            json=payload, headers=headers, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        pl.log.warning("Apollo coverage probe failed: %s", e)
        return {"total_entries": None, "sample_people": []}

    people = data.get("people", []) or data.get("contacts", [])
    sample = [
        {"organization": {"name": p.get("organization", {}).get("name")}}
        for p in people if p.get("organization", {}).get("name")
    ]
    return {"total_entries": data.get("total_entries"), "sample_people": sample}


def _estimate_company_count(sample_people: list[dict], total_entries: Optional[int]) -> int:
    """Apollo has no free company-count endpoint (organization search costs
    real paid credits — live-tested, 422 insufficient credits). Derives a
    real (if approximate) company estimate instead: the free people-search
    preview includes real, unmasked organization names, so the distinct-name
    ratio in a sample extrapolates to the full total_entries."""
    if not sample_people or not total_entries:
        return 0
    distinct = len({p["organization"]["name"] for p in sample_people if p.get("organization", {}).get("name")})
    ratio = distinct / len(sample_people)
    return round(total_entries * ratio)


def _coverage_rating(estimated_people: int, target: int) -> int:
    """1-5 stars, tied to something concretely actionable — will this ICP
    likely be able to find `target` verified leads — not an arbitrary
    quality score."""
    target = max(target, 1)
    if estimated_people >= target * 50:
        return 5
    if estimated_people >= target * 20:
        return 4
    if estimated_people >= target * 5:
        return 3
    if estimated_people >= target:
        return 2
    return 1


def _detect_narrow_filters(icp: dict, baseline_total: int) -> list[dict]:
    """For each of a fixed, bounded set of filter dimensions, probes what
    the match count would be WITHOUT that filter and flags it only if
    removing it would roughly triple (or more) the result — a real,
    data-backed narrowness signal, not a guess."""
    if not baseline_total:
        return []

    dimension_labels = {
        "technology": "Confirmed/likely technology filter",
        "size": "Company size filter",
        "location": "Location filter",
        "exclusions": "Exclusion filter",
    }
    flags = []
    for dim in _COVERAGE_NARROW_DIMENSIONS:
        probe = pl._apollo_search_probe(icp, per_page=1, omit_dimension=dim)
        without_total = probe["total_entries"]
        if not without_total or without_total <= baseline_total:
            continue
        ratio = without_total / max(baseline_total, 1)
        if ratio >= _COVERAGE_NARROW_THRESHOLD:
            flags.append({
                "dimension": dim,
                "label": dimension_labels[dim],
                "current_estimate": baseline_total,
                "without_estimate": without_total,
                "pct_reduction": round((1 - baseline_total / without_total) * 100),
            })
    return flags


def _generate_coverage_suggestions(icp: dict, baseline_total: int) -> list[dict]:
    """Candidates come from ICP fields Gemini already generates but that no
    adapter reads today (technology_intelligence.competing_products,
    industry_intelligence.adjacent_industries) — no new AI call needed,
    Gemini already produced good candidates. field_to_apply tells the
    caller which BI field to write an accepted suggestion into so it
    actually affects a real run — pl._bi_keyword_pool() reads
    likely_technologies/industry_keywords, but never competing_products/
    adjacent_industries directly, so leaving an accepted term in its
    original field would silently do nothing."""
    candidates = []
    for value in pl._bi_competing_products(icp)[:_COVERAGE_MAX_SUGGESTIONS_PER_TYPE]:
        candidates.append(("competing_product", value, "likely_technologies"))
    for value in pl._bi_adjacent_industries(icp)[:_COVERAGE_MAX_SUGGESTIONS_PER_TYPE]:
        candidates.append(("adjacent_industry", value, "industry_keywords"))

    suggestions = []
    for kind, value, field_to_apply in candidates:
        probe = pl._apollo_search_probe(icp, per_page=1, extra_keyword_tags=[value])
        with_total = probe["total_entries"]
        if not with_total or with_total <= baseline_total:
            continue
        suggestions.append({
            "type": kind,
            "value": value,
            "field_to_apply": field_to_apply,
            "estimated_reach_delta": with_total - baseline_total,
        })
    return suggestions


def _coverage_cache_key(icp: dict) -> str:
    """Plain deterministic composite string, not hashed — matches this
    codebase's established cache-key convention (see the claim-verification
    cache) over introducing hashlib for a new cache."""
    titles = "|".join(sorted(pl._bi_all_titles(icp)))
    locations = "|".join(sorted(pl._bi_all_locations(icp)))
    tags = "|".join(sorted(pl._build_apollo_keyword_tags(icp)))
    size_min, size_max = pl._bi_size_range(icp)
    exclusions = "|".join(sorted(pl._bi_negative_keywords(icp)))
    return f"{titles}::{locations}::{tags}::{size_min}-{size_max}::{exclusions}"


def _coverage_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_COVERAGE_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_COVERAGE_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS estimates (cache_key TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_coverage_estimate(cache_key: str) -> Optional[dict]:
    try:
        conn = _coverage_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM estimates WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_COVERAGE_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Coverage estimate cache read failed: %s", e)
        return None


def _cache_coverage_estimate(cache_key: str, data: dict) -> None:
    try:
        conn = _coverage_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO estimates (cache_key, response_json, fetched_at) VALUES (?, ?, ?)",
                (cache_key, json.dumps(data), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Coverage estimate cache write failed: %s", e)


def estimate_coverage(icp: dict, target: int = 25) -> dict:
    """
    Single entry point for Coverage Analysis. Returns a real, Apollo-backed
    estimate — never a fabricated number. If pl.APOLLO_API_KEY isn't
    configured, returns {"available": False, "reason": ...} rather than any
    placeholder figures.
    """
    if not pl.APOLLO_API_KEY:
        return {"available": False, "reason": "Apollo API key not configured"}

    cache_key = _coverage_cache_key(icp)
    cached = _get_cached_coverage_estimate(cache_key)
    if cached is not None:
        return cached

    pl.log.info("Coverage Analysis — probing Apollo for a real reach estimate …")
    baseline = pl._apollo_search_probe(icp, per_page=100)
    baseline_total = baseline["total_entries"]
    if not baseline_total:
        result = {"available": False, "reason": "Apollo search probe failed or returned no data"}
        _cache_coverage_estimate(cache_key, result)
        return result

    estimated_people = baseline_total
    estimated_companies = _estimate_company_count(baseline["sample_people"], baseline_total)
    narrow_flags = _detect_narrow_filters(icp, baseline_total)
    suggestions = _generate_coverage_suggestions(icp, baseline_total)

    result = {
        "available": True,
        "estimated_people": estimated_people,
        "estimated_companies": estimated_companies,
        "coverage_rating": _coverage_rating(estimated_people, target),
        "narrow_flags": narrow_flags,
        "suggestions": suggestions,
        "generated_at": datetime.now().isoformat(),
        "source": "apollo",
    }
    _cache_coverage_estimate(cache_key, result)
    pl.log.info(
        "Coverage Analysis — ~%d people / ~%d companies estimated, %d narrow flags, %d suggestions.",
        estimated_people, estimated_companies, len(narrow_flags), len(suggestions),
    )
    return result


# ─────────────────────────────────────────────
# Stage 3 — Dedupe & Clean
# ─────────────────────────────────────────────


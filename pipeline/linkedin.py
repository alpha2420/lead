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


_LINKEDIN_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "linkedin_cache.db")
_LINKEDIN_CACHE_TTL_DAYS = 30


def _linkedin_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_LINKEDIN_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_LINKEDIN_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profiles (url TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_linkedin_profile(linkedin_url: str) -> Optional[dict]:
    """Returns the cached Bright Data response if it's younger than the TTL, else None.
    LinkedIn lookups cost money per profile, so a repeat search for the same
    person within 30 days reuses the cached result instead of paying again."""
    try:
        conn = _linkedin_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM profiles WHERE url = ?", (linkedin_url,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_LINKEDIN_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("LinkedIn cache read failed for %s: %s", linkedin_url, e)
        return None


def _cache_linkedin_profile(linkedin_url: str, profile: dict) -> None:
    try:
        conn = _linkedin_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO profiles (url, response_json, fetched_at) VALUES (?, ?, ?)",
                (linkedin_url, json.dumps(profile), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("LinkedIn cache write failed for %s: %s", linkedin_url, e)


def _fuzzy_ratio(a: str, b: str) -> float:
    """0-100 similarity between two strings using stdlib difflib (no extra dependency).
    Case-insensitive, whitespace-trimmed. Empty inputs always score 0."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b).ratio() * 100, 1)


def scrape_linkedin_profile(linkedin_url: str) -> Optional[dict]:
    """
    Fetches a LinkedIn profile via Bright Data's Dataset API (synchronous
    endpoint — typically 10-30s per uncached profile). Cache-first.
    Returns None (not an exception) on any failure so callers can degrade
    gracefully instead of stopping the pipeline over one bad lookup.
    """
    if not linkedin_url:
        return None
    if not pl.LINKEDIN_API_KEY or not pl.LINKEDIN_API_URL:
        return None

    cached = _get_cached_linkedin_profile(linkedin_url)
    if cached is not None:
        return cached

    try:
        resp = requests.post(
            pl.LINKEDIN_API_URL,
            headers={"Authorization": f"Bearer {pl.LINKEDIN_API_KEY}", "Content-Type": "application/json"},
            json=[{"url": linkedin_url}],
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict) or data.get("error") or "error_code" in data:
            pl.log.warning("LinkedIn lookup returned no usable profile for %s", linkedin_url)
            return None
        _cache_linkedin_profile(linkedin_url, data)
        return data
    except Exception as e:
        pl.log.warning("LinkedIn lookup failed for %s: %s", linkedin_url, e)
        return None


def _parse_linkedin_location(city_str: str) -> dict:
    """Bright Data's 'city' field is really 'City, State, Country' (or just
    'City, Country' for profiles outside the US). Splits it into the
    city/state/country shape LeadFlow's lead records use."""
    parts = [p.strip() for p in (city_str or "").split(",") if p.strip()]
    if len(parts) >= 3:
        return {"city": parts[0], "state": parts[1], "country": parts[-1]}
    if len(parts) == 2:
        return {"city": parts[0], "state": "", "country": parts[1]}
    if len(parts) == 1:
        return {"city": "", "state": "", "country": parts[0]}
    return {"city": "", "state": "", "country": ""}


def linkedin_cross_verify(lead: dict) -> dict:
    """
    Compares a lead's scraped company/title against their live LinkedIn
    profile. Returns:
      signal              : NO_LINKEDIN | LOOKUP_FAILED | CURRENT | FORMER | UNCERTAIN
      company_match       : 0-100 fuzzy match, lead.company vs LinkedIn current employer
      title_match         : 0-100 fuzzy match, lead.title vs LinkedIn current title
      is_current_employee : True / False / None (None = can't tell either way)
      score                : 0-100 blended signal for composite scoring
      location             : {city, state, country} parsed from the LinkedIn
                              profile — None if no profile was fetched. Used
                              to backfill location fields the scraper missed,
                              since we're already paying for this lookup.

    FORMER means the lead's listed company shows up in their LinkedIn
    experience history with a past (non-"Present") end date — the classic
    "scraped data is stale, they left this job already" case.
    """
    linkedin_url = lead.get("linkedin_url", "")
    if not linkedin_url:
        return {"signal": "NO_LINKEDIN", "company_match": None, "title_match": None,
                "is_current_employee": None, "score": 50.0, "location": None}

    profile = scrape_linkedin_profile(linkedin_url)
    if profile is None:
        return {"signal": "LOOKUP_FAILED", "company_match": None, "title_match": None,
                "is_current_employee": None, "score": 50.0, "location": None}

    current_company = profile.get("current_company") or {}
    current_company_name = current_company.get("name", "")
    current_title = current_company.get("title") or profile.get("position", "")

    lead_company = lead.get("company", "")
    lead_title   = lead.get("title", "")

    company_match = _fuzzy_ratio(lead_company, current_company_name)
    title_match   = _fuzzy_ratio(lead_title, current_title)

    is_current_employee = None
    signal = "UNCERTAIN"
    if company_match >= 75:
        is_current_employee = True
        signal = "CURRENT"
    else:
        # Not their current employer per LinkedIn — check whether it shows up
        # as a PAST role (confirmed stale record) vs. just not on LinkedIn at all.
        for entry in profile.get("experience") or []:
            if not isinstance(entry, dict):
                continue
            # `.get(..., "Present")` only supplies the default when the key is
            # missing entirely — a present-but-falsy end_date (None/"", a real
            # scraper convention for an ongoing role) used to slip through as
            # not-equal-to-"Present" and get misread as a FORMER employer.
            if _fuzzy_ratio(lead_company, entry.get("company", "")) >= 75 and (entry.get("end_date") or "Present") != "Present":
                is_current_employee = False
                signal = "FORMER"
                break

    li_score = company_match * 0.7 + title_match * 0.3
    if is_current_employee is False:
        li_score *= 0.5   # confirmed stale record — heavy penalty, per design

    return {
        "signal": signal,
        "company_match": company_match,
        "title_match": title_match,
        "is_current_employee": is_current_employee,
        "score": round(li_score, 1),
        "location": _parse_linkedin_location(profile.get("city", "")),
    }


def _backfill_location(lead: dict, location: Optional[dict]) -> None:
    """Fills lead.city/state/country/location from a LinkedIn profile lookup
    — but only the fields the scraper left blank. Never overwrites data
    Apollo/Apify/the import already provided."""
    if not location:
        return
    if not lead.get("city") and location.get("city"):
        lead["city"] = location["city"]
    if not lead.get("state") and location.get("state"):
        lead["state"] = location["state"]
    if not lead.get("country") and location.get("country"):
        lead["country"] = location["country"]
    if not lead.get("location"):
        filled = ", ".join(p for p in [location.get("city"), location.get("state"), location.get("country")] if p)
        if filled:
            lead["location"] = filled


def linkedin_cross_verify_leads(leads: list[dict], on_progress=None, max_workers: int = 5) -> list[dict]:
    """Runs linkedin_cross_verify() over a batch, storing results on each lead
    as _linkedin_signal / _linkedin_company_match / _linkedin_title_match /
    _linkedin_current_employee / _linkedin_score, and backfilling city/state/
    country/location from the LinkedIn profile wherever the scraper left
    those blank (data we're already paying to fetch, so use all of it).
    Skips leads with no linkedin_url without spending an API call or a
    thread on them."""
    pl.log.info("Stage 4 — Cross-verifying %d leads against LinkedIn …", len(leads))

    to_check = [l for l in leads if l.get("linkedin_url")]
    skipped = [l for l in leads if not l.get("linkedin_url")]
    for lead in skipped:
        result = linkedin_cross_verify(lead)   # NO_LINKEDIN fast path, no network call
        lead["_linkedin_signal"] = result["signal"]
        lead["_linkedin_company_match"] = result["company_match"]
        lead["_linkedin_title_match"] = result["title_match"]
        lead["_linkedin_current_employee"] = result["is_current_employee"]
        lead["_linkedin_score"] = result["score"]

    done = len(skipped)
    total = len(leads)
    if on_progress:
        on_progress(done, total)

    if to_check:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_lead = {executor.submit(linkedin_cross_verify, lead): lead for lead in to_check}
            for future in future_to_lead:
                lead = future_to_lead[future]
                try:
                    result = future.result()
                except Exception as e:
                    pl.log.error("Unhandled error in LinkedIn cross-verify for %s: %s", lead.get("linkedin_url"), e)
                    result = {"signal": "LOOKUP_FAILED", "company_match": None, "title_match": None,
                               "is_current_employee": None, "score": 50.0, "location": None}
                lead["_linkedin_signal"] = result["signal"]
                lead["_linkedin_company_match"] = result["company_match"]
                lead["_linkedin_title_match"] = result["title_match"]
                lead["_linkedin_current_employee"] = result["is_current_employee"]
                lead["_linkedin_score"] = result["score"]
                _backfill_location(lead, result.get("location"))
                done += 1
                if on_progress:
                    on_progress(done, total)

    former_count = sum(1 for l in leads if l.get("_linkedin_signal") == "FORMER")
    if former_count:
        pl.log.warning("LinkedIn cross-verify: %d lead(s) confirmed FORMER employees — stale records.", former_count)
    return leads


# ─────────────────────────────────────────────
# Stage 5 — Domain Match
# ─────────────────────────────────────────────


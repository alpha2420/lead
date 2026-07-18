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


_COMPANY_DISCOVERY_MAX_QUERIES = 4
_RAW_COMPANIES_PER_TARGET_LEAD = 2
_MIN_COMPANY_DISCOVERY_FLOOR = 20
_MAX_COMPANY_DISCOVERY_CEILING = 300

_COMPANY_DISCOVERY_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "company_discovery_cache.db")
_COMPANY_DISCOVERY_CACHE_TTL_DAYS = 7   # a live search snapshot, not a stable fact —
                                          # same reasoning as the 3-day Coverage Analysis cache below.

_COMPANY_VALIDATION_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "company_validation_cache.db")
_COMPANY_VALIDATION_CACHE_TTL_DAYS = 30   # a company's existence/industry is a stable
                                            # fact, matching every other 30-day cache in this file.


def _company_discovery_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_COMPANY_DISCOVERY_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_COMPANY_DISCOVERY_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS discoveries (cache_key TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_company_discovery(cache_key: str) -> Optional[dict]:
    try:
        conn = _company_discovery_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM discoveries WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_COMPANY_DISCOVERY_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Company discovery cache read failed for %s: %s", cache_key, e)
        return None


def _cache_company_discovery(cache_key: str, result: dict) -> None:
    try:
        conn = _company_discovery_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO discoveries (cache_key, response_json, fetched_at) VALUES (?, ?, ?)",
                (cache_key, json.dumps(result), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Company discovery cache write failed for %s: %s", cache_key, e)


def _company_validation_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_COMPANY_VALIDATION_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_COMPANY_VALIDATION_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS validations (cache_key TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_company_validation(cache_key: str) -> Optional[dict]:
    try:
        conn = _company_validation_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM validations WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_COMPANY_VALIDATION_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Company validation cache read failed for %s: %s", cache_key, e)
        return None


def _cache_company_validation(cache_key: str, verdict: dict) -> None:
    try:
        conn = _company_validation_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO validations (cache_key, response_json, fetched_at) VALUES (?, ?, ?)",
                (cache_key, json.dumps(verdict), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Company validation cache write failed for %s: %s", cache_key, e)


def _company_discovery_cache_key(icp: dict, methods: tuple) -> str:
    """Deterministic composite string (not hashed) — same convention as
    pl._coverage_cache_key() below — built from every firmographic dimension
    that actually changes what gets discovered."""
    industry = pl._bi_industry(icp)
    company = pl._bi_company(icp)
    industry_terms = "|".join(sorted(pl._dedupe_list(
        pl._safe_list(industry.get("sub_industries")) + pl._safe_list(industry.get("primary_industry"))
    )))
    locations = "|".join(sorted(pl._bi_all_locations(icp)))
    size_min, size_max = pl._bi_size_range(icp)
    company_type = "|".join(sorted(pl._safe_list(company.get("company_type"))))
    exclusions = "|".join(sorted(pl._bi_negative_keywords(icp)))
    return f"{industry_terms}::{locations}::{size_min}-{size_max}::{company_type}::{exclusions}::{'|'.join(sorted(methods))}"


def _dedupe_companies(companies: list[dict]) -> list[dict]:
    """Order-preserving dedupe: primary key is pl.normalize_domain() when a
    company has one; falls back to a >=92 fuzzy-name match (pl._fuzzy_ratio)
    against already-kept companies when it doesn't. Merges source_methods
    on a collision so downstream logic can see "found by both the Apollo
    preview and web search" as a mild extra-confidence signal, and backfills
    a domain onto a name-only entry if a later duplicate happens to have one."""
    kept: list[dict] = []
    domain_index: dict = {}

    for company in companies:
        domain = pl.normalize_domain(company.get("domain") or "")

        if domain and domain in domain_index:
            existing = kept[domain_index[domain]]
            existing["source_methods"] = pl._dedupe_list(existing.get("source_methods", []) + company.get("source_methods", []))
            continue

        fuzzy_match_index = None
        if not domain:
            for i, existing in enumerate(kept):
                if pl._fuzzy_ratio(existing.get("name", ""), company.get("name", "")) >= 92:
                    fuzzy_match_index = i
                    break
        if fuzzy_match_index is not None:
            existing = kept[fuzzy_match_index]
            existing["source_methods"] = pl._dedupe_list(existing.get("source_methods", []) + company.get("source_methods", []))
            if not existing.get("domain") and company.get("domain"):
                existing["domain"] = company["domain"]
            continue

        kept.append(dict(company))
        if domain:
            domain_index[domain] = len(kept) - 1

    return kept


def _discover_companies_via_apollo_company_search(icp: dict, target_companies: int) -> tuple:
    """Forward-compat slot for Apollo's real company-search endpoint
    (mixed_companies/search) — live-tested during design and confirmed the
    endpoint exists and is reachable, but currently returns 422
    "insufficient credits" on this account's plan. Never raises: on any
    credit/plan error (402/403/422), logs once and returns ([], False) so
    discover_companies() falls back to the other two methods. The moment
    the account's plan changes, this starts returning real matches with
    zero code changes needed — though the exact payload shape for a
    *successful* call is unverified beyond the endpoint path/failure mode
    (can't be tested further without paid credits), so it may need
    adjustment once actually reachable."""
    if not pl.APOLLO_API_KEY:
        return [], False

    headers = {"Content-Type": "application/json", "X-Api-Key": pl.APOLLO_API_KEY}
    size_min, size_max = pl._bi_size_range(icp)
    exclusions = pl._bi_negative_keywords(icp)

    payload = {
        "page": 1,
        "per_page": min(target_companies, 100),
        "organization_num_employees_ranges": pl._build_apollo_size_range(size_min, size_max),
        "organization_locations": pl._clean_apollo_locations(pl._bi_all_locations(icp)),
        "q_organization_keyword_tags": pl._build_apollo_keyword_tags(icp),
    }
    if exclusions:
        payload["q_organization_not_search_list"] = exclusions
    payload = {k: v for k, v in payload.items() if v not in [[], None, ""]}

    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_companies/search",
            json=payload, headers=headers, timeout=30,
        )
        if resp.status_code in (402, 403, 422):
            pl.log.info(
                "Apollo company search unavailable on current plan (HTTP %d) — using fallback discovery methods.",
                resp.status_code,
            )
            return [], False
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        pl.log.warning("Apollo company search failed: %s", e)
        return [], False

    orgs = data.get("organizations") or data.get("accounts") or []
    companies = [
        {
            "name": org.get("name"),
            "domain": pl.normalize_domain(org.get("primary_domain") or org.get("website_url") or "") or None,
            "source_methods": ["apollo_company_search"],
            "raw": org,
        }
        for org in orgs if org.get("name")
    ]
    return companies, True


def _discover_companies_via_apollo_people_preview(icp: dict, target_companies: int) -> list[dict]:
    """Free method — reuses pl._apollo_search_probe()'s exact payload-building
    below (the same zero-paid-credit api_search call Coverage Analysis
    already relies on) but with person_titles omitted, so results surface
    companies rather than specific people. Live-tested during design: the
    preview's organization object reliably includes a real, unmasked
    company name (only person-level fields like the last name are
    obfuscated) but never a domain/website field — that only appears after
    per-person paid enrichment. So this method's output is name-only;
    Company Validation Tier B is what backfills a domain when possible."""
    if not pl.APOLLO_API_KEY:
        return []

    headers = {"Content-Type": "application/json", "X-Api-Key": pl.APOLLO_API_KEY}
    size_min, size_max = pl._bi_size_range(icp)
    exclusions = pl._bi_negative_keywords(icp)
    keyword_tags = pl._build_apollo_keyword_tags(icp)

    companies: list[dict] = []
    seen_names = set()
    pages = min(math.ceil(target_companies / 100), 3)   # per_page=100 is the live-tested ceiling (pl._apollo_search_probe)

    for page in range(1, pages + 1):
        payload = {
            "page": page,
            "per_page": 100,
            "organization_num_employees_ranges": pl._build_apollo_size_range(size_min, size_max),
            "person_locations": pl._clean_apollo_locations(pl._bi_all_locations(icp)),
            "q_organization_keyword_tags": keyword_tags,
        }
        if exclusions:
            payload["q_organization_not_search_list"] = exclusions
        payload = {k: v for k, v in payload.items() if v not in [[], None, ""]}

        try:
            resp = requests.post(
                "https://api.apollo.io/api/v1/mixed_people/api_search",
                json=payload, headers=headers, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            pl.log.warning("Apollo company-discovery preview failed on page %d: %s", page, e)
            break

        people = data.get("people", []) or data.get("contacts", [])
        if not people:
            break
        for p in people:
            name = (p.get("organization") or {}).get("name")
            if name and name not in seen_names:
                seen_names.add(name)
                companies.append({"name": name, "domain": None, "source_methods": ["apollo_people_preview"], "raw": {}})
        if len(companies) >= target_companies:
            break

    return companies[:target_companies]


def _extract_companies_with_gemini(search_results: dict) -> list[dict]:
    """Given {query: [snippet dicts]} from _discover_companies_via_web_search(),
    asks Gemini to extract a candidate company list — same calling pattern
    as pl.parse_search_results_with_gemini(), same "guess the domain from
    context" instruction."""
    if not pl.GEMINI_API_KEY:
        return []

    prompt = """You are a B2B research analyst extracting a list of real companies from Google search results.

Given search results below (grouped by the query that produced them), extract every distinct real company mentioned that plausibly matches the search intent. For each company, provide:
1. name — the actual company name (clean, no marketing taglines)
2. domain — infer or guess the most likely corporate website domain (e.g. "acme.com"), or null if you can't reasonably guess one

Skip anything that isn't a real, identifiable company (directories, "top 10" listicle sites themselves, generic industry association pages).

Return a JSON array of objects with these exact keys:
[
  {"name": "...", "domain": "..." }
]

Search results:
""" + json.dumps(search_results, indent=2)

    try:
        client = genai.Client(api_key=pl.GEMINI_API_KEY)
        parsed = pl.generate_json_with_retry(prompt, client)
        if isinstance(parsed, list):
            return [
                {
                    "name": c.get("name"),
                    "domain": pl.normalize_domain(c.get("domain") or "") or None,
                    "source_methods": ["web_search"],
                    "raw": {},
                }
                for c in parsed if isinstance(c, dict) and c.get("name")
            ]
        return []
    except Exception as e:
        pl.log.error("Failed to extract companies with Gemini: %s", e)
        return []


def _discover_companies_via_web_search(icp: dict) -> list[dict]:
    """Reuses the exact Apify google-search-scraper + Gemini pattern already
    built for Stage 10 (originally 8) claim verification
    (pl._run_apify_claim_search / pl.verify_company_claims_with_gemini), but
    querying for companies matching the ICP's firmographics instead of
    verifying a claim about one already-known company."""
    api_token = os.getenv("APIFY_API_TOKEN")
    if not api_token:
        return []

    industry = pl._bi_industry(icp)
    company = pl._bi_company(icp)
    search_terms = pl._dedupe_list(
        pl._safe_list(industry.get("primary_industry"))
        + pl._safe_list(company.get("company_type"))
        + pl._safe_list(pl._bi_search(icp).get("product_keywords"))
    )[:_COMPANY_DISCOVERY_MAX_QUERIES]
    locations = pl._bi_all_locations(icp)[:2]

    if not search_terms:
        return []

    queries_list = []
    for term in search_terms:
        query = f"{term} companies"
        if locations:
            query += " " + " ".join(locations)
        queries_list.append(query)

    run_input = {
        "queries": "\n".join(queries_list),
        "maxPagesPerQuery": 1,
        "resultsPerPage": 10,
        "mobileResults": False,
    }
    headers = {"Content-Type": "application/json"}
    actor_path = "apify/google-search-scraper".replace("/", "~")
    run_url = f"https://api.apify.com/v2/acts/{actor_path}/runs?token={api_token}&waitForFinish=300"

    try:
        resp = requests.post(run_url, json=run_input, headers=headers, timeout=360)
        resp.raise_for_status()
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify company-discovery search failed: %s", e)
        return []
    if not dataset_id:
        return []

    dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={api_token}&format=json"
    try:
        resp = requests.get(dataset_url, timeout=60)
        resp.raise_for_status()
        items = resp.json()
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify company-discovery dataset fetch failed: %s", e)
        return []

    if len(items) != len(queries_list):
        # zip() below pairs queries<->result pages purely by position — the
        # actor doesn't echo back which query produced which page, so a
        # dropped/reordered page silently mismatches every query after it.
        # Can't safely re-correlate without a shared key, so at minimum make
        # the mismatch loud instead of silent.
        pl.log.warning(
            "Apify company-discovery: submitted %d queries but got %d result pages back — "
            "results past the shorter length will be dropped, remaining pairs may be misaligned.",
            len(queries_list), len(items),
        )

    results_by_query: dict = {}
    for query, page in zip(queries_list, items):
        organic_results = page.get("organicResults", []) if isinstance(page, dict) else (page if isinstance(page, list) else [])
        results_by_query[query] = [
            {"title": r.get("title", ""), "description": r.get("description", "")}
            for r in organic_results if isinstance(r, dict)
        ]

    return _extract_companies_with_gemini(results_by_query)


def discover_companies(
    icp: dict,
    target_companies: Optional[int] = None,
    target: int = 25,
    enable_apollo_preview: bool = True,
    enable_web_search: bool = True,
) -> dict:
    """
    Stage 2 — finds candidate companies matching the ICP's firmographics
    (industry, size, location, company type), combining every viable
    discovery method:
      1. The real Apollo company-search endpoint, if the account's plan
         supports it (auto-detected, gracefully skipped otherwise).
      2. A free Apollo people-search preview with titles omitted.
      3. A Gemini + web-search fallback.
    Results from whichever methods actually ran are merged and deduped.
    Cached 7 days per unique ICP firmographic signature.

    Returns {"companies": [{"name", "domain", "source_methods", "raw"}, ...],
    "counts": {...}, "apollo_company_search_available": bool}.
    """
    if target_companies is None:
        target_companies = max(
            min(target * _RAW_COMPANIES_PER_TARGET_LEAD, _MAX_COMPANY_DISCOVERY_CEILING),
            _MIN_COMPANY_DISCOVERY_FLOOR,
        )

    methods = ["apollo_company_search"]
    if enable_apollo_preview:
        methods.append("apollo_people_preview")
    if enable_web_search:
        methods.append("web_search")
    cache_key = _company_discovery_cache_key(icp, tuple(methods))

    cached = _get_cached_company_discovery(cache_key)
    if cached is not None:
        pl.log.info("Stage 2 — Company Discovery: using cached results (%d companies).", len(cached.get("companies", [])))
        return cached

    pl.log.info("Stage 2 — Company Discovery: searching for ~%d candidate companies …", target_companies)

    company_search_results, apollo_available = _discover_companies_via_apollo_company_search(icp, target_companies)
    preview_results = _discover_companies_via_apollo_people_preview(icp, target_companies) if enable_apollo_preview else []
    web_results = _discover_companies_via_web_search(icp) if enable_web_search else []

    counts = {
        "apollo_company_search": len(company_search_results),
        "apollo_people_preview": len(preview_results),
        "web_search": len(web_results),
    }
    merged = _dedupe_companies(company_search_results + preview_results + web_results)

    result = {"companies": merged, "counts": counts, "apollo_company_search_available": apollo_available}
    _cache_company_discovery(cache_key, result)

    pl.log.info(
        "Stage 2 — Company Discovery: %d unique companies found (apollo_search=%d, apollo_preview=%d, web_search=%d).",
        len(merged), counts["apollo_company_search"], counts["apollo_people_preview"], counts["web_search"],
    )
    return result


def _rule_validate_company(company: dict, icp: dict) -> tuple:
    """Tier A — free, instant, runs on every discovered candidate. The only
    two HARD gates: a malformed domain (format check only, no live DNS
    lookup — stays instant across up to _MAX_COMPANY_DISCOVERY_CEILING
    candidates) and the ICP's own exclusion list. A company with no domain
    at all (a real possibility for the people-preview discovery method)
    isn't auto-rejected for a field that method doesn't supply — it's
    validated on name alone here, with ICP-fit used only for ranking
    (validate_companies()), not as a hard gate. Returns (passed, reason)."""
    domain = company.get("domain")
    if domain:
        normalized = pl.normalize_domain(domain)
        if not normalized or "." not in normalized or " " in normalized:
            return False, "malformed_domain"

    name = company.get("name") or ""
    text = f"{name} {domain or ''}".lower()
    for keyword in pl._bi_negative_keywords(icp):
        if keyword and keyword.lower() in text:
            return False, f"excluded_keyword:{keyword}"

    return True, ""


def _extract_company_validation_criteria(icp: dict) -> dict:
    """Parallel to pl._extract_verifiable_claims() (below) but broader —
    company validation checks existence/industry/location/type fit, not
    just the 2 narrow claim types (technology, industry) claim verification
    checks against an already-known lead's employer."""
    criteria: dict = {}
    industry = pl._bi_industry(icp)
    company = pl._bi_company(icp)

    if industry.get("primary_industry"):
        criteria["industry"] = str(industry["primary_industry"]).strip()

    locations = pl._bi_all_locations(icp)
    if locations:
        criteria["location"] = ", ".join(locations[:3])

    company_type = pl._safe_list(company.get("company_type"))
    if company_type:
        criteria["company_type"] = ", ".join(company_type)

    confirmed_tech = dict(pl._bi_technology_precedence_tiers(icp))["confirmed"]
    if confirmed_tech:
        criteria["technology"] = pl._bi_primary_technology(icp)

    return criteria


def _run_apify_company_validation_search(companies: list[str], criteria: dict) -> dict:
    """Same POST-then-GET mechanics as pl._run_apify_claim_search() below — one
    Apify google-search-scraper run covering every company in one batch,
    regardless of count, so cost scales with query volume inside that one
    run, not with call count."""
    api_token = os.getenv("APIFY_API_TOKEN")
    if not api_token or not companies:
        return {}

    industry = criteria.get("industry", "")
    location = criteria.get("location", "")

    queries_list = []
    for company in companies:
        term = f'"{company}"'
        if industry:
            term += f" {industry}"
        if location:
            term += f" {location}"
        queries_list.append(term)

    run_input = {
        "queries": "\n".join(queries_list),
        "maxPagesPerQuery": 1,
        "resultsPerPage": 5,
        "mobileResults": False,
    }
    headers = {"Content-Type": "application/json"}
    actor_path = "apify/google-search-scraper".replace("/", "~")
    run_url = f"https://api.apify.com/v2/acts/{actor_path}/runs?token={api_token}&waitForFinish=300"

    try:
        resp = requests.post(run_url, json=run_input, headers=headers, timeout=360)
        resp.raise_for_status()
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify company-validation search failed: %s", e)
        return {}
    if not dataset_id:
        return {}

    dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={api_token}&format=json"
    try:
        resp = requests.get(dataset_url, timeout=60)
        resp.raise_for_status()
        items = resp.json()
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify company-validation dataset fetch failed: %s", e)
        return {}

    if len(items) != len(companies):
        pl.log.warning(
            "Apify company-validation: submitted %d company queries but got %d result pages back — "
            "results past the shorter length will be dropped, remaining pairs may be misaligned.",
            len(companies), len(items),
        )

    results: dict = {}
    for company, page in zip(companies, items):
        organic_results = page.get("organicResults", []) if isinstance(page, dict) else (page if isinstance(page, list) else [])
        results[company] = [
            {"title": r.get("title", ""), "description": r.get("description", "")}
            for r in organic_results if isinstance(r, dict)
        ]
    return results


def validate_companies_with_gemini(companies_with_snippets: dict, criteria: dict) -> dict:
    """Tier B judgment — same calling pattern as
    pl.verify_company_claims_with_gemini() below, reusing the identical
    CONFIRMED/CONTRADICTED/UNCLEAR verdict vocabulary this codebase already
    uses for search-evidence judgments, plus an optional domain extracted
    from the snippets (backfills the domain gap left by the free-preview
    discovery method)."""
    if not pl.GEMINI_API_KEY:
        return {}

    criteria_lines = []
    if criteria.get("industry"):
        criteria_lines.append(f'- industry: does the company appear to operate in "{criteria["industry"]}"?')
    if criteria.get("location"):
        criteria_lines.append(f'- location: is the company based in/near "{criteria["location"]}"?')
    if criteria.get("company_type"):
        criteria_lines.append(f'- company_type: does the company match "{criteria["company_type"]}"?')
    if criteria.get("technology"):
        criteria_lines.append(f'- technology: does the company appear to use "{criteria["technology"]}"?')

    prompt = """You are a B2B research analyst validating whether companies are real and match a target profile, using web search snippets.

For each company below, you're given a small set of Google search result snippets (title + description). Based ONLY on this evidence, first confirm the company genuinely exists and is a real operating business, then assess:
""" + "\n".join(criteria_lines) + """

Give ONE overall verdict per company — exactly "CONFIRMED", "CONTRADICTED", or "UNCLEAR":
- CONFIRMED: the company clearly exists and the evidence supports it matching the profile
- CONTRADICTED: the snippets clearly show this isn't a real company, or it clearly doesn't match (e.g. evidently a completely different industry)
- UNCLEAR: not enough evidence either way

Also extract the company's website domain if it appears in the snippets (e.g. from a URL), else null.

Return a JSON object keyed by company name:
{
  "<company name>": {"verdict": "CONFIRMED|CONTRADICTED|UNCLEAR", "domain": "acme.com or null", "evidence": "one brief sentence"}
}

Companies and their search snippets:
""" + json.dumps(companies_with_snippets, indent=2)

    try:
        client = genai.Client(api_key=pl.GEMINI_API_KEY)
        parsed = pl.generate_json_with_retry(prompt, client)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except Exception as e:
        pl.log.error("Failed to validate companies with Gemini: %s", e)
        return {}


def validate_companies(
    companies: list[dict],
    icp: dict,
    max_ai_validate: Optional[int] = None,
    profile: str = pl._DEFAULT_PROFILE,
) -> dict:
    """
    Stage 3 — two-tier validation. Tier A (free, every candidate): domain
    well-formed + not on the ICP's exclusion list. Tier B (paid, capped):
    an AI fact-check via web search, run only on Tier A survivors, ranked
    by ICP fit and capped so cost stays a single bounded Apify+Gemini call
    regardless of how many candidates Discovery found. Companies beyond the
    cap stay validated (tagged rule_only) — never silently dropped just for
    being unaffordable to fact-check this run, matching this codebase's
    existing pattern of defaulting an absent/unaffordable signal to neutral
    inclusion rather than exclusion (e.g. pl.compute_composite_scores()'s
    missing-signal defaults, pl._backfill_org_fields()'s blanks-only rule).

    Returns {"validated": [...], "rejected": [...], "counts": {...}}.
    """
    if max_ai_validate is None:
        prof = pl._SOURCE_PROFILES.get(profile, pl._SOURCE_PROFILES[pl._DEFAULT_PROFILE])
        max_ai_validate = prof.get("max_ai_validate_companies", 40)

    icp_score = pl.build_icp_fit_scorer(icp)

    rule_passed: list[dict] = []
    rejected: list[dict] = []
    for company in companies:
        passed, reason = _rule_validate_company(company, icp)
        if passed:
            rule_passed.append(company)
        else:
            company["_validation_tier"] = "rule_rejected"
            company["_rejection_reason"] = reason
            rejected.append(company)

    rule_passed.sort(key=lambda c: icp_score({"company": c.get("name", "")}), reverse=True)

    to_ai_validate = rule_passed[:max_ai_validate]
    rule_only = rule_passed[max_ai_validate:]
    by_name = {c.get("name"): c for c in to_ai_validate if c.get("name")}

    criteria = _extract_company_validation_criteria(icp)
    criteria_signature = "::".join(f"{k}={v}" for k, v in sorted(criteria.items()))

    validated: list[dict] = []
    ai_confirmed = ai_unclear = ai_contradicted = 0

    if criteria and by_name:
        cache_keys = {
            name: f"{pl.normalize_domain(c.get('domain') or '') or name.lower()}::{criteria_signature}"
            for name, c in by_name.items()
        }
        verdicts: dict = {}
        names_needing_search = []
        for name, key in cache_keys.items():
            cached = _get_cached_company_validation(key)
            if cached is not None:
                verdicts[name] = cached
            else:
                names_needing_search.append(name)

        if names_needing_search:
            snippets = _run_apify_company_validation_search(names_needing_search, criteria)
            fresh = validate_companies_with_gemini(snippets, criteria) if snippets else {}
            for name in names_needing_search:
                verdict = fresh.get(name) or {"verdict": "UNCLEAR", "domain": None, "evidence": "No search evidence found."}
                _cache_company_validation(cache_keys[name], verdict)
                verdicts[name] = verdict

        for name, company in by_name.items():
            verdict = verdicts.get(name) or {"verdict": "UNCLEAR", "domain": None, "evidence": ""}
            result = verdict.get("verdict", "UNCLEAR")
            if not company.get("domain") and verdict.get("domain"):
                company["domain"] = pl.normalize_domain(verdict["domain"])
            if result == "CONTRADICTED":
                company["_validation_tier"] = "ai_contradicted"
                company["_rejection_reason"] = verdict.get("evidence", "")
                rejected.append(company)
                ai_contradicted += 1
            elif result == "CONFIRMED":
                company["_validation_tier"] = "ai_confirmed"
                validated.append(company)
                ai_confirmed += 1
            else:
                company["_validation_tier"] = "ai_unclear"
                validated.append(company)
                ai_unclear += 1
    else:
        for company in to_ai_validate:
            company["_validation_tier"] = "rule_only"
            validated.append(company)

    for company in rule_only:
        company["_validation_tier"] = "rule_only"
        validated.append(company)

    counts = {
        "rule_passed": len(rule_passed),
        "rule_rejected": len(companies) - len(rule_passed),
        "ai_validated": len(by_name) if criteria else 0,
        "ai_confirmed": ai_confirmed,
        "ai_contradicted": ai_contradicted,
        "ai_unclear": ai_unclear,
        "rule_only": len(validated) - ai_confirmed - ai_unclear,
    }

    pl.log.info(
        "Stage 3 — Company Validation: %d validated (%d rule-only, %d ai-confirmed, %d ai-unclear), %d rejected.",
        len(validated), counts["rule_only"], ai_confirmed, ai_unclear, len(rejected),
    )

    return {"validated": validated, "rejected": rejected, "counts": counts}


def _filter_leads_by_validated_companies(
    leads: list[dict], validated_companies: list[dict], min_fuzzy_score: float = 85.0
) -> list[dict]:
    """Post-filter applied by pl._call_source() when running company-first —
    keeps a lead only if its company matches a validated company, by domain
    when available (Apollo/Apify already set company_domain on every lead)
    or by fuzzy name match otherwise (Explorium leads never carry
    company_domain at all — confirmed, no such field is ever set in its
    mapper). This is a real precision/recall tradeoff for Explorium
    specifically: name-only matching can both miss real matches (legal name
    vs. DBA) and let unrelated same-named companies through. Lower-urgency
    since enable_explorium defaults off."""
    if not validated_companies:
        return leads

    valid_domains = {pl.normalize_domain(c["domain"]) for c in validated_companies if c.get("domain")}
    valid_names = [c["name"] for c in validated_companies if c.get("name")]

    kept = []
    for lead in leads:
        domain = pl.normalize_domain(lead.get("company_domain") or "")
        if domain and domain in valid_domains:
            kept.append(lead)
            continue
        company_name = lead.get("company") or ""
        if company_name and any(pl._fuzzy_ratio(company_name, vn) >= min_fuzzy_score for vn in valid_names):
            kept.append(lead)
    return kept


def discover_and_validate_companies(
    icp: dict,
    target: int = 25,
    profile: str = pl._DEFAULT_PROFILE,
    max_ai_validate_companies: Optional[int] = None,
) -> dict:
    """
    Thin composition wrapper: Company Discovery (Stage 2) then Company
    Validation (Stage 3). Single call site from app.py so the Flask
    orchestration layer doesn't need to manage intermediate state between
    the two stages itself. Returns {"discovery": {...}, "validation": {...}}
    — app.py reads validation["validated"] to pass into pl.run_lead_sources()
    as validated_companies for company-scoped People Discovery.
    """
    discovery = discover_companies(icp, target=target)
    validation = validate_companies(
        discovery["companies"], icp,
        max_ai_validate=max_ai_validate_companies, profile=profile,
    )
    return {"discovery": discovery, "validation": validation}


# ─────────────────────────────────────────────
# Coverage Analysis — pre-run reach estimation
# ─────────────────────────────────────────────
# Answers "how many companies/people would this ICP likely match?" before a
# user commits to a real (paid-enrichment) run. Apollo-only: live-tested
# during design and confirmed api_search costs zero paid credits (only a
# separate, generous rate limit — 50/min, 200/hour) even on an account whose
# enrichment credits are fully exhausted, and its free preview already
# includes real total_entries plus real (unmasked) organization names. Apify
# has no equivalent free pre-count tier and Explorium is unconfirmed, so
# this never claims a number for either — see pl.estimate_coverage()'s
# "available" flag.


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


_CLAIM_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "claim_verification_cache.db")
_CLAIM_CACHE_TTL_DAYS = 30
_CLAIM_VERDICT_SCORES = {"CONFIRMED": 90.0, "UNCLEAR": 50.0, "CONTRADICTED": 10.0}


def _claim_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_CLAIM_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_CLAIM_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS claims (cache_key TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_claim_verification(cache_key: str) -> Optional[dict]:
    """Returns the cached verdict if younger than the TTL, else None. Keyed
    by company + the exact claims being checked, so a change in the ICP's
    claims for the same company correctly misses cache."""
    try:
        conn = _claim_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM claims WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_CLAIM_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Claim verification cache read failed for %s: %s", cache_key, e)
        return None


def _cache_claim_verification(cache_key: str, data: dict) -> None:
    try:
        conn = _claim_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO claims (cache_key, response_json, fetched_at) VALUES (?, ?, ?)",
                (cache_key, json.dumps(data), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Claim verification cache write failed for %s: %s", cache_key, e)


def _extract_verifiable_claims(icp: dict) -> dict:
    """Dynamically determines which claims about this ICP are concrete
    enough to verify via a web search, and aren't already covered by an
    existing signal (job title/employer -> LinkedIn Stage 4; email domain ->
    Stage 5). Returns only the keys that apply — an ICP with no distinctive
    technology and no stated industry returns {}, making the rest of this
    stage a no-op for that run.

    technology: only confirmed_technologies (a specific named product the
    customer explicitly stated, e.g. "Duck Creek Technologies") counts as a
    checkable claim — likely_technologies is Gemini's own inference, not
    something to go verify against evidence, and a generic crm/erp/cloud
    tool is used by thousands of companies across every industry and isn't
    worth spending a search on. This is a stronger, more precise signal than
    the old technographics.other grab-first-item heuristic.
    industry: industry_intelligence.primary_industry, whenever the ICP states one.

    Out of scope for now: company size, geography, funding/company-stage,
    job title — job title is already verified via LinkedIn; the rest need
    real firmographic data (Apollo/Explorium), not web-search guessing."""
    claims: dict = {}

    confirmed = dict(pl._bi_technology_precedence_tiers(icp))["confirmed"]
    if confirmed:
        # Reuse pl._bi_primary_technology()'s cleanup (not just confirmed[0]
        # raw) so this stage checks the exact same term the search adapters
        # already search for — otherwise a malformed compound string here
        # (e.g. "Duck Creek Technologies (Policy, Billing, Claims, Rating)")
        # also breaks the vendor-detection substring check below.
        claims["technology"] = pl._bi_primary_technology(icp)

    primary_industry = pl._bi_industry(icp).get("primary_industry")
    if primary_industry:
        claims["industry"] = str(primary_industry).strip()

    return claims


def _run_apify_claim_search(companies: list[str], claims: dict, company_domains: dict | None = None) -> dict:
    """Runs one Apify google-search-scraper call, returning {company:
    [snippet dicts]}. Mirrors pl.scrape_apify()'s POST-then-GET mechanics
    exactly, but with far fewer results per query since we only need a few
    snippets to judge a claim, not exhaustive leads. Never sends anything
    but the company name and the ICP's own claim values — no raw user
    inquiry text.

    Every company gets its normal unscoped query (paired with the
    technology claim if present, else the industry claim). When a
    company's domain is also known (company_domains), a SECOND query
    scoped with site:{domain} is added — the "Website" verification leg:
    evidence from the company's own site, additive on top of the broader
    web search rather than replacing it (a company's homepage often
    doesn't literally restate a fact a press mention or review site would
    — narrowing to site: only would lose that). Both queries' snippets
    fold into the same evidence pool for that company and the same Gemini
    claim-check call below — no new verdict type, no new module. This
    roughly doubles query volume only for companies with a known domain;
    this stage already runs on the final ~20-25 lead sample only, so the
    added cost is small and bounded."""
    api_token = os.getenv("APIFY_API_TOKEN")
    if not api_token or not companies:
        return {}

    company_domains = company_domains or {}
    tech = claims.get("technology")
    industry = claims.get("industry")

    def base_term(company: str) -> str:
        term = f'"{company}"'
        if tech:
            term += f' "{tech}"'
        elif industry:
            term += f' {industry}'
        return term

    # query_owners is parallel to queries_list — which company each line's
    # results belong to, since a company may contribute one or two lines.
    queries_list: list[str] = []
    query_owners: list[str] = []
    for company in companies:
        term = base_term(company)
        queries_list.append(term)
        query_owners.append(company)
        domain = company_domains.get(company)
        if domain:
            queries_list.append(f"{term} site:{domain}")
            query_owners.append(company)

    run_input = {
        "queries": "\n".join(queries_list),
        "maxPagesPerQuery": 1,
        "resultsPerPage": 5,
        "mobileResults": False,
    }

    headers = {"Content-Type": "application/json"}
    # Deliberately NOT reading pl.APIFY_ACTOR_ID here — this function always
    # needs a general-purpose Google-search actor regardless of which actor
    # is configured for main lead discovery (Stage 2). Live-confirmed this
    # matters: once pl.APIFY_ACTOR_ID was switched to crawlerbros/lead-finder,
    # this code (still building a "queries" payload) started sending that
    # structured-filter actor a shape it doesn't understand and got a 400.
    actor_id = "apify/google-search-scraper"
    actor_path = actor_id.replace("/", "~")
    run_url = (
        f"https://api.apify.com/v2/acts/{actor_path}/runs"
        f"?token={api_token}&waitForFinish=300"
    )

    try:
        resp = requests.post(run_url, json=run_input, headers=headers, timeout=360)
        resp.raise_for_status()
        dataset_id = resp.json().get("data", {}).get("defaultDatasetId")
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify claim-verification search failed: %s", e)
        return {}

    if not dataset_id:
        return {}

    dataset_url = (
        f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        f"?token={api_token}&format=json"
    )
    try:
        resp = requests.get(dataset_url, timeout=60)
        resp.raise_for_status()
        items = resp.json()
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify claim-verification dataset fetch failed: %s", e)
        return {}

    if len(items) != len(queries_list):
        pl.log.warning(
            "Apify claim-verification: submitted %d queries but got %d result pages back — "
            "results past the shorter length will be dropped, remaining pairs may be misaligned.",
            len(queries_list), len(items),
        )

    results: dict = {}
    for owner, page in zip(query_owners, items):
        if isinstance(page, dict):
            organic_results = page.get("organicResults", [])
        elif isinstance(page, list):
            organic_results = page
        else:
            organic_results = []
        results.setdefault(owner, []).extend(
            {"title": r.get("title", ""), "description": r.get("description", "")}
            for r in organic_results if isinstance(r, dict)
        )

    return results


def verify_company_claims_with_gemini(companies_with_snippets: dict, claims: dict) -> dict:
    """Given {company: [snippet dicts]} and the claims being checked, asks
    Gemini to assess, per company, whether the search evidence CONFIRMS,
    CONTRADICTS, or leaves UNCLEAR each applicable claim — one prompt
    covering every company at once, mirroring pl.parse_search_results_with_gemini()'s
    exact pattern (single batched call, not one call per company)."""
    if not pl.GEMINI_API_KEY:
        pl.log.warning("GEMINI_API_KEY is not set. Skipping claim verification.")
        return {}

    claim_lines = []
    if claims.get("technology"):
        claim_lines.append(f'- technology: does the company appear to use "{claims["technology"]}"?')
    if claims.get("industry"):
        claim_lines.append(f'- industry: does the company appear to operate in "{claims["industry"]}"?')

    prompt = """You are a B2B research analyst verifying claims about companies using web search snippets.

For each company below, you're given a small set of Google search result snippets (title + description). Based ONLY on this evidence, assess each of these claims:
""" + "\n".join(claim_lines) + """

For each claim, respond with a verdict of exactly "CONFIRMED", "CONTRADICTED", or "UNCLEAR":
- CONFIRMED: the snippets clearly support the claim
- CONTRADICTED: the snippets clearly show the company does NOT match the claim (e.g. it's evidently in a completely different industry)
- UNCLEAR: the snippets don't contain enough information either way

Return a JSON object keyed by company name, each value having the applicable claim verdict(s) plus one brief evidence sentence. Omit a claim key entirely if it wasn't in the list above:
{
  "<company name>": {"technology_verdict": "CONFIRMED|CONTRADICTED|UNCLEAR", "industry_verdict": "CONFIRMED|CONTRADICTED|UNCLEAR", "evidence": "..."}
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
        pl.log.error("Failed to verify claims with Gemini: %s", e)
        return {}


def _summarize_claim_verdict(verdict: dict, claims: dict) -> tuple:
    """Combines the per-claim-type verdicts Gemini returned into one overall
    (signal, score) for the lead. Any CONTRADICTED claim dominates — a
    company confirmed to be in the wrong industry is a bad lead regardless
    of what else checks out; otherwise CONFIRMED beats UNCLEAR."""
    verdicts = [
        verdict.get(f"{claim_type}_verdict")
        for claim_type in claims
        if verdict.get(f"{claim_type}_verdict") in _CLAIM_VERDICT_SCORES
    ]
    if not verdicts:
        return "UNCLEAR", 50.0
    if "CONTRADICTED" in verdicts:
        return "CONTRADICTED", _CLAIM_VERDICT_SCORES["CONTRADICTED"]
    if "CONFIRMED" in verdicts:
        return "CONFIRMED", _CLAIM_VERDICT_SCORES["CONFIRMED"]
    return "UNCLEAR", _CLAIM_VERDICT_SCORES["UNCLEAR"]


def verify_claims_for_leads(leads: list[dict], icp: dict, on_progress=None) -> list[dict]:
    """Stage 8 — dynamically verifies whatever concrete claims this ICP makes
    (named technology usage, industry fit) against a targeted web search per
    unique company. Attaches _claim_verification_signal (CONFIRMED |
    CONTRADICTED | UNCLEAR) and _claim_verification_score (0-100) to every
    lead so pl.compute_composite_scores() can fold the evidence into ranking.
    A no-op (zero API calls) when the ICP has no extractable claims."""
    total = len(leads)
    claims = _extract_verifiable_claims(icp)

    if not claims:
        pl.log.info("Stage 8 — No verifiable claims in this ICP, skipping claim verification.")
        if on_progress:
            on_progress(total, total)
        return leads

    pl.log.info("Stage 8 — Verifying claims (%s) for %d leads …", ", ".join(claims.keys()), total)

    # Group by company so one search/verdict serves every lead sharing an
    # employer, instead of one lookup per lead.
    leads_by_company: dict = {}
    leads_without_company: list = []
    for lead in leads:
        company = (lead.get("company") or "").strip()
        if company:
            leads_by_company.setdefault(company, []).append(lead)
        else:
            leads_without_company.append(lead)

    done = 0
    if on_progress:
        on_progress(done, total)

    claim_signature = "::".join(f"{k}={v}" for k, v in sorted(claims.items()))

    company_verdicts: dict = {}
    companies_needing_search = []
    for company in leads_by_company:
        cache_key = f"{company.lower()}::{claim_signature}"
        cached = pl._get_cached_claim_verification(cache_key)
        if cached is not None:
            company_verdicts[company] = cached
        else:
            companies_needing_search.append(company)

    if companies_needing_search:
        # First non-empty company_domain among a company's leads, if any —
        # feeds the additive site:{domain} query leg in _run_apify_claim_search.
        company_domains = {
            company: next((l.get("company_domain") for l in company_leads if l.get("company_domain")), None)
            for company, company_leads in leads_by_company.items()
            if company in companies_needing_search
        }
        snippets_by_company = _run_apify_claim_search(companies_needing_search, claims, company_domains)
        verdicts = verify_company_claims_with_gemini(snippets_by_company, claims) if snippets_by_company else {}
        for company in companies_needing_search:
            verdict = verdicts.get(company) or {"evidence": "No search evidence found."}
            cache_key = f"{company.lower()}::{claim_signature}"
            pl._cache_claim_verification(cache_key, verdict)
            company_verdicts[company] = verdict

    tech_claim = claims.get("technology") or ""

    for company, company_leads in leads_by_company.items():
        verdict = company_verdicts.get(company, {})
        signal, score = _summarize_claim_verdict(verdict, claims)
        # A company whose own name overlaps with the claimed technology
        # (e.g. "Duck Creek Technologies" OR just "Duck Creek" itself
        # showing up as a lead for a "companies using Duck Creek" search)
        # is the vendor, not a customer implementing it — live-tested: this
        # is a common, otherwise-invisible false-positive class for
        # named-technology searches, since the vendor's own employees'
        # LinkedIn profiles trivially satisfy the "mentions this technology"
        # check. Checked both directions (company name may be a shortened
        # or expanded form of the claim — e.g. "Duck Creek" vs. "Duck Creek
        # Technologies") with a length floor on both sides to avoid a short,
        # generic word coincidentally matching.
        tech_lower, company_lower = tech_claim.lower(), company.lower()
        if (
            tech_claim and len(tech_claim) >= 4 and len(company) >= 4
            and (tech_lower in company_lower or company_lower in tech_lower)
        ):
            signal, score = "IS_VENDOR", 10.0
        for lead in company_leads:
            lead["_claim_verification_signal"] = signal
            lead["_claim_verification_score"] = score
            lead["_claim_verification_evidence"] = verdict.get("evidence", "")
        done += len(company_leads)
        if on_progress:
            on_progress(done, total)

    for lead in leads_without_company:
        lead["_claim_verification_signal"] = "UNCLEAR"
        lead["_claim_verification_score"] = 50.0
        lead["_claim_verification_evidence"] = ""
    done += len(leads_without_company)
    if on_progress:
        on_progress(done, total)

    contradicted = sum(1 for l in leads if l.get("_claim_verification_signal") == "CONTRADICTED")
    if contradicted:
        pl.log.warning("Claim verification: %d lead(s) CONTRADICTED (%s) — likely poor fit.", contradicted, ", ".join(claims.keys()))

    return leads


# ─────────────────────────────────────────────
# Stage 9 — Organization Enrichment
# ─────────────────────────────────────────────
# Runs only on the final selected sample (not the raw scraped pool) since
# Apollo credits are scarce — enriching ~20-25 leads instead of every raw
# scraped lead keeps cost proportionate. Fills biz_category/biz_description/
# technology/employee_count/market_cap for leads (mainly Apify-sourced) that
# don't already have them, via Apollo's Organization Enrichment endpoint.


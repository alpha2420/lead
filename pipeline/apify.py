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


def parse_search_results_with_gemini(organic_results: list[dict]) -> list[dict]:
    """
    Uses Gemini 2.5 Flash to parse raw Google organic results into structured B2B profiles,
    extracting precise names, job titles, companies, and guessing standard corporate email domains.
    """
    pl.log.info("Parsing %d search results with Gemini...", len(organic_results))
    
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        pl.log.warning("GEMINI_API_KEY is not set. Skipping Gemini parsing.")
        return []

    prompt = """You are an expert B2B data clean-up analyst.
Given a list of Google Search result items (each has title, url, description), extract the person's:
1. Full Name (clean and capitalized)
2. Job Title (clean and professional)
3. Company Name (the actual company they work for, e.g. "Chubb", "HTC Global Services", "Duck Creek Technologies", "Pharmacists Mutual")
4. Company Domain (e.g. "chubb.com", "htcglobal.com", "duckcreek.com", "phmic.com" - infer or guess the most likely corporate email domain for the company)
5. Location (e.g. "United States", "Greater Delhi Area", "London, UK", "New York, NY" - extract or infer from the description/snippet or title)
6. LinkedIn URL

If the item is not a personal LinkedIn profile, skip it.
Extract the ACTUAL company name they currently work at, inferring it from the description/snippet or title. Avoid generic terms like "P&C Insurance" or certification names as the company.

Return a JSON array containing objects with these exact keys:
[
  {
    "name": "...",
    "title": "...",
    "company": "...",
    "company_domain": "...",
    "location": "...",
    "linkedin_url": "..."
  }
]

Input items:
""" + json.dumps(organic_results, indent=2)

    try:
        client = genai.Client(api_key=gemini_key)
        parsed = pl.generate_json_with_retry(prompt, client)
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception as e:
        pl.log.error("Failed to parse search results with Gemini: %s", e)
        return []


def scrape_apify(
    icp: dict, max_leads: int = 50, actor_override: Optional[str] = None,
    company_names: Optional[list[str]] = None,
) -> list[dict]:
    """
    Runs an Apify actor (LinkedIn / company scraper) with the ICP criteria.

    Apify docs: https://docs.apify.com/api/v2#/reference/actors/run-collection/run-actor
    Endpoint: POST https://api.apify.com/v2/acts/{actorId}/runs

    NOTE: The `run_input` dict below is a PLACEHOLDER.  Replace its keys with
    whatever input schema your chosen Apify actor actually expects.
    Common actors and their input schemas:
      - apify/linkedin-profile-scraper  → { "profileUrls": [...] }
      - bebity/linkedin-sales-navigator-scraper → { "searchUrl": "...", ... }
    Adjust `_build_apify_input()` to match your actor's schema.

    actor_override: when provided (e.g. a per-run choice from the UI), takes
    priority over the pl.APIFY_ACTOR_ID env var — lets a caller pick a specific
    actor for one run without changing global config.

    company_names: when the People Discovery stage has already validated a
    specific company list, scopes crawlerbros/lead-finder's search to those
    companies (its schema already documents a companyNames field this code
    just never populated before company-first search existed). Ignored by
    every other actor.

    Returns a list of raw lead dicts.
    """
    api_token = os.getenv("APIFY_API_TOKEN")
    actor_id = actor_override or os.getenv("APIFY_ACTOR_ID", "apify/google-search-scraper")

    pl.log.info("Stage 2b — Running Apify actor '%s' …", actor_id)

    if not api_token:
        pl.log.warning("APIFY_API_TOKEN not set — skipping Apify scrape.")
        return []

    if actor_id == "REPLACE_WITH_YOUR_ACTOR_ID":
        pl.log.warning("APIFY_ACTOR_ID not configured — skipping Apify scrape.")
        return []

    is_leads_finder = actor_id == "code_crafter/leads-finder"
    is_crawlerbros = actor_id == "crawlerbros/lead-finder"
    is_google_maps = actor_id == "nourishing_courier/google-maps-lead-scraper"
    if is_leads_finder:
        run_input = _build_leads_finder_input(icp, max_leads)
    elif is_crawlerbros:
        run_input = _build_crawlerbros_input(icp, max_leads, company_names=company_names)
    elif is_google_maps:
        run_input = _build_google_maps_lead_scraper_input(icp, max_leads)
    else:
        run_input = _build_apify_input(icp, max_leads)
    pl.log.info("Apify Run Input payload: %s", json.dumps(run_input))

    headers = {"Content-Type": "application/json"}
    # Apify actor slug uses ~ instead of / in the URL path
    actor_path = actor_id.replace("/", "~")
    run_url = (
        f"https://api.apify.com/v2/acts/{actor_path}/runs"
        f"?token={api_token}&waitForFinish=300"
    )

    try:
        resp = requests.post(run_url, json=run_input, headers=headers, timeout=360)
        resp.raise_for_status()
        run_data = resp.json().get("data", {})
        dataset_id = run_data.get("defaultDatasetId")
        run_id = run_data.get("id")
        run_status = run_data.get("status")
    except requests.exceptions.HTTPError as e:
        pl.log.error("Apify run error %s: %s", resp.status_code, resp.text)
        return []
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify request failed: %s", e)
        return []

    if not dataset_id:
        pl.log.error("Apify run did not return a dataset ID.")
        return []

    # waitForFinish=300 can expire before a slow run genuinely finishes (a
    # higher max_leads request can legitimately take 2-3+ minutes for some
    # actors) — the run keeps going server-side, but its dataset may still
    # be empty/partial at this exact moment. Silently trusting that as "the
    # run returned 0 leads" would be misleading and hard to debug later, so
    # poll the run's real status a bit longer rather than assuming it's done.
    if run_status != "SUCCEEDED" and run_id:
        pl.log.warning(
            "Apify run %s hadn't finished after the initial wait (status=%s) — polling a bit longer …",
            run_id, run_status,
        )
        poll_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={api_token}"
        for _ in range(12):  # up to ~2 more minutes
            time.sleep(10)
            try:
                poll_resp = requests.get(poll_url, timeout=30)
                poll_resp.raise_for_status()
                run_status = poll_resp.json().get("data", {}).get("status")
            except requests.exceptions.RequestException as e:
                pl.log.warning("Apify run status poll failed: %s", e)
                break
            if run_status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                break
        pl.log.info("Apify run %s finished polling with status=%s", run_id, run_status)

    # Fetch results from the dataset
    dataset_url = (
        f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        f"?token={api_token}&format=json"
    )
    try:
        resp = requests.get(dataset_url, timeout=60)
        resp.raise_for_status()
        items = resp.json()
        os.makedirs("./output", exist_ok=True)
        with open(pl._debug_dump_path("apify_raw.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except requests.exceptions.RequestException as e:
        pl.log.error("Apify dataset fetch failed: %s", e)
        os.makedirs("./output", exist_ok=True)
        with open(pl._debug_dump_path("apify_raw.json"), "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f, indent=2)
        return []

    if is_leads_finder:
        leads = _parse_leads_finder_results(items)
        pl.log.info("Apify (leads-finder) returned %d leads.", len(leads))
        return leads

    if is_crawlerbros:
        leads = _parse_crawlerbros_results(items)
        pl.log.info("Apify (crawlerbros) returned %d leads.", len(leads))
        return leads

    if is_google_maps:
        leads = _parse_google_maps_lead_scraper_results(items)
        pl.log.info("Apify (Google Maps Lead Scraper) returned %d leads.", len(leads))
        return leads

    raw_results = []
    for page in items:
        # Handle both flat list and nested page formats
        if isinstance(page, dict):
            organic_results = page.get("organicResults", [])
        elif isinstance(page, list):
            organic_results = page
        else:
            organic_results = []
            
        for r in organic_results:
            if isinstance(r, dict) and "linkedin.com/in/" in r.get("url", "").lower():
                raw_results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", "")
                })

    pl.log.info("Found %d raw LinkedIn search results from Google.", len(raw_results))
    parsed_profiles = parse_search_results_with_gemini(raw_results)
    
    leads = []
    for p in parsed_profiles:
        name = p.get("name", "")
        title = p.get("title", "")
        company = p.get("company", "")
        domain = p.get("company_domain", "")
        location = p.get("location", "")
        url = p.get("linkedin_url", "")
        
        email = ""
        if name and domain:
            name_parts = name.split()
            first = name_parts[0].lower() if name_parts else ""
            last = name_parts[-1].lower() if len(name_parts) >= 2 else ""
            if first and last:
                email = f"{first}.{last}@{domain}"
            elif first:
                email = f"{first}@{domain}"
                
        lead = {
            "name":         pl._safe(name),
            "title":        pl._safe(title),
            "company":      pl._safe(company),
            "location":     pl._safe(location),
            "email":        pl._safe(email),
            "linkedin_url": pl._safe(url),
            "source":       "apify",
            "_verification_status": "unverified",
            "_confidence_score":    50.0,
        }

        # Preserve Apify domain and raw organic search results metadata.
        # NOTE: the email above is synthesized from this same `domain` guess,
        # so domain-match will trivially read EXACT for Apify-sourced leads —
        # it isn't an independent verification signal for this source.
        lead["apify_company_domain"] = domain
        lead["company_domain"] = pl.normalize_domain(domain)
        matching_raw = next((r for r in raw_results if r.get("url") == url), None)
        if matching_raw:
            for k, v in matching_raw.items():
                if k not in ["title", "url", "description"]:
                    lead[f"apify_{k}"] = v
                else:
                    lead[f"apify_raw_{k}"] = v

        leads.append(lead)

    pl.log.info("Apify returned %d leads.", len(leads))
    return leads


# crawlerbros/lead-finder is a purpose-built lead-search actor — live-tested
# and confirmed to work via the API with no plan restriction (unlike
# code_crafter/leads-finder below, which is code-ready but blocked on the
# current free Apify plan). Every input field is genuinely free text (no
# hidden enums — confirmed live, not just from docs) so this mapping is much
# simpler than leads-finder's. It returns clean structured contact fields
# directly (no Gemini guessing needed), but — live-confirmed — no
# firmographic data (industry/technology/description), so Org Enrichment is
# still what fills those columns once Apollo credits exist.

def _build_crawlerbros_input(icp: dict, max_leads: int, company_names: Optional[list[str]] = None) -> dict:
    """Maps the ICP to crawlerbros/lead-finder's simple free-text filter
    schema: jobTitles, locations, industries, companyNames, maxLeads.

    company_names: populated by the People Discovery stage once companies
    have been validated, scoping this actor's search to exactly that list —
    the actor's schema has always documented this field, it just went unused
    before company-first search existed. Real accepted length/format is
    unverified (the actor's live-testing notes above never exercised it) —
    worth a live check if a very large validated-company list gets truncated
    unexpectedly."""
    industry = pl._bi_industry(icp)
    run_input: dict = {"maxLeads": max_leads}

    titles = pl._bi_all_titles(icp)
    if titles:
        run_input["jobTitles"] = titles

    locations = pl._bi_all_locations(icp)
    if locations:
        run_input["locations"] = locations

    industries = pl._safe_list(industry.get("sub_industries")) or pl._safe_list(industry.get("primary_industry"))
    if industries:
        run_input["industries"] = industries

    if company_names:
        run_input["companyNames"] = company_names

    return run_input


def _parse_crawlerbros_results(items) -> list[dict]:
    """Maps crawlerbros/lead-finder's structured output directly into the
    lead schema — no Gemini call needed."""
    leads = []
    for page in items:
        if isinstance(page, list):
            records = page
        elif isinstance(page, dict):
            records = [page]
        else:
            records = []

        for r in records:
            if not isinstance(r, dict):
                continue
            lead = {
                "name":         pl._safe(r.get("full_name")),
                "title":        pl._safe(r.get("title")),
                "company":      pl._safe(r.get("company_name")),
                "location":     pl._safe(r.get("location")),
                "email":        pl._safe(r.get("email")),
                "linkedin_url": pl._safe(r.get("linkedin_url")),
                "source":       "apify",
                "_verification_status": "unverified",
                "_confidence_score":    50.0,
            }
            lead["company_domain"] = pl.normalize_domain(r.get("company_domain") or "")
            leads.append(lead)

    return leads


# nourishing_courier/google-maps-lead-scraper — a BUSINESS/company-level Google
# Maps scraper, not a person-search actor like Apollo/crawlerbros. It takes
# free-text queries exactly like you'd type into Google Maps (e.g. "insurance
# agencies in Columbus Ohio") and returns one row per business, optionally
# visiting each business's website to extract a real contact email. There is
# no person name/title in its output — leads from this source are
# company-shaped (company/email/phone/address), with name/title left blank.
# Both the input schema (pulled live from Apify's own actor-definition API,
# not marketing docs) and the output field names below (from a real live test
# run — see _parse_google_maps_lead_scraper_results()) were confirmed live,
# not assumed from documentation, per this session's established discipline.

_GOOGLE_MAPS_MAX_QUERIES = 4   # bounded — each query costs actor runtime/compute


def _build_google_maps_lead_scraper_input(icp: dict, max_leads: int) -> dict:
    """Combines the ICP's industry/business terms with its locations into
    free-text "X in Y" queries — exactly what a user would type into Google
    Maps search, per the actor's real input schema (searchQueries: array of
    strings). Capped at _GOOGLE_MAPS_MAX_QUERIES to bound cost."""
    industry = pl._bi_industry(icp)
    terms = pl._dedupe_list(
        pl._safe_list(industry.get("sub_industries"))
        or ([industry.get("primary_industry")] if industry.get("primary_industry") else [])
    )[:2]
    locations = pl._bi_all_locations(icp)[:2]

    queries = []
    if terms and locations:
        for term in terms:
            for loc in locations:
                queries.append(f"{term} in {loc}")
    elif terms:
        queries = list(terms)
    elif locations:
        primary = industry.get("primary_industry") or "businesses"
        queries = [f"{primary} in {loc}" for loc in locations]

    return {
        "searchQueries": queries[:_GOOGLE_MAPS_MAX_QUERIES],
        "maxResults": min(max_leads, 500),   # live-tested schema: 1-500 per query
        "extractEmails": True,                # the actor's own "killer feature" — live-tested, real emails come back
        "extractPhotos": False,
        "extractReviews": False,
    }


def _parse_google_maps_lead_scraper_results(items) -> list[dict]:
    """Maps the actor's real output shape (confirmed via a live test run —
    see module docstring above) into the lead schema. No person name/title
    is available from Google Maps — those fields stay blank; company/email/
    phone/address are the useful fields here."""
    leads = []
    for page in items:
        if isinstance(page, list):
            records = page
        elif isinstance(page, dict):
            records = [page]
        else:
            records = []

        for r in records:
            if not isinstance(r, dict):
                continue
            emails = pl._safe_list(r.get("emails")) or pl._safe_list(r.get("email"))
            lead = {
                "name":         "",
                "title":        "",
                "company":      pl._safe(r.get("name")),
                "location":     pl._safe(r.get("address")),
                "email":        pl._safe(emails[0] if emails else "").lower(),
                "linkedin_url": "",
                "source":       "apify",
                "_verification_status": "unverified",
                "_confidence_score":    50.0,
            }
            lead["email2"] = pl._safe(emails[1]).lower() if len(emails) > 1 else ""
            lead["phone"] = pl._safe(r.get("phone"))
            lead["biz_address"] = pl._safe(r.get("address"))
            lead["biz_category"] = pl._join_list_str(r.get("categories") or r.get("category"))
            lead["biz_description"] = pl._safe(r.get("category"))
            lead["company_domain"] = pl.normalize_domain(r.get("website") or "")
            # Preserve every other raw field with a prefix, matching the
            # apollo_*/apify_* convention — nothing silently dropped.
            for k, v in r.items():
                if k not in ("name", "address", "phone", "email", "emails", "category", "categories", "website"):
                    lead[f"google_maps_{k}"] = v
            leads.append(lead)

    return leads


# code_crafter/leads-finder is a structured B2B lead database (like Apollo),
# not a search scraper — it takes real filters and returns real firmographic
# data directly (industry, company_description, company_technologies, etc.),
# so unlike the google-search-scraper path above, no Gemini guessing step is
# needed at all. Kept side-by-side with the original path (not a replacement)
# so pl.APIFY_ACTOR_ID can be switched back with no code change.

# code_crafter/leads-finder validates several fields against fixed enums that
# its documentation page does NOT accurately describe — discovered by
# live-testing against the actor's real input validation (same lesson as the
# Bright Data docs earlier this session: trust a live API response over
# stale/incomplete docs). Kept as module-level constants since there's no way
# to fetch them dynamically without an API call, and they change rarely.

_LEADS_FINDER_INDUSTRIES = (
    "information technology & services", "construction", "marketing & advertising", "real estate",
    "health, wellness & fitness", "management consulting", "computer software", "internet", "retail",
    "financial services", "consumer services", "hospital & health care", "automotive", "restaurants",
    "education management", "food & beverages", "design", "hospitality", "accounting", "events services",
    "nonprofit organization management", "entertainment", "electrical/electronic manufacturing",
    "leisure, travel & tourism", "professional training & coaching", "transportation/trucking/railroad",
    "law practice", "apparel & fashion", "architecture & planning", "mechanical or industrial engineering",
    "insurance", "telecommunications", "human resources", "staffing & recruiting", "sports",
    "legal services", "oil & energy", "media production", "machinery", "wholesale", "consumer goods",
    "music", "photography", "medical practice", "cosmetics", "environmental services", "graphic design",
    "business supplies & equipment", "renewables & environment", "facilities services", "publishing",
    "food production", "arts & crafts", "building materials", "civil engineering",
    "religious institutions", "public relations & communications", "higher education", "printing",
    "furniture", "mining & metals", "logistics & supply chain", "research", "pharmaceuticals",
    "individual & family services", "medical devices", "civic & social organization", "e-learning",
    "security & investigations", "chemicals", "government administration", "online media",
    "investment management", "farming", "writing & editing", "textiles", "mental health care",
    "primary/secondary education", "broadcast media", "biotechnology", "information services",
    "international trade & development", "motion pictures & film", "consumer electronics", "banking",
    "import & export", "industrial automation", "recreational facilities & services",
    "performing arts", "utilities", "sporting goods", "fine art", "airlines/aviation",
    "computer & network security", "maritime", "luxury goods & jewelry", "veterinary",
    "venture capital & private equity", "wine & spirits", "plastics", "aviation & aerospace",
    "commercial real estate", "computer games", "packaging & containers", "executive office",
    "computer hardware", "computer networking", "market research", "outsourcing/offshoring",
    "program development", "translation & localization", "philanthropy", "public safety",
    "alternative medicine", "museums & institutions", "warehousing", "defense & space", "newspapers",
    "paper & forest products", "law enforcement", "investment banking", "government relations",
    "fund-raising", "think tanks", "glass, ceramics & concrete", "capital markets", "semiconductors",
    "animation", "political organization", "package/freight delivery", "wireless",
    "international affairs", "public policy", "libraries", "gambling & casinos",
    "railroad manufacture", "ranching", "military", "fishery", "supermarkets", "dairy", "tobacco",
    "shipbuilding", "judiciary", "alternative dispute resolution", "nanotechnology", "agriculture",
    "legislative office",
)

# A representative subset of the actor's much larger country enum, covering
# the countries this app's ICPs are realistically going to name — either
# directly (a specific country) or via a broad region that needs expanding
# into countries, since the actor has no concept of "North America"/"Europe"/
# "APAC" as a single filter value.
_LEADS_FINDER_REGION_COUNTRIES = {
    "north america": ["united states", "canada", "mexico"],
    "europe": ["united kingdom", "germany", "france", "netherlands", "spain", "italy", "ireland",
               "sweden", "belgium", "switzerland", "poland", "austria", "denmark", "finland", "portugal"],
    "apac": ["china", "india", "japan", "singapore", "australia", "hong kong", "south korea",
             "taiwan", "indonesia", "malaysia", "philippines", "thailand", "vietnam", "new zealand"],
    "asia pacific": ["china", "india", "japan", "singapore", "australia", "hong kong", "south korea",
                      "taiwan", "indonesia", "malaysia", "philippines", "thailand", "vietnam", "new zealand"],
    "latam": ["brazil", "mexico", "argentina", "colombia", "chile", "peru"],
    "latin america": ["brazil", "mexico", "argentina", "colombia", "chile", "peru"],
    "middle east": ["united arab emirates", "saudi arabia", "israel", "qatar", "kuwait"],
    "africa": ["south africa", "nigeria", "kenya", "egypt", "ghana"],
}

_LEADS_FINDER_REVENUE_BUCKETS = (
    ("100K", 100_000), ("500K", 500_000), ("1M", 1_000_000), ("5M", 5_000_000),
    ("10M", 10_000_000), ("25M", 25_000_000), ("50M", 50_000_000), ("100M", 100_000_000),
    ("500M", 500_000_000), ("1B", 1_000_000_000), ("5B", 5_000_000_000), ("10B", 10_000_000_000),
)


def _map_industry_to_leads_finder_enum(text: str) -> Optional[str]:
    """Finds the closest match for a free-text industry string in the
    actor's fixed taxonomy — checked longest-value-first so a specific match
    (e.g. "insurance") isn't shadowed by a shorter, less precise one."""
    text_lower = (text or "").lower()
    if not text_lower:
        return None
    for value in sorted(_LEADS_FINDER_INDUSTRIES, key=len, reverse=True):
        if value in text_lower or text_lower in value:
            return value
    return None


def _map_locations_to_leads_finder(geography: list[str]) -> list[str]:
    """Expands broad regions ("North America", "Europe", "APAC") into the
    actor's country-only location enum, passing already-specific country
    names through directly."""
    countries = []
    for g in geography:
        g_lower = g.strip().lower()
        if g_lower in _LEADS_FINDER_REGION_COUNTRIES:
            countries.extend(_LEADS_FINDER_REGION_COUNTRIES[g_lower])
        else:
            countries.append(g_lower)
    return pl._dedupe_list(countries)


def _nearest_leads_finder_revenue_bucket(value: float) -> str:
    """The actor's min_revenue/max_revenue only accept fixed bucket labels
    (e.g. "100M"), not raw numbers — picks the closest one to the ICP's
    actual figure."""
    return min(_LEADS_FINDER_REVENUE_BUCKETS, key=lambda b: abs(b[1] - value))[0]


def _build_leads_finder_input(icp: dict, max_leads: int) -> dict:
    """Maps the ICP to code_crafter/leads-finder's structured filter schema.
    Deliberately conservative: email_status is left unset (Stage 6 already
    re-verifies every lead's email regardless of source, so pre-filtering
    here has no correctness benefit and risks starving results the way an
    over-constrained filter set did earlier for _build_apify_input()) and
    company_stage values with no clean funding-stage equivalent (e.g.
    "Enterprise") are skipped rather than guessed."""
    run_input: dict = {"fetch_count": max_leads}

    titles = pl._bi_all_titles(icp)
    if titles:
        run_input["contact_job_title"] = titles

    # seniority_level enum (live-validated): founder, owner, c_suite, director,
    # partner, vp, head, manager, senior, entry, trainee
    mapped_seniority = []
    for s in pl._bi_seniority(icp):
        s_lower = s.lower()
        if "c-level" in s_lower or "chief" in s_lower or "cxo" in s_lower or "c-suite" in s_lower:
            mapped_seniority.append("c_suite")
        elif "vp" in s_lower:
            mapped_seniority.append("vp")
        elif "director" in s_lower:
            mapped_seniority.append("director")
        elif "partner" in s_lower:
            mapped_seniority.append("partner")
        elif "head" in s_lower:
            mapped_seniority.append("head")
        elif "manager" in s_lower:
            mapped_seniority.append("manager")
        elif "founder" in s_lower:
            mapped_seniority.append("founder")
        elif "owner" in s_lower:
            mapped_seniority.append("owner")
        elif "senior" in s_lower:
            mapped_seniority.append("senior")
        elif "entry" in s_lower:
            mapped_seniority.append("entry")
        elif "trainee" in s_lower or "intern" in s_lower:
            mapped_seniority.append("trainee")
    if mapped_seniority:
        run_input["seniority_level"] = pl._dedupe_list(mapped_seniority)

    # functional_level enum (live-validated): c_suite, finance, product_management,
    # engineering, design, education, human_resources, information_technology,
    # legal, marketing, operations, sales, support
    mapped_functional = []
    for d in pl._bi_departments(icp):
        d_lower = d.lower()
        if "it" in d_lower or "information technology" in d_lower:
            mapped_functional.append("information_technology")
        elif "tech" in d_lower or "engineer" in d_lower:
            mapped_functional.append("engineering")
        elif "sale" in d_lower:
            mapped_functional.append("sales")
        elif "market" in d_lower:
            mapped_functional.append("marketing")
        elif "finance" in d_lower or "accounting" in d_lower:
            mapped_functional.append("finance")
        elif "hr" in d_lower or "people" in d_lower or "talent" in d_lower:
            mapped_functional.append("human_resources")
        elif "product" in d_lower:
            mapped_functional.append("product_management")
        elif "legal" in d_lower:
            mapped_functional.append("legal")
        elif "operation" in d_lower:
            mapped_functional.append("operations")
        elif "support" in d_lower or "success" in d_lower:
            mapped_functional.append("support")
        elif "design" in d_lower:
            mapped_functional.append("design")
        elif "education" in d_lower or "training" in d_lower:
            mapped_functional.append("education")
    if mapped_functional:
        run_input["functional_level"] = pl._dedupe_list(mapped_functional)

    locations = _map_locations_to_leads_finder(pl._bi_all_locations(icp))
    if locations:
        run_input["contact_location"] = locations

    industry = pl._bi_industry(icp)
    industry_candidates = pl._safe_list(industry.get("sub_industries")) + pl._safe_list(industry.get("primary_industry"))
    mapped_industries = []
    for i in industry_candidates:
        mapped = _map_industry_to_leads_finder_enum(i)
        if mapped:
            mapped_industries.append(mapped)
    if mapped_industries:
        run_input["company_industry"] = pl._dedupe_list(mapped_industries)

    # The actor-native way to do what Claim Verification has to work around
    # via a separate web search: ask for the specific named technology
    # directly as a search-time filter, using the same cleaned/prioritized
    # term the Apollo adapter uses (confirmed_technologies over generic crm).
    keywords = []
    tech = pl._bi_primary_technology(icp)
    if tech:
        keywords.append(tech)
    keywords.extend(pl._bi_keyword_pool(icp)[:5])
    if keywords:
        run_input["company_keywords"] = pl._dedupe_list(keywords)

    # negative_keywords/exclude_industries/excluded_locations are short
    # terms now (see pl._icp_schema_block()), but this actor's own
    # company_not_keywords semantics haven't been live-validated for this
    # schema yet, so left out here rather than guessing.

    # size enum (live-validated): 1-10, 11-20, 21-50, 51-100, 101-200, 201-500,
    # 501-1000, 1001-2000, 2001-5000, 5001-10000, 10001-20000, 20001-50000, 50000+
    min_sz, max_sz = pl._bi_size_range(icp)
    if min_sz is not None or max_sz is not None:
        size_buckets = [
            ("1-10", 1, 10), ("11-20", 11, 20), ("21-50", 21, 50), ("51-100", 51, 100),
            ("101-200", 101, 200), ("201-500", 201, 500), ("501-1000", 501, 1000),
            ("1001-2000", 1001, 2000), ("2001-5000", 2001, 5000), ("5001-10000", 5001, 10000),
            ("10001-20000", 10001, 20000), ("20001-50000", 20001, 50000), ("50000+", 50000, 100_000_000),
        ]
        eff_min = min_sz if min_sz is not None else 0
        eff_max = max_sz if max_sz is not None else 100_000_000
        selected = [label for label, low, high in size_buckets if low <= eff_max and high >= eff_min]
        if selected:
            run_input["size"] = selected

    # min_revenue/max_revenue enum (live-validated): 100K, 500K, 1M, 5M, 10M,
    # 25M, 50M, 100M, 500M, 1B, 5B, 10B — fixed bucket labels, not raw numbers.
    revenue_min, revenue_max = pl._bi_revenue_range(icp)
    if revenue_min is not None:
        run_input["min_revenue"] = _nearest_leads_finder_revenue_bucket(float(revenue_min))
    if revenue_max is not None:
        run_input["max_revenue"] = _nearest_leads_finder_revenue_bucket(float(revenue_max))

    # funding enum (live-validated): seed, angel, series_a..series_f,
    # venture_round, debt_financing, convertible_note, private_equity_round, other_round
    mapped_funding = []
    for s in pl._bi_company_stage(icp):
        s_lower = s.lower()
        if "seed" in s_lower:
            mapped_funding.append("seed")
        elif "series a" in s_lower:
            mapped_funding.append("series_a")
        elif "series b" in s_lower:
            mapped_funding.append("series_b")
        elif "series c" in s_lower:
            mapped_funding.append("series_c")
        elif "series d" in s_lower:
            mapped_funding.append("series_d")
        elif "series e" in s_lower:
            mapped_funding.append("series_e")
        elif "series f" in s_lower:
            mapped_funding.append("series_f")
        elif "venture" in s_lower:
            mapped_funding.append("venture_round")
        elif "growth" in s_lower or "late-stage" in s_lower or "public" in s_lower or "private equity" in s_lower:
            mapped_funding.append("private_equity_round")
        # "Enterprise" and other ambiguous stages have no clean funding-stage
        # equivalent — skipped rather than guessed.
    if mapped_funding:
        run_input["funding"] = pl._dedupe_list(mapped_funding)

    return run_input


def _parse_leads_finder_results(items) -> list[dict]:
    """Maps code_crafter/leads-finder's structured output directly into the
    lead schema — no Gemini call needed, unlike the google-search-scraper
    path, since the actor already returns real fields instead of a snippet
    to guess from."""
    leads = []
    for page in items:
        if isinstance(page, list):
            records = page
        elif isinstance(page, dict):
            records = [page]
        else:
            records = []

        for r in records:
            if not isinstance(r, dict):
                continue
            name = r.get("full_name") or " ".join(
                p for p in [r.get("first_name"), r.get("last_name")] if p
            )
            domain = r.get("company_domain") or r.get("company_website") or ""

            lead = {
                "name":             pl._safe(name),
                "title":            pl._safe(r.get("job_title") or r.get("headline")),
                "company":          pl._safe(r.get("company_name")),
                "email":            pl._safe(r.get("email")),
                "email2":           pl._safe(r.get("personal_email")),
                "phone":            pl._safe(r.get("mobile_number")),
                "linkedin_url":     pl._safe(r.get("linkedin")),
                "city":             pl._safe(r.get("city")),
                "state":            pl._safe(r.get("state")),
                "country":          pl._safe(r.get("country")),
                "level":            pl._safe(r.get("seniority_level")),
                "employee_count":   pl._safe(r.get("company_size")),
                "industry":         pl._safe(r.get("industry")),
                "biz_description":  pl._safe(r.get("company_description")),
                "technology":       pl._join_list_str(r.get("company_technologies")),
                "biz_category":     pl._join_list_str(r.get("keywords")),
                "market_cap":       pl._safe(r.get("company_market_cap")),
                "biz_address":      pl._safe(r.get("company_full_address") or r.get("company_street_address")),
                "source":           "apify",
                "_verification_status": "unverified",
                "_confidence_score":    50.0,
            }
            lead["company_domain"] = pl.normalize_domain(domain)
            leads.append(lead)

    return leads


def _build_apify_input(icp: dict, max_leads: int) -> dict:
    """
    Builds the input payload for the `apify/google-search-scraper` actor.
    """
    queries_list = []

    industry = pl._bi_industry(icp)
    titles = pl._bi_all_titles(icp)
    main_tech = pl._bi_primary_technology(icp)
    industries = pl._safe_list(industry.get("sub_industries")) or pl._safe_list(industry.get("primary_industry"))
    main_ind = industries[0] if industries else ""
    locations = pl._bi_all_locations(icp)

    # Exclusions
    exclusions = pl._bi_negative_keywords(icp)

    exclusion_str = ""
    if exclusions:
        exclusion_str = " " + " ".join([f'-"{e}"' for e in exclusions])

    # Pair each job title with ONE distinguishing term — technology when
    # available, else industry. Live-tested combining both unconditionally
    # (requiring two exact quoted phrases plus title plus location all on
    # the same page) and it collapsed real Google result counts from 52 raw
    # leads to 1 — Google's exact-phrase-AND search is too restrictive for
    # that many simultaneous quoted requirements. The real fix (see
    # pl._bi_primary_technology()) is picking the RIGHT single term, not
    # stacking more of them.
    for title in titles[:6]:  # Limit to top 6 titles
        term = f'"{title}"'
        if main_tech:
            term += f' "{main_tech}"'
        elif main_ind:
            term += f' "{main_ind}"'
        if locations:
            term += f' "{locations[0]}"'
        term += exclusion_str
        queries_list.append(f"site:linkedin.com/in/ {term}")

    # Also add a deterministic fallback query built from the canonical keyword pool
    search_keywords = pl._bi_keyword_pool(icp)
    if search_keywords:
        kw_term = " ".join(f'"{kw}"' for kw in search_keywords[:6])
        queries_list.append(f"site:linkedin.com/in/ {kw_term}{exclusion_str}")

    full_queries = "\n".join(queries_list)

    return {
        "queries":          full_queries,
        "maxPagesPerQuery": 2,
        "resultsPerPage":   50,
        "mobileResults":    False
    }



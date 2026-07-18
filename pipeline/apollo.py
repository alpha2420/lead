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


_APOLLO_ENRICH_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "apollo_enrich_cache.db")
_APOLLO_ENRICH_CACHE_TTL_DAYS = 30


def _apollo_enrich_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_APOLLO_ENRICH_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_APOLLO_ENRICH_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS people (person_id TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_apollo_enrichment(person_id: str) -> Optional[dict]:
    """Returns the cached enrichment if younger than the TTL, else None.
    Enrichment costs real Apollo credits per person, so a repeat lookup for
    the same person within 30 days reuses the cached result."""
    try:
        conn = _apollo_enrich_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM people WHERE person_id = ?", (person_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_APOLLO_ENRICH_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Apollo enrichment cache read failed for %s: %s", person_id, e)
        return None


def _cache_apollo_enrichment(person_id: str, data: dict) -> None:
    try:
        conn = _apollo_enrich_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO people (person_id, response_json, fetched_at) VALUES (?, ?, ?)",
                (person_id, json.dumps(data), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Apollo enrichment cache write failed for %s: %s", person_id, e)


def _enrich_apollo_person(person_id: str) -> Optional[dict]:
    """Cache-first call to Apollo's People Enrichment endpoint for a single
    candidate. Returns the raw `person` dict (with a fully-populated
    embedded `organization`) on success, or None on any failure — enrichment
    is a per-candidate best effort, one failure shouldn't break the batch."""
    if not person_id:
        return None

    cached = pl._get_cached_apollo_enrichment(person_id)
    if cached is not None:
        return cached

    headers = {"Content-Type": "application/json", "X-Api-Key": pl.APOLLO_API_KEY}
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/people/match",
            json={"id": person_id, "reveal_personal_emails": True},
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        person = resp.json().get("person")
    except requests.exceptions.RequestException as e:
        pl.log.warning("Apollo enrichment failed for person %s: %s", person_id, e)
        return None

    if not person:
        return None

    pl._cache_apollo_enrichment(person_id, person)
    return person


def _map_apollo_person_to_lead(person: dict) -> dict:
    """Maps a fully-enriched Apollo person (real name/email + embedded
    organization) into the standard lead schema."""
    lead = {
        "name":         pl._safe(person.get("name")),
        "title":        pl._safe(person.get("title")),
        "company":      pl._safe(
                            person.get("organization", {}).get("name")
                            or person.get("company_name")
                        ),
        "location":     pl._safe(
                            ", ".join(
                                filter(
                                    None,
                                    [
                                        person.get("city"),
                                        person.get("state"),
                                        person.get("country"),
                                    ],
                                )
                            )
                        ),
        "email":        pl._safe(person.get("email")).lower(),
        "linkedin_url": pl._safe(person.get("linkedin_url")),
        "source":       "apollo",
        # Internal scoring fields — filled in later stages
        "_verification_status": "unverified",
        "_confidence_score":    50.0,
    }

    # Export-column normalization — best-effort extraction from Apollo's
    # nested person/organization payload into the flat CRM export shape.
    org = person.get("organization")
    if not isinstance(org, dict):
        org = {}

    phone_numbers = person.get("phone_numbers") or []
    phone1 = _phone_from_entry(phone_numbers[0]) if phone_numbers else ""
    phone2 = _phone_from_entry(phone_numbers[1]) if len(phone_numbers) > 1 else ""
    if not phone1:
        phone1 = org.get("phone") or org.get("sanitized_phone") or ""

    personal_emails = person.get("personal_emails") or []

    lead["phone"]           = pl._safe(phone1)
    lead["phone2"]          = pl._safe(phone2)
    lead["city"]            = pl._safe(person.get("city"))
    lead["state"]           = pl._safe(person.get("state"))
    lead["country"]         = pl._safe(person.get("country"))
    lead["zip_code"]        = pl._safe(org.get("postal_code") or person.get("postal_code"))
    lead["employee_count"]  = pl._safe(org.get("estimated_num_employees"))
    lead["level"]           = pl._safe(person.get("seniority"))
    lead["email2"]          = pl._safe(personal_emails[0]) if personal_emails else ""
    lead["biz_address"]     = pl._safe(org.get("raw_address") or org.get("street_address"))
    lead["market_cap"]      = pl._safe(org.get("market_cap"))
    lead["industry"]        = pl._safe(org.get("industry"))
    lead["biz_category"]    = _join_list_str(org.get("keywords") or org.get("secondary_industries"))
    lead["biz_description"] = pl._safe(org.get("short_description"))
    lead["technology"]      = _join_list_str(org.get("technology_names"))
    lead["company_domain"]  = normalize_domain(org.get("primary_domain") or org.get("website_url") or "")

    # Preserve all other raw Apollo columns/metadata
    for k, v in person.items():
        if k not in ["name", "title", "email", "linkedin_url"]:
            if isinstance(v, dict):
                for subk, subv in v.items():
                    lead[f"apollo_{k}_{subk}"] = subv
            else:
                lead[f"apollo_{k}"] = v

    return lead


def scrape_apollo(
    icp: dict, max_leads: int = 50, page: int = 1,
    organization_domains: Optional[list[str]] = None,
) -> list[dict]:
    """
    Queries Apollo.io's People Search (api_search) for candidates, then
    enriches the ones with has_email=true (Apollo's own signal that
    revealing a real email is likely to succeed) via People Enrichment to
    get real, usable leads — api_search alone only returns obfuscated
    previews. Supports pagination via the `page` argument so the pipeline
    can keep fetching more leads until the verified target is reached.

    organization_domains: when the People Discovery stage has already
    validated a specific company list, this scopes the search to exactly
    those companies via q_organization_domains_list — live-tested and
    confirmed working (a domain-scoped search for "engineer" against
    stripe.com/salesforce.com returned only that company's people). None
    (the default) searches across any company matching the ICP's other
    filters, same as before company-first search existed.
    """
    pl.log.info("Stage 2a — Scraping Apollo.io page %d (max %d leads) …", page, max_leads)

    if not pl.APOLLO_API_KEY:
        pl.log.warning("APOLLO_API_KEY not set — skipping Apollo scrape.")
        return []

    # Apollo requires the key in the X-Api-Key header (not body/query param)
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key":    pl.APOLLO_API_KEY,
    }

    # Person titles — buying-committee titles + phrasing variations
    person_titles = _bi_all_titles(icp)

    # Exclusions — flattened from exclude_industries/excluded_locations/negative_keywords
    exclusions = _bi_negative_keywords(icp)

    # Build the Apollo search payload — live-tested against api_search
    # directly and confirmed every one of these parameters is still honored
    # on the new endpoint (same names, same behavior as the deprecated one).
    # Industry/tech/keywords use q_organization_keyword_tags (an array,
    # OR'd across tags) rather than q_keywords (a single free-text string
    # live-tested to AND-match every word — a realistic multi-concept ICP
    # string reliably returned 0 candidates on real queries).
    keyword_tags = _build_apollo_keyword_tags(icp)
    size_min, size_max = _bi_size_range(icp)

    payload = {
        "page":     page,
        "per_page": min(max_leads, 25),   # Apollo free tier caps at 25/page
        # Job titles
        "person_titles": person_titles,
        # Company size range (Apollo uses num_employees_ranges as strings)
        # Format: ["1,10", "11,50", "51,200"]  — build dynamically:
        "organization_num_employees_ranges": _build_apollo_size_range(size_min, size_max),
        # Location
        "person_locations": _clean_apollo_locations(_bi_all_locations(icp)),
        # Industry/tech/keywords — short OR'd tags, not one AND-matched string
        "q_organization_keyword_tags": keyword_tags,
    }
    if exclusions:
        payload["q_organization_not_search_list"] = exclusions
    if organization_domains:
        payload["q_organization_domains_list"] = organization_domains

    payload = {k: v for k, v in payload.items() if v not in [[], None, ""]}

    url = "https://api.apollo.io/api/v1/mixed_people/api_search"

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        os.makedirs("./output", exist_ok=True)
        with open(pl._debug_dump_path("apollo_raw.json"), "w", encoding="utf-8") as f:
            f.write(resp.text)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        pl.log.error("Apollo HTTP error %s: %s", resp.status_code, resp.text)
        os.makedirs("./output", exist_ok=True)
        with open(pl._debug_dump_path("apollo_raw.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"error": resp.text, "status_code": resp.status_code}))
        return []
    except requests.exceptions.RequestException as e:
        pl.log.error("Apollo request failed: %s", e)
        os.makedirs("./output", exist_ok=True)
        with open(pl._debug_dump_path("apollo_raw.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"error": str(e)}))
        return []

    candidates = data.get("people", []) or data.get("contacts", [])
    # Only pay to enrich candidates Apollo itself signals will likely yield
    # a real email — capped at max_leads, same per-page budget as before.
    to_enrich = [c for c in candidates if c.get("has_email") and c.get("id")][:max_leads]

    leads = []
    if to_enrich:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_candidate = {
                executor.submit(_enrich_apollo_person, c["id"]): c for c in to_enrich
            }
            for future in future_to_candidate:
                try:
                    person = future.result()
                except Exception as e:
                    candidate = future_to_candidate[future]
                    pl.log.error("Unhandled error enriching Apollo person %s: %s", candidate.get("id"), e)
                    person = None
                if person:
                    leads.append(_map_apollo_person_to_lead(person))

    pl.log.info("Apollo returned %d leads.", len(leads))
    return leads


def _build_apollo_size_range(min_size: Optional[int], max_size: Optional[int]) -> list[str]:
    """
    Converts company_size_min / max to Apollo's bracket format.
    Apollo expects strings like "1,10", "11,50", "51,200", "201,500", "501,1000", "1001,2000".
    We pick all brackets that overlap with [min_size, max_size].
    """
    if min_size is None and max_size is None:
        return []

    brackets = [
        (1,    10),
        (11,   50),
        (51,   200),
        (201,  500),
        (501,  1000),
        (1001, 2000),
        (2001, 5000),
        (5001, 10000),
    ]
    lo = min_size or 0
    hi = max_size or 999_999

    result = []
    for blo, bhi in brackets:
        if bhi >= lo and blo <= hi:
            result.append(f"{blo},{bhi}")
    return result


def _clean_apollo_locations(geography) -> list[str]:
    """Strips parenthetical qualifiers from geography values before sending
    them as Apollo person_locations entries. Gemini sometimes describes a
    region descriptively rather than naming a real place Apollo recognizes
    (e.g. "United States (Eastern Time Zone cities)", "United States (EST
    Zone)") — live-tested, and that exact string returns 0 matches on
    Apollo, even though the country name alone (after stripping the
    parenthetical) matches hundreds of thousands of real profiles. This is
    especially damaging when it's the *only* geography value, since it
    zeroes out the whole query rather than just narrowing it."""
    cleaned = [re.split(r"[(]", loc)[0].strip() for loc in _safe_list(geography)]
    return _dedupe_list([c for c in cleaned if c])


def _safe_list(val) -> list[str]:
    """Ensures input is a list of strings, filtering out nulls/empty strings."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if v]
    return [str(val).strip()]


def _dedupe_list(items: list[str]) -> list[str]:
    """Order-preserving, case-insensitive dedupe."""
    seen = set()
    result = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item.strip())
    return result


def _join_list_str(val) -> str:
    """Joins a list into a comma-separated string; passes scalars through; '' for None."""
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(str(v).strip() for v in val if v)
    return str(val).strip()


def _phone_from_entry(entry) -> str:
    """Extracts a phone number string from a provider's phone-number entry,
    which may be a plain string or a dict like {"raw_number"/"number": ..., "sanitized_number": ...}."""
    if isinstance(entry, dict):
        return entry.get("sanitized_number") or entry.get("phone_number") or entry.get("raw_number") or entry.get("number") or ""
    return str(entry) if entry else ""


def normalize_domain(raw: str) -> str:
    """Strips protocol/www/path/port from a URL or bare domain, lowercased.
    'https://www.Acme.com/about' and 'acme.com' both normalize to 'acme.com'."""
    if not raw:
        return ""
    d = str(raw).strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split(":")[0]
    return d.strip()


# ─────────────────────────────────────────────
# Business Intelligence accessor layer
# ─────────────────────────────────────────────
# The seam between the BI schema's shape (see pl._icp_schema_block()) and every
# provider adapter/scorer below. No adapter should reach into icp.get(...)
# directly for a BI field — it goes through one of these instead, so if the
# BI schema's internal representation ever changes again, only this section
# needs to change, not every adapter.

def _bi_industry(icp: dict) -> dict:
    return icp.get("industry_intelligence") or {}


def _bi_geography(icp: dict) -> dict:
    return icp.get("geography_intelligence") or {}


def _bi_technology(icp: dict) -> dict:
    return icp.get("technology_intelligence") or {}


def _bi_buying_committee(icp: dict) -> dict:
    return icp.get("buying_committee_intelligence") or {}


def _bi_company(icp: dict) -> dict:
    return icp.get("company_intelligence") or {}


def _bi_market(icp: dict) -> dict:
    return icp.get("market_intelligence") or {}


def _bi_intent(icp: dict) -> dict:
    return icp.get("intent_intelligence") or {}


def _bi_search(icp: dict) -> dict:
    return icp.get("search_intelligence") or {}


def _bi_all_locations(icp: dict) -> list[str]:
    """Flattens geography_intelligence's countries/states/cities into one
    deduped list of literal place-name strings — the shape every location
    filter (Apollo, crawlerbros) expects. Deliberately excludes `regions`
    (loose labels like "North America" aren't real filter values — the
    prompt rules require regions to always be expanded into real
    countries/states too, so this list should already have full coverage)."""
    geo = _bi_geography(icp)
    flat = _safe_list(geo.get("countries")) + _safe_list(geo.get("states")) + _safe_list(geo.get("cities"))
    return _dedupe_list(flat)


def _bi_hidden_expansion(icp: dict) -> dict:
    """hidden_semantic_expansion is a machine-only recall layer — never
    rendered in the UI, only consumed here to widen search-term pools."""
    return icp.get("hidden_semantic_expansion") or {}


def _bi_hidden_expansion_terms(icp: dict, concept: str, tightness_levels=("exact", "close", "broad")) -> list[str]:
    """concept: 'industry_terms' | 'title_terms' | 'technology_terms' | 'description_phrases'.
    Defensively caps at 12 regardless of what the model actually returned,
    same discipline as _trim_keyword_tag() elsewhere — never fully trust a
    stated prompt limit. Note: these are lists of {term, tightness} objects,
    not strings, so _safe_list() (string-only) doesn't apply here."""
    raw_items = _bi_hidden_expansion(icp).get(concept)
    items = raw_items[:12] if isinstance(raw_items, list) else []
    return _dedupe_list([
        str(it.get("term", "")).strip()
        for it in items
        if isinstance(it, dict) and it.get("tightness") in tightness_levels and it.get("term")
    ])


def _bi_ai_suggestions(icp: dict) -> list[dict]:
    """AI's own unverified opinion on how to adjust the ICP, generated at
    ICP-generation time with no live search data behind it — distinct from
    Coverage Analysis's real, data-backed suggestions. A list of dicts, not
    strings, so _safe_list() (string-only) doesn't apply here."""
    suggestions = icp.get("ai_suggestions")
    return suggestions if isinstance(suggestions, list) else []


def _bi_all_titles(icp: dict) -> list[str]:
    """Flattens buying_committee_intelligence's primary_titles + title_variations,
    then the hidden-expansion title tail (exact/close tiers only) as the
    lowest-priority fallback."""
    committee = _bi_buying_committee(icp)
    flat = (
        _safe_list(committee.get("primary_titles"))
        + _safe_list(committee.get("title_variations"))
        + _bi_hidden_expansion_terms(icp, "title_terms", ("exact", "close"))
    )
    return _dedupe_list(flat)


def _bi_departments(icp: dict) -> list[str]:
    return _safe_list(_bi_buying_committee(icp).get("departments"))


def _bi_seniority(icp: dict) -> list[str]:
    return _safe_list(_bi_buying_committee(icp).get("seniority"))


# ── Technology Intelligence: canonical search-precedence chain ───────────
# technology_intelligence carries 6 fields + hidden_semantic_expansion's
# technology_terms. Each has exactly ONE responsibility, and every
# consumer that needs "the technology/technologies to search or match
# with" resolves through _bi_technology_precedence_tiers() /
# _bi_technology_signal() below — never by reading technology_intelligence
# fields directly — so precedence can never independently drift between
# _bi_primary_technology(), _bi_keyword_pool(), _bi_all_technologies(),
# and every Apollo/Apify mapper (all of which call one of those three
# rather than technology_intelligence fields directly).
#
# SEARCH-SIGNAL fields (this precedence chain, strongest first):
#   1. confirmed_technologies — customer explicitly named these; the
#      strongest signal, and claim verification's ground truth
#      (pl._extract_verifiable_claims) — must never contain a guess.
#   2. likely_technologies    — Gemini's inference for this industry/size.
#   3. technology_keywords    — generic literal search terms.
#   4. hidden_semantic_expansion.technology_terms, tightness="exact"
#   5. hidden_semantic_expansion.technology_terms, tightness="close"
#   ("broad" tightness is excluded from the default chain, matching
#   _bi_keyword_pool()'s existing broad-tier exclusion — only opted into
#   via _bi_technology_signal(include_broad=True), used by fit scoring.)
#
# NON-SEARCH fields (deliberately outside this chain — one different
# responsibility each, never search terms):
#   - competing_products    — Coverage Analysis suggestion candidate only
#                              (_bi_competing_products), plus fit-scoring.
#   - replacement_targets    — fit-scoring signal only.
#   - technology_categories  — fit-scoring signal only (e.g. "CRM" matching
#                               a lead's broader tech stack).

def _bi_technology_precedence_tiers(icp: dict) -> list[tuple]:
    """The single source of truth for technology search precedence, as
    ordered (tier_name, terms) pairs, strongest signal first. Both
    _bi_technology_signal() (flattens every tier) and _bi_keyword_pool()
    (interleaves these same tiers, in this same relative order, with
    non-technology terms at specific priority points) read from here —
    never re-deriving these terms independently."""
    tech = _bi_technology(icp)
    return [
        ("confirmed", _safe_list(tech.get("confirmed_technologies"))),
        ("likely", _safe_list(tech.get("likely_technologies"))),
        ("keywords", _safe_list(tech.get("technology_keywords"))),
        ("hidden_exact", _bi_hidden_expansion_terms(icp, "technology_terms", ("exact",))),
        ("hidden_close", _bi_hidden_expansion_terms(icp, "technology_terms", ("close",))),
    ]


def _bi_technology_signal(icp: dict, include_broad: bool = False) -> list[str]:
    """Canonical, precedence-ordered flat list of technology search terms —
    see _bi_technology_precedence_tiers() for the tier definitions this
    flattens. include_broad=True additionally appends the broad-tightness
    hidden-expansion tier (used only by _bi_all_technologies()'s fit
    scoring, which wants maximum recall; search adapters default to False,
    matching the existing curated-first discipline)."""
    terms = [t for _, tier in _bi_technology_precedence_tiers(icp) for t in tier]
    if include_broad:
        terms += _bi_hidden_expansion_terms(icp, "technology_terms", ("broad",))
    return _dedupe_list(terms)


def _bi_all_technologies(icp: dict) -> list[str]:
    """FIT SCORING pool (pl.build_icp_fit_scorer()'s bag-of-words match) — a
    deliberately broader, more permissive pool than _bi_keyword_pool()'s
    literal search-provider filter terms: the full technology search-
    precedence chain (_bi_technology_signal(), including the broad hidden-
    expansion tier) plus the 3 fields that exist ONLY for scoring —
    competing_products, replacement_targets, technology_categories — which
    are never search terms and never enter _bi_keyword_pool(). See the
    module comment above _bi_technology_precedence_tiers() for the full
    per-field responsibility map. Consumed as an unordered bag-of-words
    (pl.build_icp_fit_scorer() does `set.update(...)` on it), so internal
    ordering here carries no behavioral meaning."""
    tech = _bi_technology(icp)
    flat = _bi_technology_signal(icp, include_broad=True)
    for key in ("competing_products", "replacement_targets", "technology_categories"):
        flat.extend(_safe_list(tech.get(key)))
    return _dedupe_list(flat)


def _bi_primary_technology(icp: dict) -> str:
    """Returns the single most distinctive named technology from the ICP,
    for pairing with job-title searches — the head of the canonical
    technology search-precedence chain (_bi_technology_signal()), so this
    can never drift out of sync with the multi-value chain other consumers
    use (this used to hand-roll its own 3-field precedence, independently
    of _bi_keyword_pool()'s — now both resolve through the same source)."""
    signal = _bi_technology_signal(icp)
    return _clean_search_term(signal[0]) if signal else ""


def _bi_keyword_pool(icp: dict) -> list[str]:
    """The canonical keyword aggregator shared by every provider adapter that
    needs a flat search-term list. Curated/specific terms come first,
    generic/broad ones last — order matters, since capped downstream fields
    (e.g. Apollo's max_tags) truncate this list, and a live-tested production
    bug this session was exactly generic technographics (Salesforce, SAP…)
    crowding out real niche terms in a capped array. industry terms,
    explicitly confirmed technologies, and search_intelligence's curated
    business/product keywords all win the budget first; likely_technologies
    and technology_keywords are Gemini's own generic inference (the modern
    equivalent of the old flattened technographics block) and are
    deliberately last, exactly like the original fix.

    Technology terms here (confirmed/likely/keywords/hidden-exact/hidden-
    close) are pulled from _bi_technology_precedence_tiers() — the same
    single source of truth _bi_primary_technology() and
    _bi_all_technologies() use — interleaved at the same relative priority
    positions among the non-technology terms, unchanged from before."""
    industry = _bi_industry(icp)
    search = _bi_search(icp)
    tech_tiers = dict(_bi_technology_precedence_tiers(icp))
    raw = []
    primary = industry.get("primary_industry")
    if primary:
        raw.append(str(primary).strip())
    raw.extend(_safe_list(industry.get("sub_industries")))
    raw.extend(_safe_list(industry.get("business_variations")))
    raw.extend(tech_tiers["confirmed"])
    raw.extend(_safe_list(industry.get("industry_keywords")))
    raw.extend(_safe_list(search.get("business_keywords")))
    raw.extend(_safe_list(search.get("product_keywords")))
    raw.extend(tech_tiers["likely"])
    raw.extend(tech_tiers["keywords"])
    # hidden_semantic_expansion — lowest-priority tail. "exact" tier before
    # "close" across concepts, preserving the same curated-first discipline
    # the original crowding-out fix established; "broad" is deliberately
    # never auto-pooled here (reserved for a future low-result fallback
    # path, not blindly mixed into every search today).
    raw.extend(_bi_hidden_expansion_terms(icp, "industry_terms", ("exact",)))
    raw.extend(tech_tiers["hidden_exact"])
    raw.extend(_bi_hidden_expansion_terms(icp, "description_phrases", ("exact",)))
    raw.extend(_bi_hidden_expansion_terms(icp, "industry_terms", ("close",)))
    raw.extend(tech_tiers["hidden_close"])
    raw.extend(_bi_hidden_expansion_terms(icp, "description_phrases", ("close",)))
    return _dedupe_list([t for t in raw if t])


def _bi_negative_keywords(icp: dict) -> list[str]:
    """Flattens the ICP's exclusion signals into one list of short literal
    terms — replaces the old prose-heavy top-level exclusion_criteria."""
    flat = (
        _safe_list(_bi_industry(icp).get("exclude_industries"))
        + _safe_list(_bi_geography(icp).get("excluded_locations"))
        + _safe_list(_bi_search(icp).get("negative_keywords"))
    )
    return _dedupe_list(flat)


def _to_number(v):
    """Best-effort coercion of a Gemini-derived numeric field (which the
    prompt asks for as a plain number, but isn't schema-enforced, so it
    can arrive as a string like "50" or "$50M") into a float, or None if
    it can't be parsed. Every downstream consumer of company size/revenue
    routes through _bi_size_range/_bi_revenue_range below, so coercing
    once here — instead of at each of the ~6 call sites that used to do
    raw int()/float() on these values — closes the whole bug class at
    the source."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _bi_size_range(icp: dict) -> tuple:
    company = _bi_company(icp)
    return _to_number(company.get("company_size_min")), _to_number(company.get("company_size_max"))


def _bi_revenue_range(icp: dict) -> tuple:
    company = _bi_company(icp)
    return _to_number(company.get("revenue_min")), _to_number(company.get("revenue_max"))


def _bi_company_stage(icp: dict) -> list[str]:
    return _safe_list(_bi_company(icp).get("company_stage"))


def _bi_lead_scoring(icp: dict) -> list[dict]:
    return icp.get("lead_scoring") or []


def _bi_adjacent_industries(icp: dict) -> list[str]:
    """Related industries Gemini already generates for coverage but that no
    adapter reads today — the natural candidate list for Coverage Analysis
    suggestions (see pl._generate_coverage_suggestions())."""
    return _safe_list(_bi_industry(icp).get("adjacent_industries"))


def _bi_competing_products(icp: dict) -> list[str]:
    """Alternatives to the confirmed/likely technology — Coverage Analysis's
    suggestion-candidate list. Deliberately outside the technology search-
    precedence chain (_bi_technology_precedence_tiers()) — never a search
    term, only ever a suggestion candidate (plus a fit-scoring signal via
    _bi_all_technologies())."""
    return _safe_list(_bi_technology(icp).get("competing_products"))


def _clean_search_term(term: str) -> str:
    """Strips a term down to something safe to use as a literal quoted Google
    search phrase. Gemini sometimes returns compound/descriptive strings for
    a single technographics entry (e.g. "Duck Creek (Policy, Billing,
    Claims)" instead of just "Duck Creek") — live-tested, and a parenthetical
    like that appearing verbatim on a real webpage is vanishingly unlikely,
    which starves search results. Cuts at the first parenthesis or comma and
    trims whitespace, leaving already-clean terms (e.g. "Duck Creek
    Technologies") untouched."""
    term = re.split(r"[(,]", term)[0].strip()
    return term


def _revenue_range_keywords(revenue_min, revenue_max) -> list[str]:
    """Converts a numeric revenue_min/max pair into human-readable bucket
    words (dollar-figure labels + a coarse tier label) so revenue can
    participate in pl.build_icp_fit_scorer()'s keyword matching the same way
    industry/technographics do. No provider API can hard-filter on revenue,
    and no scraped lead carries a literal revenue figure, so this is a
    best-effort soft signal, not a precise filter."""
    lo = _to_number(revenue_min)
    hi = _to_number(revenue_max)
    if lo is None and hi is None:
        return []

    def _fmt(n: float) -> str:
        if n >= 1_000_000_000:
            return f"${n / 1_000_000_000:.0f}B"
        if n >= 1_000_000:
            return f"${n / 1_000_000:.0f}M"
        if n >= 1_000:
            return f"${n / 1_000:.0f}K"
        return f"${n:.0f}"

    labels = [_fmt(v) for v in (lo, hi) if v is not None]

    tier_basis = hi if hi is not None else lo
    if tier_basis < 1_000_000:
        labels.append("early-revenue")
    elif tier_basis < 10_000_000:
        labels.append("smb")
    elif tier_basis < 100_000_000:
        labels.append("growth")
    else:
        labels.append("enterprise")

    return labels


def _trim_keyword_tag(term: str, max_words: int = 2) -> str:
    """Trims a term to its first `max_words` significant words for use as an
    Apollo q_organization_keyword_tags entry. Live-tested against the real
    api_search endpoint: each tag is matched as a near-exact phrase against
    an org's profile/tag data, not a fuzzy search — "medical bed
    manufacturer" (3 words, a descriptive phrase Gemini invented) returned 0
    matches, while "medical beds" (2 words) returned 47 and single words
    returned thousands. Recognized short entity names (e.g. "Duck Creek
    Technologies") can still match at 3 words, but there's no reliable way
    to tell those apart from invented descriptive phrases up front, so
    capping at 2 words is the safe default that avoids silently zeroing out
    results for narrow/niche ICPs. Drops bare connector tokens like "&"."""
    term = _clean_search_term(term)
    words = [w for w in term.split() if w != "&"]
    return " ".join(words[:max_words])


def _build_apollo_keyword_tags(icp: dict, max_tags: int = 15) -> list[str]:
    """Builds Apollo's q_organization_keyword_tags array. This field is an
    OR of independently-matched short tags — unlike q_keywords (a single
    free-text string live-tested to AND-match every word it contains, which
    zeroes out results for any ICP whose keywords don't appear verbatim
    together as one long phrase on a company's profile). Pulls the raw term
    list from _bi_keyword_pool() (already curated-first-priority-ordered —
    see that function's docstring), then applies Apollo-specific mechanics
    on top: each term trimmed to 2 words (_trim_keyword_tag() — live-tested,
    Apollo near-exact-phrase-matches each tag and 3+-word invented phrases
    usually return 0 matches) and capped at max_tags=15 (live-tested safe up
    to 19 with no errors)."""
    raw = _bi_keyword_pool(icp)
    tags = [_trim_keyword_tag(t) for t in raw if t]
    tags = [t for t in tags if t]
    return _dedupe_list(tags)[:max_tags]


# ─────────────────────────────────────────────
# Stage 2b — Apify Scraping
# ─────────────────────────────────────────────


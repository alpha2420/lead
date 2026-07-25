import re
from typing import Optional

import pipeline as pl


# ─────────────────────────────────────────────
# Generic list/string/number utilities
# ─────────────────────────────────────────────
# Shared by every provider adapter and scorer in this package — not specific
# to any one lead-sourcing platform.

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


def _to_number(v):
    """Best-effort coercion of a Gemini-derived numeric field (which the
    prompt asks for as a plain number, but isn't schema-enforced, so it
    can arrive as a string like "50" or "$50M") into a float, or None if
    it can't be parsed. Every downstream consumer of company size/revenue
    routes through _bi_size_range/_bi_revenue_range below, so coercing
    once here — instead of at each call site that used to do raw
    int()/float() on these values — closes the whole bug class at
    the source."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _clean_search_term(term: str) -> str:
    """Strips a term down to something safe to use as a literal quoted search
    phrase. Gemini sometimes returns compound/descriptive strings for a
    single technographics entry (e.g. "Duck Creek (Policy, Billing, Claims)"
    instead of just "Duck Creek") — a parenthetical like that appearing
    verbatim on a real webpage is vanishingly unlikely, which starves search
    results. Cuts at the first parenthesis or comma and trims whitespace,
    leaving already-clean terms (e.g. "Duck Creek Technologies") untouched."""
    term = re.split(r"[(,]", term)[0].strip()
    return term


def _clean_geography_list(geography) -> list[str]:
    """Strips parenthetical qualifiers from geography values before they're
    used as literal location filter values. Gemini sometimes describes a
    region descriptively rather than naming a real place a provider
    recognizes (e.g. "United States (Eastern Time Zone cities)", "United
    States (EST Zone)") — live-tested against Apollo, and that exact string
    returned 0 matches even though the country name alone (after stripping
    the parenthetical) matches hundreds of thousands of real profiles. This
    is especially damaging when it's the *only* geography value, since it
    zeroes out the whole query rather than just narrowing it.

    Also runs pl.expand_timezone_labels() on the result — a deterministic
    code-level backstop for a value that's *itself* a bare timezone label
    with no parenthetical qualifier to strip (e.g. a lone "US Eastern
    Time Zone" entry), on top of the ICP prompt's own instruction to
    expand these at generation time. Applied after stripping, not before,
    so a value like "United States (Eastern Time Zone cities)" still
    resolves to the broader, already-correct "United States" rather than
    being narrowed to just the Eastern-zone states — the parenthetical
    there is descriptive context on a country-level entry, not itself a
    request to search only that region."""
    stripped = [c for c in (re.split(r"[(]", loc)[0].strip() for loc in _safe_list(geography)) if c]
    expanded = pl.expand_timezone_labels(stripped)
    return _dedupe_list(expanded)


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
    filter (Apify's leads-finder actor, etc.) expects. Deliberately excludes
    `regions` (loose labels like "North America" aren't real filter values —
    the prompt rules require regions to always be expanded into real
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
    ICP-generation time with no live search data behind it. A list of dicts,
    not strings, so _safe_list() (string-only) doesn't apply here."""
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
# and every provider adapter (all of which call one of those three
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
#   - competing_products    — fit-scoring signal only.
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
    use."""
    signal = _bi_technology_signal(icp)
    return _clean_search_term(signal[0]) if signal else ""


def _bi_keyword_pool(icp: dict) -> list[str]:
    """The canonical keyword aggregator shared by every provider adapter that
    needs a flat search-term list. Curated/specific terms come first,
    generic/broad ones last — order matters, since capped downstream fields
    truncate this list, and a live-tested production bug was exactly generic
    technographics (Salesforce, SAP…) crowding out real niche terms in a
    capped array. industry terms, explicitly confirmed technologies, and
    search_intelligence's curated business/product keywords all win the
    budget first; likely_technologies and technology_keywords are Gemini's
    own generic inference and are deliberately last, exactly like the
    original fix.

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
    adapter reads today."""
    return _safe_list(_bi_industry(icp).get("adjacent_industries"))


def _bi_competing_products(icp: dict) -> list[str]:
    """Alternatives to the confirmed/likely technology — a fit-scoring
    signal via _bi_all_technologies(). Deliberately outside the technology
    search-precedence chain (_bi_technology_precedence_tiers()) — never a
    search term."""
    return _safe_list(_bi_technology(icp).get("competing_products"))

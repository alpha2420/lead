from typing import Optional
import pipeline as pl


_DEFAULT_PROFILE = "balanced"

# Each profile bundles a per-page Apify max_leads limit plus how many
# rule-validated companies get the paid AI fact-check (Company Validation
# Tier B) — see pl.validate_companies(). Companies beyond that cap stay
# validated (rule-only), never dropped. Apify is the sole lead-sourcing
# platform this pipeline uses.
_SOURCE_PROFILES = {
    "cost_conscious": {
        "source_limits": {"apify": {"max_leads": 25}},
        "max_ai_validate_companies": 15,
    },
    "balanced": {
        "source_limits": {"apify": {"max_leads": 50}},
        "max_ai_validate_companies": 40,
    },
    "maximum_coverage": {
        "source_limits": {"apify": {"max_leads": 50}},
        "max_ai_validate_companies": 100,
    },
}


def _call_source(
    source: str, icp: dict, profile: dict,
    validated_companies: Optional[list[dict]] = None,
    search_plan: Optional[dict] = None,
) -> list[dict]:
    """Dispatches to the named source's adapter with that profile's configured
    max_leads. Never raises — the adapter already returns [] gracefully on
    any failure.

    validated_companies: when People Discovery is running company-first
    (post Company Discovery/Validation), Apify's leads-finder actor has no
    company-name scoping filter, so results get post-filtered afterward
    instead (pl._filter_leads_by_validated_companies).

    search_plan: Stage 2's output (pipeline/search_planner.py) — threaded
    straight through to pl.scrape_apify(); see that function and
    pl._build_leads_finder_input() for how it's used."""
    if source != "apify":
        return []
    max_leads = profile["source_limits"].get(source, {}).get("max_leads", 25)
    leads = pl.scrape_apify(icp, max_leads=max_leads, search_plan=search_plan)

    if validated_companies:
        leads = pl._filter_leads_by_validated_companies(leads, validated_companies)
    return leads


def run_lead_sources(
    icp: dict,
    page: int,
    profile: str = _DEFAULT_PROFILE,
    validated_companies: Optional[list[dict]] = None,
    search_plan: Optional[dict] = None,
) -> dict:
    """
    Runs one page's worth of Apify sourcing according to the named profile.
    Returns {"leads": [...], "counts": {"apify": n}}.

    Apify never fires when page != 1 — its "expensive, run once" nature is
    inherent to the adapter (it has no page param).

    validated_companies: People Discovery stage output from
    pl.discover_and_validate_companies() — when supplied, this becomes a
    company-scoped search instead of an open ICP-wide search. None preserves
    the original people-first behavior exactly.

    search_plan: Stage 2's output (pipeline/search_planner.py) — None
    preserves the original ICP-only mapping in pl._build_leads_finder_input()
    exactly.
    """
    prof = _SOURCE_PROFILES.get(profile, _SOURCE_PROFILES[_DEFAULT_PROFILE])
    counts = {"apify": 0}
    if page != 1:
        return {"leads": [], "counts": counts}

    try:
        batch = _call_source("apify", icp, prof, validated_companies, search_plan)
    except Exception as e:
        # A malformed ICP field (e.g. a non-numeric company_size/revenue
        # value that slipped past coercion) must not kill the whole run.
        pl.log.error("Source Orchestrator — apify raised: %s", e)
        batch = []
    counts["apify"] = len(batch)
    return {"leads": batch, "counts": counts}


# ─────────────────────────────────────────────
# Stage 3/4 — Company Discovery + Company Validation (company-first search)
# ─────────────────────────────────────────────
# Runs before run_lead_sources() when the caller wants a company-first
# search: find candidate companies matching the ICP's firmographics first,
# validate they actually fit, then scope People Discovery
# (run_lead_sources(..., validated_companies=...)) to exactly that list.
#
# No provider in this codebase has a working company-search API — Apollo's
# real company-search endpoint (mixed_companies/search) returned 422
# "insufficient credits" during design, Explorium had no company/business
# endpoint at all, and no Apify actor does structured ICP-driven company
# discovery. Both Apollo and Explorium have since been dropped as data
# sources entirely (see pl.run_lead_sources above — Apify only). Discovery
# below runs on Gemini + live web search instead.


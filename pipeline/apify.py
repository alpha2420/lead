import os
import json
import time
import math
import re
import requests
from typing import Optional
import pipeline as pl


# code_crafter/leads-finder is a structured B2B lead database — it takes real
# filters and returns real firmographic data directly (industry,
# company_description, company_technologies, etc.), so no Gemini guessing
# step is needed at all.

# code_crafter/leads-finder validates several fields against fixed enums that
# its documentation page does NOT accurately describe — discovered by
# live-testing against the actor's real input validation. Kept as
# module-level constants since there's no way to fetch them dynamically
# without an API call, and they change rarely.

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


_LEADS_FINDER_COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states",
    "uk": "united kingdom", "uae": "united arab emirates",
}


def _map_locations_to_leads_finder(geography: list[str]) -> list[str]:
    """Expands broad regions ("North America", "Europe", "APAC") into the
    actor's country enum, reformats recognized US states into the actor's
    "<state>, us" sub-country location shape, and passes recognized country
    names through (normalizing common aliases like "usa"/"uk" to the
    actor's actual enum spelling).

    Live-tested: contact_location supports state/city granularity, but ONLY
    in that exact "<region>, <country>" format (e.g. "california, us") — a
    bare state name like "alabama" is rejected outright, and since the
    actor validates the whole request atomically, ONE bad value 400s the
    ENTIRE call, not just that one location (confirmed: a real run sending
    "united states" plus all 50 bare state names failed completely on
    "alabama", the first non-country value). Anything not confidently
    mappable to the actor's enum is therefore dropped rather than guessed —
    sending fewer, valid locations beats a single bad one zeroing out every
    other filter in the request too."""
    mapped = []
    for g in geography:
        g_lower = g.strip().lower()
        if g_lower in _LEADS_FINDER_REGION_COUNTRIES:
            mapped.extend(_LEADS_FINDER_REGION_COUNTRIES[g_lower])
        elif g_lower in pl._KNOWN_US_STATES:
            mapped.append(f"{g_lower}, us")
        elif g_lower in _LEADS_FINDER_COUNTRY_ALIASES:
            mapped.append(_LEADS_FINDER_COUNTRY_ALIASES[g_lower])
        elif g_lower in pl._KNOWN_COUNTRIES:
            mapped.append(g_lower)
        # else: not confidently mappable (an unrecognized state/city/country)
        # — dropped rather than sent as a guess, see docstring above.
    return pl._dedupe_list(mapped)


def _nearest_leads_finder_revenue_bucket(value: float) -> str:
    """The actor's min_revenue/max_revenue only accept fixed bucket labels
    (e.g. "100M"), not raw numbers — picks the closest one to the ICP's
    actual figure."""
    return min(_LEADS_FINDER_REVENUE_BUCKETS, key=lambda b: abs(b[1] - value))[0]


def _build_leads_finder_input(icp: dict, max_leads: int, search_plan: Optional[dict] = None) -> dict:
    """Maps the ICP to code_crafter/leads-finder's structured filter schema.
    Deliberately conservative: email_status is left unset (Stage 6 already
    re-verifies every lead's email regardless of source, so pre-filtering
    here has no correctness benefit and risks starving results) and
    company_stage values with no clean funding-stage equivalent (e.g.
    "Enterprise") are skipped rather than guessed.

    search_plan (pipeline/search_planner.py's Stage 2 output) — when
    supplied, replaces the industry-enum matching and the keyword
    selection below with the Planner's semantic classification / curated
    priority tiers, since both are known-weak points of the plain
    ICP->actor mapping (see search_planner.py's module docstring).
    search_plan=None preserves the exact previous behavior, unchanged, for
    every existing caller."""
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

    if search_plan and search_plan.get("industry_candidates"):
        # Search Planner already picked real, enum-verified values via
        # semantic classification (see search_planner.py) — use them
        # directly instead of the literal substring matcher below, which
        # can silently return nothing (or a wrong bucket) for a specific
        # or compound industry phrase.
        mapped_industries = [
            c["value"] for c in search_plan["industry_candidates"]
            if c.get("value") in _LEADS_FINDER_INDUSTRIES
        ]
    else:
        industry = pl._bi_industry(icp)
        industry_candidates = pl._safe_list(industry.get("sub_industries")) + pl._safe_list(industry.get("primary_industry"))
        mapped_industries = []
        for i in industry_candidates:
            mapped = _map_industry_to_leads_finder_enum(i)
            if mapped:
                mapped_industries.append(mapped)
    if mapped_industries:
        run_input["company_industry"] = pl._dedupe_list(mapped_industries)

    if search_plan and (search_plan.get("high_priority_keywords") or search_plan.get("secondary_keywords")):
        # Planner-curated, priority-ranked terms — not a blind first-5
        # truncation of the raw BI keyword pool (see search_planner.py's
        # module docstring for why that truncation loses specificity).
        keywords = pl._dedupe_list(
            list(search_plan.get("high_priority_keywords") or [])
            + list(search_plan.get("company_type_terms") or [])
            + list(search_plan.get("secondary_keywords") or [])
        )
    else:
        # The actor-native way to do what Claim Verification has to work around
        # via a separate web search: ask for the specific named technology
        # directly as a search-time filter, using the same cleaned/prioritized
        # term used elsewhere (confirmed_technologies over generic crm).
        keywords = []
        tech = pl._bi_primary_technology(icp)
        if tech:
            keywords.append(tech)
        keywords.extend(pl._bi_keyword_pool(icp)[:5])
    if keywords:
        run_input["company_keywords"] = pl._dedupe_list(keywords)

    # negative_keywords/exclude_industries/excluded_locations (from the ICP
    # directly, or from search_plan["negative_keywords"] — see
    # search_planner.py) are deliberately never sent to the actor as a
    # request field: this actor's own company_not_keywords semantics have
    # never been live-validated for this schema, and one bad field 400s
    # the entire atomic request. Applied instead as a client-side post-
    # filter on already-returned leads — see _apply_negative_keywords()
    # below, called from scrape_apify().

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
    lead schema — the actor already returns real fields, no guessing step
    needed."""
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


def _apply_negative_keywords(leads: list[dict], negative_keywords: list[str]) -> list[dict]:
    """Client-side post-filter for search_plan["negative_keywords"] (see
    pipeline/search_planner.py) — deliberately never sent to the actor as
    a request field (see the negative_keywords comment in
    _build_leads_finder_input() above). Drops a lead if any negative term
    appears in its company/title/description/keyword text."""
    terms = [t.strip().lower() for t in (negative_keywords or []) if t and t.strip()]
    if not terms:
        return leads

    kept = []
    for lead in leads:
        text = " ".join(
            str(lead.get(f) or "") for f in ("company", "title", "biz_description", "biz_category")
        ).lower()
        if any(term in text for term in terms):
            continue
        kept.append(lead)
    return kept


def scrape_apify(icp: dict, max_leads: int = 50, search_plan: Optional[dict] = None) -> list[dict]:
    """
    Runs the code_crafter/leads-finder Apify actor with the ICP criteria —
    the sole lead-sourcing platform this pipeline uses.

    Apify docs: https://docs.apify.com/api/v2#/reference/actors/run-collection/run-actor
    Endpoint: POST https://api.apify.com/v2/acts/{actorId}/runs

    search_plan: Stage 2's output (pipeline/search_planner.py) — see
    _build_leads_finder_input() for how it changes the request, and
    _apply_negative_keywords() for how its negative_keywords get applied
    to the response instead. None preserves prior behavior exactly.

    Returns a list of raw lead dicts.
    """
    api_token = os.getenv("APIFY_API_TOKEN")
    actor_id = os.getenv("APIFY_ACTOR_ID", "code_crafter/leads-finder")

    pl.log.info("Stage 2 — Running Apify actor '%s' …", actor_id)

    if not api_token:
        pl.log.warning("APIFY_API_TOKEN not set — skipping Apify scrape.")
        return []

    run_input = _build_leads_finder_input(icp, max_leads, search_plan=search_plan)
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
    # higher max_leads request can legitimately take 2-3+ minutes) — the run
    # keeps going server-side, but its dataset may still be empty/partial at
    # this exact moment. Silently trusting that as "the run returned 0
    # leads" would be misleading and hard to debug later, so poll the run's
    # real status a bit longer rather than assuming it's done.
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

    leads = _parse_leads_finder_results(items)
    pl.log.info("Apify (leads-finder) returned %d leads.", len(leads))

    if search_plan and search_plan.get("negative_keywords"):
        before = len(leads)
        leads = _apply_negative_keywords(leads, search_plan["negative_keywords"])
        if len(leads) != before:
            pl.log.info(
                "Search Planner negative keywords filtered out %d/%d leads.",
                before - len(leads), before,
            )

    return leads

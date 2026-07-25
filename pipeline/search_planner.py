"""Stage 2 — Search Planner.

code_crafter/leads-finder is NOT Apollo: Apollo's actual company-search
endpoint understood loose structured filters reasonably well, but
leads-finder's `company_industry` is a fixed ~130-value enum and its
`company_keywords` is a flat literal-term array — both need something
closer to a hand-tuned query than a mechanical BI-field dump. Two gaps in
that direct dump, both confirmed live against the real code:

  1. `apify._map_industry_to_leads_finder_enum()` requires a literal
     two-way substring match against the fixed enum. A correct, specific
     industry the ICP generates (e.g. "Hospital Bed Manufacturing")
     silently maps to nothing; a plausible-looking one ("Healthcare
     Furniture") can literal-match a wrong bucket ("furniture").
  2. `apify._build_leads_finder_input()` truncates the BI keyword pool to
     its first 5 terms — there's no notion of which terms actually
     distinguish this target from an adjacent-but-wrong one.

This stage runs one Gemini call per ICP between Stage 1 (ICP parsing) and
Stage 3/4 (Company Discovery / People Search), handed the actor's real
enum list and the full (uncapped) BI keyword pool, and asks it to do what
an LLM is actually good at — semantic classification into a fixed label
set, and judgment-based term prioritization — instead of leaving that to
apify.py's mechanical substring/truncation logic. apify.py still owns
every other field mapping (titles, seniority, location, size, revenue,
funding) unchanged; this only replaces the industry + keyword decisions,
and only when a plan is supplied (search_plan=None preserves the exact
previous behavior for every existing caller/test).

negative_keywords are deliberately never sent to the actor — apify.py's
own _build_leads_finder_input() comment already notes the actor's
negative-keyword field has never been live-validated against this
schema, and the actor validates its whole request atomically (one bad
field 400s everything, not just that filter). They're applied instead as
a client-side post-fetch filter on already-returned leads
(apify._apply_negative_keywords()) — same intent, zero validation risk.

Planner History (bottom of this file) closes the loop: every run logs
which industry enum value(s) it actually used against that run's real
outcome (composite score, sample fill rate), and future prompts see each
candidate's historical success rate alongside Gemini's own semantic
judgment — e.g. "medical devices" earning real leads 95% of the time vs.
"machinery" only 18%, not just which one sounds like a better fit. This
makes the mapping empirically self-correcting over time instead of
relying purely on a fresh semantic guess every run. Cold-start: with no
history yet, get_industry_success_rates() returns {} and the prompt
section is simply omitted — behavior is identical to before History
existed until enough runs accumulate (_MIN_HISTORY_RUNS_FOR_SIGNAL).
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import google.genai as genai

import pipeline as pl

_SEARCH_PLAN_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "search_plan_cache.db")
_SEARCH_PLAN_CACHE_TTL_DAYS = 7   # a judgment call over a fixed enum, not a stable fact —
                                   # same reasoning as Company Discovery's 7-day cache.

_MAX_INDUSTRY_CANDIDATES = 3
_MAX_HIGH_PRIORITY_KEYWORDS = 12
_MAX_SECONDARY_KEYWORDS = 8
_MAX_NEGATIVE_KEYWORDS = 10
_MAX_COMPANY_TYPE_TERMS = 5


def _search_plan_cache_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_SEARCH_PLAN_CACHE_PATH), exist_ok=True)
    conn = sqlite3.connect(_SEARCH_PLAN_CACHE_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plans (cache_key TEXT PRIMARY KEY, response_json TEXT, fetched_at TEXT)"
    )
    return conn


def _get_cached_search_plan(cache_key: str) -> Optional[dict]:
    try:
        conn = _search_plan_cache_conn()
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM plans WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        response_json, fetched_at = row
        fetched = datetime.fromisoformat(fetched_at)
        if datetime.now() - fetched > timedelta(days=_SEARCH_PLAN_CACHE_TTL_DAYS):
            return None
        return json.loads(response_json)
    except Exception as e:
        pl.log.warning("Search plan cache read failed for %s: %s", cache_key, e)
        return None


def _cache_search_plan(cache_key: str, plan: dict) -> None:
    try:
        conn = _search_plan_cache_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO plans (cache_key, response_json, fetched_at) VALUES (?, ?, ?)",
                (cache_key, json.dumps(plan), datetime.now().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Search plan cache write failed for %s: %s", cache_key, e)


def _search_plan_cache_key(icp: dict) -> str:
    """Deterministic composite string (not hashed) — same convention as
    pl._company_discovery_cache_key(). Built from every field the prompt
    below actually reads, so a change to any of them correctly misses
    cache instead of silently reusing a stale plan for a different ICP."""
    industry = pl._bi_industry(icp)
    company = pl._bi_company(icp)
    industry_terms = "|".join(sorted(pl._dedupe_list(
        pl._safe_list(industry.get("primary_industry"))
        + pl._safe_list(industry.get("sub_industries"))
        + pl._safe_list(industry.get("business_variations"))
    )))
    keyword_pool = "|".join(pl._bi_keyword_pool(icp))
    company_type = "|".join(sorted(pl._safe_list(company.get("company_type"))))
    return f"{industry_terms}::{keyword_pool}::{company_type}"


def _fallback_search_plan(icp: dict) -> dict:
    """Deterministic, no-Gemini-call plan — used when GEMINI_API_KEY is
    unset or the real call fails, so a Search Planner outage degrades to
    (not worse than) the pre-Search-Planner behavior rather than breaking
    the run. Reuses apify.py's existing literal enum matcher and the BI
    keyword pool's existing priority order."""
    industry = pl._bi_industry(icp)
    company = pl._bi_company(icp)

    industry_candidates = []
    for text in pl._dedupe_list(
        pl._safe_list(industry.get("primary_industry")) + pl._safe_list(industry.get("sub_industries"))
    ):
        mapped = pl._map_industry_to_leads_finder_enum(text)
        if mapped and not any(c["value"] == mapped for c in industry_candidates):
            industry_candidates.append({"value": mapped, "confidence": 50.0})
        if len(industry_candidates) >= _MAX_INDUSTRY_CANDIDATES:
            break

    keyword_pool = pl._bi_keyword_pool(icp)
    return {
        "industry_candidates": industry_candidates,
        "high_priority_keywords": keyword_pool[:_MAX_HIGH_PRIORITY_KEYWORDS],
        "secondary_keywords": keyword_pool[_MAX_HIGH_PRIORITY_KEYWORDS:_MAX_HIGH_PRIORITY_KEYWORDS + _MAX_SECONDARY_KEYWORDS],
        "negative_keywords": pl._bi_negative_keywords(icp)[:_MAX_NEGATIVE_KEYWORDS],
        "company_type_terms": pl._dedupe_list(
            pl._safe_list(company.get("company_type")) + pl._safe_list(industry.get("business_variations"))
        )[:_MAX_COMPANY_TYPE_TERMS],
        "confidence": 40.0,
        "reasoning": "Rule-based fallback (no Gemini call) — reuses the existing literal industry-enum matcher and raw keyword-pool ordering.",
    }


def _search_planner_prompt(icp: dict) -> str:
    industry = pl._bi_industry(icp)
    company = pl._bi_company(icp)

    industry_candidates_text = pl._dedupe_list(
        pl._safe_list(industry.get("primary_industry"))
        + pl._safe_list(industry.get("sub_industries"))
        + pl._safe_list(industry.get("business_variations"))
    )
    keyword_pool = pl._bi_keyword_pool(icp)
    company_type_terms = pl._dedupe_list(
        pl._safe_list(company.get("company_type")) + pl._safe_list(industry.get("business_variations"))
    )
    enum_values = "\n".join(f"- {v}" for v in sorted(pl._LEADS_FINDER_INDUSTRIES))

    history = pl.get_industry_success_rates()
    history_block = ""
    if history:
        ranked = sorted(history.items(), key=lambda kv: kv[1]["success_rate"], reverse=True)
        history_lines = "\n".join(
            f'- "{value}": {stats["success_rate"]}% of past runs using this value produced a good outcome ({stats["runs"]} runs)'
            for value, stats in ranked
        )
        history_block = """

HISTORICAL PERFORMANCE (real outcomes from past runs of THIS system — not semantic guessing. A low rate means leads actually sourced under that industry value tended to bounce, under-fill the sample, or score poorly, regardless of how good a semantic fit it seemed. Weigh this alongside your own judgment — don't let it override an obviously correct match that simply has little history yet, but let it break ties or downrank a value with a poor track record):
""" + history_lines

    return """You are a B2B search strategist. A downstream lead-search actor (code_crafter/leads-finder) only accepts industries from ONE FIXED enum list (below), and only structured literal keyword filters — never free text. Your job is to translate a target customer profile into the best possible search plan GIVEN THOSE EXACT CONSTRAINTS — not to restate the profile again.

TARGET CUSTOMER PROFILE:
- Industry / company-type candidates: """ + json.dumps(industry_candidates_text) + """
- Full keyword pool (already priority-ordered, most specific/curated first): """ + json.dumps(keyword_pool) + """
- Company type / supply-chain role terms: """ + json.dumps(company_type_terms) + """
- Summary: """ + str(icp.get("icp_summary") or "") + """

THE ACTOR'S FIXED INDUSTRY ENUM (you may ONLY use values from this exact list, verbatim, character-for-character — never invent, combine, or slightly reword one):
""" + enum_values + history_block + """

TASK — return a JSON object with exactly these keys:
1. industry_candidates: up to """ + str(_MAX_INDUSTRY_CANDIDATES) + """ values FROM THE ENUM LIST ABOVE that plausibly fit, ranked best-first, each as {"value": "<exact enum string>", "confidence": 0-100}. If nothing in the enum is a reasonable fit, return an empty array — never force a bad match just to fill this in.
2. high_priority_keywords: up to """ + str(_MAX_HIGH_PRIORITY_KEYWORDS) + """ short, literal search terms that are SPECIFIC enough to distinguish this exact target from adjacent-but-wrong targets (e.g. for "hospital bed manufacturer", include "hospital bed manufacturer" and "ICU bed" — not just "hospital" or "beds" alone, which would also match hospitals themselves or unrelated furniture sellers). Draw from the keyword pool above; you may add an obviously-implied close variant it's missing, but don't invent unrelated terms.
3. secondary_keywords: up to """ + str(_MAX_SECONDARY_KEYWORDS) + """ broader/fallback terms, only useful if the high-priority terms under-return.
4. negative_keywords: up to """ + str(_MAX_NEGATIVE_KEYWORDS) + """ short literal terms/phrases whose presence in a result should EXCLUDE it — e.g. signals this is actually the end-customer rather than the target company type, a staffing/recruiting listing, a jobs posting, a used/rental/repair listing, etc. (These are applied to results after the fact, never sent to the actor as a request filter — so don't hold back for fear of them being too aggressive as a request-time filter.)
5. company_type_terms: which of the company-type/role terms above this target actually IS (e.g. manufacturer, OEM, distributor, wholesaler) — not the terms for its end-customers.
6. confidence: your own overall 0-100 confidence in this plan.
7. reasoning: one or two sentences on your key judgment calls (e.g. why you excluded an enum value that looked tempting).

Output ONLY the JSON object. No markdown, no preamble, no comments inside the JSON.
"""


def _sanitize_search_plan(raw: dict, fallback: dict) -> dict:
    """Defends against a malformed/partial Gemini response: every field
    falls back to the rule-based plan's value for that field independently
    (not an all-or-nothing reject), and industry_candidates values are
    re-validated against the real enum — a hallucinated value here would
    otherwise reach apify.py's company_industry filter unchecked."""
    if not isinstance(raw, dict):
        return fallback

    industry_candidates = []
    for c in raw.get("industry_candidates") or []:
        if not isinstance(c, dict):
            continue
        value = str(c.get("value") or "").strip().lower()
        if value in pl._LEADS_FINDER_INDUSTRIES and not any(x["value"] == value for x in industry_candidates):
            try:
                confidence = float(c.get("confidence", 50.0))
            except (TypeError, ValueError):
                confidence = 50.0
            industry_candidates.append({"value": value, "confidence": max(0.0, min(100.0, confidence))})
        if len(industry_candidates) >= _MAX_INDUSTRY_CANDIDATES:
            break
    if not industry_candidates:
        industry_candidates = fallback["industry_candidates"]

    def _str_list(key: str, cap: int, fallback_key: str = None) -> list[str]:
        values = raw.get(key)
        if not isinstance(values, list) or not values:
            return fallback.get(fallback_key or key, [])
        return pl._dedupe_list([str(v).strip() for v in values if str(v or "").strip()])[:cap]

    try:
        confidence = float(raw.get("confidence", fallback["confidence"]))
    except (TypeError, ValueError):
        confidence = fallback["confidence"]

    return {
        "industry_candidates": industry_candidates,
        "high_priority_keywords": _str_list("high_priority_keywords", _MAX_HIGH_PRIORITY_KEYWORDS),
        "secondary_keywords": _str_list("secondary_keywords", _MAX_SECONDARY_KEYWORDS),
        "negative_keywords": _str_list("negative_keywords", _MAX_NEGATIVE_KEYWORDS),
        "company_type_terms": _str_list("company_type_terms", _MAX_COMPANY_TYPE_TERMS),
        "confidence": max(0.0, min(100.0, confidence)),
        "reasoning": str(raw.get("reasoning") or "").strip(),
    }


def build_search_plan(icp: dict) -> dict:
    """
    Stage 2 — builds a search plan tailored to code_crafter/leads-finder's
    real constraints (see module docstring). Cached 7 days per unique
    (industry/keyword/company-type) ICP signature.

    Always returns a well-formed dict (never None/raises) — falls back to
    _fallback_search_plan() on a missing API key or any Gemini failure, so
    a Search Planner outage never breaks the run; callers get the same
    plan shape either way.
    """
    # Every cross-function call below goes through pl.NAME rather than the
    # bare local name — same discipline pipeline/config.py's module
    # docstring documents for the whole package, so
    # unittest.mock.patch("pipeline.NAME", ...) affects this function
    # regardless of it living in the same module as NAME.
    cache_key = pl._search_plan_cache_key(icp)
    cached = pl._get_cached_search_plan(cache_key)
    if cached is not None:
        pl.log.info("Stage 2 — Search Planner: using cached plan.")
        return cached

    fallback = pl._fallback_search_plan(icp)
    if not pl.GEMINI_API_KEY:
        pl.log.warning("GEMINI_API_KEY is not set — Search Planner falling back to rule-based plan.")
        return fallback

    pl.log.info("Stage 2 — Search Planner: building search plan …")
    try:
        client = genai.Client(api_key=pl.GEMINI_API_KEY)
        raw = pl.generate_json_with_retry(pl._search_planner_prompt(icp), client)
    except Exception as e:
        pl.log.warning("Search Planner Gemini call failed (%s) — falling back to rule-based plan.", e)
        return fallback

    plan = pl._sanitize_search_plan(raw, fallback)
    pl._cache_search_plan(cache_key, plan)
    pl.log.info(
        "Stage 2 — Search Planner: industries=%s, high_priority=%d keywords, negative=%d keywords, confidence=%.0f",
        [c["value"] for c in plan["industry_candidates"]],
        len(plan["high_priority_keywords"]), len(plan["negative_keywords"]), plan["confidence"],
    )
    return plan


# ─────────────────────────────────────────────
# Planner History — logs each run's real outcome against the industry
# value(s) its plan actually used, and aggregates it back into
# _search_planner_prompt() above (see module docstring). This is the
# substrate a future recall/precision Simulator would read from, rather
# than a live per-run probe against the actor.
# ─────────────────────────────────────────────

_SEARCH_PLAN_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "search_plan_history.db")

# A "good outcome" heuristic, not a tuned threshold — deliberately simple
# and adjustable once enough real runs exist to calibrate against. A run
# counts as successful for an industry value if the final sample both hit
# a reasonable quality bar (average composite score) AND actually filled
# close to the requested target (fill rate) — either one alone can be
# misleading (a tiny, high-scoring sample, or a full sample of mediocre
# leads, are both a worse outcome than this pair implies on its own).
_SUCCESS_MIN_COMPOSITE_SCORE = 70.0
_SUCCESS_MIN_FILL_RATE = 0.8

# Below this many logged runs for a given industry value, its "success
# rate" is one lucky/unlucky run away from being meaningless - excluded
# from the prompt entirely rather than shown with false confidence.
_MIN_HISTORY_RUNS_FOR_SIGNAL = 3


def _search_plan_history_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_SEARCH_PLAN_HISTORY_PATH), exist_ok=True)
    conn = sqlite3.connect(_SEARCH_PLAN_HISTORY_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outcomes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "recorded_at TEXT, "
        "industry_value TEXT, "
        "avg_composite_score REAL, "
        "sample_fill_rate REAL, "
        "sample_size INTEGER, "
        "target INTEGER"
        ")"
    )
    return conn


def _sample_outcome_metrics(sample: list[dict], target: int) -> tuple[float, float]:
    """Pure helper — (avg_composite_score, sample_fill_rate) for one run's
    final sample. Split out from record_search_plan_outcome() so the
    actual metric definition is directly unit-testable without sqlite."""
    if not sample:
        return 0.0, 0.0
    scores = [l.get("_composite_score") for l in sample if isinstance(l.get("_composite_score"), (int, float))]
    avg_composite = sum(scores) / len(scores) if scores else 0.0
    fill_rate = min(1.0, len(sample) / target) if target else 0.0
    return avg_composite, fill_rate


def record_search_plan_outcome(search_plan: dict, sample: list[dict], target: int) -> None:
    """Logs one history row per industry candidate this run's plan
    actually used (not just the top pick — every candidate apify.py sent
    shares credit/blame for the run's real outcome, since the actor was
    given all of them together). Called once, at the end of a completed
    run (see app/pipeline_runner.py) — never raises, since a logging
    failure must not affect the run it's describing."""
    if not search_plan or not search_plan.get("industry_candidates"):
        return

    avg_composite, fill_rate = _sample_outcome_metrics(sample, target)
    try:
        conn = _search_plan_history_conn()
        try:
            for candidate in search_plan["industry_candidates"]:
                value = candidate.get("value")
                if not value:
                    continue
                conn.execute(
                    "INSERT INTO outcomes (recorded_at, industry_value, avg_composite_score, sample_fill_rate, sample_size, target) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(), value, avg_composite, fill_rate, len(sample), target),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Search plan history logging failed: %s", e)


def _compute_success_rates(rows: list[tuple]) -> dict:
    """Pure aggregation over (industry_value, avg_composite_score,
    sample_fill_rate) rows — split out from get_industry_success_rates()
    so the actual success-rate definition is directly unit-testable
    without sqlite. Returns {industry_value: {"runs": n, "success_rate": 0-100}},
    omitting any value with fewer than _MIN_HISTORY_RUNS_FOR_SIGNAL rows."""
    by_industry: dict = {}
    for value, composite, fill_rate in rows:
        by_industry.setdefault(value, []).append((composite, fill_rate))

    result = {}
    for value, outcomes in by_industry.items():
        if len(outcomes) < _MIN_HISTORY_RUNS_FOR_SIGNAL:
            continue
        successes = sum(
            1 for composite, fill_rate in outcomes
            if composite >= _SUCCESS_MIN_COMPOSITE_SCORE and fill_rate >= _SUCCESS_MIN_FILL_RATE
        )
        result[value] = {
            "runs": len(outcomes),
            "success_rate": round(successes / len(outcomes) * 100, 1),
        }
    return result


def get_industry_success_rates() -> dict:
    """Reads every logged outcome and returns per-industry historical
    success rates (see _compute_success_rates()) — the data
    _search_planner_prompt() folds back in as real evidence. Returns {}
    (never raises) on any read failure or empty history, so "no history
    yet" is a normal, expected state the prompt already handles by simply
    omitting that section."""
    try:
        conn = _search_plan_history_conn()
        try:
            rows = conn.execute(
                "SELECT industry_value, avg_composite_score, sample_fill_rate FROM outcomes"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        pl.log.warning("Search plan history read failed: %s", e)
        return {}
    return _compute_success_rates(rows)

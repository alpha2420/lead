"""Stage 12 — AI reranking of the final, already-verified lead sample.
One batched Gemini call over the small (~20-25 lead) final sample —
never the raw scraped pool — judging holistic fit in a way keyword
overlap can't (e.g. a title that keyword-matches "Director" but is
"Director of Facilities", or a company whose description reveals it's
the excluded business type despite matching every literal filter).

Runs after claim verification and org enrichment (the richest evidence
the sample will ever have), not right after select_sample() — an earlier
pass would be judging leads before their strongest signals exist.

Deliberately reorder-only: never drops a lead from the sample, mirroring
scoring.py::select_sample()'s min_composite_score philosophy ("warn, don't
shrink the sample silently") — a visible quality signal beats a silently
smaller deliverable, and dropping leads here would risk under-filling the
sample right after the expensive AI call that was supposed to improve it."""

import json

import google.genai as genai

import pipeline as pl

# Fields sent to Gemini for reranking — deliberately narrow: enough
# context to judge fit, nothing that isn't already a derived/summarized
# field (no raw scraped page text, no email addresses, no phone numbers).
_RERANK_LEAD_FIELDS = (
    "name", "title", "company", "industry", "biz_description", "technology",
    "location", "employee_count",
)


def _rerank_prompt(sample: list[dict], icp: dict) -> str:
    icp_summary = icp.get("icp_summary") or ""
    leads_for_prompt = [
        {"index": i, **{f: lead.get(f, "") for f in _RERANK_LEAD_FIELDS}}
        for i, lead in enumerate(sample)
    ]
    return """You are a B2B lead-qualification analyst doing a final holistic review of an already-verified list of leads.

Target customer profile:
""" + icp_summary + """

For each lead below (identified by "index"), assess how well it actually fits the target profile — catching things a literal keyword match would miss, e.g. a title that superficially matches a target word but is a different role ("Director of Facilities" vs. a target of "Director of Sales"), or a company description that reveals it's actually outside the target (e.g. an agency/vendor rather than the target end-customer type).

Return ONLY a strict JSON object mapping each lead's index (as a string) to {"score": 0-100, "rationale": "one short sentence"}. Do not omit any index. No markdown, no preamble.

Leads:
""" + json.dumps(leads_for_prompt, indent=2)


def rerank_final_sample(sample: list[dict], icp: dict) -> list[dict]:
    """
    Attaches _rerank_score (0-100 or None) and _rerank_rationale to every
    lead in `sample`, then re-sorts by _rerank_score (leads Gemini
    couldn't/didn't score sink to the bottom rather than being dropped).
    Never overwrites _composite_score/_icp_fit_score — this is a
    supplementary holistic judgment shown alongside the existing
    deterministic breakdown, not a replacement for it. Never changes
    sample membership.

    Graceful no-op (original order, no new fields written) on a missing
    API key or any Gemini failure — a reranking failure should never
    break a run that got this far.
    """
    if not sample:
        return sample
    if not pl.GEMINI_API_KEY:
        pl.log.warning("GEMINI_API_KEY is not set. Skipping AI reranking.")
        return sample

    try:
        client = genai.Client(api_key=pl.GEMINI_API_KEY)
        parsed = pl.generate_json_with_retry(_rerank_prompt(sample, icp), client)
    except Exception as e:
        pl.log.warning("AI reranking failed, keeping original order: %s", e)
        return sample

    if not isinstance(parsed, dict):
        pl.log.warning("AI reranking returned an unexpected shape, keeping original order.")
        return sample

    for i, lead in enumerate(sample):
        verdict = parsed.get(str(i)) or {}
        score = verdict.get("score")
        lead["_rerank_score"] = float(score) if isinstance(score, (int, float)) else None
        lead["_rerank_rationale"] = str(verdict.get("rationale") or "")

    sample.sort(key=lambda l: l["_rerank_score"] if l["_rerank_score"] is not None else -1.0, reverse=True)

    pl.log.info("Stage 12 — AI reranking complete for %d leads.", len(sample))
    return sample

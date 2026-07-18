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


_FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "protonmail.com", "live.com", "msn.com", "gmx.com",
}


def domain_match_signal(email: str, company_domain: str) -> dict:
    """
    Compares an email's domain against the lead's company website domain.

    Returns {"signal": "EXACT" | "PERSONAL" | "MISMATCH" | "UNKNOWN", "score": float}

      EXACT    — email domain matches the company's domain (100)
      PERSONAL — email is on a free/personal provider, not company-specific (55)
      MISMATCH — email domain differs from the company's known domain — often
                 means the person changed jobs and the record is stale (15)
      UNKNOWN  — no company domain data available to compare against (50, neutral)
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return {"signal": "UNKNOWN", "score": 50.0}
    email_domain = pl.normalize_domain(email.split("@", 1)[1])

    if email_domain in _FREE_EMAIL_DOMAINS:
        return {"signal": "PERSONAL", "score": 55.0}

    company_domain = pl.normalize_domain(company_domain)
    if not company_domain:
        return {"signal": "UNKNOWN", "score": 50.0}

    if email_domain == company_domain:
        return {"signal": "EXACT", "score": 100.0}

    return {"signal": "MISMATCH", "score": 15.0}


def domain_match_leads(leads: list[dict]) -> list[dict]:
    """Runs domain_match_signal() over a batch, storing results on each lead
    as _domain_signal / _domain_score. Independent of email verification —
    only needs the email and whatever company_domain scraping found."""
    pl.log.info("Stage 5 — Checking company-domain match for %d leads …", len(leads))
    for lead in leads:
        result = domain_match_signal(lead.get("email", ""), lead.get("company_domain", ""))
        lead["_domain_signal"] = result["signal"]
        lead["_domain_score"]  = result["score"]
    mismatches = sum(1 for l in leads if l.get("_domain_signal") == "MISMATCH")
    if mismatches:
        pl.log.warning("Domain match: %d lead(s) show a MISMATCH — likely stale employer records.", mismatches)
    return leads


# ─────────────────────────────────────────────
# Stage 6 — Email Verification (Waterfall + ZeroBounce)
# ─────────────────────────────────────────────

def verify_email_custom(email: str) -> dict:
    """
    Performs a free, custom email verification using DNS MX lookups and SMTP handshakes.
    
    Returns a dict with:
        status : valid | invalid | catch-all | unknown
        score  : float
    """
    email = email.strip()
    if not email:
        return {"status": "invalid", "score": 0.0}

    # 1. Syntax Check
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return {"status": "invalid", "score": 0.0}

    domain = email.split('@')[-1]

    # 2. DNS MX Lookup
    try:
        resolver = dns.resolver.Resolver()
        resolver.nameservers = ['8.8.8.8', '8.8.4.4']  # Google DNS
        resolver.timeout = 5
        resolver.lifetime = 5
        mx_records = resolver.resolve(domain, 'MX')
        mx_hosts = sorted([(r.preference, str(r.exchange).strip().rstrip('.')) for r in mx_records], key=lambda x: x[0])
    except Exception as e:
        pl.log.warning("MX lookup failed for %s: %s", domain, e)
        return {"status": "invalid", "score": 0.0}

    if not mx_hosts:
        return {"status": "invalid", "score": 0.0}

    # 3. SMTP Handshake Check
    mx_host = mx_hosts[0][1]
    try:
        smtp = smtplib.SMTP(timeout=5)
        code, message = smtp.connect(mx_host, 25)
        
        msg_str = str(message).lower()
        if "block" in msg_str or "listed" in msg_str or "spam" in msg_str or "pbl" in msg_str or "blacklist" in msg_str:
            smtp.close()
            return {"status": "unknown", "score": 50.0}
            
        smtp.helo("gmail.com")
        
        code, message = smtp.mail("sender.verify.email@gmail.com")
        msg_str = str(message).lower()
        if code != 250 or "block" in msg_str or "listed" in msg_str or "spam" in msg_str or "blacklist" in msg_str:
            smtp.close()
            return {"status": "unknown", "score": 50.0}
            
        code, message = smtp.rcpt(email)
        msg_str = str(message).lower()
        smtp.quit()

        if "block" in msg_str or "listed" in msg_str or "spam" in msg_str or "pbl" in msg_str or "blacklist" in msg_str or "policy" in msg_str:
            return {"status": "unknown", "score": 50.0}

        # SMTP Response Codes
        if code in (250, 251):
            return {"status": "valid", "score": 100.0}
        elif code in (550, 551, 552, 553, 554):
            if "unknown" in msg_str or "does not exist" in msg_str or "not found" in msg_str or "rejected" in msg_str:
                return {"status": "invalid", "score": 0.0}
            return {"status": "unknown", "score": 50.0}
        else:
            return {"status": "catch-all", "score": 60.0}
    except Exception as e:
        # Port 25 outbound is commonly blocked by local ISPs/networks.
        # Fall back to "unknown" (50.0 score) so the lead is kept.
        pl.log.warning("SMTP validation check skipped/failed for %s via %s: %s (falling back to unknown status)", email, mx_host, e)
        return {"status": "unknown", "score": 50.0}


def verify_emails(leads: list[dict], provider: str = None, on_progress=None) -> list[dict]:
    """
    Validates each lead email. Can use the native custom DNS/SMTP verifier
    or the paid ZeroBounce API depending on the EMAIL_VERIFIER_PROVIDER setting.

    on_progress(done, total), if given, is called after each email (custom
    provider) or each batch (ZeroBounce) completes — lets callers stream
    live "N of M verified" progress instead of waiting for the whole batch.
    """
    if provider is None:
        provider = os.getenv("EMAIL_VERIFIER_PROVIDER", "custom").lower().strip()
    else:
        provider = provider.lower().strip()

    pl.log.info("Stage 6 — Verifying emails for %d leads (using provider: %s) …", len(leads), provider)

    # Build a lookup: email → lead (only for leads that have an email)
    email_to_lead: dict[str, dict] = {}
    for lead in leads:
        if lead["email"]:
            email_to_lead[lead["email"]] = lead
        else:
            # No email — keep lead but mark clearly; may still be useful in fallback
            lead["_verification_status"] = "no_email"
            lead["_confidence_score"]    = 30.0   # low but non-zero for fallback

    emails_to_verify = list(email_to_lead.keys())
    if not emails_to_verify:
        return leads

    # 1. Run custom DNS/SMTP verification in parallel
    if provider == "custom":
        from concurrent.futures import ThreadPoolExecutor
        pl.log.info("Running custom DNS/SMTP verifier concurrently using ThreadPoolExecutor...")
        done = 0
        with ThreadPoolExecutor(max_workers=15) as executor:
            future_to_email = {executor.submit(verify_email_custom, email): email for email in emails_to_verify}
            for future in future_to_email:
                email = future_to_email[future]
                try:
                    res = future.result()
                except Exception as e:
                    pl.log.error("Unhandled error verifying email %s: %s", email, e)
                    res = {"status": "unknown", "score": 50.0}

                lead = email_to_lead[email]
                lead["_verification_status"] = res["status"]
                lead["_confidence_score"]    = res["score"]
                done += 1
                if on_progress:
                    on_progress(done, len(emails_to_verify))

        verified_count = sum(1 for l in leads if l.get("_verification_status") == "valid")
        pl.log.info("Email verification complete — %d valid emails.", verified_count)
        return leads

    # 2. Run ZeroBounce verification
    zb_key = os.getenv("ZEROBOUNCE_API_KEY")
    if not zb_key:
        pl.log.warning("ZEROBOUNCE_API_KEY not set — falling back to custom email verification.")
        from concurrent.futures import ThreadPoolExecutor
        done = 0
        with ThreadPoolExecutor(max_workers=15) as executor:
            future_to_email = {executor.submit(verify_email_custom, email): email for email in emails_to_verify}
            for future in future_to_email:
                email = future_to_email[future]
                try:
                    res = future.result()
                except Exception as e:
                    pl.log.error("Unhandled error verifying email %s: %s", email, e)
                    res = {"status": "unknown", "score": 50.0}

                lead = email_to_lead[email]
                lead["_verification_status"] = res["status"]
                lead["_confidence_score"]    = res["score"]
                done += 1
                if on_progress:
                    on_progress(done, len(emails_to_verify))

        verified_count = sum(1 for l in leads if l.get("_verification_status") == "valid")
        pl.log.info("Email verification complete — %d valid emails.", verified_count)
        return leads

    BATCH_SIZE = 100   # ZeroBounce accepts up to 100 per batch request

    for batch_start in range(0, len(emails_to_verify), BATCH_SIZE):
        batch = emails_to_verify[batch_start : batch_start + BATCH_SIZE]
        payload = {
            "api_key":    zb_key,
            "email_batch": [{"email_address": e} for e in batch],
        }

        try:
            resp = requests.post(
                "https://api.zerobounce.net/v2/validatebatch",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            resp_json = resp.json()
            
            # Check for API-level errors (e.g. credit exhaustion)
            if "errors" in resp_json and resp_json["errors"]:
                for err in resp_json["errors"]:
                    err_msg = err.get("error", "").lower()
                    if "out of credits" in err_msg or "invalid api key" in err_msg:
                        pl.log.warning("ZeroBounce API error: %s. Remaining leads in this batch will be marked as unverified (50.0 confidence).", err.get("error"))
                        for email in batch:
                            lead = email_to_lead[email]
                            lead["_verification_status"] = "unverified"
                            lead["_confidence_score"]    = 50.0
                results = []
            else:
                results = resp_json.get("email_batch", [])
        except requests.exceptions.HTTPError as e:
            pl.log.error("ZeroBounce HTTP error %s: %s", resp.status_code, resp.text)
            for email in batch:
                lead = email_to_lead[email]
                lead["_verification_status"] = "unverified"
                lead["_confidence_score"]    = 50.0
            continue
        except requests.exceptions.RequestException as e:
            pl.log.error("ZeroBounce request failed: %s", e)
            for email in batch:
                lead = email_to_lead[email]
                lead["_verification_status"] = "unverified"
                lead["_confidence_score"]    = 50.0
            continue

        for result in results:
            email  = result.get("address", "").lower().strip()
            status = result.get("status", "unknown").lower()          # valid/invalid/catch-all/…
            score  = _zb_confidence_score(status, result.get("sub_status", ""))

            if email in email_to_lead:
                email_to_lead[email]["_verification_status"] = status
                email_to_lead[email]["_confidence_score"]    = score

        if on_progress:
            on_progress(min(batch_start + BATCH_SIZE, len(emails_to_verify)), len(emails_to_verify))

        # Rate-limit courtesy pause between batches
        if batch_start + BATCH_SIZE < len(emails_to_verify):
            time.sleep(1)

    verified_count = sum(1 for l in leads if l.get("_verification_status") == "valid")
    pl.log.info("Email verification complete — %d valid emails.", verified_count)
    return leads


def _zb_confidence_score(status: str, sub_status: str) -> float:
    """
    Maps ZeroBounce status/sub_status to a 0–100 confidence score.
    Valid = 100, catch-all = 60, unknown = 40, everything else = 0.
    """
    mapping = {
        "valid":     100.0,
        "catch-all":  60.0,
        "unknown":    40.0,
        "spamtrap":    0.0,
        "abuse":       0.0,
        "do_not_mail": 0.0,
        "invalid":     0.0,
        "error":       0.0,
        "no_email":    0.0,
    }
    base = mapping.get(status, 20.0)

    # Penalise certain sub-statuses even if the top-level status looks OK
    bad_sub = {"mailbox_not_found", "failed_smtp_connection", "possible_trap"}
    if sub_status in bad_sub:
        base = min(base, 40.0)

    return base


# ─────────────────────────────────────────────
# Stage 7 — Composite Scoring
# ─────────────────────────────────────────────

def compute_composite_scores(leads: list[dict], icp: dict) -> list[dict]:
    """
    Blends every independent signal gathered so far into one _composite_score
    per lead (0-100), stored alongside the individual signals for transparency:

      40% — email confidence (_confidence_score, from Stage 6's waterfall/ZeroBounce)
      25% — LinkedIn company match (_linkedin_company_match, from Stage 4)
      10% — domain match (_domain_score, from Stage 5)
      10% — LinkedIn title match (_linkedin_title_match, from Stage 4)
      5%  — ICP fit (title/company match against the ICP, rescaled to 0-100)
      10% — claim verification (_claim_verification_score, from Stage 8)

    Email confidence stays weighted heaviest — it's the most direct "will
    this bounce" signal. LinkedIn company match is next: it's the strongest
    available "is this person still there" signal. Domain match, LinkedIn
    title match, and ICP fit are secondary corroboration — ICP fit in
    particular is a soft keyword-bag signal, and carries less weight than it
    used to now that claim verification exists as a strictly more precise
    check for the specific things (named technology, industry) it was only
    ever approximating via keyword overlap.

    This function is called twice per run: once in Stage 7 (before Stage 8
    has run, so _claim_verification_score is absent and defaults to neutral
    50 — sample selection is unaffected, exactly like before this signal
    existed), and again after Stage 8 attaches real claim evidence, to
    re-rank the small final sample with that evidence folded in.

    On top of the weighted sum, a confirmed FORMER employer on LinkedIn
    (lead._linkedin_current_employee is False — the company shows up in
    their LinkedIn experience with a past end date) applies a flat 0.5x
    penalty to the whole composite score, not just the LinkedIn component —
    a stale "this person doesn't work there anymore" record should tank the
    lead's overall priority, not just one sub-signal.

    Any signal that wasn't computed (e.g. no LinkedIn URL) defaults to a
    neutral 50 so a run without LinkedIn access still produces sane scores.
    """
    pl.log.info("Stage 7 — Computing composite scores for %d leads …", len(leads))
    icp_score = build_icp_fit_scorer(icp)

    for lead in leads:
        email_score = lead.get("_confidence_score", 0.0)
        li_company_score = lead.get("_linkedin_company_match")
        li_company_score = 50.0 if li_company_score is None else li_company_score
        li_title_score = lead.get("_linkedin_title_match")
        li_title_score = 50.0 if li_title_score is None else li_title_score
        domain_score = lead.get("_domain_score", 50.0)   # neutral if Stage 5 wasn't run
        claim_score = lead.get("_claim_verification_score", 50.0)   # neutral until Stage 8 runs
        # icp_score() is unbounded and centered on 0 — rescale to a bounded
        # 0-100 signal for blending (50 = neutral, no ICP signal either way).
        icp_fit = max(0.0, min(100.0, 50.0 + icp_score(lead) * 2.5))

        composite = (
            email_score * 0.40 +
            li_company_score * 0.25 +
            domain_score * 0.10 +
            li_title_score * 0.10 +
            icp_fit * 0.05 +
            claim_score * 0.10
        )
        if lead.get("_linkedin_current_employee") is False:
            composite *= 0.5   # confirmed stale record — heavy penalty

        lead["_icp_fit_score"]  = round(icp_fit, 1)
        lead["_composite_score"] = round(composite, 1)

    return leads


def build_icp_fit_scorer(icp: dict):
    """
    Builds a reusable icp_score(lead) -> float function that measures how
    well a lead's title/company text matches the ICP's keywords and any
    explicit lead_scoring factors. Unbounded, roughly centered on 0 (a
    lead matching nothing scores ~0; a strong match can run +30 or more;
    an explicit "negative" factor can push it well below 0).

    Shared by select_sample() (tier-ranking) and compute_composite_scores()
    (Stage 7) so there's a single definition of "ICP fit."
    """
    company = pl._bi_company(icp)

    icp_keywords = set()
    for title in pl._bi_all_titles(icp):
        icp_keywords.update(title.lower().split())

    # Industry intelligence — primary industry / sub-industries
    industry = pl._bi_industry(icp)
    primary_industry = industry.get("primary_industry")
    if primary_industry:
        icp_keywords.update(str(primary_industry).lower().split())
    for item in pl._safe_list(industry.get("sub_industries")):
        icp_keywords.update(item.lower().split())

    business_model = company.get("business_model")
    if business_model:
        icp_keywords.update(str(business_model).lower().split())
    for item in pl._bi_company_stage(icp):
        icp_keywords.update(item.lower().split())
    revenue_min, revenue_max = pl._bi_revenue_range(icp)
    for item in pl._revenue_range_keywords(revenue_min, revenue_max):
        icp_keywords.update(item.lower().split())

    # Canonical keyword pool, technologies, and intent signals
    for item in pl._bi_keyword_pool(icp):
        icp_keywords.update(item.lower().split())
    for item in pl._bi_all_technologies(icp):
        icp_keywords.update(item.lower().split())
    intent = pl._bi_intent(icp)
    for key in ("growth_signals", "technology_signals", "hiring_signals",
                "financial_signals", "expansion_signals", "executive_change_signals"):
        for item in pl._safe_list(intent.get(key)):
            icp_keywords.update(item.lower().split())

    # Lead scoring factors — explicit weighted bonus/penalty from the ICP
    lead_scoring = pl._bi_lead_scoring(icp)
    score_weights = {"high": 15, "medium": 8, "low": 3, "negative": -15}

    # Fields a scraped lead may carry beyond title/company (see pl.scrape_apollo/
    # pl.scrape_explorium's export-column normalization). Matching against all of
    # them - not just title+company - is what gives firmographic/technographic/
    # business-model keywords a realistic chance to ever fire, since none of
    # them are likely to appear in a person's job title or bare company name.
    LEAD_TEXT_FIELDS = (
        "title", "company", "industry", "biz_category",
        "biz_description", "technology", "market_cap", "employee_count",
    )

    def icp_score(lead: dict) -> float:
        text = " ".join(str(lead.get(f) or "") for f in LEAD_TEXT_FIELDS).lower()
        matches = sum(1 for kw in icp_keywords if kw in text)
        bonus = matches / max(len(icp_keywords), 1) * 10   # 0–10 bonus

        for factor in lead_scoring:
            if not isinstance(factor, dict):
                continue
            factor_text = str(factor.get("factor", "")).strip().lower()
            weight = str(factor.get("weight", "")).strip().lower()
            if factor_text and factor_text in text:
                bonus += score_weights.get(weight, 0)

        return bonus

    return icp_score


def select_sample(
    leads: list[dict],
    icp: dict,
    min_count: int = 20,
    max_count: Optional[int] = 25,
    min_confidence: float = 95.0,
) -> list[dict]:
    """
    Picks the best 20-25 leads from the verified pool.

    Uses tiered confidence thresholds:
      1st pass : min_confidence (default 95) — fully verified emails only
      2nd pass : 50 — unverified / catch-all emails included
      3rd pass : 0  — everything except hard-invalid (last resort)

    Within a tier, leads are ranked by _composite_score if Stage 7 already
    computed one (blends email confidence + LinkedIn + domain match + ICP fit);
    falls back to confidence + raw ICP fit for callers that skip composite scoring.

    Logs clearly which tier was used so the user understands quality.
    """
    pl.log.info("Stage 7 — Selecting sample from %d verified leads …", len(leads))

    # Hard disqualified — never include these regardless of threshold
    HARD_DISQUALIFIED = {"invalid", "spamtrap", "abuse", "do_not_mail"}

    icp_score = build_icp_fit_scorer(icp)

    def rank_key(lead: dict) -> float:
        if "_composite_score" in lead:
            return lead["_composite_score"]
        return lead.get("_confidence_score", 0) + icp_score(lead)

    def pick(threshold: float) -> list[dict]:
        eligible = [
            lead for lead in leads
            if lead.get("_confidence_score", 0) >= threshold
            and lead.get("_verification_status", "") not in HARD_DISQUALIFIED
        ]
        eligible.sort(key=rank_key, reverse=True)
        if max_count is not None:
            return eligible[:max_count]
        return eligible

    # ── Tiered selection ──────────────────────────────────────────────────────
    sample = pick(min_confidence)   # Tier 1: fully verified (95+)
    tier   = 1

    if len(sample) < min_count:
        pl.log.warning(
            "Tier 1 (≥95%% confidence): only %d leads. Trying Tier 2 (≥50%%)…",
            len(sample),
        )
        sample = pick(50.0)         # Tier 2: unverified / catch-all
        tier   = 2

    if len(sample) < min_count:
        pl.log.warning(
            "Tier 2 (≥50%% confidence): only %d leads. Using Tier 3 (all non-invalid)…",
            len(sample),
        )
        sample = pick(0.0)          # Tier 3: everything not hard-invalid
        tier   = 3

    if len(sample) == 0:
        pl.log.warning(
            "⚠ 0 leads available after all tiers. "
            "Likely cause: scrapers returned no leads with emails on the free API tier, "
            "or ZeroBounce credits are exhausted. Check API quotas."
        )
    elif len(sample) < min_count:
        pl.log.warning(
            "⚠ Only %d leads available (target: %d). Used Tier %d selection "
            "(lower verification confidence). "
            "To improve: upgrade Apollo/Apify plan, check ZeroBounce credits, "
            "or broaden the ICP.",
            len(sample), min_count, tier,
        )
    else:
        tier_label = ["fully verified", "unverified/catch-all included", "all non-invalid"][tier - 1]
        pl.log.info("Selected %d leads (Tier %d — %s).", len(sample), tier, tier_label)

    return sample


# ─────────────────────────────────────────────
# Stage 8 — Claim Verification
# ─────────────────────────────────────────────
# Nothing else in this pipeline verifies industry fit or named-technology
# usage (job title/employer are covered by LinkedIn Stage 4, email domain by
# Stage 5). This stage dynamically figures out, per ICP, whether either of
# those two claims is concrete enough to check via a targeted web search,
# and — only if so — verifies it for each unique company in the final
# sample. An ICP with nothing distinctive to check (e.g. "Find CTOs at SaaS
# companies") makes this a complete no-op: zero extra API calls.


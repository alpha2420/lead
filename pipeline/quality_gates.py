"""Post-scrape lead quality gates: reject genuinely unusable records,
flag (without rejecting) weaker-but-still-usable ones. Runs on every
batch right after dedupe_leads() and before any paid LinkedIn/ZeroBounce
call — see app/pipeline_runner.py."""

import pipeline as pl

_ROLE_BASED_EMAIL_PREFIXES = {
    "info", "admin", "support", "sales", "contact", "hello", "help",
    "office", "team", "careers", "hr", "billing",
}


def is_role_based_email(email: str) -> bool:
    """True for a generic shared-inbox address (info@, admin@, ...)
    rather than a named individual's — usually not a real decision-maker's
    contact, even when the address itself verifies as deliverable."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    prefix = email.split("@", 1)[0]
    return prefix in _ROLE_BASED_EMAIL_PREFIXES


def apply_quality_gates(leads: list[dict], icp: dict) -> tuple[list[dict], list[dict]]:
    """
    Hard/soft split, same philosophy as
    company_discovery.py::_rule_validate_company() ("only hard-gate what's
    truly unusable — everything else is ranking, not rejection").

    Hard reject: no email AND no phone AND no linkedin_url — a record with
    literally no way to reach the person.

    Soft flag only (kept, not dropped): role-based email, a company_domain
    that's actually a free email provider (gmail.com etc. — a spam/
    placeholder signal), or a missing employee_count. These stay soft
    because a company-only source (e.g. Google Maps) may legitimately have
    info@ as its only contact — hard-rejecting these now would silently
    gut that source's leads for no gain until per-vertical source routing
    (which would type these leads distinctly) exists.

    Returns (passed, rejected). Rejected leads carry `_quality_gate_reason`;
    passed leads carry `_quality_flags` (possibly empty) for anything soft.
    """
    passed: list[dict] = []
    rejected: list[dict] = []

    for lead in leads:
        if not any(lead.get(f) for f in pl._REQUIRED_CONTACT_FIELDS):
            lead["_quality_gate_reason"] = "no_contact_method"
            rejected.append(lead)
            continue

        flags = []
        if is_role_based_email(lead.get("email")):
            flags.append("role_based_email")
        domain = (lead.get("company_domain") or "").lower()
        if domain in pl._FREE_EMAIL_DOMAINS:
            flags.append("spam_domain")
        if not lead.get("employee_count"):
            flags.append("no_employee_count")

        lead["_quality_flags"] = flags
        passed.append(lead)

    if rejected:
        pl.log.info(
            "Quality gates: rejected %d/%d leads with no contact method.",
            len(rejected), len(leads),
        )

    return passed, rejected

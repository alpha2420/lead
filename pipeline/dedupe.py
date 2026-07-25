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


def dedupe_leads(
    leads: list[dict],
    seen_emails: set | None = None,
    seen_fingerprints: set | None = None,
    seen_linkedin_ids: set | None = None,
    seen_phones: set | None = None,
) -> tuple[list[dict], set, set, set, set]:
    """
    Deduplicates and standardises a batch of leads.

    Accepts optional external seen-sets so deduplication is cumulative
    across multiple pages/batches in the pagination loop.

    Checks email, LinkedIn URL, and phone independently — a match on ANY
    one is enough to reject a record (OR semantics, same philosophy as
    csv_mapper.py's dedupe_csv_rows()/_DEDUP_KEY_EXTRACTORS). Previously
    this only checked email (falling back to a name+company fingerprint),
    so the same person showing up with a different email across pages but
    an identical LinkedIn URL or phone number would slip through as a
    "new" lead. The name+company fingerprint fallback now only applies
    when a record has none of email/LinkedIn/phone at all.

    Returns:
        (cleaned_leads, updated_seen_emails, updated_seen_fingerprints,
         updated_seen_linkedin_ids, updated_seen_phones)
    """
    pl.log.info("Stage 3 — Deduplicating %d raw leads …", len(leads))

    if seen_emails is None:
        seen_emails = set()
    if seen_fingerprints is None:
        seen_fingerprints = set()
    if seen_linkedin_ids is None:
        seen_linkedin_ids = set()
    if seen_phones is None:
        seen_phones = set()

    cleaned: list[dict] = []

    for lead in leads:
        # ── Standardize ──────────────────────────────────────────────────────
        lead["name"]         = pl._title_case(pl._safe(lead.get("name")))
        lead["title"]        = pl._safe(lead.get("title"))
        lead["company"]      = pl._safe(lead.get("company"))
        lead["email"]        = pl._safe(lead.get("email", "")).lower().strip()
        lead["linkedin_url"] = pl._safe(lead.get("linkedin_url"))
        lead["phone"]        = pl._safe(lead.get("phone"))

        # Drop entirely empty records
        if not lead["name"] and not lead["email"]:
            continue

        linkedin_id = pl._normalize_linkedin_value(lead["linkedin_url"]).get("id")
        phone_norm = pl._normalize_phone_value(lead["phone"]).get("value")

        # ── Dedupe by email / LinkedIn / phone (any match = duplicate) ─────────
        if lead["email"] and lead["email"] in seen_emails:
            continue
        if linkedin_id and linkedin_id in seen_linkedin_ids:
            continue
        if phone_norm and phone_norm in seen_phones:
            continue

        if not lead["email"] and not linkedin_id and not phone_norm:
            # Fallback fingerprint: hash(name + company)
            fp = hashlib.md5(
                (lead["name"].lower() + lead["company"].lower()).encode()
            ).hexdigest()
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

        if lead["email"]:
            seen_emails.add(lead["email"])
        if linkedin_id:
            seen_linkedin_ids.add(linkedin_id)
        if phone_norm:
            seen_phones.add(phone_norm)

        cleaned.append(lead)

    pl.log.info("Batch deduped: %d new unique leads.", len(cleaned))
    return cleaned, seen_emails, seen_fingerprints, seen_linkedin_ids, seen_phones


# ─────────────────────────────────────────────
# Stage 4 — LinkedIn Cross-Verification (Bright Data)
# ─────────────────────────────────────────────


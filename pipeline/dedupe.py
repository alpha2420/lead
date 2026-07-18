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
) -> tuple[list[dict], set, set]:
    """
    Deduplicates and standardises a batch of leads.

    Accepts optional external seen-sets so deduplication is cumulative
    across multiple pages/batches in the pagination loop.

    Returns:
        (cleaned_leads, updated_seen_emails, updated_seen_fingerprints)
    """
    pl.log.info("Stage 3 — Deduplicating %d raw leads …", len(leads))

    if seen_emails is None:
        seen_emails = set()
    if seen_fingerprints is None:
        seen_fingerprints = set()

    cleaned: list[dict] = []

    for lead in leads:
        # ── Standardize ──────────────────────────────────────────────────────
        lead["name"]         = pl._title_case(pl._safe(lead.get("name")))
        lead["title"]        = pl._safe(lead.get("title"))
        lead["company"]      = pl._safe(lead.get("company"))
        lead["email"]        = pl._safe(lead.get("email", "")).lower().strip()
        lead["linkedin_url"] = pl._safe(lead.get("linkedin_url"))

        # Drop entirely empty records
        if not lead["name"] and not lead["email"]:
            continue

        # ── Dedupe by email ──────────────────────────────────────────────────
        if lead["email"]:
            if lead["email"] in seen_emails:
                continue
            seen_emails.add(lead["email"])
        else:
            # Fallback fingerprint: hash(name + company)
            fp = hashlib.md5(
                (lead["name"].lower() + lead["company"].lower()).encode()
            ).hexdigest()
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

        cleaned.append(lead)

    pl.log.info("Batch deduped: %d new unique leads.", len(cleaned))
    return cleaned, seen_emails, seen_fingerprints


# ─────────────────────────────────────────────
# Stage 4 — LinkedIn Cross-Verification (Bright Data)
# ─────────────────────────────────────────────


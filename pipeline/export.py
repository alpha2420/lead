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


def export_csv(
    sample: list[dict],
    all_leads_raw: int,
    all_leads_deduped: int,
    all_leads_verified: int,
    output_dir: str = ".",
) -> tuple[str, str]:
    """
    Writes the sample to a timestamped CSV file and prints a summary report.

    Returns: (csv_filepath, report_text)
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = os.path.join(output_dir, f"leads_sample_{timestamp}.csv")

    # Dynamic fieldnames: standard clean ones first (in CRM-import order),
    # then all other raw provider columns for debugging/reference.
    standard_keys = {
        "company":               "Company",
        "name":                  "Person Name",
        "title":                 "Title",
        "email":                 "Email Id",
        "phone":                 "Phone",
        "city":                  "City",
        "state":                 "State",
        "country":               "Country",
        "zip_code":              "Zip Code",
        "employee_count":        "Employee Count",
        "level":                 "Level",
        "phone2":                "Phone 2",
        "email2":                "Email 2",
        "biz_address":           "Biz Address",
        "location":              "Location",
        "market_cap":            "Market Cap",
        "industry":              "Industry",
        "biz_category":          "Biz Category",
        "biz_description":       "Biz Description",
        "technology":            "Technology",
        "linkedin_url":          "LinkedIn URL",
        "_verification_status":  "Verification Status",
        "_confidence_score":     "Confidence Score",
        "_composite_score":      "Composite Score",
        "_claim_verification_signal":   "Claim Verification",
        "_claim_verification_evidence": "Claim Evidence",
        "source":                "Source",
    }

    headers = list(standard_keys.values())
    other_keys = set()
    for lead in sample:
        for k in lead.keys():
            if k not in standard_keys and not k.startswith("_"):
                other_keys.add(k)
    sorted_other_keys = sorted(list(other_keys))
    headers.extend(sorted_other_keys)

    pl.log.info("Stage 10 — Exporting %d leads to %s (including all raw columns) …", len(sample), csv_path)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for lead in sample:
            row = {}
            for key, header in standard_keys.items():
                if key == "_confidence_score":
                    row[header] = f"{lead.get(key, 0):.1f}"
                else:
                    row[header] = lead.get(key, "")

            for k in sorted_other_keys:
                row[k] = lead.get(k, "")
                
            writer.writerow(row)

    # ── Summary report ────────────────────────────────────────────────────────
    bounced = sum(
        1 for l in sample
        if l.get("_verification_status") in {"invalid", "catch-all", "spamtrap"}
    )
    bounce_rate = (bounced / len(sample) * 100) if sample else 0.0

    report = (
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║        LEAD SAMPLE — PIPELINE REPORT     ║\n"
        "╚══════════════════════════════════════════╝\n"
        f"  Total scraped          : {all_leads_raw}\n"
        f"  After deduplication   : {all_leads_deduped}\n"
        f"  Passed email verify   : {all_leads_verified}\n"
        f"  Final sample size     : {len(sample)}\n"
        f"  Bounce rate (sample)  : {bounce_rate:.1f}%\n"
        f"  Output CSV            : {csv_path}\n"
        "──────────────────────────────────────────\n"
    )

    print(report)

    # Also save to a .txt file
    report_path = os.path.join(output_dir, f"report_{timestamp}.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    pl.log.info("Summary report saved to %s", report_path)

    return csv_path, report


# ─────────────────────────────────────────────
# CSV Structure Mapper — a standalone personal utility, unrelated to lead
# generation. Maps an arbitrary uploaded CSV's columns onto this app's own
# canonical 19-column lead structure (the exact `standard_keys` header set
# export_csv() above already writes), using cheap rule-based synonym
# matching first and falling back to Gemini only for columns that don't
# resolve — never as the default path, per its cost/latency.
# ─────────────────────────────────────────────


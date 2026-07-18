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


def scrape_explorium(icp: dict, max_leads: int = 50, page: int = 1, api_key: str = None) -> list[dict]:
    """
    Queries Explorium.ai AgentSource Prospects Search API and enriches results in bulk.
    """
    pl.log.info("Stage 2c — Scraping Explorium.ai page %d (max %d leads) …", page, max_leads)

    key = api_key or pl.EXPLORIUM_API_KEY
    if not key:
        pl.log.warning("EXPLORIUM_API_KEY not set — skipping Explorium scrape.")
        return []

    headers = {
        "Content-Type": "application/json",
        "api_key": key,
        "accept": "application/json",
    }

    # 1. Build Explorium filters from ICP
    filters = {}

    # has_email: true (we want contacts with emails)
    filters["has_email"] = {"value": "true"}

    # Seniority levels -> job_level
    # Categories: [director, manager, vp, partner, cxo, non-managerial, senior, entry, training, unpaid]
    seniorities = pl._bi_seniority(icp)
    mapped_levels = []
    for s in seniorities:
        s_lower = s.lower()
        if "vp" in s_lower:
            mapped_levels.append("vp")
        elif "director" in s_lower:
            mapped_levels.append("director")
        elif "c-level" in s_lower or "cxo" in s_lower:
            mapped_levels.append("cxo")
        elif "manager" in s_lower:
            mapped_levels.append("manager")
        elif "senior" in s_lower:
            mapped_levels.append("senior")
        elif "partner" in s_lower:
            mapped_levels.append("partner")

    if mapped_levels:
        filters["job_level"] = {"values": list(set(mapped_levels))}

    # Departments -> job_department
    # Categories: [customer service, design, education, engineering, finance, general, health, sales, ...]
    depts = pl._bi_departments(icp)
    mapped_depts = []
    for d in depts:
        d_lower = d.lower()
        if "it" in d_lower or "tech" in d_lower or "sys" in d_lower:
            mapped_depts.append("engineering")
        elif "sale" in d_lower:
            mapped_depts.append("sales")
        elif "market" in d_lower:
            mapped_depts.append("marketing")
        elif "finance" in d_lower or "accounting" in d_lower:
            mapped_depts.append("finance")
        elif "hr" in d_lower or "people" in d_lower or "talent" in d_lower:
            mapped_depts.append("human resources")

    if mapped_depts:
        filters["job_department"] = {"values": list(set(mapped_depts))}

    # Country / Location -> country_code
    # Two-letter codes.
    locs = pl._bi_all_locations(icp)
    mapped_countries = []
    for l in locs:
        l_lower = l.lower()
        if "united states" in l_lower or "us" == l_lower or "usa" == l_lower:
            mapped_countries.append("us")
        elif "canada" in l_lower or "ca" == l_lower:
            mapped_countries.append("ca")
        elif "united kingdom" in l_lower or "uk" == l_lower or "gb" == l_lower:
            mapped_countries.append("gb")
        elif "germany" in l_lower or "de" == l_lower:
            mapped_countries.append("de")
        elif "australia" in l_lower or "au" == l_lower:
            mapped_countries.append("au")
        elif "india" in l_lower or "in" == l_lower:
            mapped_countries.append("in")

    if mapped_countries:
        filters["country_code"] = {"values": list(set(mapped_countries))}

    # Company Size -> company_size
    # Options: [1-10, 11-50, 51-200, 201-500, 501-1000, 1001-5000, 5001-10000, 10001+]
    min_sz, max_sz = pl._bi_size_range(icp)
    if min_sz is not None or max_sz is not None:
        size_ranges = ["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001-10000", "10001+"]
        selected_ranges = []
        for r in size_ranges:
            if r == "10001+":
                low, high = 10001, 10000000
            else:
                low_str, high_str = r.split("-")
                low = int(low_str)
                if high_str.endswith("k") or high_str.endswith("K"):
                    high = int(high_str[:-1]) * 1000
                elif high_str.endswith("m") or high_str.endswith("M"):
                    high = int(high_str[:-1]) * 1000000
                else:
                    high = int(high_str)

            eff_min = min_sz if min_sz is not None else 0
            eff_max = max_sz if max_sz is not None else 10000000
            if (low <= eff_max) and (high >= eff_min):
                selected_ranges.append(r)
        if selected_ranges:
            filters["company_size"] = {"values": selected_ranges}

    # Job Title (Explorium single value text filter)
    job_titles = pl._bi_all_titles(icp)
    if job_titles:
        filters["job_title"] = {"value": job_titles[0]}

    payload = {
        "mode": "full",
        "size": max_leads,
        "page_size": min(max_leads, 50),
        "page": page,
        "filters": filters
    }

    url = "https://api.explorium.ai/v1/prospects"

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        search_data = resp.json()
    except requests.exceptions.RequestException as e:
        pl.log.error("Explorium prospects search failed: %s", e)
        return []

    prospects = search_data.get("data", [])
    if not prospects:
        pl.log.info("Explorium returned 0 prospects.")
        return []

    # 2. Bulk Enrichment of contact details for the prospect IDs
    prospect_ids = [p.get("prospect_id") for p in prospects if p.get("prospect_id")]
    if not prospect_ids:
        pl.log.info("Explorium: No valid prospect IDs to enrich.")
        return []

    enriched_map = {}
    for i in range(0, len(prospect_ids), 50):
        chunk = prospect_ids[i:i+50]
        enrich_url = "https://api.explorium.ai/v1/prospects/contacts_information/bulk_enrich"
        enrich_payload = {"prospect_ids": chunk}
        try:
            enrich_resp = requests.post(enrich_url, json=enrich_payload, headers=headers, timeout=30)
            enrich_resp.raise_for_status()
            enrich_data = enrich_resp.json().get("data", [])
            for item in enrich_data:
                pid = item.get("prospect_id")
                if pid:
                    enriched_map[pid] = item.get("data", {})
        except requests.exceptions.RequestException as e:
            pl.log.error("Explorium bulk contact enrichment failed for chunk: %s", e)

    # 3. Create standard leads and preserve all raw columns
    leads = []
    for p in prospects:
        pid = p.get("prospect_id")
        enrich_info = enriched_map.get(pid, {}) if pid else {}

        # Extract emails
        emails_list = enrich_info.get("emails", []) or []
        email = ""
        for e in emails_list:
            if e.get("type") in ["professional", "current_professional"]:
                email = e.get("address", "")
                break
        if not email and emails_list:
            email = emails_list[0].get("address", "")

        # Extract phone
        phones = enrich_info.get("phone_numbers", []) or []
        phone = enrich_info.get("mobile_phone", "")
        if not phone and phones:
            phone = phones[0].get("phone_number") if isinstance(phones[0], dict) else str(phones[0])

        lead = {
            "name": pl._safe(p.get("full_name") or p.get("name")),
            "title": pl._safe(p.get("job_title") or p.get("title")),
            "company": pl._safe(p.get("company_name")),
            "location": pl._safe(", ".join(filter(None, [p.get("city"), p.get("region_name"), p.get("country_name")]))),
            "email": pl._safe(email).lower(),
            "linkedin_url": pl._safe(p.get("linkedin")),
            "source": "explorium",
            "_verification_status": "unverified",
            "_confidence_score": 50.0,
        }

        # Add Explorium raw columns
        for k, v in p.items():
            if k not in ["full_name", "name", "job_title", "title", "company_name", "linkedin"]:
                lead[f"explorium_{k}"] = v

        lead["explorium_phone"] = phone

        # Export-column normalization — best-effort extraction into the flat
        # CRM export shape. Explorium's exact field names for company-level
        # attributes vary by plan; unavailable ones are left blank rather
        # than guessed.
        phone2 = pl._phone_from_entry(phones[1]) if len(phones) > 1 else ""
        email2 = ""
        if len(emails_list) > 1:
            email2 = emails_list[1].get("address", "") if isinstance(emails_list[1], dict) else str(emails_list[1])

        lead["phone"]           = pl._safe(phone)
        lead["phone2"]          = pl._safe(phone2)
        lead["city"]            = pl._safe(p.get("city"))
        lead["state"]           = pl._safe(p.get("region_name"))
        lead["country"]         = pl._safe(p.get("country_name"))
        lead["zip_code"]        = pl._safe(p.get("zip_code") or p.get("postal_code"))
        lead["employee_count"]  = pl._safe(p.get("company_size") or p.get("employees_count"))
        lead["level"]           = pl._safe(p.get("job_level") or p.get("seniority"))
        lead["email2"]          = pl._safe(email2)
        lead["biz_address"]     = pl._safe(p.get("company_address") or p.get("address"))
        lead["market_cap"]      = pl._safe(p.get("market_cap"))
        lead["industry"]        = pl._safe(p.get("company_industry") or p.get("industry"))
        lead["biz_category"]    = pl._join_list_str(p.get("company_category") or p.get("naics_category"))
        lead["biz_description"] = pl._safe(p.get("company_description"))
        lead["technology"]      = pl._join_list_str(p.get("technologies") or p.get("company_technologies"))

        leads.append(lead)

    pl.log.info("Explorium returned %d enriched leads.", len(leads))
    return leads


# ─────────────────────────────────────────────
# Stage 2d — Source Orchestrator
# ─────────────────────────────────────────────
# Decides which of Apollo/Apify/Explorium to call, in what order, and how
# (sequential-with-waterfall-skip vs concurrent) for a given page. Does not
# change any adapter's internal behavior — pl.scrape_apollo/pl.scrape_apify/
# scrape_explorium are called exactly as they always were.


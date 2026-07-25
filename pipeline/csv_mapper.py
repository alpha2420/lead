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


_CSV_MAPPER_FIELDS = {
    # canonical_key: (display_header, [normalized synonyms])
    "company":          ("Company", ["company", "company_name", "organization", "org", "business_name"]),
    "name":             ("Person Name", ["name", "person_name", "full_name", "contact_name"]),
    "title":            ("Title", ["title", "job_title", "position", "designation", "role"]),
    "email":            ("Email Id", ["email", "email_id", "email_address", "primary_email"]),
    "phone":            ("Phone", ["phone", "phone_number", "mobile", "contact_number", "primary_phone"]),
    "linkedin_url":     ("LinkedIn URL", ["linkedin", "linkedin_url", "li_url", "linkedin_profile", "li_profile", "profile_url", "linkedin_id"]),
    "city":             ("City", ["city"]),
    "state":            ("State", ["state", "province"]),
    "country":          ("Country", ["country"]),
    "zip_code":         ("Zip Code", ["zip_code", "zip", "postal_code"]),
    "employee_count":   ("Employee Count", ["employee_count", "employees", "headcount", "company_size"]),
    "level":            ("Level", ["level", "seniority", "seniority_level"]),
    "phone2":           ("Phone 2", ["phone_2", "secondary_phone", "alt_phone", "alternate_phone"]),
    "email2":           ("Email 2", ["email_2", "secondary_email", "alt_email", "alternate_email"]),
    "biz_address":      ("Biz Address", ["biz_address", "business_address", "address"]),
    "website":          ("Website", ["website", "company_website", "url", "domain", "company_domain"]),
    "location":         ("Location", ["location"]),
    "market_cap":       ("Market Cap", ["market_cap"]),
    "industry":         ("Industry", ["industry", "sector"]),
    "biz_category":     ("Biz Category", ["biz_category", "business_category", "category"]),
    "biz_description":  ("Biz Description", ["biz_description", "business_description", "description"]),
    "technology":       ("Technology", ["technology", "tech_stack", "technologies"]),
}


def _normalize_header(h: str) -> str:
    """Lowercases and collapses spaces/hyphens/slashes into single underscores,
    so 'Zip Code', 'zip-code', and 'ZIP_CODE' all normalize identically."""
    h = re.sub(r"[\s\-/]+", "_", h.strip().lower())
    return re.sub(r"_+", "_", h).strip("_")


def _csv_mapping_prompt(unmapped_headers: list[str], unmapped_fields: list[str], sample_row: dict) -> str:
    field_lines = "\n".join(f"- {k}: {_CSV_MAPPER_FIELDS[k][0]}" for k in unmapped_fields)
    header_lines = "\n".join(
        f'- "{h}" (example value: {sample_row.get(h, "")!r})' for h in unmapped_headers
    )
    return f"""You are mapping CSV column headers from an unknown lead-list export onto a fixed target schema.

Unmapped source columns (with one example value each):
{header_lines}

Candidate target fields (key: description):
{field_lines}

For each source column, choose the single best-matching target field key, or null if none of the
candidates are a reasonable match. Output ONLY a strict JSON object mapping each source column
name exactly (as given above) to a target field key or null. No markdown, no preamble, no comments."""


def suggest_csv_column_mapping(headers: list[str], sample_row: dict) -> dict:
    """
    Rule-based synonym match first (cheap, deterministic); Gemini fills in only
    the columns still unresolved after that pass, and only if any remain.

    A canonical field may legitimately be claimed by more than one source
    header — e.g. merging multiple CSVs (different tools, different naming:
    "Email" vs "Contact Email") both describing the same field. clean_csv_row()
    resolves the actual value per row by preferring whichever header has data,
    so candidate fields here are NOT removed once one header claims them —
    doing so would starve every other file's differently-named column of a
    home, silently blanking out its data on export.

    Returns {"mapping": {source_header: canonical_key_or_None, ...}, "ai_used_for": [source_header, ...]}.
    """
    mapping = {}
    for h in headers:
        norm = _normalize_header(h)
        match = next((k for k, (_, syns) in _CSV_MAPPER_FIELDS.items() if norm in syns), None)
        mapping[h] = match

    unmapped_headers = [h for h, v in mapping.items() if v is None]
    unmapped_fields = list(_CSV_MAPPER_FIELDS.keys())
    ai_used_for = []

    if unmapped_headers and unmapped_fields and pl.GEMINI_API_KEY:
        prompt = _csv_mapping_prompt(unmapped_headers, unmapped_fields, sample_row)
        try:
            client = genai.Client(api_key=pl.GEMINI_API_KEY)
            ai_mapping = pl.generate_json_with_retry(prompt, client)
            for h, k in (ai_mapping or {}).items():
                if h in unmapped_headers and k in unmapped_fields:
                    mapping[h] = k
                    ai_used_for.append(h)
        except Exception as e:
            pl.log.warning("CSV mapper: Gemini fallback failed, leaving remaining columns unmapped: %s", e)

    return {"mapping": mapping, "ai_used_for": ai_used_for}


# ── CSV Mapper — value cleaning, validation, dedup, templates ──────────────
# Runs after the header-mapping step above. Stdlib-only (no `phonenumbers` /
# `email-validator` / etc. installed), so email/phone normalization below is
# best-effort format cleanup, not authoritative per-country validation.

_EMAIL_SYNTAX_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINKEDIN_URL_RE = re.compile(r"linkedin\.com/(in|company)/([^/?#]+)", re.IGNORECASE)
_REQUIRED_CONTACT_FIELDS = ("email", "phone", "linkedin_url")

_EMPLOYEE_COUNT_BUCKETS = [
    (1, 10, "1-10"), (11, 50, "11-50"), (51, 200, "51-200"), (201, 500, "201-500"),
    (501, 1000, "501-1000"), (1001, 5000, "1001-5000"), (5001, 10000, "5001-10000"),
    (10001, None, "10001+"),
]

# Fuzzy dedup is O(n^2) over unmatched rows — fine for a personal utility's
# typical CSV sizes, but skipped above this count to avoid pathological hangs
# on very large uploads.
_MAX_FUZZY_DEDUP_CANDIDATES = 3000


def _normalize_email_value(raw: str) -> dict:
    """Trims/lowercases an email and checks its syntax only (no MX/SMTP —
    those are network calls, too slow to run per-row over a bulk CSV;
    pl.verify_email_custom() above is the place for that heavier check)."""
    value = (raw or "").strip().lower()
    if not value:
        return {"value": None, "valid": False}
    return {"value": value, "valid": bool(_EMAIL_SYNTAX_RE.match(value))}


def _normalize_phone_value(raw: str) -> dict:
    """Best-effort E.164-shape cleanup: strips formatting characters, keeps a
    leading '+', and requires 7-15 digits. No `phonenumbers` library is
    installed, so this can't validate per-country numbering rules — it's a
    format sanity check, not authoritative validation."""
    value = (raw or "").strip()
    if not value:
        return {"value": None, "valid": False}
    has_plus = value.startswith("+")
    digits = re.sub(r"\D", "", value)
    cleaned = ("+" if has_plus else "") + digits
    return {"value": cleaned if digits else None, "valid": 7 <= len(digits) <= 15}


def _normalize_linkedin_value(raw: str) -> dict:
    """Strips tracking query params/fragments from a LinkedIn URL, validates
    it matches a real profile/company URL shape, and extracts the slug as a
    stable LinkedIn ID for dedup purposes."""
    value = (raw or "").strip()
    if not value:
        return {"value": None, "id": None, "valid": False}
    probe = value if value.lower().startswith(("http://", "https://")) else "https://" + value
    parsed = urllib.parse.urlsplit(probe)
    match = _LINKEDIN_URL_RE.search(parsed.netloc + parsed.path)
    if not match:
        return {"value": value, "id": None, "valid": False}
    slug = match.group(2).strip("/")
    clean_url = f"https://www.linkedin.com/{match.group(1)}/{slug}"
    return {"value": clean_url, "id": slug.lower(), "valid": True}


def _normalize_name_value(raw: str) -> dict:
    """Trims/collapses whitespace, title-cases, and splits into first/last
    (first token / last token — middle names are dropped from `last`, the
    same tradeoff the existing inline name-split near line 1407 already
    makes for synthesizing guessed emails)."""
    value = re.sub(r"\s+", " ", (raw or "").strip())
    if not value:
        return {"value": None, "first": None, "last": None}
    value = value.title()
    parts = value.split(" ")
    return {"value": value, "first": parts[0], "last": parts[-1] if len(parts) >= 2 else None}


def _normalize_company_value(raw: str) -> Optional[str]:
    """Trims/collapses whitespace only — deliberately doesn't re-case, since
    aggressive title-casing would mangle real company names ('eBay', 'iHeartMedia')."""
    value = re.sub(r"\s+", " ", (raw or "").strip())
    return value or None


def _normalize_title_value(raw: str) -> Optional[str]:
    value = re.sub(r"\s+", " ", (raw or "").strip())
    return value.title() if value else None


def _normalize_location_value(raw: str) -> dict:
    """Splits a combined 'City, State, Country' column using the same
    comma-split logic as pl._parse_linkedin_location() — that function's logic
    is generic despite its LinkedIn-specific name/docstring."""
    return pl._parse_linkedin_location(raw)


def _normalize_website_value(raw: str) -> dict:
    domain = pl.normalize_domain(raw)
    return {"domain": domain or None, "valid": bool(domain and "." in domain and " " not in domain)}


def _bucket_employee_count(raw) -> Optional[str]:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return None
    n = int(digits)
    for lo, hi, label in _EMPLOYEE_COUNT_BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            return label
    return None


def _convert_empty_to_null(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def clean_csv_row(row: dict, mapping: dict) -> dict:
    """
    Normalizes one CSV row's mapped values and flags validation issues.
    `mapping` is {source_header: canonical_key_or_None} from
    suggest_csv_column_mapping() (possibly user-edited in the UI).

    Derived values (linkedin_id, first/last name, company_domain,
    employee_size_range, city/state/country parsed out of a combined
    Location column) are always computed into `extra` — dedup needs them
    regardless of whether the caller wants enrichment columns in the export;
    process_csv_mapper_rows() decides whether `extra` gets written out.

    Returns {"cleaned": {canonical_key: value}, "extra": {derived_key: value},
    "issues": [{"field", "problem"}], "quality": 0-100}.
    """
    cleaned: dict = {}
    extra: dict = {}
    issues: list = []

    # More than one source header can map to the same canonical key (merged
    # CSVs with different naming conventions for the same field — see
    # suggest_csv_column_mapping()). Each row only ever has data under ONE of
    # those headers (it came from one file), so resolve per row: prefer
    # whichever mapped header actually has a value here, falling back to the
    # first one. Plain last-key-wins iteration would let a later, empty
    # header silently blank out an earlier header's real value.
    resolved_headers: dict = {}
    for source_header, canonical_key in mapping.items():
        if not canonical_key:
            continue
        current = resolved_headers.get(canonical_key)
        if current is None or (not _safe(row.get(current)) and _safe(row.get(source_header))):
            resolved_headers[canonical_key] = source_header

    for canonical_key, source_header in resolved_headers.items():
        raw = row.get(source_header, "")

        if canonical_key in ("email", "email2"):
            result = _normalize_email_value(raw)
            cleaned[canonical_key] = result["value"]
            if result["value"] and not result["valid"]:
                issues.append({"field": canonical_key, "problem": "invalid_format"})
        elif canonical_key in ("phone", "phone2"):
            result = _normalize_phone_value(raw)
            cleaned[canonical_key] = result["value"]
            if result["value"] and not result["valid"]:
                issues.append({"field": canonical_key, "problem": "invalid_format"})
        elif canonical_key == "linkedin_url":
            result = _normalize_linkedin_value(raw)
            cleaned[canonical_key] = result["value"]
            if result["value"] and not result["valid"]:
                issues.append({"field": canonical_key, "problem": "invalid_format"})
            if result.get("id"):
                extra["linkedin_id"] = result["id"]
        elif canonical_key == "name":
            result = _normalize_name_value(raw)
            cleaned[canonical_key] = result["value"]
            extra["first_name"] = result["first"]
            extra["last_name"] = result["last"]
        elif canonical_key == "company":
            cleaned[canonical_key] = _normalize_company_value(raw)
        elif canonical_key == "title":
            cleaned[canonical_key] = _normalize_title_value(raw)
        elif canonical_key == "website":
            result = _normalize_website_value(raw)
            cleaned[canonical_key] = result["domain"]
            if raw and not result["valid"]:
                issues.append({"field": canonical_key, "problem": "invalid_format"})
        elif canonical_key == "location":
            cleaned[canonical_key] = _convert_empty_to_null(raw)
            loc = _normalize_location_value(raw)
            for k in ("city", "state", "country"):
                if loc.get(k):
                    extra.setdefault(k, loc[k])
        elif canonical_key == "employee_count":
            cleaned[canonical_key] = _convert_empty_to_null(raw)
            extra["employee_size_range"] = _bucket_employee_count(raw)
        else:
            cleaned[canonical_key] = _convert_empty_to_null(raw)

    domain = cleaned.get("website")
    if not domain and cleaned.get("email") and "@" in cleaned["email"]:
        domain = pl.normalize_domain(cleaned["email"].split("@", 1)[-1])
    if domain:
        extra["company_domain"] = domain

    if not cleaned.get("company"):
        issues.append({"field": "company", "problem": "missing_required"})
    if not any(cleaned.get(f) for f in _REQUIRED_CONTACT_FIELDS):
        issues.append({"field": "contact", "problem": "missing_required"})

    total_mapped = len({k for k in mapping.values() if k})
    filled = sum(1 for v in cleaned.values() if v)
    quality = round(100 * filled / total_mapped, 1) if total_mapped else 0.0
    quality = max(0.0, quality - 10 * len(issues))

    return {"cleaned": cleaned, "extra": {k: v for k, v in extra.items() if v}, "issues": issues, "quality": quality}


_DEDUP_KEY_EXTRACTORS = {
    "email":          lambda c, e: (c.get("email") or "").lower() or None,
    "linkedin":       lambda c, e: e.get("linkedin_id") or None,
    "phone":          lambda c, e: c.get("phone") or None,
    "company_domain": lambda c, e: e.get("company_domain") or None,
    "company_name":   lambda c, e: (c.get("company") or "").lower() or None,
}


def dedupe_csv_rows(rows: list[dict], dedup_keys: list[str], fuzzy: bool = False) -> dict:
    """
    Groups duplicate rows. `rows` is a list of clean_csv_row() results.
    Strict pass groups rows sharing a non-empty match on ANY of the selected
    dedup_keys (OR semantics, same as typical CRM dedup). Optional fuzzy pass
    additionally flags remaining rows as duplicates when their company name
    AND person name are both >92% similar (via pl._fuzzy_ratio, stdlib
    difflib) — catches near-duplicates strict matching misses.

    Returns {"duplicate_groups": [[row_index, ...], ...], "kept_indices": [...]}.
    kept_indices defaults to the first row of every group plus every
    non-duplicate row; callers may override which survivor to keep.
    """
    dedup_keys = [k for k in dedup_keys if k in _DEDUP_KEY_EXTRACTORS] or ["email"]

    value_to_indices: dict = {}
    for i, r in enumerate(rows):
        cleaned, extra = r["cleaned"], r["extra"]
        for key in dedup_keys:
            val = _DEDUP_KEY_EXTRACTORS[key](cleaned, extra)
            if val:
                value_to_indices.setdefault((key, val), []).append(i)

    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for indices in value_to_indices.values():
        for i in indices[1:]:
            union(indices[0], i)

    if fuzzy:
        grouped_already = {i for indices in value_to_indices.values() for i in indices}
        candidates = [i for i in range(len(rows)) if i not in grouped_already]
        if len(candidates) <= _MAX_FUZZY_DEDUP_CANDIDATES:
            for a_pos in range(len(candidates)):
                for b_pos in range(a_pos + 1, len(candidates)):
                    a, b = candidates[a_pos], candidates[b_pos]
                    ca, cb = rows[a]["cleaned"], rows[b]["cleaned"]
                    company_sim = pl._fuzzy_ratio(ca.get("company") or "", cb.get("company") or "")
                    name_sim = pl._fuzzy_ratio(ca.get("name") or "", cb.get("name") or "")
                    if company_sim > 92 and name_sim > 92:
                        union(a, b)

    groups: dict = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)

    duplicate_groups = [members for members in groups.values() if len(members) > 1]
    duped_indices = {i for group in duplicate_groups for i in group[1:]}
    kept_indices = [i for i in range(len(rows)) if i not in duped_indices]

    return {"duplicate_groups": duplicate_groups, "kept_indices": kept_indices}


_CSV_MAPPER_TEMPLATES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "csv_mapper_templates.db")


def _csv_mapper_templates_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_CSV_MAPPER_TEMPLATES_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_CSV_MAPPER_TEMPLATES_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS templates ("
        "name TEXT PRIMARY KEY, mapping_json TEXT, settings_json TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    return conn


def list_csv_mapping_templates() -> list[dict]:
    conn = _csv_mapper_templates_conn()
    try:
        rows = conn.execute(
            "SELECT name, mapping_json, settings_json, created_at, updated_at FROM templates ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"name": n, "mapping": json.loads(m), "settings": json.loads(s), "created_at": c, "updated_at": u}
        for n, m, s, c, u in rows
    ]


def save_csv_mapping_template(name: str, mapping: dict, settings: dict) -> None:
    conn = _csv_mapper_templates_conn()
    try:
        now = datetime.now().isoformat()
        existing = conn.execute("SELECT created_at FROM templates WHERE name = ?", (name,)).fetchone()
        created_at = existing[0] if existing else now
        conn.execute(
            "INSERT OR REPLACE INTO templates (name, mapping_json, settings_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, json.dumps(mapping), json.dumps(settings), created_at, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_csv_mapping_template(name: str) -> None:
    conn = _csv_mapper_templates_conn()
    try:
        conn.execute("DELETE FROM templates WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


def write_normalized_csv(
    cleaned_rows: list[dict],
    canonical_fields: list,
    extra_columns: list[str],
    output_dir: str = "./output/csv_mapper",
) -> str:
    """
    Writes cleaned+deduped rows to a timestamped CSV under output_dir, the
    same append-only convention pl.export_csv() above uses. `canonical_fields`
    is the ordered [key, label] list from _CSV_MAPPER_FIELDS; `extra_columns`
    are derived enrichment keys (first_name, company_domain, ...).
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(output_dir, f"normalized_{timestamp}.csv")

    headers = [label for _, label in canonical_fields] + [c.replace("_", " ").title() for c in extra_columns]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in cleaned_rows:
            cleaned, extra = row["cleaned"], row["extra"]
            writer.writerow(
                [cleaned.get(k, "") or "" for k, _ in canonical_fields]
                + [extra.get(c, "") or "" for c in extra_columns]
            )

    return csv_path


def process_csv_mapper_rows(rows: list[dict], mapping: dict, settings: dict) -> dict:
    """
    Orchestrates the CSV Mapper's clean → validate → dedup → export pipeline
    for an already-uploaded, already-mapped CSV (called by
    POST /api/csv-mapper/process). `settings` keys: dedup_keys (list),
    fuzzy (bool), skip_duplicates (bool, default True), enrich (bool, adds
    derived columns to the export), save (bool, default True),
    keep_overrides ({duplicate_group_index: row_index_to_keep}).
    """
    settings = settings or {}
    cleaned_rows = [clean_csv_row(row, mapping) for row in rows]

    dedup_keys = settings.get("dedup_keys") or ["email", "linkedin"]
    dedupe_result = dedupe_csv_rows(cleaned_rows, dedup_keys, fuzzy=bool(settings.get("fuzzy")))

    kept_indices = set(dedupe_result["kept_indices"])
    for group_index, keep_index in (settings.get("keep_overrides") or {}).items():
        group = dedupe_result["duplicate_groups"][int(group_index)]
        kept_indices -= set(group)
        kept_indices.add(int(keep_index))

    skip_duplicates = settings.get("skip_duplicates", True)
    export_indices = sorted(kept_indices) if skip_duplicates else list(range(len(cleaned_rows)))

    valid = sum(1 for r in cleaned_rows if not r["issues"])
    missing_fields = sum(1 for r in cleaned_rows if any(i["problem"] == "missing_required" for i in r["issues"]))
    quality_score = round(sum(r["quality"] for r in cleaned_rows) / len(cleaned_rows), 1) if cleaned_rows else 0.0

    canonical_fields = [[k, v[0]] for k, v in _CSV_MAPPER_FIELDS.items()]
    extra_columns = sorted({c for r in cleaned_rows for c in r["extra"].keys()}) if settings.get("enrich") else []

    saved_path = None
    if settings.get("save", True):
        export_rows = [cleaned_rows[i] for i in export_indices]
        saved_path = write_normalized_csv(export_rows, canonical_fields, extra_columns)

    return {
        "cleaned_rows": cleaned_rows,
        "duplicate_groups": dedupe_result["duplicate_groups"],
        "kept_indices": sorted(kept_indices),
        "export_indices": export_indices,
        "validation": {
            "total": len(cleaned_rows),
            "valid": valid,
            "invalid": len(cleaned_rows) - valid,
            "missing_fields": missing_fields,
            "duplicates": len(cleaned_rows) - len(kept_indices),
        },
        "quality_score": quality_score,
        "saved_path": saved_path,
        "import_report": {
            "imported": len(export_indices),
            "skipped_duplicates": len(cleaned_rows) - len(kept_indices) if skip_duplicates else 0,
            "failed": len(cleaned_rows) - valid,
        },
    }


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _safe(value) -> str:
    """Return a stripped string, or '' if value is None / not a string."""
    if value is None:
        return ""
    return str(value).strip()


def _title_case(s: str) -> str:
    """Title-case a name, handling empty strings safely."""
    return s.title() if s else ""


# ─────────────────────────────────────────────
# Main Orchestrator
# ─────────────────────────────────────────────


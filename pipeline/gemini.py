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


def extract_json(raw_text: str) -> str:
    """
    Extracts the first valid JSON block (object or array) from a raw string.
    Robust against markdown code fences, leading/trailing notes, or spaces.
    """
    raw_text = raw_text.strip()
    # 1. Match code blocks starting with ```json or ```
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_text, re.DOTALL | re.IGNORECASE)
    if match:
        raw_text = match.group(1).strip()
    else:
        # 2. Find first occurrence of { or [ and the last occurrence of } or ]
        first_brace = raw_text.find('{')
        first_bracket = raw_text.find('[')
        start_idx = -1
        end_char = ''
        if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
            start_idx = first_brace
            end_char = '}'
        elif first_bracket != -1:
            start_idx = first_bracket
            end_char = ']'
            
        if start_idx != -1:
            end_idx = raw_text.rfind(end_char)
            if end_idx != -1 and end_idx > start_idx:
                raw_text = raw_text[start_idx:end_idx + 1]
    return raw_text

def generate_content_with_retry(prompt: str, client: genai.Client = None, models: list[str] = None) -> str:
    """
    Generates content using the Gemini client with retries and model fallbacks.
    Tries each model in the list, performing up to 3 retries with backoff on transient errors (503, 429, 500).
    """
    if client is None:
        client = genai.Client(api_key=pl.GEMINI_API_KEY)
    
    if models is None:
        models = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro"]
        
    last_err = None
    for model in models:
        for attempt in range(1, 4):
            try:
                pl.log.info("Querying model %s (attempt %d/3)...", model, attempt)
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )
                return response.text.strip()
            except Exception as e:
                err_str = str(e)
                # Check for transient errors (503, 429, 500)
                is_transient = any(code in err_str for code in ["503", "429", "500", "UNAVAILABLE", "RESOURCE_EXHAUSTED"])
                if is_transient and attempt < 3:
                    sleep_time = 2 ** attempt
                    pl.log.warning("Transient error with model %s on attempt %d: %s. Retrying in %ds...", model, attempt, err_str, sleep_time)
                    time.sleep(sleep_time)
                else:
                    pl.log.error("Model %s failed: %s", model, err_str)
                    last_err = e
                    break # try next model
                    
    raise last_err or Exception("All Gemini models failed.")


def generate_json_with_retry(prompt: str, client: genai.Client = None, models: list[str] = None, max_attempts: int = 3):
    """
    generate_content_with_retry() only retries on transient HTTP-level errors
    (503/429/500) — it has no protection against the model successfully
    responding with syntactically invalid JSON, which happens occasionally
    for long/complex generations (e.g. the full ICP schema, ~200+ lines once
    Gemini fills in every array field). A single malformed response used to
    kill the entire pipeline run immediately with no recovery attempt (see
    the "Expecting ',' delimiter" failures this was built to fix).

    Re-generates from scratch on a JSON parse failure (asking the model to
    "fix" its own broken output isn't reliably better than a fresh attempt)
    up to max_attempts times, then re-raises the JSONDecodeError so callers'
    existing error handling still applies on final failure.
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        raw = generate_content_with_retry(prompt, client, models)
        try:
            return json.loads(extract_json(raw))
        except json.JSONDecodeError as e:
            last_err = e
            if attempt < max_attempts:
                pl.log.warning(
                    "Gemini returned invalid JSON on attempt %d/%d (%s) — retrying …",
                    attempt, max_attempts, e,
                )
            else:
                pl.log.error("Gemini returned invalid JSON after %d attempts: %s", max_attempts, e)
    raise last_err


# ─────────────────────────────────────────────
# Shared ICP schema — single source of truth used by both
# pl.parse_inquiry() and pl.chat_icp() so the two prompts can never drift.
# Every field here must help find companies, find decision makers,
# personalize outreach, or prioritize leads — see field-by-field
# usage in pl.scrape_apollo / pl.scrape_apify / pl.scrape_explorium / pl.select_sample.
# ─────────────────────────────────────────────


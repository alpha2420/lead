import os
import csv
import json
import time
import math
import logging
import hashlib
import re
import smtplib
import imaplib
import socket
import sqlite3
import difflib
import urllib.parse
import dns.resolver
import requests
import contextvars
from datetime import datetime, timedelta
from email.message import EmailMessage
from email import message_from_bytes
from email.utils import parsedate_to_datetime
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


# ─────────────────────────────────────────────
# Stage 6 alternate provider — Gmail send-and-watch-for-bounces
# ─────────────────────────────────────────────
# Unlike verify_email_custom() (a passive SMTP probe that never delivers
# anything) and ZeroBounce (a third-party lookup service), this provider
# ACTUALLY EMAILS a capped number of addresses (see
# _GMAIL_BOUNCE_MAX_REAL_SENDS_PER_RUN, currently 1/run) from a real Gmail
# account, then watches that account's inbox for real bounce (NDR/DSN)
# replies. Real messages land in a real inbox for that one address — this
# is a deliberate, higher-risk tradeoff the user asked for, not a default.
# Two real costs apply that the other providers don't have, both
# meaningfully reduced (not eliminated) by the per-run send cap: (1)
# bulk-sending near-identical short emails to many addresses in a short
# window is exactly the pattern spam filters watch for, and can get a
# personal Gmail account rate-limited or suspended — capping real sends to
# one per run removes the "bulk" from that pattern entirely,
# GMAIL_SEND_DELAY_SECONDS remains as defense-in-depth if the cap is ever
# raised; (2) a real bounce can arrive anywhere from seconds to days later,
# but this runs synchronously inside one pipeline stage, so it can only wait
# a bounded window (_GMAIL_BOUNCE_WAIT_SECONDS) before moving on — with only
# one real send per run, this bounded-window risk now applies to at most one
# address instead of the whole batch. Every address beyond the cap is
# deliberately left "unverified" (score 50.0, neutral) rather than actually
# emailed — see verify_emails_via_gmail_bounce()'s docstring.

_GMAIL_SMTP_HOST = "smtp.gmail.com"
_GMAIL_SMTP_PORT = 587
_GMAIL_IMAP_HOST = "imap.gmail.com"
_GMAIL_IMAP_PORT = 993
_GMAIL_BOUNCE_WAIT_SECONDS = 90
_GMAIL_SEND_DELAY_SECONDS = 1.5

# Safety cap: at most this many addresses get a REAL email per pipeline run
# (not per call — verify_emails_via_gmail_bounce() is called once per scrape
# page, so this budget is shared across those calls; see its docstring).
# Fixed, not user-configurable, by design.
_GMAIL_BOUNCE_MAX_REAL_SENDS_PER_RUN = 1

# Tracks how many real sends each pipeline run has already used, keyed by
# pipeline.get_current_run_id(). A plain module-level dict is fine here: one
# small int per run, and runs are created/discarded far less often than,
# say, leads are processed.
_gmail_bounce_sends_used_by_run: dict = {}

_GMAIL_BOUNCE_SENDER_HINTS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
_GMAIL_BOUNCE_SUBJECT_HINTS = (
    "delivery status notification", "undelivered mail", "failure notice",
    "delivery has failed", "returned to sender", "undeliverable", "delivery incomplete",
)
_EMAIL_ADDR_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def _send_gmail_verification_ping(smtp_conn: smtplib.SMTP, from_addr: str, to_addr: str) -> bool:
    """Sends one minimal, non-promotional email. Returns True if Gmail's
    outbound relay accepted the send for delivery — that's a queued
    acceptance, NOT proof of final delivery. Gmail almost never rejects
    synchronously; a real bounce, if any, arrives later as an async NDR to
    the inbox, which is what _check_gmail_inbox_for_bounces() looks for."""
    msg = EmailMessage()
    msg["Subject"] = "Quick check"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content("Just confirming this address is reachable — no action needed.")
    try:
        smtp_conn.send_message(msg)
        return True
    except smtplib.SMTPRecipientsRefused:
        return False  # rejected immediately — rare, but treat as an instant hard bounce
    except Exception as e:
        pl.log.warning("Gmail bounce-check: send to %s failed: %s", to_addr, e)
        return False


def _is_bounce_message(msg) -> bool:
    if msg.get_content_type() == "multipart/report":
        return True
    from_header = (msg.get("From") or "").lower()
    if any(h in from_header for h in _GMAIL_BOUNCE_SENDER_HINTS):
        return True
    subject = (msg.get("Subject") or "").lower()
    return any(h in subject for h in _GMAIL_BOUNCE_SUBJECT_HINTS)


def _extract_failed_recipient(msg) -> Optional[str]:
    """Parses an NDR/bounce email for the address delivery failed for.
    Prefers the RFC 3464 machine-readable delivery-status part
    (Final-Recipient / Original-Recipient, formatted "rfc822;user@x.com");
    falls back to scanning the plain-text body for the first email address,
    since real-world bounce formats (esp. from corporate mail filters)
    routinely deviate from the RFC."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "message/delivery-status":
                continue
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else [payload]
            for block in blocks:
                recipient = block.get("Final-Recipient") or block.get("Original-Recipient")
                if not recipient:
                    continue
                addr = recipient.split(";", 1)[-1].strip()
                m = _EMAIL_ADDR_RE.search(addr)
                if m:
                    return m.group(0).lower()

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
    else:
        try:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            body = str(msg.get_payload())

    matches = _EMAIL_ADDR_RE.findall(body)
    return matches[0].lower() if matches else None


def _check_gmail_inbox_for_bounces(imap_conn: imaplib.IMAP4_SSL, since_time: datetime) -> set:
    """Searches INBOX for bounce/NDR messages received since `since_time`,
    returns the set of failed-recipient email addresses found. IMAP SINCE
    is date-granularity only, so messages are re-filtered by their real
    Date header to exclude anything actually older than since_time."""
    bounced = set()
    imap_conn.select("INBOX")
    since_str = since_time.strftime("%d-%b-%Y")
    status, data = imap_conn.search(None, f'(SINCE "{since_str}")')
    if status != "OK" or not data or not data[0]:
        return bounced

    for num in data[0].split():
        status, msg_data = imap_conn.fetch(num, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = message_from_bytes(msg_data[0][1])

        try:
            msg_date = parsedate_to_datetime(msg.get("Date"))
            if msg_date and msg_date.replace(tzinfo=None) < since_time:
                continue
        except Exception:
            pass

        if not _is_bounce_message(msg):
            continue
        recipient = _extract_failed_recipient(msg)
        if recipient:
            bounced.add(recipient)

    return bounced


def verify_emails_via_gmail_bounce(emails: list[str], on_progress=None, run_id: str = None) -> dict:
    """
    Sends a REAL email to at most _GMAIL_BOUNCE_MAX_REAL_SENDS_PER_RUN
    addresses (currently 1) for the whole pipeline run this call is part
    of, waits _GMAIL_BOUNCE_WAIT_SECONDS, then checks the Gmail inbox for a
    bounce (NDR) on just that address. Every other address handed to this
    call is deliberately left untouched — no email is ever sent to it — and
    comes back {"status": "unverified", "score": 50.0}: a neutral "we chose
    not to check this one," not a claim about deliverability either way.
    See the module comment above for why this provider is risky enough to
    cap in the first place.

    The cap is tracked per pipeline run (via pipeline.get_current_run_id()
    by default), not per call — a run can call this function once per
    scrape page, so the budget is shared across those calls and only the
    first address across the whole run is ever actually emailed. Pass
    run_id explicitly to isolate/override this (e.g. in tests).

    "unverified" (deliberately skipped) is distinct from "unknown" (a real
    attempt was warranted but the sender wasn't configured, or SMTP login
    failed, so we genuinely couldn't get a signal) — keeping these separate
    means a run that comes back all-"unverified" can be told apart at a
    glance from one where Gmail is actually misconfigured.

    An address with no bounce found in the wait window is scored 95.0 (not
    100.0) — a pragmatic "probably valid" rather than a claim of certainty,
    since real bounces can legitimately arrive after this function returns.
    95.0 is deliberately at the pipeline's own >=95.0 "high confidence"
    threshold (see app/pipeline_runner.py) so a clean send still counts
    toward the target instead of stalling pagination.

    Returns {email: {"status": "valid"|"invalid"|"unverified"|"unknown", "score": float}}.
    """
    if run_id is None:
        run_id = pl.get_current_run_id()

    already_used = _gmail_bounce_sends_used_by_run.get(run_id, 0)
    budget_left  = max(0, _GMAIL_BOUNCE_MAX_REAL_SENDS_PER_RUN - already_used)
    to_send      = emails[:budget_left]
    to_skip      = emails[budget_left:]

    if to_skip:
        pl.log.info(
            "Gmail bounce-check: real-send cap is %d/run (already used %d) — "
            "leaving %d of %d address(es) unverified this call.",
            _GMAIL_BOUNCE_MAX_REAL_SENDS_PER_RUN, already_used, len(to_skip), len(emails),
        )

    if not to_send:
        if on_progress:
            on_progress(len(emails), len(emails))
        return {e: {"status": "unverified", "score": 50.0} for e in emails}

    sender = pl.GMAIL_SENDER_ADDRESS
    password = pl.GMAIL_APP_PASSWORD
    if not sender or not password:
        pl.log.warning("GMAIL_SENDER_ADDRESS/GMAIL_APP_PASSWORD not set — cannot run Gmail bounce-check verification.")
        if on_progress:
            on_progress(len(emails), len(emails))
        return {e: {"status": "unknown", "score": 50.0} for e in emails}

    started_at = datetime.now()
    sent_ok = set()

    try:
        smtp_conn = smtplib.SMTP(_GMAIL_SMTP_HOST, _GMAIL_SMTP_PORT, timeout=15)
        smtp_conn.starttls()
        smtp_conn.login(sender, password)
    except Exception as e:
        pl.log.error("Gmail bounce-check: SMTP login failed: %s", e)
        if on_progress:
            on_progress(len(emails), len(emails))
        return {e: {"status": "unknown", "score": 50.0} for e in emails}

    # Budget is consumed only now that login succeeded and a real send is
    # about to be attempted — a login failure above never reaches here, so
    # a transient failure on one page doesn't burn the run's only shot; the
    # send stays available on a later page.
    _gmail_bounce_sends_used_by_run[run_id] = already_used + len(to_send)

    done = 0
    total = len(emails)
    try:
        for addr in to_send:
            if _send_gmail_verification_ping(smtp_conn, sender, addr):
                sent_ok.add(addr)
            done += 1
            if on_progress:
                on_progress(done, total)
            time.sleep(_GMAIL_SEND_DELAY_SECONDS)
    finally:
        try:
            smtp_conn.quit()
        except Exception:
            pass

    # Mark the deliberately-skipped addresses "done" too, so on_progress
    # still reaches `total` by the time this call returns — otherwise a
    # live "N of M verified" UI indicator would stall partway through this
    # page instead of completing it.
    if to_skip:
        done += len(to_skip)
        if on_progress:
            on_progress(done, total)

    pl.log.info(
        "Gmail bounce-check: sent %d/%d verification emails, waiting %ds for bounces …",
        len(sent_ok), len(to_send), _GMAIL_BOUNCE_WAIT_SECONDS,
    )
    time.sleep(_GMAIL_BOUNCE_WAIT_SECONDS)

    bounced = set()
    try:
        imap_conn = imaplib.IMAP4_SSL(_GMAIL_IMAP_HOST, _GMAIL_IMAP_PORT)
        imap_conn.login(sender, password)
        bounced = _check_gmail_inbox_for_bounces(imap_conn, started_at)
        imap_conn.logout()
    except Exception as e:
        pl.log.error("Gmail bounce-check: IMAP inbox check failed: %s", e)

    results = {}
    for addr in to_send:
        if addr not in sent_ok or addr.lower() in bounced:
            results[addr] = {"status": "invalid", "score": 0.0}
        else:
            results[addr] = {"status": "valid", "score": 95.0}
    for addr in to_skip:
        results[addr] = {"status": "unverified", "score": 50.0}

    bounced_count = sum(1 for r in results.values() if r["status"] == "invalid")
    pl.log.info(
        "Gmail bounce-check complete — %d/%d addresses bounced (%d left unverified under the %d/run send cap).",
        bounced_count, len(to_send), len(to_skip), _GMAIL_BOUNCE_MAX_REAL_SENDS_PER_RUN,
    )
    return results


def verify_emails(leads: list[dict], provider: str = None, on_progress=None) -> list[dict]:
    """
    Validates each lead email. provider is one of "custom" (native DNS/SMTP
    probe, default), "gmail_bounce" (sends a real email from a configured
    Gmail account and watches for a real bounce — see
    verify_emails_via_gmail_bounce()'s docstring for the tradeoffs), or
    "zerobounce" (paid API), set via the EMAIL_VERIFIER_PROVIDER setting.

    on_progress(done, total), if given, is called after each email (custom/
    gmail_bounce) or each batch (ZeroBounce) completes — lets callers stream
    live "N of M verified" progress instead of waiting for the whole batch.
    """
    if provider is None:
        provider = os.getenv("EMAIL_VERIFIER_PROVIDER", "gmail_bounce").lower().strip()
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
            future_to_email = {pl.submit_with_context(executor, verify_email_custom, email): email for email in emails_to_verify}
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

    # 2. Run Gmail send-and-watch-for-bounces verification
    if provider == "gmail_bounce":
        results = verify_emails_via_gmail_bounce(emails_to_verify, on_progress=on_progress)
        for email_addr, res in results.items():
            lead = email_to_lead[email_addr]
            lead["_verification_status"] = res["status"]
            lead["_confidence_score"]    = res["score"]

        verified_count = sum(1 for l in leads if l.get("_verification_status") == "valid")
        pl.log.info("Email verification complete — %d valid emails.", verified_count)
        return leads

    # 3. Run ZeroBounce verification
    zb_key = os.getenv("ZEROBOUNCE_API_KEY")
    if not zb_key:
        pl.log.warning("ZEROBOUNCE_API_KEY not set — falling back to custom email verification.")
        from concurrent.futures import ThreadPoolExecutor
        done = 0
        with ThreadPoolExecutor(max_workers=15) as executor:
            future_to_email = {pl.submit_with_context(executor, verify_email_custom, email): email for email in emails_to_verify}
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

    # Penalise certain sub-statuses even if the top-level status looks OK.
    # "role_based" (info@/admin@/support@/...) is included here so a shared
    # inbox never scores identically to a named contact's verified email —
    # see pipeline/quality_gates.py for the complementary pre-verification
    # role-based flag on leads regardless of provider.
    bad_sub = {"mailbox_not_found", "failed_smtp_connection", "possible_trap", "role_based"}
    if sub_status in bad_sub:
        base = min(base, 40.0)

    return base


# ─────────────────────────────────────────────
# Stage 7 — Composite Scoring
# ─────────────────────────────────────────────

_EMPLOYEE_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
_EMPLOYEE_PLUS_RE = re.compile(r"(\d[\d,]*)\s*\+")


def _parse_employee_count(raw) -> Optional[tuple]:
    """Employee-count values arrive in different shapes depending on
    source — a plain headcount ("120", from org enrichment's
    estimated_num_employees) or a bucket range ("51-200"/"10001+", from
    Apify's company_size field). Returns (low, high) as floats, high=None
    for an open-ended "+" bucket, or None if unparseable."""
    if not raw:
        return None
    s = str(raw).strip()
    m = _EMPLOYEE_RANGE_RE.search(s)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _EMPLOYEE_PLUS_RE.search(s)
    if m:
        return float(m.group(1).replace(",", "")), None
    digits = re.sub(r"[^\d]", "", s)
    if digits:
        n = float(digits)
        return n, n
    return None


def _company_size_fit_score(lead: dict, icp: dict) -> float:
    """100 if the lead's employee count falls inside the ICP's
    company_size_min/max range, decaying the further outside it falls.
    Neutral 50 if either the ICP has no size constraint or the lead has
    no employee-count data — this never penalizes a lead for a gap in
    scraped data, only for an actual mismatch."""
    size_min, size_max = pl._bi_size_range(icp)
    if size_min is None and size_max is None:
        return 50.0

    parsed = _parse_employee_count(lead.get("employee_count"))
    if parsed is None:
        return 50.0
    lo, hi = parsed
    point = (lo + hi) / 2 if hi is not None else lo

    icp_lo = size_min if size_min is not None else 0.0
    icp_hi = size_max if size_max is not None else float("inf")

    if icp_lo <= point <= icp_hi:
        return 100.0

    if point < icp_lo:
        distance, scale = icp_lo - point, icp_lo or 1.0
    else:
        distance, scale = point - icp_hi, (icp_hi if icp_hi != float("inf") else point)

    return max(0.0, 100.0 - (distance / max(scale, 1.0)) * 100.0)


def _location_fit_score(lead: dict, icp: dict) -> float:
    """100 if any of the lead's city/state/country/location fields
    mentions one of the ICP's target locations (geography-backstop
    expanded — see pl._clean_geography_list()/pl.expand_timezone_labels()),
    20 if the lead has location data that matches none of them, neutral 50
    if either the ICP has no location constraint or the lead has no
    location data at all."""
    target_locations = pl._clean_geography_list(pl._bi_all_locations(icp))
    if not target_locations:
        return 50.0

    lead_text = " ".join(str(lead.get(f) or "") for f in ("city", "state", "country", "location")).lower().strip()
    if not lead_text:
        return 50.0

    return 100.0 if any(loc.lower() in lead_text for loc in target_locations) else 20.0


def compute_composite_scores(leads: list[dict], icp: dict) -> list[dict]:
    """
    Blends every independent signal gathered so far into one _composite_score
    per lead (0-100), stored alongside the individual signals for transparency:

      35% — email confidence (_confidence_score, from Stage 6's waterfall/ZeroBounce)
      20% — LinkedIn company match (_linkedin_company_match, from Stage 4)
      10% — domain match (_domain_score, from Stage 5)
      10% — LinkedIn title match (_linkedin_title_match, from Stage 4)
      5%  — ICP fit (title/company match against the ICP, rescaled to 0-100)
      10% — claim verification (_claim_verification_score, from Stage 8)
      5%  — company size fit (_company_size_fit_score, employee_count vs. ICP range)
      5%  — location fit (_location_fit_score, lead location vs. ICP geography)

    Email confidence stays weighted heaviest — it's the most direct "will
    this bounce" signal. LinkedIn company match is next: it's the strongest
    available "is this person still there" signal. Domain match, LinkedIn
    title match, and ICP fit are secondary corroboration — ICP fit in
    particular is a soft keyword-bag signal, and carries less weight than it
    used to now that claim verification, company size fit, and location fit
    exist as strictly more precise checks for the specific things (named
    technology/industry, headcount, geography) it was only ever
    approximating via keyword overlap. Size/location fit are new,
    deliberately small (5% each) weights — start conservative and adjust
    after seeing real runs, rather than assuming these under-tested
    heuristics deserve equal footing with email/LinkedIn signals immediately.

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
        size_fit_score = _company_size_fit_score(lead, icp)
        location_fit_score = _location_fit_score(lead, icp)
        # icp_score() is unbounded and centered on 0 — rescale to a bounded
        # 0-100 signal for blending (50 = neutral, no ICP signal either way).
        icp_fit = max(0.0, min(100.0, 50.0 + icp_score(lead) * 2.5))

        composite = (
            email_score * 0.35 +
            li_company_score * 0.20 +
            domain_score * 0.10 +
            li_title_score * 0.10 +
            icp_fit * 0.05 +
            claim_score * 0.10 +
            size_fit_score * 0.05 +
            location_fit_score * 0.05
        )
        if lead.get("_linkedin_current_employee") is False:
            composite *= 0.5   # confirmed stale record — heavy penalty

        lead["_icp_fit_score"] = round(icp_fit, 1)
        lead["_size_fit_score"] = round(size_fit_score, 1)
        lead["_location_fit_score"] = round(location_fit_score, 1)
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

    # Fields a scraped lead may carry beyond title/company (see
    # pl.scrape_apify's export-column normalization). Matching against all of
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
    min_composite_score: Optional[float] = None,
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

    If min_composite_score is set, it's applied as an additional filter
    within each tier (never in place of the tier waterfall above) — leads
    below the floor are excluded from that tier's eligible pool. This can
    legitimately drop the result below min_count; when that happens, the
    caller's `run["results"]["stats"]["score_floor_underfilled"]` gets set
    (see app/pipeline_runner.py) rather than the floor being silently
    ignored or the sample silently topped back up with sub-floor leads —
    delivering fewer than the target is preferred over the earlier
    "always fill 20-25" behavior masking a real quality gap.
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
            and (min_composite_score is None or rank_key(lead) >= min_composite_score)
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
        reason = "lower verification confidence"
        if min_composite_score is not None:
            reason += f" and/or the {min_composite_score} composite-score floor"
        pl.log.warning(
            "⚠ Only %d leads available (target: %d). Used Tier %d selection (%s). "
            "To improve: upgrade the Apify plan, check ZeroBounce credits, "
            "lower the score floor, or broaden the ICP.",
            len(sample), min_count, tier, reason,
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


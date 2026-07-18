"""Background thread bodies that drive the lead-generation pipeline for a
single run, pushing progress/results onto that run's SSE queue."""

import logging
import os
import queue

import pipeline as pl

from .logging_utils import RunQueueHandler
from .state import RunRegistry, save_run_metadata

logger = logging.getLogger(__name__)


def run_pipeline_thread(run_registry: RunRegistry, run_id: str, inquiry: str, target: int = 25, max_pages: int = 10,
                        enable_apollo: bool = True, enable_apify: bool = True,
                        enable_explorium: bool = False, explorium_api_key: str = None,
                        verifier_provider: str = "custom", icp: dict = None,
                        profile: str = "balanced", apify_actor: str = None):
    run = run_registry.get(run_id)
    q: queue.Queue = run["queue"]

    # Attach our custom handler to the pipeline module's logger
    handler = RunQueueHandler(q)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pl.log.addHandler(handler)

    def push_stage(stage: int, label: str):
        q.put({"type": "stage", "stage": stage, "label": label})

    def push_stats(**kwargs):
        q.put({"type": "stats", **kwargs})

    def push_progress(collected: int, target: int, page: int):
        q.put({"type": "progress", "collected": collected, "target": target, "page": page})

    def push_stage_progress(stage: int, done: int, total: int):
        q.put({"type": "stage_progress", "stage": stage, "done": done, "total": total})

    try:
        run["status"] = "running"

        # ── Stage 1 ───────────────────────────────────────────────────────────
        if icp is None:
            push_stage(1, "Parsing Inquiry")
            icp = pl.parse_inquiry(inquiry)
        else:
            push_stage(1, "Using Custom ICP")
        q.put({"type": "icp", "data": icp})

        # ── Stage 2: Company Discovery ───────────────────────────────────────
        # Company-first search: find candidate companies matching the ICP's
        # firmographics before searching for any people at all. See
        # pl.discover_companies() — combines a real Apollo company-search
        # attempt (auto-falls back gracefully if the account's plan doesn't
        # support it), a free Apollo people-search preview, and a
        # Gemini+web-search fallback.
        push_stage(2, "Discovering Companies")
        discovery = pl.discover_companies(icp, target=target)
        q.put({"type": "companies_discovered", "data": discovery["counts"]})

        # ── Stage 3: Company Validation ──────────────────────────────────────
        # Two tiers: free rule-based screen on every candidate, then a
        # capped AI fact-check via web search on the top-ranked survivors
        # (see pl.validate_companies() / _SOURCE_PROFILES[profile]
        # ["max_ai_validate_companies"]). Companies beyond the cap stay
        # validated (rule-only), never silently dropped.
        push_stage(3, "Validating Companies")
        validation = pl.validate_companies(discovery["companies"], icp, profile=profile)
        validated_companies = validation["validated"]
        q.put({"type": "companies_validated", "data": validation["counts"]})

        # ── Pagination loop (Stages 4-8) ─────────────────────────────────────
        push_stage(4, "Searching Leads at Validated Companies")

        verified_pool:     list[dict] = []
        seen_emails:       set        = set()
        seen_fingerprints: set        = set()
        total_raw         = 0
        total_deduped     = 0
        apollo_total      = 0
        apify_total       = 0
        explorium_total   = 0
        invalid_count     = 0
        catchall_count    = 0
        page              = 1

        verified_high_conf_count = 0
        while verified_high_conf_count < target and page <= max_pages:
            # Scrape — via the Source Orchestrator (priority waterfall in
            # sequential-mode profiles, concurrent fan-out in parallel-mode
            # profiles; see pl.run_lead_sources())
            result = pl.run_lead_sources(
                icp, page, profile=profile,
                enable_apollo=enable_apollo, enable_apify=enable_apify, enable_explorium=enable_explorium,
                explorium_api_key=explorium_api_key, apify_actor_override=apify_actor,
                target=target, max_pages=max_pages,
                validated_companies=validated_companies,
            )
            raw_batch = result["leads"]
            apollo_total += result["counts"]["apollo"]
            apify_total  += result["counts"]["apify"]
            explorium_total += result["counts"]["explorium"]
            total_raw    += len(raw_batch)
            push_stats(raw=total_raw, apollo=apollo_total, apify=apify_total, explorium=explorium_total)

            if not raw_batch:
                pl.log.warning("Page %d returned 0 leads — stopping.", page)
                break

            # Dedupe (cumulative across pages)
            push_stage(5, f"Deduplication — page {page}")
            clean_batch, seen_emails, seen_fingerprints = pl.dedupe_leads(
                raw_batch, seen_emails, seen_fingerprints
            )
            total_deduped += len(clean_batch)
            push_stats(deduped=total_deduped)

            if not clean_batch:
                page += 1
                continue

            # LinkedIn cross-verify — company/title vs. LinkedIn, before other checks
            push_stage(6, f"LinkedIn Cross-Verify — page {page}")
            clean_batch = pl.linkedin_cross_verify_leads(
                clean_batch,
                on_progress=lambda done, total: push_stage_progress(6, done, total),
            )

            # Domain match — independent of email verification
            push_stage(7, f"Domain Match — page {page}")
            clean_batch = pl.domain_match_leads(clean_batch)

            # Verify
            push_stage(8, f"Email Verification — page {page}")
            verified_batch = pl.verify_emails(
                clean_batch, provider=verifier_provider,
                on_progress=lambda done, total: push_stage_progress(8, done, total),
            )

            newly = [l for l in verified_batch if l.get("_confidence_score", 0) >= 95.0]
            verified_pool.extend(verified_batch)
            verified_high_conf_count += len(newly)

            invalid_count  += sum(1 for l in verified_batch if l.get("_verification_status") == "invalid")
            catchall_count += sum(1 for l in verified_batch if l.get("_verification_status") == "catch-all")
            push_stats(
                verified=verified_high_conf_count,
                invalid=invalid_count,
                catchall=catchall_count,
            )
            push_progress(verified_high_conf_count, target, page)

            if verified_high_conf_count >= target:
                break
            page += 1

        # ── Stage 9: Composite Scoring + Sample Selection ────────────────────
        push_stage(9, "Composite Scoring & Sample Selection")
        verified_pool = pl.compute_composite_scores(verified_pool, icp)
        sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=None)
        push_stats(sample=len(sample))

        # ── Stage 10: Claim Verification ─────────────────────────────────────
        push_stage(10, "Verifying Claims")
        sample = pl.verify_claims_for_leads(
            sample, icp, on_progress=lambda done, total: push_stage_progress(10, done, total),
        )
        sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

        # ── Stage 11: Organization Enrichment ────────────────────────────────
        push_stage(11, "Enriching Organizations")
        sample = pl.enrich_organizations_for_leads(
            sample, on_progress=lambda done, total: push_stage_progress(11, done, total),
        )

        # ── Stage 12: Exporting CSV ────────────────────────────────────────────
        push_stage(12, "Exporting CSV")
        os.makedirs("./output", exist_ok=True)
        csv_path, report = pl.export_csv(
            sample=sample,
            all_leads_raw=total_raw,
            all_leads_deduped=total_deduped,
            all_leads_verified=len(verified_pool),
            output_dir="./output",
        )

        bounce_rate = round(
            sum(1 for l in sample if l.get("_verification_status") == "invalid")
            / max(len(sample), 1) * 100, 1
        )

        # Serialise leads (preserve all keys for the UI, don't cut anything!)
        def clean_lead(l):
            item = {k: v for k, v in l.items() if not k.startswith("_")}
            item["status"] = l.get("_verification_status", "")
            # "score" is now the Stage 7 composite (email + LinkedIn + domain
            # match + ICP fit) so existing sort/filter/badge UI gets the
            # smarter ranking for free; raw signals are exposed too for the
            # drawer breakdown.
            item["score"] = l.get("_composite_score", l.get("_confidence_score", 0.0))
            item["email_confidence"] = l.get("_confidence_score", 0.0)
            item["domain_signal"] = l.get("_domain_signal", "UNKNOWN")
            item["linkedin_signal"] = l.get("_linkedin_signal", "NO_LINKEDIN")
            item["linkedin_company_match"] = l.get("_linkedin_company_match")
            item["linkedin_title_match"] = l.get("_linkedin_title_match")
            item["linkedin_current_employee"] = l.get("_linkedin_current_employee")
            item["claim_verification_signal"] = l.get("_claim_verification_signal")
            item["claim_verification_evidence"] = l.get("_claim_verification_evidence")
            if "location" not in item:
                item["location"] = l.get("location", "")
            return item

        run["results"] = {
            "sample":   [clean_lead(l) for l in sample],
            "csv_path": csv_path,
            "stats": {
                "raw":         total_raw,
                "apollo":      apollo_total,
                "apify":       apify_total,
                "explorium":   explorium_total,
                "deduped":     total_deduped,
                "verified":    len(verified_pool),
                "invalid":     invalid_count,
                "catchall":    catchall_count,
                "sample":      len(sample),
                "bounce_rate": bounce_rate,
                "pages":       page,
            },
            "icp": icp,
        }
        save_run_metadata(run, csv_path, icp)
        run["status"] = "complete"
        q.put({"type": "complete", "data": run["results"]})

    except Exception as exc:
        run["status"] = "error"
        run["error"]  = str(exc)
        q.put({"type": "error", "message": str(exc)})
        logger.exception("Pipeline error for run %s", run_id)

    finally:
        pl.log.removeHandler(handler)
        q.put(None)   # sentinel — tells the SSE generator to stop


def run_pipeline_imported_thread(run_registry: RunRegistry, run_id: str, inquiry: str, raw_leads: list[dict], target: int = 25, verifier_provider: str = "custom"):
    run = run_registry.get(run_id)
    q: queue.Queue = run["queue"]

    # Attach our custom handler to the pipeline module's logger
    handler = RunQueueHandler(q)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pl.log.addHandler(handler)

    def push_stage(stage: int, label: str):
        q.put({"type": "stage", "stage": stage, "label": label})

    def push_stats(**kwargs):
        q.put({"type": "stats", **kwargs})

    def push_stage_progress(stage: int, done: int, total: int):
        q.put({"type": "stage_progress", "stage": stage, "done": done, "total": total})

    try:
        run["status"] = "running"

        # ── Stage 1: Parse ───────────────────────────────────────────────────
        push_stage(1, "Parsing Inquiry")
        icp = pl.parse_inquiry(inquiry)
        q.put({"type": "icp", "data": icp})

        # ── Stages 2-3: Company Discovery/Validation (skipped — companies ────
        # already exist in the uploaded file). Fired instantly as no-ops
        # purely so this thread's stage numbering stays a single shared
        # contract with run_pipeline_thread's — both drive the same sidebar
        # by absolute stage number.
        push_stage(2, "Company Discovery (skipped — companies from import)")
        push_stage(3, "Company Validation (skipped — companies from import)")

        # ── Stage 4: Scrape (Skipped / Imported) ─────────────────────────────
        push_stage(4, "Importing Leads (Scraping Skipped)")
        pl.log.info("Processing %d raw leads uploaded from file...", len(raw_leads))
        push_stats(raw=len(raw_leads), apollo=0, apify=len(raw_leads))

        # ── Stage 5: Deduplicate ─────────────────────────────────────────────
        push_stage(5, "Deduplication & Cleaning")
        clean_batch, seen_emails, seen_fingerprints = pl.dedupe_leads(raw_leads)
        total_deduped = len(clean_batch)
        push_stats(deduped=total_deduped)

        if not clean_batch:
            raise ValueError("All imported leads were duplicates or empty.")

        # ── Stage 6: LinkedIn Cross-Verify ────────────────────────────────────
        push_stage(6, "LinkedIn Cross-Verify")
        clean_batch = pl.linkedin_cross_verify_leads(
            clean_batch,
            on_progress=lambda done, total: push_stage_progress(6, done, total),
        )

        # ── Stage 7: Domain Match ─────────────────────────────────────────────
        push_stage(7, "Domain Match")
        clean_batch = pl.domain_match_leads(clean_batch)

        # ── Stage 8: Verify ──────────────────────────────────────────────────
        push_stage(8, "Email Verification")
        verified_batch = pl.verify_emails(
            clean_batch, provider=verifier_provider,
            on_progress=lambda done, total: push_stage_progress(8, done, total),
        )
        verified_pool = verified_batch
        verified_high_conf = [l for l in verified_batch if l.get("_confidence_score", 0) >= 95.0]

        invalid_count  = sum(1 for l in verified_batch if l.get("_verification_status") == "invalid")
        catchall_count = sum(1 for l in verified_batch if l.get("_verification_status") == "catch-all")
        push_stats(
            verified=len(verified_high_conf),
            invalid=invalid_count,
            catchall=catchall_count,
        )

        # ── Stage 9: Composite Scoring + Select sample ───────────────────────
        push_stage(9, "Composite Scoring & Sample Selection")
        verified_pool = pl.compute_composite_scores(verified_pool, icp)
        sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=target)
        push_stats(sample=len(sample))

        # ── Stage 10: Claim Verification ─────────────────────────────────────
        push_stage(10, "Verifying Claims")
        sample = pl.verify_claims_for_leads(
            sample, icp, on_progress=lambda done, total: push_stage_progress(10, done, total),
        )
        sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

        # ── Stage 11: Organization Enrichment ────────────────────────────────
        push_stage(11, "Enriching Organizations")
        sample = pl.enrich_organizations_for_leads(
            sample, on_progress=lambda done, total: push_stage_progress(11, done, total),
        )

        # ── Stage 12: Exporting CSV ────────────────────────────────────────────
        push_stage(12, "Exporting CSV")
        os.makedirs("./output", exist_ok=True)
        csv_path, report = pl.export_csv(
            sample=sample,
            all_leads_raw=len(raw_leads),
            all_leads_deduped=total_deduped,
            all_leads_verified=len(verified_pool),
            output_dir="./output",
        )

        bounce_rate = round(
            sum(1 for l in sample if l.get("_verification_status") == "invalid")
            / max(len(sample), 1) * 100, 1
        )

        # Serialise leads (strip internal _ keys for the UI)
        def clean_lead(l):
            # Preserve every field the uploaded file/scraper actually had
            # (city, state, phone, employee_count, etc.) instead of the
            # narrow hardcoded list this used to return — that was silently
            # dropping columns even when the source data had them.
            item = {k: v for k, v in l.items() if not k.startswith("_")}
            item["status"] = l.get("_verification_status", "")
            item["score"] = l.get("_composite_score", l.get("_confidence_score", 0))
            item["email_confidence"] = l.get("_confidence_score", 0)
            item["domain_signal"] = l.get("_domain_signal", "UNKNOWN")
            item["linkedin_signal"] = l.get("_linkedin_signal", "NO_LINKEDIN")
            item["linkedin_company_match"] = l.get("_linkedin_company_match")
            item["linkedin_title_match"] = l.get("_linkedin_title_match")
            item["linkedin_current_employee"] = l.get("_linkedin_current_employee")
            item["claim_verification_signal"] = l.get("_claim_verification_signal")
            item["claim_verification_evidence"] = l.get("_claim_verification_evidence")
            return item

        run["results"] = {
            "sample":   [clean_lead(l) for l in sample],
            "csv_path": csv_path,
            "stats": {
                "raw":         len(raw_leads),
                "apollo":      0,
                "apify":       len(raw_leads),
                "deduped":     total_deduped,
                "verified":    len(verified_pool),
                "invalid":     invalid_count,
                "catchall":    catchall_count,
                "sample":      len(sample),
                "bounce_rate": bounce_rate,
                "pages":       1,
            },
            "icp": icp,
        }
        save_run_metadata(run, csv_path, icp)
        run["status"] = "complete"
        q.put({"type": "complete", "data": run["results"]})

    except Exception as exc:
        run["status"] = "error"
        run["error"]  = str(exc)
        q.put({"type": "error", "message": str(exc)})
        logger.exception("Pipeline error for run %s", run_id)

    finally:
        pl.log.removeHandler(handler)
        q.put(None)   # sentinel — tells the SSE generator to stop

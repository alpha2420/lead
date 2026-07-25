"""Background thread bodies that drive the lead-generation pipeline for a
single run, pushing progress/results onto that run's SSE queue."""

import logging
import os
import queue
import time

import pipeline as pl

from .logging_utils import RunIdFilter, RunQueueHandler
from .state import RunRegistry, save_run_metadata

logger = logging.getLogger(__name__)


def run_pipeline_thread(run_registry: RunRegistry, run_id: str, inquiry: str, target: int = 25, max_pages: int = 10,
                        verifier_provider: str = "gmail_bounce", icp: dict = None,
                        profile: str = "balanced",
                        min_composite_score: float = None):
    run = run_registry.get(run_id)
    q: queue.Queue = run["queue"]

    # Tag this thread as the active run (see pipeline.current_run_id) and
    # attach our custom handler to the pipeline module's logger, filtered to
    # only this run's records — without the filter, concurrent runs' SSE
    # streams would leak each other's log lines through the one shared
    # `pl.log` logger.
    pl.set_current_run_id(run_id)
    handler = RunQueueHandler(q)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RunIdFilter(run_id))
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

        geo_warnings = pl.validate_locations(pl._bi_all_locations(icp))["unrecognized"]
        if geo_warnings:
            q.put({"type": "geography_warning", "data": geo_warnings})

        # ── Stage 2: Search Planning ──────────────────────────────────────────
        # Translates the BI-layer ICP into a search strategy tailored to
        # code_crafter/leads-finder's real constraints (fixed industry enum,
        # literal keyword filters, no free text) — see
        # pipeline/search_planner.py's module docstring for the two gaps this
        # closes. Always returns a well-formed plan (falls back to a
        # rule-based one on any Gemini failure), so this stage never breaks
        # the run even without an API key.
        push_stage(2, "Planning Search Strategy")
        search_plan = pl.build_search_plan(icp)
        q.put({"type": "search_plan", "data": search_plan})

        # ── Stage 3: Company Discovery ───────────────────────────────────────
        # Company-first search: find candidate companies matching the ICP's
        # firmographics before searching for any people at all. See
        # pl.discover_companies() — a Gemini+web-search method.
        push_stage(3, "Discovering Companies")
        discovery = pl.discover_companies(icp, target=target, search_plan=search_plan)
        q.put({"type": "companies_discovered", "data": discovery["counts"]})

        # ── Stage 4: Company Validation ──────────────────────────────────────
        # Two tiers: free rule-based screen on every candidate, then a
        # capped AI fact-check via web search on the top-ranked survivors
        # (see pl.validate_companies() / _SOURCE_PROFILES[profile]
        # ["max_ai_validate_companies"]). Companies beyond the cap stay
        # validated (rule-only), never silently dropped.
        push_stage(4, "Validating Companies")
        validation = pl.validate_companies(discovery["companies"], icp, profile=profile)
        validated_companies = validation["validated"]
        q.put({"type": "companies_validated", "data": validation["counts"]})

        # ── Pagination loop (Stages 5-9) ─────────────────────────────────────
        push_stage(5, "Searching Leads")

        verified_pool:     list[dict] = []
        seen_emails:       set        = set()
        seen_fingerprints: set        = set()
        seen_linkedin_ids: set        = set()
        seen_phones:       set        = set()
        total_raw         = 0
        total_deduped     = 0
        total_quality_rejected = 0
        apify_total       = 0
        invalid_count     = 0
        catchall_count    = 0
        page              = 1

        verified_high_conf_count = 0
        while verified_high_conf_count < target and page <= max_pages:
            # Scrape — via the Source Orchestrator; see pl.run_lead_sources().
            # NOT scoped to validated_companies: Apify's leads-finder actor
            # has no company-name input to search "at" in the first place
            # (unlike Apollo/crawlerbros, which the company-first design
            # originally targeted) — its results already come from its own
            # ICP-filtered search (title/industry/size/keywords/location).
            # Post-filtering those against Company Discovery's separate,
            # much weaker web-search-snippet company list (independently
            # sourced, rarely name-matches) was live-confirmed to discard
            # 100% of real, correctly-targeted leads for no good reason.
            result = pl.run_lead_sources(icp, page, profile=profile, search_plan=search_plan)
            raw_batch = result["leads"]
            apify_total  += result["counts"]["apify"]
            total_raw    += len(raw_batch)
            push_stats(raw=total_raw, apify=apify_total)

            if not raw_batch:
                pl.log.warning("Page %d returned 0 leads — stopping.", page)
                break

            # Dedupe (cumulative across pages)
            push_stage(6, f"Deduplication — page {page}")
            clean_batch, seen_emails, seen_fingerprints, seen_linkedin_ids, seen_phones = pl.dedupe_leads(
                raw_batch, seen_emails, seen_fingerprints, seen_linkedin_ids, seen_phones
            )
            total_deduped += len(clean_batch)
            push_stats(deduped=total_deduped)

            # Quality gates — reject unusable records (no email/phone/LinkedIn)
            # before spending a paid LinkedIn/ZeroBounce call on them.
            clean_batch, rejected_batch = pl.apply_quality_gates(clean_batch, icp)
            total_quality_rejected += len(rejected_batch)
            push_stats(quality_rejected=total_quality_rejected)

            if not clean_batch:
                page += 1
                continue

            # LinkedIn cross-verify — company/title vs. LinkedIn, before other checks
            push_stage(7, f"LinkedIn Cross-Verify — page {page}")
            clean_batch = pl.linkedin_cross_verify_leads(
                clean_batch,
                on_progress=lambda done, total: push_stage_progress(7, done, total),
            )

            # Domain match — independent of email verification
            push_stage(8, f"Domain Match — page {page}")
            clean_batch = pl.domain_match_leads(clean_batch)

            # Verify
            push_stage(9, f"Email Verification — page {page}")
            verified_batch = pl.verify_emails(
                clean_batch, provider=verifier_provider,
                on_progress=lambda done, total: push_stage_progress(9, done, total),
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
            if verifier_provider == "gmail_bounce" and len(verified_pool) >= target:
                # gmail_bounce deliberately caps real sends to 1/run (see
                # pl.verify_emails_via_gmail_bounce()), so the >=95
                # "high confidence" gate above can almost never reach
                # `target` the way it does for custom/zerobounce, where
                # most leads get a real score quickly. Stop once we've
                # scraped/processed roughly `target` total leads instead
                # of always exhausting max_pages.
                break
            page += 1

        # ── Stage 10: Composite Scoring + Sample Selection ───────────────────
        push_stage(10, "Composite Scoring & Sample Selection")
        verified_pool = pl.compute_composite_scores(verified_pool, icp)
        sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=None, min_composite_score=min_composite_score)
        score_floor_underfilled = min_composite_score is not None and len(sample) < target
        push_stats(sample=len(sample))

        # ── Stage 11: Claim Verification ─────────────────────────────────────
        push_stage(11, "Verifying Claims")
        sample = pl.verify_claims_for_leads(
            sample, icp, on_progress=lambda done, total: push_stage_progress(11, done, total),
        )
        sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

        # ── Stage 12: Organization Enrichment ────────────────────────────────
        push_stage(12, "Enriching Organizations")
        sample = pl.enrich_organizations_for_leads(
            sample, on_progress=lambda done, total: push_stage_progress(12, done, total),
        )

        # ── Stage 13: AI Reranking ─────────────────────────────────────────────
        # Runs last, after claim verification + org enrichment, so it judges
        # the sample with its richest available evidence. Reorder-only —
        # never changes which leads are in the sample.
        push_stage(13, "AI Reranking")
        sample = pl.rerank_final_sample(sample, icp)

        # ── Stage 14: Exporting CSV ────────────────────────────────────────────
        push_stage(14, "Exporting CSV")
        os.makedirs("./output", exist_ok=True)
        csv_path, report = pl.export_csv(
            sample=sample,
            all_leads_raw=total_raw,
            all_leads_deduped=total_deduped,
            all_leads_verified=len(verified_pool),
            output_dir="./output",
            invalid_count=invalid_count,
        )

        # Planner History — logs this run's real outcome against the
        # industry value(s) Stage 2's plan actually used, so future runs'
        # prompts see empirical success rates alongside Gemini's semantic
        # judgment (see pipeline/search_planner.py). Never affects this
        # run — a logging failure is swallowed inside the function itself.
        pl.record_search_plan_outcome(search_plan, sample, target)

        # Pool-wide, not sample-wide: select_sample() hard-disqualifies every
        # "invalid" lead from ever reaching `sample`, so counting within
        # `sample` always yields 0.0% regardless of the run's real bounce rate.
        bounce_rate = round(invalid_count / max(len(verified_pool), 1) * 100, 1)

        # Serialise leads (preserve all keys for the UI, don't cut anything!)
        def clean_lead(l):
            item = {k: v for k, v in l.items() if not k.startswith("_")}
            item["status"] = l.get("_verification_status", "")
            # "score" is now the Stage 10 composite (email + LinkedIn + domain
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
            item["rerank_score"] = l.get("_rerank_score")
            item["rerank_rationale"] = l.get("_rerank_rationale")
            if "location" not in item:
                item["location"] = l.get("location", "")
            return item

        run["results"] = {
            "sample":   [clean_lead(l) for l in sample],
            "csv_path": csv_path,
            "stats": {
                "raw":         total_raw,
                "apify":       apify_total,
                "deduped":     total_deduped,
                "verified":    len(verified_pool),
                "invalid":     invalid_count,
                "catchall":    catchall_count,
                "sample":      len(sample),
                "bounce_rate": bounce_rate,
                "pages":       page,
                "quality_rejected": total_quality_rejected,
                "score_floor_underfilled": score_floor_underfilled,
                "runtime_seconds": round(time.time() - run.get("created_at", time.time()), 1),
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
        pl.set_current_run_id(None)
        q.put(None)   # sentinel — tells the SSE generator to stop


def run_pipeline_imported_thread(run_registry: RunRegistry, run_id: str, inquiry: str, raw_leads: list[dict], target: int = 25, verifier_provider: str = "gmail_bounce",
                                 min_composite_score: float = None):
    run = run_registry.get(run_id)
    q: queue.Queue = run["queue"]

    # Tag this thread as the active run (see pipeline.current_run_id) and
    # attach our custom handler to the pipeline module's logger, filtered to
    # only this run's records — without the filter, concurrent runs' SSE
    # streams would leak each other's log lines through the one shared
    # `pl.log` logger.
    pl.set_current_run_id(run_id)
    handler = RunQueueHandler(q)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RunIdFilter(run_id))
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

        # ── Stages 2-4: Search Planning/Company Discovery/Validation (skipped —
        # companies and their contacts already exist in the uploaded file, so
        # there's no search left to plan or scope). Fired instantly as no-ops
        # purely so this thread's stage numbering stays a single shared
        # contract with run_pipeline_thread's — both drive the same sidebar
        # by absolute stage number.
        push_stage(2, "Search Planning (skipped — companies from import)")
        push_stage(3, "Company Discovery (skipped — companies from import)")
        push_stage(4, "Company Validation (skipped — companies from import)")

        # ── Stage 5: Scrape (Skipped / Imported) ─────────────────────────────
        push_stage(5, "Importing Leads (Scraping Skipped)")
        pl.log.info("Processing %d raw leads uploaded from file...", len(raw_leads))
        push_stats(raw=len(raw_leads), apify=len(raw_leads))

        # ── Stage 6: Deduplicate ─────────────────────────────────────────────
        push_stage(6, "Deduplication & Cleaning")
        clean_batch, seen_emails, seen_fingerprints, seen_linkedin_ids, seen_phones = pl.dedupe_leads(raw_leads)
        total_deduped = len(clean_batch)
        push_stats(deduped=total_deduped)

        # Quality gates — reject unusable records (no email/phone/LinkedIn)
        # before spending a paid LinkedIn/ZeroBounce call on them.
        clean_batch, rejected_batch = pl.apply_quality_gates(clean_batch, icp)
        push_stats(quality_rejected=len(rejected_batch))

        if not clean_batch:
            raise ValueError("All imported leads were duplicates, empty, or failed quality gates.")

        # ── Stage 7: LinkedIn Cross-Verify ────────────────────────────────────
        push_stage(7, "LinkedIn Cross-Verify")
        clean_batch = pl.linkedin_cross_verify_leads(
            clean_batch,
            on_progress=lambda done, total: push_stage_progress(7, done, total),
        )

        # ── Stage 8: Domain Match ─────────────────────────────────────────────
        push_stage(8, "Domain Match")
        clean_batch = pl.domain_match_leads(clean_batch)

        # ── Stage 9: Verify ──────────────────────────────────────────────────
        push_stage(9, "Email Verification")
        verified_batch = pl.verify_emails(
            clean_batch, provider=verifier_provider,
            on_progress=lambda done, total: push_stage_progress(9, done, total),
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

        # ── Stage 10: Composite Scoring + Select sample ──────────────────────
        push_stage(10, "Composite Scoring & Sample Selection")
        verified_pool = pl.compute_composite_scores(verified_pool, icp)
        sample = pl.select_sample(verified_pool, icp, min_count=target, max_count=target, min_composite_score=min_composite_score)
        score_floor_underfilled = min_composite_score is not None and len(sample) < target
        push_stats(sample=len(sample))

        # ── Stage 11: Claim Verification ─────────────────────────────────────
        push_stage(11, "Verifying Claims")
        sample = pl.verify_claims_for_leads(
            sample, icp, on_progress=lambda done, total: push_stage_progress(11, done, total),
        )
        sample = pl.compute_composite_scores(sample, icp)   # re-rank with claim evidence folded in

        # ── Stage 12: Organization Enrichment ────────────────────────────────
        push_stage(12, "Enriching Organizations")
        sample = pl.enrich_organizations_for_leads(
            sample, on_progress=lambda done, total: push_stage_progress(12, done, total),
        )

        # ── Stage 13: AI Reranking ─────────────────────────────────────────────
        push_stage(13, "AI Reranking")
        sample = pl.rerank_final_sample(sample, icp)

        # ── Stage 14: Exporting CSV ────────────────────────────────────────────
        push_stage(14, "Exporting CSV")
        os.makedirs("./output", exist_ok=True)
        csv_path, report = pl.export_csv(
            sample=sample,
            all_leads_raw=len(raw_leads),
            all_leads_deduped=total_deduped,
            all_leads_verified=len(verified_pool),
            output_dir="./output",
            invalid_count=invalid_count,
        )

        # Pool-wide, not sample-wide: select_sample() hard-disqualifies every
        # "invalid" lead from ever reaching `sample`, so counting within
        # `sample` always yields 0.0% regardless of the run's real bounce rate.
        bounce_rate = round(invalid_count / max(len(verified_pool), 1) * 100, 1)

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
            item["rerank_score"] = l.get("_rerank_score")
            item["rerank_rationale"] = l.get("_rerank_rationale")
            return item

        run["results"] = {
            "sample":   [clean_lead(l) for l in sample],
            "csv_path": csv_path,
            "stats": {
                "raw":         len(raw_leads),
                "apify":       len(raw_leads),
                "deduped":     total_deduped,
                "verified":    len(verified_pool),
                "invalid":     invalid_count,
                "catchall":    catchall_count,
                "sample":      len(sample),
                "bounce_rate": bounce_rate,
                "pages":       1,
                "quality_rejected": len(rejected_batch),
                "score_floor_underfilled": score_floor_underfilled,
                "runtime_seconds": round(time.time() - run.get("created_at", time.time()), 1),
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
        pl.set_current_run_id(None)
        q.put(None)   # sentinel — tells the SSE generator to stop

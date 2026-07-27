    // ── Search mode toggle (AI chat vs. manual criteria form) ───────────────────
    function setSearchMode(mode) {
      document.getElementById('aiSearchPanel').style.display = mode === 'ai' ? 'flex' : 'none';
      document.getElementById('manualSearchPanel').style.display = mode === 'manual' ? 'flex' : 'none';
      document.getElementById('btn-mode-ai').classList.toggle('active', mode === 'ai');
      document.getElementById('btn-mode-manual').classList.toggle('active', mode === 'manual');
      if (mode === 'manual') renderManualSearchTags();
    }

    // ── Manual Search: same tag-editor pattern as the AI refinement panel,
    // just backed by local arrays instead of an AI-parsed ICP ───────────────────
    function renderManualSearchTags() {
      const rerender = () => renderManualSearchTags();
      renderRefinementTags('manual-job-titles', manualICP.jobTitles,
        v => { manualICP.jobTitles.push(v); rerender(); },
        i => { manualICP.jobTitles.splice(i, 1); rerender(); });
      renderRefinementTags('manual-industries', manualICP.industries,
        v => { manualICP.industries.push(v); rerender(); },
        i => { manualICP.industries.splice(i, 1); rerender(); });
      renderRefinementTags('manual-locations', manualICP.locations,
        v => { manualICP.locations.push(v); rerender(); },
        i => { manualICP.locations.splice(i, 1); rerender(); });
      renderRefinementTags('manual-technologies', manualICP.technologies,
        v => { manualICP.technologies.push(v); rerender(); },
        i => { manualICP.technologies.splice(i, 1); rerender(); });
      renderRefinementTags('manual-keywords', manualICP.keywords,
        v => { manualICP.keywords.push(v); rerender(); },
        i => { manualICP.keywords.splice(i, 1); rerender(); });
      renderRefinementTags('manual-exclusions', manualICP.exclusions,
        v => { manualICP.exclusions.push(v); rerender(); },
        i => { manualICP.exclusions.splice(i, 1); rerender(); });
    }

    // Manual Search now shares the one persistent #alertBox (see
    // templates/index.html) instead of its own #manualAlertBox — kept as
    // a thin alias so call sites don't need to change.
    function showManualAlert(type, msg) {
      showAlert(type, msg);
    }

    // Builds the same ICP object shape the AI produces, from manually-entered
    // criteria. Returns true/false rather than running the pipeline itself
    // — static/js/search-wizard.js's continueFromManual() calls this, then
    // advances to Section 2 (Refine ICP) on success, same as the AI/Website
    // paths. Stops short of startRun() (unlike this function's predecessor,
    // runManualSearch()) since Manual Search no longer has its own
    // one-click run shortcut — see search-wizard.js's module docstring.
    function buildICPFromManualFields() {
      if (!manualICP.jobTitles.length && !manualICP.industries.length &&
          !manualICP.locations.length && !manualICP.keywords.length) {
        showManualAlert('warning', 'Add at least a job title, industry, location, or keyword to search.');
        return false;
      }

      // Clear any leftover text in the AI chat box so startRun()'s
      // stale-ICP check can't second-guess the ICP we're about to build here.
      document.getElementById('inquiry').value = '';

      const empMin = parseInt(document.getElementById('manualEmpMin').value) || null;
      const empMax = parseInt(document.getElementById('manualEmpMax').value) || null;
      const revMin = parseInt(document.getElementById('manualRevMin').value) || null;
      const revMax = parseInt(document.getElementById('manualRevMax').value) || null;

      lastParsedICP = {
        icp_summary: 'Manually defined search criteria.',
        industry_intelligence: {
          primary_industry: manualICP.industries[0] || '',
          sub_industries: [...manualICP.industries],
          business_variations: [], adjacent_industries: [], industry_keywords: [],
          company_description_terms: [], exclude_industries: [],
        },
        geography_intelligence: {
          regions: [], countries: [...manualICP.locations], states: [], cities: [],
          priority_locations: [], excluded_locations: [],
        },
        technology_intelligence: {
          confirmed_technologies: [...manualICP.technologies],
          likely_technologies: [], competing_products: [], replacement_targets: [],
          technology_categories: [], technology_keywords: [],
        },
        buying_committee_intelligence: {
          primary_titles: [...manualICP.jobTitles], title_variations: [],
          departments: [], seniority: [],
          buying_role: manualICP.jobTitles.map(() => 'Influencer'),
          responsibilities: [], likely_kpis: [], common_pain_points: [],
        },
        company_intelligence: {
          company_type: [], business_model: '', distribution_model: [], manufacturing_model: [],
          customer_segments: [], sales_channels: [], service_regions: [],
          company_size_min: empMin, company_size_max: empMax,
          revenue_min: revMin, revenue_max: revMax,
          company_stage: [], ownership: [], languages: [],
        },
        market_intelligence: {
          competitors: [], market_position: [], certifications: [], industry_associations: [],
          compliance_requirements: [], procurement_patterns: [],
        },
        intent_intelligence: {
          growth_signals: [], technology_signals: [], hiring_signals: [],
          financial_signals: [], expansion_signals: [], executive_change_signals: [],
        },
        search_intelligence: {
          business_keywords: [...manualICP.keywords], product_keywords: [],
          buying_signal_keywords: [], negative_keywords: [...manualICP.exclusions],
        },
        lead_scoring: [],
        data_sources: [],
        confidence: { score: null, reasoning: 'Manually defined — not AI-scored.', missing_info: [], clarifying_questions: [] }
      };

      renderICP(lastParsedICP);
      return true;
    }

    function syncRefinementPanelOnly() {
      const emptyState = document.getElementById('refinementEmptyState');
      if (!lastParsedICP) {
        document.getElementById('refinementSection').style.display = 'none';
        if (emptyState) emptyState.style.display = 'block';
        return;
      }
      if (emptyState) emptyState.style.display = 'none';
      document.getElementById('refinementSection').style.display = 'flex';

      // Lazily initialize every BI module this panel touches — guards
      // against a missing key without assuming anything about a field's
      // *shape* (a differently-restructured schema would still need its own
      // migration, same as before).
      ['industry_intelligence', 'geography_intelligence', 'technology_intelligence',
       'buying_committee_intelligence', 'company_intelligence', 'market_intelligence',
       'intent_intelligence', 'search_intelligence'].forEach(m => {
        if (!lastParsedICP[m]) lastParsedICP[m] = {};
      });

      const rerender = () => { syncRefinementPanelOnly(); renderICP(lastParsedICP); };

      // Generic binder: wires one refinement-input-wrap element to one BI
      // module's array field, lazily initializing the array itself.
      const bindTagField = (elementId, module, field, onChangeExtra) => {
        if (!Array.isArray(lastParsedICP[module][field])) lastParsedICP[module][field] = [];
        renderRefinementTags(
          elementId,
          lastParsedICP[module][field],
          (val) => {
            lastParsedICP[module][field].push(val);
            if (onChangeExtra) onChangeExtra();
            rerender();
          },
          (index) => {
            lastParsedICP[module][field].splice(index, 1);
            if (onChangeExtra) onChangeExtra();
            rerender();
          }
        );
      };

      // ── Industry Intelligence ────────────────────────────────────────────
      bindTagField('refine-sub-industries', 'industry_intelligence', 'sub_industries', () => {
        lastParsedICP.industry_intelligence.primary_industry = lastParsedICP.industry_intelligence.sub_industries[0] || '';
      });
      bindTagField('refine-business-variations', 'industry_intelligence', 'business_variations');
      bindTagField('refine-adjacent-industries', 'industry_intelligence', 'adjacent_industries');
      bindTagField('refine-exclude-industries', 'industry_intelligence', 'exclude_industries');
      const primaryIndustryNote = document.getElementById('industry-primary-note');
      if (primaryIndustryNote) {
        primaryIndustryNote.textContent = 'Primary industry: ' + (lastParsedICP.industry_intelligence.primary_industry || '—');
      }

      // ── Geography Intelligence ───────────────────────────────────────────
      bindTagField('refine-regions', 'geography_intelligence', 'regions');
      bindTagField('refine-countries', 'geography_intelligence', 'countries');
      bindTagField('refine-states', 'geography_intelligence', 'states');
      bindTagField('refine-cities', 'geography_intelligence', 'cities');

      // ── Technology Intelligence ──────────────────────────────────────────
      bindTagField('refine-confirmed-tech', 'technology_intelligence', 'confirmed_technologies');
      bindTagField('refine-likely-tech', 'technology_intelligence', 'likely_technologies');
      bindTagField('refine-competing-products', 'technology_intelligence', 'competing_products');

      // ── Buying Committee ─────────────────────────────────────────────────
      bindTagField('refine-primary-titles', 'buying_committee_intelligence', 'primary_titles');
      bindTagField('refine-title-variations', 'buying_committee_intelligence', 'title_variations');
      bindTagField('refine-departments', 'buying_committee_intelligence', 'departments');
      bindTagField('refine-buying-role', 'buying_committee_intelligence', 'buying_role');

      // ── Company Intelligence ─────────────────────────────────────────────
      bindTagField('refine-company-stage', 'company_intelligence', 'company_stage');
      bindTagField('refine-ownership', 'company_intelligence', 'ownership');
      const sizeMinEl = document.getElementById('refine-company-size-min');
      const sizeMaxEl = document.getElementById('refine-company-size-max');
      if (sizeMinEl) sizeMinEl.value = lastParsedICP.company_intelligence.company_size_min ?? '';
      if (sizeMaxEl) sizeMaxEl.value = lastParsedICP.company_intelligence.company_size_max ?? '';

      // ── Market Intelligence — Competitors editable, rest AI-set display ──
      bindTagField('refine-competitors', 'market_intelligence', 'competitors');
      const marketPositionEl = document.getElementById('market-position-static');
      if (marketPositionEl) {
        const rawPositions = lastParsedICP.market_intelligence.market_position;
        const positions = Array.isArray(rawPositions) ? rawPositions : (rawPositions ? [rawPositions] : []);
        marketPositionEl.innerHTML = positions.length
          ? positions.map(p => `<span class="tag">${escapeHtml(String(p))}</span>`).join('')
          : '<span style="color:var(--text-dim); font-size:0.66rem;">None specified</span>';
      }

      // ── Intent Signals — AI-set, display-only ────────────────────────────
      const intentEl = document.getElementById('intent-signals-static');
      if (intentEl) {
        const intent = lastParsedICP.intent_intelligence || {};
        const allSignals = [
          ...(intent.growth_signals || []), ...(intent.technology_signals || []),
          ...(intent.hiring_signals || []), ...(intent.financial_signals || []),
          ...(intent.expansion_signals || []), ...(intent.executive_change_signals || []),
        ];
        intentEl.innerHTML = allSignals.length
          ? allSignals.map(s => `<span class="tag">${escapeHtml(String(s))}</span>`).join('')
          : '<span style="color:var(--text-dim); font-size:0.66rem;">None specified</span>';
      }

      // ── Search Intelligence ──────────────────────────────────────────────
      bindTagField('refine-business-terms', 'search_intelligence', 'business_keywords');
      bindTagField('refine-website-terms', 'search_intelligence', 'product_keywords');
      bindTagField('refine-negative-terms', 'search_intelligence', 'negative_keywords');
    }

    function onCompanyIntelligenceNumberChange() {
      if (!lastParsedICP) return;
      if (!lastParsedICP.company_intelligence) lastParsedICP.company_intelligence = {};
      const minVal = document.getElementById('refine-company-size-min').value;
      const maxVal = document.getElementById('refine-company-size-max').value;
      lastParsedICP.company_intelligence.company_size_min = minVal ? parseInt(minVal, 10) : null;
      lastParsedICP.company_intelligence.company_size_max = maxVal ? parseInt(maxVal, 10) : null;
    }

    function toggleStrategyModule(headerEl) {
      const mod = headerEl.closest('.strategy-module');
      if (mod) mod.classList.toggle('open');
    }

    // ── ICP Panel ──────────────────────────────────────────────────────────────
    // Shared by renderICP() and renderBuyerReport() — hoisted to module scope
    // so the Buyer ICP tab doesn't duplicate this markup.
    function makeTags(arr) {
      if (!arr || !arr.length) return `<span style="color:var(--text-dim); font-size:0.75rem;">None specified</span>`;
      return `<div class="icp-tags">${arr.map(t => `<span class="tag">${escapeHtml(String(t))}</span>`).join('')}</div>`;
    }

    function makeList(arr) {
      if (!arr || !arr.length) return `<span style="color:var(--text-dim); font-size:0.75rem;">None specified</span>`;
      return `<ul style="margin: 0; padding-left: 18px; font-size: 0.75rem; color: var(--text); line-height: 1.4; display: flex; flex-direction: column; gap: 4px;">
          ${arr.map(item => `<li>${escapeHtml(String(item))}</li>`).join('')}
        </ul>`;
    }

    function renderICP(icp) {
      lastParsedICP = icp;
      syncRefinementPanelOnly();
      const panel = document.getElementById('icpPanel');

      const card  = document.getElementById('icpCard');
      panel.innerHTML = '';

      // Defensive fallbacks at every level — old history items (pre-BI-schema)
      // and partial AI output both degrade to empty sections instead of
      // crashing. Business Intelligence layer: 8 intelligence modules, each
      // provider-agnostic — see pipeline.py's _icp_schema_block().
      const industry   = icp.industry_intelligence || {};
      const geo        = icp.geography_intelligence || {};
      const tech       = icp.technology_intelligence || {};
      const committee  = icp.buying_committee_intelligence || {};
      const company    = icp.company_intelligence || {};
      const market     = icp.market_intelligence || {};
      const intent     = icp.intent_intelligence || {};
      const search     = icp.search_intelligence || {};
      const leadScoring  = Array.isArray(icp.lead_scoring) ? icp.lead_scoring : [];
      const confidence   = icp.confidence || {};
      const icpSummary   = icp.icp_summary || 'No summary generated yet.';
      const inputFlags   = icp.input_flags || {};
      const aiSuggestions = Array.isArray(icp.ai_suggestions) ? icp.ai_suggestions : [];

      const committeeTitles = committee.primary_titles || [];
      const buyingRoles = committee.buying_role || [];
      const industryList = _dedupeArr(industry.primary_industry ? [industry.primary_industry, ...(industry.sub_industries || [])] : (industry.sub_industries || []));
      const allLocations = _dedupeArr([...(geo.countries || []), ...(geo.states || []), ...(geo.cities || [])]);
      const allExclusions = _dedupeArr([...(industry.exclude_industries || []), ...(geo.excluded_locations || []), ...(search.negative_keywords || [])]);
      const allTechFlat = _dedupeArr([...(tech.confirmed_technologies || []), ...(tech.likely_technologies || []), ...(tech.technology_categories || [])]);
      const searchKeywords = _dedupeArr([...(search.business_keywords || []), ...(search.product_keywords || [])]);

      const roleClass   = (v) => ({'decision maker':'icp-role-decision-maker','champion':'icp-role-champion','influencer':'icp-role-influencer','blocker':'icp-role-blocker'}[(v||'').toLowerCase()] || '');
      const weightClass = (v) => ({high:'icp-weight-high', medium:'icp-weight-medium', low:'icp-weight-low', negative:'icp-weight-negative'}[(v||'').toLowerCase()] || '');

      // Populate Top Bar
      const topBar = document.getElementById('icpTopBar');
      if (topBar) {
        const titlesText = committeeTitles.length ? committeeTitles.join(', ') : '';
        const industryText = industryList.join(', ');

        let businessParts = [];
        if (company.company_size_min || company.company_size_max) {
          businessParts.push(`${company.company_size_min || 0}-${company.company_size_max || '∞'}`);
        }
        if (company.company_stage && company.company_stage.length) businessParts.push(company.company_stage.join(', '));
        const businessText = businessParts.length ? businessParts.join(' · ') : '';

        const locationText = allLocations.length ? allLocations.join(', ') : '';

        topBar.innerHTML = `
          <div class="icp-top-card">
            <span class="icp-top-label">Title</span>
            <input type="text" class="icp-top-input" id="icp-edit-title" value="${escapeHtml(titlesText)}" placeholder="e.g. Founder, CEO">
          </div>
          <div class="icp-top-card">
            <span class="icp-top-label">Industry</span>
            <input type="text" class="icp-top-input" id="icp-edit-industry" value="${escapeHtml(industryText)}" placeholder="e.g. SaaS, Tech">
          </div>
          <div class="icp-top-card">
            <span class="icp-top-label">Business</span>
            <input type="text" class="icp-top-input" id="icp-edit-business" value="${escapeHtml(businessText)}" placeholder="e.g. 50-200, B2B">
          </div>
          <div class="icp-top-card">
            <span class="icp-top-label">Location</span>
            <input type="text" class="icp-top-input" id="icp-edit-location" value="${escapeHtml(locationText)}" placeholder="e.g. United States">
          </div>
          <button class="btn-run-custom" onclick="runCustomICP()">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            Scrape
          </button>
        `;
      }

      // Create strategic grid content — Business Intelligence layer: 8
      // intelligence modules + scoring/sources/confidence. Targeting-critical
      // modules (industry/geo/tech/committee/company/search) stay always
      // visible; supplementary/explanatory ones collapse into "More
      // Intelligence" accordions (same component as the AI Target Strategy
      // sidebar) so the tab doesn't read as a wall of 12 equal-weight cards.
      panel.innerHTML = `
        ${inputFlags.possible_injection ? `
        <div class="icp-warning">
          <span>⚠</span>
          <span>Part of your input looked like an instruction and was ignored — this ICP reflects only your legitimate targeting criteria.</span>
        </div>` : ''}

        <!-- Summary + Confidence -->
        <div class="icp-hero" style="grid-column: span 2;">
          <div class="icp-hero-main">
            <div class="icp-hero-label">ICP Summary</div>
            <div class="icp-hero-text">${escapeHtml(icpSummary)}</div>
          </div>
          <div class="icp-hero-confidence">
            <div class="icp-confidence-score">${confidence.score != null ? confidence.score : '—'}<span>/100</span></div>
            <div class="icp-confidence-label">Confidence</div>
          </div>
        </div>

        <!-- Industry -->
        <div class="icp-group">
          <div class="icp-section-title"><span class="icp-section-dot icp-dot-industry"></span>Industry</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Primary & Sub-Industries</div>
              ${makeTags(industryList)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Business-Type Variations</div>
              ${makeTags(industry.business_variations)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Adjacent Industries</div>
              ${makeTags(industry.adjacent_industries)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Industry Keywords</div>
              ${makeTags(industry.industry_keywords)}
            </div>
          </div>
        </div>

        <!-- Geography -->
        <div class="icp-group">
          <div class="icp-section-title"><span class="icp-section-dot icp-dot-geo"></span>Geography</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Regions</div>
              ${makeTags(geo.regions)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Countries / States / Cities</div>
              ${makeTags(allLocations)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Priority Locations</div>
              ${makeTags(geo.priority_locations)}
            </div>
          </div>
        </div>

        <!-- Technology -->
        <div class="icp-group">
          <div class="icp-section-title"><span class="icp-section-dot icp-dot-tech"></span>Technology</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Confirmed Technologies</div>
              ${makeTags(tech.confirmed_technologies)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Likely Technologies</div>
              ${makeTags(tech.likely_technologies)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Competing Products</div>
              ${makeTags(tech.competing_products)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Replacement Targets</div>
              ${makeTags(tech.replacement_targets)}
            </div>
          </div>
        </div>

        <!-- Buying Committee -->
        <div class="icp-group">
          <div class="icp-section-title"><span class="icp-section-dot icp-dot-committee"></span>Buying Committee</div>
          ${committeeTitles.length ? `
          <div class="icp-table-wrap">
            <table>
              <thead>
                <tr><th>Title</th><th>Buying Role</th></tr>
              </thead>
              <tbody>
                ${committeeTitles.map((t, i) => `
                  <tr>
                    <td style="white-space: normal; font-weight: 600; color: var(--text);">${escapeHtml(t)}</td>
                    <td class="icp-cell-strong ${roleClass(buyingRoles[i])}">${escapeHtml(buyingRoles[i] || '—')}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>` : `<span class="icp-empty">No buying committee members specified</span>`}
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px;">
            <div class="icp-item">
              <div class="icp-item-label">Title Variations</div>
              ${makeTags(committee.title_variations)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Departments & Seniority</div>
              ${makeTags(_dedupeArr([...(committee.departments || []), ...(committee.seniority || [])]))}
            </div>
          </div>
          <div class="icp-item" style="margin-top: 12px;">
            <div class="icp-item-label">Common Pain Points</div>
            ${makeList(committee.common_pain_points)}
          </div>
        </div>

        <!-- Company -->
        <div class="icp-group">
          <div class="icp-section-title"><span class="icp-section-dot icp-dot-company"></span>Company</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Company Type & Business Model</div>
              <div style="font-size: 0.75rem; color: var(--text); margin-bottom: 6px;">${escapeHtml(company.business_model || 'Not specified')}</div>
              ${makeTags(company.company_type)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Size & Stage</div>
              <div style="font-size: 0.75rem; color: var(--text); margin-bottom: 6px;">
                ${(company.company_size_min || company.company_size_max) ? `${company.company_size_min || 0} – ${company.company_size_max || '∞'} employees` : 'Any size'}
              </div>
              ${makeTags(company.company_stage)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Revenue & Ownership</div>
              <div style="font-size: 0.75rem; color: var(--text); line-height: 1.6;">
                <strong>Revenue:</strong> ${(company.revenue_min || company.revenue_max) ? `$${(company.revenue_min||0).toLocaleString()} – $${(company.revenue_max||'∞').toLocaleString ? company.revenue_max.toLocaleString() : company.revenue_max}` : 'Not specified'}<br>
                <strong>Ownership:</strong> ${company.ownership && company.ownership.length ? company.ownership.join(', ') : 'Any'}
              </div>
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Distribution & Sales Channels</div>
              ${makeTags(_dedupeArr([...(company.distribution_model || []), ...(company.sales_channels || [])]))}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Customer Segments & Service Regions</div>
              ${makeTags(_dedupeArr([...(company.customer_segments || []), ...(company.service_regions || [])]))}
            </div>
          </div>
        </div>

        <!-- Search Intelligence -->
        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-section-title"><span class="icp-section-dot icp-dot-search"></span>Search Intelligence</div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
            <div class="icp-item">
              <div class="icp-item-label">Business Keywords</div>
              ${makeTags(search.business_keywords)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Product Keywords</div>
              ${makeTags(search.product_keywords)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Buying Signal Keywords</div>
              ${makeTags(search.buying_signal_keywords)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label" style="color: var(--red);">Exclusions</div>
              ${makeList(allExclusions)}
            </div>
          </div>
        </div>

        <!-- Confidence detail — kept visible (not collapsed) since it's the
             most actionable feedback for refining the next prompt. -->
        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-section-title"><span class="icp-section-dot" style="background: var(--amber-light);"></span>Confidence & Next Steps</div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
            <div class="icp-item">
              <div class="icp-item-label">Reasoning</div>
              <div style="font-size: 0.75rem; color: var(--text);">${escapeHtml(confidence.reasoning || 'Not specified')}</div>
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Missing Information</div>
              ${makeList(confidence.missing_info)}
            </div>
            <div class="icp-item" style="grid-column: span 2;">
              <div class="icp-item-label">Clarifying Questions</div>
              ${makeList(confidence.clarifying_questions)}
            </div>
          </div>
        </div>

        <!-- More Intelligence — supplementary/explanatory modules, collapsed by default -->
        <div class="icp-more-title">More Intelligence</div>
        <div class="icp-more-group">
          <div class="strategy-module" data-module="icp-market">
            <div class="strategy-module-header" onclick="toggleStrategyModule(this)">
              <span class="strategy-module-icon">📈</span> Market Intelligence
              <span class="strategy-module-chevron">▾</span>
            </div>
            <div class="strategy-module-body">
              <div class="icp-item">
                <div class="icp-item-label">Competitors</div>
                ${makeTags(market.competitors)}
              </div>
              <div class="icp-item">
                <div class="icp-item-label">Market Position</div>
                ${makeTags(market.market_position)}
              </div>
              <div class="icp-item">
                <div class="icp-item-label">Certifications & Associations</div>
                ${makeTags(_dedupeArr([...(market.certifications || []), ...(market.industry_associations || [])]))}
              </div>
              <div class="icp-item">
                <div class="icp-item-label">Compliance Requirements</div>
                ${makeTags(market.compliance_requirements)}
              </div>
            </div>
          </div>

          <div class="strategy-module" data-module="icp-intent">
            <div class="strategy-module-header" onclick="toggleStrategyModule(this)">
              <span class="strategy-module-icon">⚡</span> Intent Signals
              <span class="strategy-module-chevron">▾</span>
            </div>
            <div class="strategy-module-body">
              <div class="icp-item">
                <div class="icp-item-label">Growth & Expansion Signals</div>
                ${makeTags(_dedupeArr([...(intent.growth_signals || []), ...(intent.expansion_signals || [])]))}
              </div>
              <div class="icp-item">
                <div class="icp-item-label">Technology & Hiring Signals</div>
                ${makeTags(_dedupeArr([...(intent.technology_signals || []), ...(intent.hiring_signals || [])]))}
              </div>
              <div class="icp-item">
                <div class="icp-item-label">Financial & Executive Signals</div>
                ${makeTags(_dedupeArr([...(intent.financial_signals || []), ...(intent.executive_change_signals || [])]))}
              </div>
            </div>
          </div>

          <div class="strategy-module" data-module="icp-scoring">
            <div class="strategy-module-header" onclick="toggleStrategyModule(this)">
              <span class="strategy-module-icon">📊</span> Lead Scoring Factors
              <span class="strategy-module-chevron">▾</span>
            </div>
            <div class="strategy-module-body">
              ${leadScoring.length ? `
              <div class="icp-table-wrap">
                <table>
                  <thead>
                    <tr><th>Factor</th><th style="width: 90px;">Weight</th><th>Reasoning</th></tr>
                  </thead>
                  <tbody>
                    ${leadScoring.map(f => `
                      <tr>
                        <td style="white-space: normal; font-weight: 600; color: var(--text);">${escapeHtml(f.factor || '—')}</td>
                        <td class="icp-cell-strong ${weightClass(f.weight)}">${escapeHtml(f.weight || '—')}</td>
                        <td style="white-space: normal; color: var(--text-muted);">${escapeHtml(f.reasoning || '—')}</td>
                      </tr>
                    `).join('')}
                  </tbody>
                </table>
              </div>` : `<span class="icp-empty">No scoring factors specified</span>`}
            </div>
          </div>

          ${aiSuggestions.length ? `
          <div class="strategy-module" data-module="icp-ai-ideas">
            <div class="strategy-module-header" onclick="toggleStrategyModule(this)">
              <span class="strategy-module-icon">✨</span> AI Ideas
              <span style="font-weight: 500; font-size: 0.62rem; color: var(--text-dim); text-transform: none; letter-spacing: normal; margin-left: 2px;">unverified opinion</span>
              <span class="strategy-module-chevron">▾</span>
            </div>
            <div class="strategy-module-body">
              ${aiSuggestions.map(s => `
                <div class="icp-item">
                  <div style="font-size: 0.78rem; color: var(--text); font-weight: 500;">${escapeHtml(s.suggestion || '')}</div>
                  <div style="font-size: 0.68rem; color: var(--text-dim); margin-top: 3px; line-height: 1.4;">
                    ${s.expected_coverage_impact ? `<strong>Coverage:</strong> ${escapeHtml(s.expected_coverage_impact)} &nbsp;·&nbsp; ` : ''}${s.expected_quality_impact ? `<strong>Quality:</strong> ${escapeHtml(s.expected_quality_impact)} &nbsp;·&nbsp; ` : ''}${s.tradeoff ? `<strong>Tradeoff:</strong> ${escapeHtml(s.tradeoff)}` : ''}
                  </div>
                </div>
              `).join('')}
            </div>
          </div>` : ''}
        </div>
      `;

      // Render search filters inside the dedicated #searchPanel element
      const searchPanel = document.getElementById('searchPanel');
      if (searchPanel) {
        const cleanTitles = committeeTitles;
        const cleanTitlesOR = cleanTitles.map(t => `"${t}"`).join(' OR ');
        const cleanIndustriesOR = industryList.map(i => `"${i}"`).join(' OR ');
        const cleanKeywords = searchKeywords;
        const cleanKeywordsOR = cleanKeywords.map(k => `"${k}"`).join(' OR ');

        const titleQuery = cleanTitles.length ? `(${cleanTitles.map(t => `title:"${t}"`).join(' OR ')})` : '';
        const industryQuery = industryList.length ? `(${industryList.map(i => `industry:"${i}"`).join(' OR ')})` : '';
        const keywordQuery = cleanKeywords.length ? `(${cleanKeywords.map(k => `"${k}"`).join(' OR ')})` : '';
        const combinedQuery = [titleQuery, industryQuery, keywordQuery].filter(Boolean).join(' AND ');

        searchPanel.innerHTML = `
          <!-- AI Search Plan (Stage 2) — filled in separately by renderSearchPlan()
               once that stage's SSE event arrives, right after this ICP-triggered
               reset so a stale prior run's plan never lingers on screen. -->
          <div id="searchPlanSection"></div>

          <!-- Target Search Filters Board -->
          <div class="apollo-filters-container">
            <div class="apollo-filters-header">
              <h3 style="display: flex; align-items: center; gap: 8px;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--purple-light)" stroke-width="2.5">
                  <circle cx="12" cy="12" r="10"/>
                  <polyline points="12 16 16 12 12 8"/>
                  <line x1="8" y1="12" x2="16" y2="12"/>
                </svg>
                🎯 Target Search Filter Settings
              </h3>
              <span style="font-size: 0.72rem; color: var(--text-dim);">Copy or reference these parameters directly for database search or prospecting filters</span>
            </div>
            <div class="apollo-filters-grid">
              <!-- 1. Employees -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">👥 # Employees</div>
                  <button class="apollo-btn-copy" id="btn-copy-employees" onclick="copyToClipboard('${jsStringAttr(`${(company.company_size_min || '')}${company.company_size_min && company.company_size_max ? '-' : ''}${(company.company_size_max || '')}`)}', 'btn-copy-employees')">Copy</button>
                </div>
                <div class="apollo-filter-value">
                  <span class="tag" style="background: rgba(99, 102, 241, 0.1); border-color: rgba(99, 102, 241, 0.2); font-weight: 700; font-size: 0.8rem; color: var(--purple-light); cursor: pointer;" onclick="copySingleTag(this, '${jsStringAttr(`${(company.company_size_min || 0)} - ${(company.company_size_max || '∞')}`)}')">
                    ${(company.company_size_min || 0)} - ${(company.company_size_max || '∞')}
                  </span>
                  <span style="font-size: 0.7rem; color: var(--text-muted); margin-left: 6px;">employees</span>
                </div>
              </div>

              <!-- 2. Industry -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🏷️ Industry</div>
                  <button class="apollo-btn-copy" id="btn-copy-industry" onclick="copyToClipboard('${jsStringAttr(industryList.join(', '))}', 'btn-copy-industry')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${industryList.length ? industryList.map(ind => `<span class="tag" style="background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.15); color: var(--amber); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(ind).replace(/'/g, "\\'")}')">${escapeHtml(ind)}</span>`).join('') : '<span class="tag" style="background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.15); color: var(--amber); cursor: pointer;" onclick="copySingleTag(this, \'Any\')">Any</span>'}
                </div>
              </div>

              <!-- 3. Job Titles -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🪪 Job Titles</div>
                  <button class="apollo-btn-copy" id="btn-copy-titles" onclick="copyToClipboard('${jsStringAttr(cleanTitles.join(', '))}', 'btn-copy-titles')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${cleanTitles.length ? cleanTitles.map(t => `<span class="tag" style="background: rgba(99, 102, 241, 0.08); border-color: rgba(99, 102, 241, 0.15); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(t).replace(/'/g, "\\'")}')">${escapeHtml(t)}</span>`).join('') : 'None'}
                </div>
              </div>

              <!-- 4. Title Variations -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🗝️ Title Variations</div>
                  <button class="apollo-btn-copy" id="btn-copy-dmk" onclick="copyToClipboard('${jsStringAttr((committee.title_variations || []).join(', '))}', 'btn-copy-dmk')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${(committee.title_variations || []).length ? committee.title_variations.map(t => `<span class="tag" style="background: rgba(255,255,255,0.02); opacity: 0.85; cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(t).replace(/'/g, "\\'")}')">${escapeHtml(t)}</span>`).join('') : 'None'}
                </div>
              </div>

              <!-- 5. Company (stage include / exclusions) -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🏢 Company</div>
                  <button class="apollo-btn-copy" id="btn-copy-company" onclick="copyToClipboard('${jsStringAttr(`Include: ${company.company_stage ? company.company_stage.join(', ') : ''} | Exclude: ${allExclusions.join(', ')}`)}', 'btn-copy-company')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="flex-direction: column; align-items: flex-start; gap: 10px; font-size: 0.72rem; width: 100%;">
                  <div style="width: 100%;">
                    <div style="color: var(--green); font-weight: 700; display: flex; align-items: center; gap: 4px; margin-bottom: 6px;">
                      <span>✔</span> <span>Include Stages:</span>
                    </div>
                    <div style="display: flex; flex-wrap: wrap; gap: 4px;">
                      ${company.company_stage && company.company_stage.length ? company.company_stage.map(s => `<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(s).replace(/'/g, "\\'")}')">${escapeHtml(s)}</span>`).join('') : '<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green); cursor: pointer;" onclick="copySingleTag(this, \'Any\')">Any</span>'}
                    </div>
                  </div>
                  <div style="width: 100%; border-top: 1px solid var(--border); padding-top: 8px; margin-top: 4px;">
                    <div style="color: var(--red); font-weight: 700; display: flex; align-items: center; gap: 4px; margin-bottom: 6px;">
                      <span>✖</span> <span>Exclude:</span>
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 6px;">
                      ${allExclusions.length ? allExclusions.map(s => `
                        <div style="display: flex; align-items: flex-start; gap: 6px; background: rgba(239, 68, 68, 0.05); border: 1px solid rgba(239, 68, 68, 0.12); border-radius: 6px; padding: 6px 10px; color: var(--red); font-size: 0.72rem; line-height: 1.35; cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(s).replace(/'/g, "\\'")}')">
                           <span style="font-weight: bold; margin-top: -1px; font-size: 0.8rem; flex-shrink: 0;">•</span>
                           <span>${escapeHtml(s)}</span>
                        </div>
                      `).join('') : '<div style="color: var(--text-muted); font-size: 0.7rem; font-style: italic; padding-left: 4px;">None</div>'}
                    </div>
                  </div>
                </div>
              </div>

              <!-- 6. Location -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">📍 Location</div>
                  <button class="apollo-btn-copy" id="btn-copy-location" onclick="copyToClipboard('${jsStringAttr(allLocations.join(', '))}', 'btn-copy-location')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${allLocations.length ? allLocations.map(g => `<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green-light); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(g).replace(/'/g, "\\'")}')">${escapeHtml(g)}</span>`).join('') : '<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green-light); cursor: pointer;" onclick="copySingleTag(this, \'Global\')">Global</span>'}
                </div>
              </div>

              <!-- 7. Technologies -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">⚙️ Technologies</div>
                  <button class="apollo-btn-copy" id="btn-copy-tech" onclick="copyToClipboard('${jsStringAttr(allTechFlat.join(', '))}', 'btn-copy-tech')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${allTechFlat.length ? allTechFlat.map(t => `<span class="tag" style="background: rgba(139, 92, 246, 0.08); border-color: rgba(139, 92, 246, 0.15); color: var(--violet); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(t).replace(/'/g, "\\'")}')">${escapeHtml(t)}</span>`).join('') : 'None'}
                </div>
              </div>

              <!-- 8. Copy-Paste Search Queries -->
              <div class="apollo-filter-card" style="grid-column: span 2;">
                <div class="apollo-filter-card-header" style="border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 12px;">
                  <div style="display: flex; flex-direction: column; gap: 4px;">
                    <div class="apollo-filter-title" style="font-size: 0.9rem; font-weight: 700;">⌨️ Copy-Paste Search Queries (Boolean)</div>
                    <span style="font-size: 0.72rem; color: var(--text-dim); line-height: 1.4;">
                      Use these pre-formatted search strings to quickly filter leads on LinkedIn Sales Navigator or other databases.
                    </span>
                  </div>
                </div>

                <div style="display: flex; flex-direction: column; gap: 14px; width: 100%;">
                  <!-- Row 1: Job Title Sub-Query -->
                  <div style="display: flex; flex-direction: column; gap: 6px;">
                    <div style="display: flex; align-items: center; justify-content: space-between;">
                      <div style="display: flex; flex-direction: column; gap: 2px;">
                        <span style="font-size: 0.72rem; font-weight: 700; color: var(--text-muted);">🪪 Job Title Filter Query</span>
                        <span style="font-size: 0.65rem; color: var(--text-dim);">Copy as OR query (e.g. "CFO" OR "VP Finance") for Title search fields</span>
                      </div>
                      <button class="apollo-btn-copy" id="btn-copy-sub-titles" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${jsTemplateAttr(cleanTitlesOR)}\`, 'btn-copy-sub-titles')">Copy Query</button>
                    </div>
                    <div class="boolean-box" style="display: flex; gap: 6px; flex-wrap: wrap; padding: 10px; max-height: none; overflow: visible; width: 100%;">
                      ${cleanTitles.map(t => `<span class="tag" style="background: rgba(99, 102, 241, 0.08); border-color: rgba(99, 102, 241, 0.15); font-weight: 500; font-size: 0.7rem; color: var(--purple-light); padding: 4px 8px; border-radius: 99px; line-height: 1.2; cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(t).replace(/'/g, "\\'")}')">${escapeHtml(t)}</span>`).join('')}
                    </div>
                  </div>

                  <!-- Row 2: Industry Sub-Query -->
                  <div style="display: flex; flex-direction: column; gap: 6px;">
                    <div style="display: flex; align-items: center; justify-content: space-between;">
                      <div style="display: flex; flex-direction: column; gap: 2px;">
                        <span style="font-size: 0.72rem; font-weight: 700; color: var(--text-muted);">🏷️ Industry Filter Query</span>
                        <span style="font-size: 0.65rem; color: var(--text-dim);">Copy as OR query (e.g. "finance" OR "software") for Industry search fields</span>
                      </div>
                      <button class="apollo-btn-copy" id="btn-copy-sub-industries" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${jsTemplateAttr(cleanIndustriesOR)}\`, 'btn-copy-sub-industries')">Copy Query</button>
                    </div>
                    <div class="boolean-box" style="display: flex; gap: 6px; flex-wrap: wrap; padding: 10px; max-height: none; overflow: visible; width: 100%;">
                      ${industryList.map(ind => `<span class="tag" style="background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.15); font-weight: 500; font-size: 0.7rem; color: var(--amber); padding: 4px 8px; border-radius: 99px; line-height: 1.2; cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(ind).replace(/'/g, "\\'")}')">${escapeHtml(ind)}</span>`).join('')}
                    </div>
                  </div>

                  <!-- Row 3: Keywords Sub-Query -->
                  <div style="display: flex; flex-direction: column; gap: 6px;">
                    <div style="display: flex; align-items: center; justify-content: space-between;">
                      <div style="display: flex; flex-direction: column; gap: 2px;">
                        <span style="font-size: 0.72rem; font-weight: 700; color: var(--text-muted);">🔍 Keywords Filter Query</span>
                        <span style="font-size: 0.65rem; color: var(--text-dim);">Copy as OR query (e.g. "security" OR "api") for Keyword search fields</span>
                      </div>
                      <button class="apollo-btn-copy" id="btn-copy-sub-keywords" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${jsTemplateAttr(cleanKeywordsOR)}\`, 'btn-copy-sub-keywords')">Copy Query</button>
                    </div>
                    <div class="boolean-box" style="display: flex; gap: 6px; flex-wrap: wrap; padding: 10px; max-height: none; overflow: visible; width: 100%;">
                      ${cleanKeywords.map(k => `<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); font-weight: 500; font-size: 0.7rem; color: var(--green-light); padding: 4px 8px; border-radius: 99px; line-height: 1.2; cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(k).replace(/'/g, "\\'")}')">${escapeHtml(k)}</span>`).join('')}
                    </div>
                  </div>

                  <!-- Row 4: Combined Query -->
                  <div style="display: flex; flex-direction: column; gap: 4px; border-top: 1px solid var(--border); padding-top: 10px; margin-top: 4px;">
                    <div style="display: flex; align-items: center; justify-content: space-between;">
                      <div style="display: flex; flex-direction: column; gap: 2px;">
                        <span style="font-size: 0.72rem; font-weight: 700; color: var(--purple-light);">🔗 Full Combined Search Query</span>
                        <span style="font-size: 0.65rem; color: var(--text-dim);">Deterministically built from titles, industries & keywords — paste into the main search bar</span>
                      </div>
                      <button class="apollo-btn-copy" id="btn-copy-boolean" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${jsTemplateAttr(combinedQuery)}\`, 'btn-copy-boolean')">Copy Combined Query</button>
                    </div>
                    <div class="boolean-box" style="font-family: monospace; font-size: 0.65rem; background: rgba(0,0,0,0.25); padding: 8px; border-radius: 6px; border: 1px solid rgba(99,102,241,0.15); max-height: 100px; overflow-y: auto; overflow-x: hidden; width: 100%; white-space: pre-wrap; word-break: break-word; line-height: 1.45;">
                      ${escapeHtml(combinedQuery)}
                    </div>
                  </div>
                </div>
              </div>

              <!-- 9. Raw ICP JSON Profile -->
              <div class="apollo-filter-card" style="grid-column: span 2;">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">📄 Raw ICP JSON Profile</div>
                  <button class="apollo-btn-copy" id="btn-copy-json" onclick="copyToClipboard(\`${jsTemplateAttr(JSON.stringify(icp, null, 2))}\`, 'btn-copy-json')">Copy JSON</button>
                </div>
                <pre class="boolean-box" style="font-family: monospace; font-size: 0.65rem; background: rgba(0,0,0,0.2); padding: 8px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); max-height: 200px; overflow: auto; width: 100%; margin: 0; white-space: pre-wrap; word-break: break-all; color: var(--purple-light);">${escapeHtml(JSON.stringify(icp, null, 2))}</pre>
              </div>
            </div>
          </div>
        `;
        document.getElementById('searchPlaceholder').style.display = 'none';
        document.getElementById('searchCard').style.display = 'block';
      }

      document.getElementById('icpPlaceholder').style.display = 'none';
      card.style.display = 'block';
      switchTab('tab-search');

      // Every ICP-producing path (chat, website, manual, history reload,
      // the ICP-tab's custom-run card) funnels through this one function —
      // hooking the wizard's Section 2 unlock here, rather than in each
      // caller, means it can never drift out of sync with one of them.
      if (typeof onICPReady === 'function') onICPReady();
    }

    // ── Search Plan Panel (Stage 2) ──────────────────────────────────────────
    // Renders pipeline/search_planner.py's real output for this run — what
    // Stage 2 actually resolved against the actor's fixed industry enum and
    // sent as company_keywords, not a client-side approximation like the
    // Apollo-style board below it. Reuses that board's exact CSS classes
    // (.apollo-filter-card, .tag, .icp-table-wrap) so the two sit together
    // as one visual system instead of introducing new styling.
    function renderSearchPlan(plan) {
      const section = document.getElementById('searchPlanSection');
      if (!section || !plan) return;

      const industries = Array.isArray(plan.industry_candidates) ? plan.industry_candidates : [];
      const industryRows = industries.map(c => `
        <tr>
          <td style="white-space: normal; font-weight: 600; color: var(--text);">${escapeHtml(c.value || '')}</td>
          <td class="icp-cell-strong" style="color: var(--amber);">${c.confidence != null ? Math.round(c.confidence) : '—'}%</td>
        </tr>
      `).join('');

      const posTag = (label) => `<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green-light);">${escapeHtml(label)}</span>`;
      const negTag = (label) => `<span class="tag" style="background: rgba(239, 68, 68, 0.08); border-color: rgba(239, 68, 68, 0.15); color: var(--red);">${escapeHtml(label)}</span>`;
      const typeTag = (label) => `<span class="tag" style="background: rgba(99, 102, 241, 0.08); border-color: rgba(99, 102, 241, 0.15); color: var(--purple-light);">${escapeHtml(label)}</span>`;

      const highPriority = Array.isArray(plan.high_priority_keywords) ? plan.high_priority_keywords : [];
      const secondary = Array.isArray(plan.secondary_keywords) ? plan.secondary_keywords : [];
      const negative = Array.isArray(plan.negative_keywords) ? plan.negative_keywords : [];
      const companyTypes = Array.isArray(plan.company_type_terms) ? plan.company_type_terms : [];

      section.innerHTML = `
        <div class="apollo-filters-container" style="margin-bottom: 16px;">
          <div class="apollo-filters-header">
            <h3 style="display: flex; align-items: center; gap: 8px;">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--violet)" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><polyline points="12 16 16 12 12 8"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
              🧭 AI Search Plan
            </h3>
            <span style="font-size: 0.72rem; color: var(--text-dim);">What Stage 2 actually resolved against the actor's fixed schema and used for this run — not a manual reference board.</span>
          </div>
          <div class="apollo-filters-grid">
            <div class="apollo-filter-card">
              <div class="apollo-filter-card-header">
                <div class="apollo-filter-title">🏷️ Resolved Industry (ranked)</div>
              </div>
              ${industryRows ? `
                <div class="icp-table-wrap">
                  <table><thead><tr><th>Value</th><th>Confidence</th></tr></thead><tbody>${industryRows}</tbody></table>
                </div>` : `<span class="icp-empty">No confident match in the actor's enum — industry filter omitted this run</span>`}
            </div>
            <div class="apollo-filter-card">
              <div class="apollo-filter-card-header">
                <div class="apollo-filter-title">🏢 Company Type</div>
              </div>
              <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                ${companyTypes.length ? companyTypes.map(typeTag).join('') : 'None'}
              </div>
            </div>
            <div class="apollo-filter-card" style="grid-column: span 2;">
              <div class="apollo-filter-card-header">
                <div class="apollo-filter-title">🎯 High-Priority Keywords</div>
                <button class="apollo-btn-copy" id="btn-copy-plan-hp" onclick="copyToClipboard('${jsStringAttr(highPriority.join(', '))}', 'btn-copy-plan-hp')">Copy</button>
              </div>
              <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                ${highPriority.length ? highPriority.map(posTag).join('') : 'None'}
              </div>
            </div>
            <div class="apollo-filter-card" style="grid-column: span 2;">
              <div class="apollo-filter-card-header">
                <div class="apollo-filter-title">🔎 Secondary Keywords <span style="font-weight:400; color: var(--text-dim); font-size: 0.65rem;">(fallback only)</span></div>
              </div>
              <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                ${secondary.length ? secondary.map(t => `<span class="tag" style="opacity: 0.75;">${escapeHtml(t)}</span>`).join('') : 'None'}
              </div>
            </div>
            <div class="apollo-filter-card" style="grid-column: span 2;">
              <div class="apollo-filter-card-header">
                <div class="apollo-filter-title">✖ Negative Keywords <span style="font-weight:400; color: var(--text-dim); font-size: 0.65rem;">(filtered from results after the fact — never sent to the actor)</span></div>
              </div>
              <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                ${negative.length ? negative.map(negTag).join('') : 'None'}
              </div>
            </div>
            <div class="apollo-filter-card" style="grid-column: span 2;">
              <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;">
                <div style="font-size: 0.75rem; color: var(--text); line-height: 1.5; flex: 1;">${escapeHtml(plan.reasoning || 'No reasoning provided.')}</div>
                <div style="text-align:center; flex-shrink: 0;">
                  <div style="font-size: 1.3rem; font-weight: 800; color: var(--violet);">${plan.confidence != null ? Math.round(plan.confidence) : '—'}<span style="font-size:0.7rem; color: var(--text-dim);">/100</span></div>
                  <div style="font-size: 0.62rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.03em;">Plan Confidence</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function _dedupeArr(arr) {
      const seen = new Set();
      const result = [];
      (arr || []).forEach(item => {
        const key = String(item).trim().toLowerCase();
        if (key && !seen.has(key)) { seen.add(key); result.push(String(item).trim()); }
      });
      return result;
    }


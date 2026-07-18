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

    function showManualAlert(type, msg) {
      const el = document.getElementById('manualAlertBox');
      el.className = `alert ${type} show`;
      el.textContent = msg;
    }

    // Builds the same ICP object shape the AI produces, from manually-entered
    // criteria, then runs the pipeline through the normal startRun() path.
    function runManualSearch() {
      if (!manualICP.jobTitles.length && !manualICP.industries.length &&
          !manualICP.locations.length && !manualICP.keywords.length) {
        showManualAlert('warning', 'Add at least a job title, industry, location, or keyword to search.');
        return;
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
      showManualAlert('success', 'Criteria applied — starting pipeline run…');
      startRun();
    }

    function syncRefinementPanelOnly() {
      if (!lastParsedICP) {
        document.getElementById('refinementSection').style.display = 'none';
        return;
      }
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
        const positions = lastParsedICP.market_intelligence.market_position || [];
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

    // ── Coverage Analysis ─────────────────────────────────────────────────────
    async function analyzeCoverage() {
      if (!lastParsedICP) return;
      const btn = document.getElementById('btnAnalyzeCoverage');
      const resultsEl = document.getElementById('coverageResults');
      const target = parseInt(document.getElementById('targetLeads')?.value) || 25;

      btn.disabled = true;
      btn.textContent = 'Analyzing…';
      resultsEl.style.display = 'none';

      try {
        const res = await fetch('/api/estimate-coverage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ icp: lastParsedICP, target }),
        });
        const data = await res.json();
        if (data.status !== 'success') throw new Error(data.message || 'Estimate failed');
        renderCoverageResults(data.estimate);
      } catch (err) {
        resultsEl.innerHTML = `<div class="coverage-unavailable">Coverage estimate failed: ${escapeHtml(err.message)}</div>`;
        resultsEl.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Re-analyze Coverage';
      }
    }

    function renderCoverageResults(estimate) {
      const resultsEl = document.getElementById('coverageResults');
      resultsEl.style.display = 'block';

      if (!estimate || !estimate.available) {
        const reason = (estimate && estimate.reason) || 'Coverage estimates are currently unavailable.';
        resultsEl.innerHTML = `<div class="coverage-unavailable">${escapeHtml(reason)}</div>`;
        return;
      }

      const stars = '★'.repeat(estimate.coverage_rating) + '☆'.repeat(5 - estimate.coverage_rating);

      const flagsHtml = estimate.narrow_flags.length
        ? estimate.narrow_flags.map(f => `
            <div class="coverage-flag">
              <span>⚠</span>
              <span>${escapeHtml(f.label)} may be reducing your results by ~${f.pct_reduction}% (${f.current_estimate.toLocaleString()} vs. ${f.without_estimate.toLocaleString()} without it)</span>
            </div>
          `).join('')
        : `<div class="coverage-empty-note">No unusually narrow filters detected.</div>`;

      const suggestionsHtml = estimate.suggestions.length
        ? estimate.suggestions.map((s, i) => `
            <div class="coverage-suggestion">
              <div class="coverage-suggestion-text">
                Include <strong>${escapeHtml(s.value)}</strong>
                <span class="coverage-suggestion-delta">+${s.estimated_reach_delta.toLocaleString()} estimated people</span>
              </div>
              <button class="coverage-include-btn" id="coverage-suggest-btn-${i}" onclick="includeCoverageSuggestion(${i}, this)">Include</button>
            </div>
          `).join('')
        : `<div class="coverage-empty-note">No broadening suggestions right now.</div>`;

      resultsEl.innerHTML = `
        <div class="coverage-stats">
          <div class="coverage-stat">
            <div class="coverage-stat-value">${estimate.estimated_companies.toLocaleString()}</div>
            <div class="coverage-stat-label">Est. Companies</div>
          </div>
          <div class="coverage-stat">
            <div class="coverage-stat-value">${estimate.estimated_people.toLocaleString()}</div>
            <div class="coverage-stat-label">Est. People</div>
          </div>
        </div>
        <div class="coverage-rating-row">${stars}</div>
        <div class="coverage-source-note">Estimated via Apollo.io — Apify/Explorium not yet supported</div>
        <div class="coverage-section-title">Potential Issues</div>
        ${flagsHtml}
        <div class="coverage-section-title">Suggestions</div>
        ${suggestionsHtml}
      `;

      // Stash suggestions on the element so includeCoverageSuggestion() can
      // read the exact value/field_to_apply without re-parsing the DOM.
      resultsEl._coverageSuggestions = estimate.suggestions;
    }

    function includeCoverageSuggestion(index, btnEl) {
      const resultsEl = document.getElementById('coverageResults');
      const suggestion = resultsEl._coverageSuggestions && resultsEl._coverageSuggestions[index];
      if (!suggestion || !lastParsedICP) return;

      const [module, field] = suggestion.field_to_apply === 'likely_technologies'
        ? ['technology_intelligence', 'likely_technologies']
        : ['industry_intelligence', 'industry_keywords'];

      if (!lastParsedICP[module]) lastParsedICP[module] = {};
      if (!Array.isArray(lastParsedICP[module][field])) lastParsedICP[module][field] = [];
      if (!lastParsedICP[module][field].includes(suggestion.value)) {
        lastParsedICP[module][field].push(suggestion.value);
      }

      btnEl.disabled = true;
      btnEl.textContent = 'Included';
      syncRefinementPanelOnly();
      renderICP(lastParsedICP);
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
      const dataSources  = icp.data_sources || [];
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

      const influenceColor = (v) => ({'decision maker':'#10b981','champion':'#34d399','influencer':'#f59e0b','blocker':'#ef4444'}[(v||'').toLowerCase()] || 'var(--text-muted)');
      const weightColor    = (v) => ({high:'#10b981', medium:'#f59e0b', low:'#9d9da8', negative:'#ef4444'}[(v||'').toLowerCase()] || 'var(--text-muted)');

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
      // intelligence modules + scoring/sources/confidence
      panel.innerHTML = `
        ${inputFlags.possible_injection ? `
        <div class="icp-group" style="grid-column: span 2; background: rgba(220, 38, 38, 0.06); border-color: rgba(220, 38, 38, 0.25);">
          <div style="font-size: 0.72rem; color: var(--text);">⚠ Part of your input looked like an instruction and was ignored — this ICP reflects only your legitimate targeting criteria.</div>
        </div>` : ''}

        <!-- 1. ICP Summary -->
        <div class="icp-group" style="grid-column: span 2; background: rgba(99, 102, 241, 0.03);">
          <div class="icp-group-title" style="color: var(--purple-light); font-size: 0.8rem;">⚡ 1. ICP Summary</div>
          <div style="font-size: 0.8rem; color: var(--text); line-height: 1.55; font-weight: 500;">${escapeHtml(icpSummary)}</div>
        </div>

        <!-- 2. Industry Intelligence -->
        <div class="icp-group">
          <div class="icp-group-title">🏷️ 2. Industry Intelligence</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Primary Industry & Sub-Industries</div>
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

        <!-- 3. Geography Intelligence -->
        <div class="icp-group">
          <div class="icp-group-title">🌍 3. Geography Intelligence</div>
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

        <!-- 4. Technology Intelligence -->
        <div class="icp-group">
          <div class="icp-group-title">💻 4. Technology Intelligence</div>
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

        <!-- 5. Buying Committee Intelligence -->
        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">🎯 5. Buying Committee Intelligence</div>
          ${committeeTitles.length ? `
          <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; font-size: 0.72rem; text-align: left;">
              <thead>
                <tr style="border-bottom: 1px solid var(--border);">
                  <th style="padding: 6px 12px 6px 0; color: var(--text-dim); text-transform: uppercase;">Title</th>
                  <th style="padding: 6px 0 6px 12px; color: var(--text-dim); text-transform: uppercase;">Buying Role</th>
                </tr>
              </thead>
              <tbody>
                ${committeeTitles.map((t, i) => `
                  <tr style="border-bottom: 1px solid var(--border); vertical-align: top;">
                    <td style="padding: 8px 12px 8px 0; font-weight: 600; color: var(--text);">${escapeHtml(t)}</td>
                    <td style="padding: 8px 0 8px 12px; font-weight: 700; color: ${influenceColor(buyingRoles[i])};">${escapeHtml(buyingRoles[i] || '—')}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>` : `<span style="color:var(--text-dim); font-size:0.75rem;">No buying committee members specified</span>`}
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Title Variations</div>
              ${makeTags(committee.title_variations)}
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Departments & Seniority</div>
              ${makeTags(_dedupeArr([...(committee.departments || []), ...(committee.seniority || [])]))}
            </div>
          </div>
          <div class="icp-item" style="margin-top: 10px;">
            <div class="icp-item-label">Common Pain Points</div>
            ${makeList(committee.common_pain_points)}
          </div>
        </div>

        <!-- 6. Company Intelligence -->
        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">🏢 6. Company Intelligence</div>
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
              <div style="font-size: 0.75rem; color: var(--text);">
                💰 <strong>Revenue:</strong> ${(company.revenue_min || company.revenue_max) ? `$${(company.revenue_min||0).toLocaleString()} – $${(company.revenue_max||'∞').toLocaleString ? company.revenue_max.toLocaleString() : company.revenue_max}` : 'Not specified'}<br>
                🏛️ <strong>Ownership:</strong> ${company.ownership && company.ownership.length ? company.ownership.join(', ') : 'Any'}
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

        <!-- 7. Market Intelligence -->
        <div class="icp-group">
          <div class="icp-group-title">📈 7. Market Intelligence</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
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

        <!-- 8. Intent Intelligence -->
        <div class="icp-group" style="background: rgba(16, 185, 129, 0.02); border-color: rgba(16, 185, 129, 0.1);">
          <div class="icp-group-title" style="color: var(--green-light); border-bottom-color: rgba(16, 185, 129, 0.08);">⚡ 8. Intent Intelligence</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
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

        <!-- 9. Search Intelligence & Exclusions -->
        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">🔍 9. Search Intelligence</div>
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

        <!-- 10. Lead Scoring Factors -->
        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">📊 10. Lead Scoring Factors</div>
          ${leadScoring.length ? `
          <div style="overflow-x: auto;">
            <table style="width: 100%; border-collapse: collapse; font-size: 0.72rem; text-align: left;">
              <thead>
                <tr style="border-bottom: 1px solid var(--border);">
                  <th style="padding: 6px 12px 6px 0; color: var(--text-dim); text-transform: uppercase;">Factor</th>
                  <th style="padding: 6px 12px; color: var(--text-dim); text-transform: uppercase; width: 90px;">Weight</th>
                  <th style="padding: 6px 0 6px 12px; color: var(--text-dim); text-transform: uppercase;">Reasoning</th>
                </tr>
              </thead>
              <tbody>
                ${leadScoring.map(f => `
                  <tr style="border-bottom: 1px solid var(--border); vertical-align: top;">
                    <td style="padding: 8px 12px 8px 0; font-weight: 600; color: var(--text);">${escapeHtml(f.factor || '—')}</td>
                    <td style="padding: 8px 12px; font-weight: 700; color: ${weightColor(f.weight)};">${escapeHtml(f.weight || '—')}</td>
                    <td style="padding: 8px 0 8px 12px; color: var(--text-muted);">${escapeHtml(f.reasoning || '—')}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>` : `<span style="color:var(--text-dim); font-size:0.75rem;">No scoring factors specified</span>`}
        </div>

        <!-- 11. Data Sources -->
        <div class="icp-group">
          <div class="icp-group-title">🗂️ 11. Data Sources</div>
          ${makeTags(dataSources)}
        </div>

        ${aiSuggestions.length ? `
        <!-- AI Ideas — the AI's own unverified opinion, distinct from Coverage Analysis's real-data-backed suggestions -->
        <div class="icp-group" style="grid-column: span 2; background: rgba(139, 92, 246, 0.03); border-color: rgba(139, 92, 246, 0.15);">
          <div class="icp-group-title" style="color: var(--violet); border-bottom-color: rgba(139, 92, 246, 0.1);">
            ✨ AI Ideas <span style="font-weight: 500; font-size: 0.62rem; color: var(--text-dim); text-transform: none; letter-spacing: normal;">(unverified opinion — not measured against real search data)</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
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

        <!-- 12. Confidence & Clarifying Questions -->
        <div class="icp-group" style="background: rgba(251, 191, 36, 0.02); border-color: rgba(251, 191, 36, 0.1);">
          <div class="icp-group-title" style="color: var(--amber-light); border-bottom-color: rgba(251, 191, 36, 0.08);">💡 12. Confidence</div>
          <div style="display: flex; flex-direction: column; gap: 10px;">
            <div class="icp-item">
              <div class="icp-item-label">Score</div>
              <div style="font-size: 0.9rem; font-weight: 700; color: var(--amber-light);">${confidence.score != null ? confidence.score : '—'} / 100</div>
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Reasoning</div>
              <div style="font-size: 0.75rem; color: var(--text);">${escapeHtml(confidence.reasoning || 'Not specified')}</div>
            </div>
            <div class="icp-item">
              <div class="icp-item-label">Missing Information</div>
              ${makeList(confidence.missing_info)}
            </div>
          </div>
        </div>
        <div class="icp-group" style="background: rgba(16, 185, 129, 0.02); border-color: rgba(16, 185, 129, 0.1);">
          <div class="icp-group-title" style="color: var(--green-light); border-bottom-color: rgba(16, 185, 129, 0.08);">❓ Clarifying Questions</div>
          ${makeList(confidence.clarifying_questions)}
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
                  <button class="apollo-btn-copy" id="btn-copy-employees" onclick="copyToClipboard('${(company.company_size_min || '')}${company.company_size_min && company.company_size_max ? '-' : ''}${(company.company_size_max || '')}', 'btn-copy-employees')">Copy</button>
                </div>
                <div class="apollo-filter-value">
                  <span class="tag" style="background: rgba(99, 102, 241, 0.1); border-color: rgba(99, 102, 241, 0.2); font-weight: 700; font-size: 0.8rem; color: var(--purple-light); cursor: pointer;" onclick="copySingleTag(this, '${(company.company_size_min || 0)} - ${(company.company_size_max || '∞')}')">
                    ${(company.company_size_min || 0)} - ${(company.company_size_max || '∞')}
                  </span>
                  <span style="font-size: 0.7rem; color: var(--text-muted); margin-left: 6px;">employees</span>
                </div>
              </div>

              <!-- 2. Industry -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🏷️ Industry</div>
                  <button class="apollo-btn-copy" id="btn-copy-industry" onclick="copyToClipboard('${industryList.join(', ').replace(/'/g, "\\'")}', 'btn-copy-industry')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${industryList.length ? industryList.map(ind => `<span class="tag" style="background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.15); color: var(--amber); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(ind).replace(/'/g, "\\'")}')">${escapeHtml(ind)}</span>`).join('') : '<span class="tag" style="background: rgba(245, 158, 11, 0.08); border-color: rgba(245, 158, 11, 0.15); color: var(--amber); cursor: pointer;" onclick="copySingleTag(this, \'Any\')">Any</span>'}
                </div>
              </div>

              <!-- 3. Job Titles -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🪪 Job Titles</div>
                  <button class="apollo-btn-copy" id="btn-copy-titles" onclick="copyToClipboard('${cleanTitles.join(', ')}', 'btn-copy-titles')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${cleanTitles.length ? cleanTitles.map(t => `<span class="tag" style="background: rgba(99, 102, 241, 0.08); border-color: rgba(99, 102, 241, 0.15); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(t).replace(/'/g, "\\'")}')">${escapeHtml(t)}</span>`).join('') : 'None'}
                </div>
              </div>

              <!-- 4. Title Variations -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🗝️ Title Variations</div>
                  <button class="apollo-btn-copy" id="btn-copy-dmk" onclick="copyToClipboard('${(committee.title_variations || []).join(', ')}', 'btn-copy-dmk')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${(committee.title_variations || []).length ? committee.title_variations.map(t => `<span class="tag" style="background: rgba(255,255,255,0.02); opacity: 0.85; cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(t).replace(/'/g, "\\'")}')">${escapeHtml(t)}</span>`).join('') : 'None'}
                </div>
              </div>

              <!-- 5. Company (stage include / exclusions) -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">🏢 Company</div>
                  <button class="apollo-btn-copy" id="btn-copy-company" onclick="copyToClipboard('Include: ${company.company_stage ? company.company_stage.join(', ') : ''} | Exclude: ${allExclusions.join(', ')}', 'btn-copy-company')">Copy</button>
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
                  <button class="apollo-btn-copy" id="btn-copy-location" onclick="copyToClipboard('${allLocations.join(', ')}', 'btn-copy-location')">Copy</button>
                </div>
                <div class="apollo-filter-value" style="display: flex; gap: 4px; flex-wrap: wrap;">
                  ${allLocations.length ? allLocations.map(g => `<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green-light); cursor: pointer;" onclick="copySingleTag(this, '${escapeHtml(g).replace(/'/g, "\\'")}')">${escapeHtml(g)}</span>`).join('') : '<span class="tag" style="background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.15); color: var(--green-light); cursor: pointer;" onclick="copySingleTag(this, \'Global\')">Global</span>'}
                </div>
              </div>

              <!-- 7. Technologies -->
              <div class="apollo-filter-card">
                <div class="apollo-filter-card-header">
                  <div class="apollo-filter-title">⚙️ Technologies</div>
                  <button class="apollo-btn-copy" id="btn-copy-tech" onclick="copyToClipboard('${allTechFlat.join(', ')}', 'btn-copy-tech')">Copy</button>
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
                      Use these pre-formatted search strings to quickly filter leads on LinkedIn Sales Navigator, Apollo, or other databases.
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
                      <button class="apollo-btn-copy" id="btn-copy-sub-titles" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${cleanTitlesOR.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`, 'btn-copy-sub-titles')">Copy Query</button>
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
                      <button class="apollo-btn-copy" id="btn-copy-sub-industries" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${cleanIndustriesOR.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`, 'btn-copy-sub-industries')">Copy Query</button>
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
                      <button class="apollo-btn-copy" id="btn-copy-sub-keywords" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${cleanKeywordsOR.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`, 'btn-copy-sub-keywords')">Copy Query</button>
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
                      <button class="apollo-btn-copy" id="btn-copy-boolean" style="padding: 3px 10px; font-size: 0.68rem;" onclick="copyToClipboard(\`${combinedQuery.replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`, 'btn-copy-boolean')">Copy Combined Query</button>
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
                  <button class="apollo-btn-copy" id="btn-copy-json" onclick="copyToClipboard(\`${JSON.stringify(icp, null, 2).replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$/g, '\\$')}\`, 'btn-copy-json')">Copy JSON</button>
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


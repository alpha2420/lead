    // ── Buyer ICP Generator — "who would buy a database describing this audience?" ──
    let lastBuyerICP = null;
    let lastBuyerReport = null;

    async function generateBuyerICP() {
      const description = document.getElementById('buyerDescription').value.trim();
      if (!description) {
        showAlert('warning', 'Describe the dataset or audience first.');
        return;
      }

      const btn = document.getElementById('btnGenerateBuyerICP');
      const originalLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Generating…';

      try {
        const res = await fetch('/api/generate-buyer-icp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ description }),
        });
        const data = await res.json();
        if (data.status !== 'success') throw new Error(data.message || 'Failed to generate buyer profile');

        lastBuyerICP = data.icp;
        lastBuyerReport = data.buyer_report;
        renderBuyerReport(lastBuyerReport);
        document.getElementById('buyerResults').style.display = 'block';
      } catch (err) {
        showAlert('error', `Failed to generate buyer profile: ${err.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    }

    function renderBuyerReport(report) {
      const panel = document.getElementById('buyerPanel');
      const size = report.company_size || {};
      const geo = report.target_geography || {};
      const searchKw = report.search_keywords || {};
      const ideal = report.ideal_customers || {};

      panel.innerHTML = `
        <div class="icp-group" style="grid-column: span 2; background: var(--blue-tint); border-color: #BFDBFE;">
          <div class="icp-group-title">🎯 Buyer ICP Summary</div>
          <div style="font-size:0.8rem; color:var(--text); line-height:1.55; font-weight:500;">${escapeHtml(report.icp_summary || '')}</div>
        </div>

        <div class="icp-group">
          <div class="icp-item-label" style="margin-bottom:6px;">Target Industries (${(report.target_industries || []).length})</div>
          ${makeTags(report.target_industries)}
        </div>

        <div class="icp-group">
          <div class="icp-item-label" style="margin-bottom:6px;">Target Company Types</div>
          ${makeTags(report.target_company_types)}
        </div>

        <div class="icp-group">
          <div class="icp-group-title">Company Size</div>
          <div style="font-size:0.78rem; color:var(--text); display:flex; flex-direction:column; gap:4px;">
            <div><b>${size.min_employees ?? '—'}</b> – <b>${size.max_employees ?? '—'}</b> employees</div>
            <div>${escapeHtml(size.revenue_range || 'Revenue range not specified')}</div>
          </div>
        </div>

        <div class="icp-group">
          <div class="icp-group-title">Target Geography</div>
          <div style="display:flex; flex-direction:column; gap:8px;">
            <div class="icp-item"><div class="icp-item-label">Countries</div>${makeTags(geo.countries)}</div>
            <div class="icp-item"><div class="icp-item-label">States</div>${makeTags(geo.states)}</div>
            <div class="icp-item"><div class="icp-item-label">Cities</div>${makeTags(geo.cities)}</div>
          </div>
        </div>

        <div class="icp-group">
          <div class="icp-item-label" style="margin-bottom:6px;">Buying Departments</div>
          ${makeTags(report.buying_departments)}
        </div>

        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">Decision Makers (${(report.decision_makers || []).length})</div>
          ${makeTags(report.decision_makers)}
        </div>

        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">Buying Keywords (${(report.buying_keywords || []).length})</div>
          ${makeTags(report.buying_keywords)}
        </div>

        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">Search Keywords by Platform</div>
          <div style="display:flex; flex-direction:column; gap:10px;">
            ${['apollo', 'amplify', 'explorium', 'google', 'linkedin'].map(platform => `
              <div class="icp-item">
                <div class="icp-item-label">${platform.charAt(0).toUpperCase() + platform.slice(1)}</div>
                ${makeTags(searchKw[platform])}
              </div>
            `).join('')}
          </div>
        </div>

        <div class="icp-group">
          <div class="icp-group-title">Buying Signals (${(report.buying_signals || []).length})</div>
          ${makeList(report.buying_signals)}
        </div>

        <div class="icp-group">
          <div class="icp-group-title">Exclusion Criteria</div>
          ${makeList(report.exclusion_criteria)}
        </div>

        <div class="icp-group" style="grid-column: span 2;">
          <div class="icp-group-title">Ideal Customers</div>
          <div style="display:flex; flex-direction:column; gap:10px; font-size:0.78rem; color:var(--text); line-height:1.5;">
            <div><div class="icp-item-label">Who</div>${escapeHtml(ideal.who || '')}</div>
            <div><div class="icp-item-label">Why</div>${escapeHtml(ideal.why || '')}</div>
            <div><div class="icp-item-label">How</div>${escapeHtml(ideal.how || '')}</div>
          </div>
        </div>
      `;
    }

    async function findBuyers() {
      if (!lastBuyerICP) {
        showAlert('warning', 'Generate a buyer profile first.');
        return;
      }

      const target = parseInt(document.getElementById('targetLeads').value) || 25;
      const max_pages = parseInt(document.getElementById('maxPages').value) || 10;
      const enable_apollo = document.getElementById('enableApollo').checked;
      const enable_apify = document.getElementById('enableApify').checked;
      const enable_explorium = document.getElementById('enableExplorium').checked;
      const explorium_api_key = document.getElementById('exploriumApiKey').value.trim();
      const verifier_provider = document.querySelector('input[name="verifierProvider"]:checked').value;

      if (!enable_apollo && !enable_apify && !enable_explorium) {
        showAlert('warning', 'Please enable at least one scraper (Apollo, Apify, or Explorium) to search for buyers.');
        return;
      }

      hideAlert();
      switchTab('tab-crm');
      resetUI();
      startPipelineTimer();
      setRunStatus('running', 'Finding buyers…');
      document.getElementById('globalProgressLine').style.display = 'block';

      const btn = document.getElementById('btnFindBuyers');
      const originalLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Starting…';

      try {
        const res = await fetch('/api/run-custom', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            icp: lastBuyerICP,
            target,
            max_pages,
            enable_apollo,
            enable_apify,
            enable_explorium,
            explorium_api_key,
            verifier_provider,
            profile: selectedSourceProfile,
            apify_actor: getSelectedApifyActor(),
          }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        connectSSE(data.run_id);
      } catch (err) {
        showAlert('error', `Failed to start buyer search: ${err.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    }

    function downloadBuyerReport() {
      if (!lastBuyerReport) return;
      const blob = new Blob([JSON.stringify(lastBuyerReport, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `buyer_profile_${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
    }

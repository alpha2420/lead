    // ── Helpers ────────────────────────────────────────────────────────────────
    // These two exist because dynamic (AI-generated or user-typed) text gets
    // embedded straight into inline onclick="..." attributes throughout the
    // ICP tab's "Copy" buttons — a value containing a literal quote character
    // breaks out of the JS string literal AND/OR the surrounding HTML
    // attribute otherwise, which is a real injectable-onclick bug, not just
    // theoretical (job titles/industries can contain apostrophes/quotes).
    // jsStringAttr: safe inside '...' (single-quoted JS string) inside "..."
    // (the onclick HTML attribute) — escape the JS-string layer first
    // (backslash, single-quote), then HTML-escape the result so a literal
    // double-quote can't end the HTML attribute early.
    function jsStringAttr(text) {
      return String(text ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '&quot;');
    }
    // jsTemplateAttr: same idea for `...` (template-literal) onclick args.
    function jsTemplateAttr(text) {
      return String(text ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/`/g, '\\`')
        .replace(/\$/g, '\\$')
        .replace(/"/g, '&quot;');
    }

    function copyToClipboard(text, btnId) {
      navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById(btnId);
        if (!btn) return;
        const oldText = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = oldText;
          btn.classList.remove('copied');
        }, 1500);
      }).catch(err => {
        console.error('Failed to copy:', err);
      });
    }

    function copySingleTag(element, text) {
      navigator.clipboard.writeText(text).then(() => {
        const oldText = element.textContent;
        element.textContent = 'Copied!';
        const oldBg = element.style.background;
        const oldBc = element.style.borderColor;
        const oldColor = element.style.color;
        
        element.style.setProperty('background', 'rgba(16, 185, 129, 0.15)', 'important');
        element.style.setProperty('border-color', 'rgba(16, 185, 129, 0.4)', 'important');
        element.style.setProperty('color', 'var(--green)', 'important');
        
        setTimeout(() => {
          element.textContent = oldText;
          element.style.background = oldBg;
          element.style.borderColor = oldBc;
          element.style.color = oldColor;
        }, 1200);
      }).catch(err => {
        console.error('Failed to copy single tag:', err);
      });
    }

    function setRunStatus(state, label) {
      const dot  = document.getElementById('statusDot');
      const text = document.getElementById('statusText');
      dot.className  = `status-dot ${state}`;
      text.textContent = label;
    }

    function resetUI() {
      selectedLeadKeys.clear();
      // Clear log
      const log = document.getElementById('logConsole');
      log.innerHTML = '';
      // Reset stages
      renderStages();
      // Pipeline Progress is collapsed by default (nothing to show before a
      // run starts) — expand it now, since a run is exactly what's happening.
      togglePipelineProgress(true);
      // Reset stats
      ['statRaw','statDeduped','statVerified','statRuntime'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = id === 'statRuntime' ? '0s' : '—';
      });
      const sourcesEl = document.getElementById('statSources');
      if (sourcesEl) sourcesEl.textContent = 'Apify source';
      // Hide table + ICP
      document.getElementById('panelTable').style.display = 'none';
      document.getElementById('crmPlaceholder').style.display = 'flex';
      document.getElementById('icpCard').style.display    = 'none';
      document.getElementById('icpPlaceholder').style.display = 'flex';
      document.getElementById('searchCard').style.display    = 'none';
      document.getElementById('searchPlaceholder').style.display = 'flex';
      // Reset charts
      if (chartVerify) { chartVerify.data.datasets[0].data = [0,0,0,0]; chartVerify.update(); }
    }

    function resetButton() {
      const btn = document.getElementById('btnRun');
      btn.disabled = false;
      btn.classList.remove('loading');
      document.getElementById('btnLabel').textContent = 'Run Pipeline';

      const btnImport = document.getElementById('btnImportRun');
      if (btnImport) {
        btnImport.disabled = false;
        btnImport.textContent = 'Upload & Run';
      }

      const btnRefine = document.getElementById('btnRefineRun');
      const spinnerRefine = document.getElementById('spinnerRefine');
      if (btnRefine) btnRefine.disabled = false;
      if (spinnerRefine) spinnerRefine.classList.remove('loading');


      document.getElementById('globalProgressLine').style.display = 'none';
    }

    function showAlert(type, msg) {
      const el = document.getElementById('alertBox');
      el.className = `alert ${type} show`;
      el.textContent = msg;
    }
    function hideAlert() {
      document.getElementById('alertBox').className = 'alert';
    }

    async function runCustomICP() {
      const titleVal = document.getElementById('icp-edit-title').value.trim();
      const industryVal = document.getElementById('icp-edit-industry').value.trim();
      const businessVal = document.getElementById('icp-edit-business').value.trim();
      const locationVal = document.getElementById('icp-edit-location').value.trim();

      let company_size_min = null;
      let company_size_max = null;
      const sizeMatch = businessVal.match(/(\d+)\s*[-–—]\s*(\d+)/);
      if (sizeMatch) {
        company_size_min = parseInt(sizeMatch[1]);
        company_size_max = parseInt(sizeMatch[2]);
      }
      // Remaining comma-separated terms (not the size range) become company_stage tags
      const businessTerms = businessVal
        .replace(/(\d+)\s*[-–—]\s*(\d+)/, '')
        .split(',').map(s => s.trim()).filter(Boolean);

      if (!lastParsedICP) {
        lastParsedICP = {};
      }

      const titles = titleVal.split(',').map(s => s.trim()).filter(Boolean);
      const industries = industryVal.split(',').map(s => s.trim()).filter(Boolean);
      const locations = locationVal.split(',').map(s => s.trim()).filter(Boolean);
      const prevIndustry = lastParsedICP.industry_intelligence || {};
      const prevGeo = lastParsedICP.geography_intelligence || {};
      const prevCompany = lastParsedICP.company_intelligence || {};
      const prevCommittee = lastParsedICP.buying_committee_intelligence || {};

      // Build every top-level key explicitly: fields the top bar edits are
      // fully replaced, fields it doesn't touch are carried forward with
      // safe defaults — no shallow-spreading stale nested objects.
      const customICP = {
        icp_summary: lastParsedICP.icp_summary || '',
        industry_intelligence: {
          ...prevIndustry,
          primary_industry: industries[0] || '',
          sub_industries: industries,
        },
        geography_intelligence: {
          ...prevGeo,
          countries: locations,
        },
        technology_intelligence: lastParsedICP.technology_intelligence || {},
        buying_committee_intelligence: {
          ...prevCommittee,
          primary_titles: titles,
          buying_role: titles.map(() => 'Decision Maker'),
        },
        company_intelligence: {
          ...prevCompany,
          company_size_min,
          company_size_max,
          company_stage: businessTerms.length ? businessTerms : (prevCompany.company_stage || []),
        },
        market_intelligence: lastParsedICP.market_intelligence || {},
        intent_intelligence: lastParsedICP.intent_intelligence || {},
        search_intelligence: lastParsedICP.search_intelligence || {},
        lead_scoring: lastParsedICP.lead_scoring || [],
        data_sources: lastParsedICP.data_sources || [],
        confidence: lastParsedICP.confidence || {},
      };

      const target = parseInt(document.getElementById('targetLeads').value) || 25;

      hideAlert();
      resetUI();
      startPipelineTimer();
      setRunStatus('running', 'Running custom search…');
      document.getElementById('globalProgressLine').style.display = 'block';

      // Keep button loading state
      const btn = document.getElementById('btnRun');
      const lbl = document.getElementById('btnLabel');
      if (btn) {
        btn.disabled = true;
        btn.classList.add('loading');
      }
      if (lbl) {
        lbl.textContent = 'Running pipeline…';
      }

      try {
        const res = await fetch('/api/run-custom', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            icp: customICP,
            target,
            verifier_provider: 'gmail_bounce',
            profile: selectedSourceProfile,
          })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        connectSSE(data.run_id);
      } catch (err) {
        showAlert('error', `Failed to start custom search run: ${err.message}`);
        resetButton();
      }
    }


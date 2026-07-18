    // ── State ──────────────────────────────────────────────────────────────────
    let allLeads = [];
    let sortKey  = null;
    let sortDir  = 1;
    let selectedLeadKeys = new Set();
    let chartSource = null;
    let chartVerify = null;
    let currentRunId = null;
    let pipelineStartTime = null;
    let pipelineTimerInterval = null;
    let lastParsedICP = null;
    let chatHistory = [];
    let manualICP = { jobTitles: [], industries: [], locations: [], technologies: [], keywords: [], exclusions: [] };

    function startPipelineTimer() {
      if (pipelineTimerInterval) clearInterval(pipelineTimerInterval);
      pipelineStartTime = Date.now();
      const el = document.getElementById('statRuntime');
      if (el) el.textContent = '0s';
      pipelineTimerInterval = setInterval(() => {
        const elapsed = Date.now() - pipelineStartTime;
        if (el) el.textContent = formatDuration(elapsed);
      }, 1000);
    }

    function stopPipelineTimer() {
      if (pipelineTimerInterval) {
        clearInterval(pipelineTimerInterval);
        pipelineTimerInterval = null;
      }
    }

    function formatDuration(ms) {
      const totalSecs = Math.floor(ms / 1000);
      const mins = Math.floor(totalSecs / 60);
      const secs = totalSecs % 60;
      if (mins > 0) {
        return `${mins}m ${secs}s`;
      }
      return `${secs}s`;
    }

    // CRM state
    let viewMode = 'table'; // 'table' or 'board'
    let selectedLeadIndex = null;
    let leadNotes = {};

    // Load notes from localStorage on startup
    try {
      const storedNotes = localStorage.getItem('leadNotes');
      if (storedNotes) {
        leadNotes = JSON.parse(storedNotes);
      }
    } catch (e) {
      console.error("Could not load lead notes", e);
    }

    const STAGES = [
      { id: 1, label: "Generate\nICP" },
      { id: 2, label: "Discover\nCompanies" },
      { id: 3, label: "Validate\nCompanies" },
      { id: 4, label: "Search\nLeads" },
      { id: 5, label: "Dedupe\n& Clean" },
      { id: 6, label: "LinkedIn\nVerify" },
      { id: 7, label: "Domain\nMatch" },
      { id: 8, label: "Email\nWaterfall" },
      { id: 9, label: "Composite\nScore" },
      { id: 10, label: "Verify\nClaims" },
      { id: 11, label: "Org\nEnrichment" },
      { id: 12, label: "Export\nCSV" },
    ];

    // ── Init ───────────────────────────────────────────────────────────────────
    function toggleTheme() {
      document.body.classList.toggle('dark-theme');
      const isDark = document.body.classList.contains('dark-theme');
      localStorage.setItem('theme', isDark ? 'dark' : 'light');
      const icon = document.getElementById('themeToggleIcon');
      const text = document.getElementById('themeToggleText');
      if (icon) icon.textContent = isDark ? '☀️' : '🌙';
      if (text) text.textContent = isDark ? 'Light' : 'Dark';
      updateChartThemeColors(!isDark);
    }

    document.addEventListener('DOMContentLoaded', () => {
      // Initialize Theme — light by default (Apollo-style), dark is opt-in
      const savedTheme = localStorage.getItem('theme') || 'light';
      const isDark = savedTheme === 'dark';
      if (isDark) {
        document.body.classList.add('dark-theme');
        const icon = document.getElementById('themeToggleIcon');
        const text = document.getElementById('themeToggleText');
        if (icon) icon.textContent = '☀️';
        if (text) text.textContent = 'Light';
      }

      renderStages();
      initCharts();
      updateChartThemeColors(!isDark);
      checkHealth();
      loadHistory(); // pre-load history
      renderManualSearchTags();
      initDrawerWidth();
      loadCsvMapperTemplateList();
      loadCsvMapperDownloads();
      loadCsvMapperAllLeads();
    });

    function updateChartThemeColors(isLight) {
      if (!chartVerify || !chartSource) return;
      
      const textColor = isLight ? '#52525b' : '#9d9da8';
      const gridColor = isLight ? 'rgba(9,9,11,0.06)' : 'rgba(255,255,255,0.05)';
      
      chartVerify.options.plugins.legend.labels.color = textColor;
      if (chartVerify.options.scales.x) {
        chartVerify.options.scales.x.ticks.color = isLight ? '#52525b' : '#63636e';
        chartVerify.options.scales.x.grid.color = gridColor;
      }
      if (chartVerify.options.scales.y) {
        chartVerify.options.scales.y.ticks.color = isLight ? '#52525b' : '#63636e';
        chartVerify.options.scales.y.grid.color = gridColor;
      }
      chartVerify.update();

      chartSource.options.plugins.legend.labels.color = textColor;
      chartSource.update();
    }

    // ── API Status Dropdown Toggle ─────────────────────────────────────────────
    let _apiPanelOpen = false;

    function toggleApiPanel() {
      _apiPanelOpen = !_apiPanelOpen;
      const panel   = document.getElementById('apiStatusPanel');
      const chevron = document.getElementById('apiStatusChevron');
      panel.style.display = _apiPanelOpen ? 'block' : 'none';
      if (chevron) chevron.style.transform = _apiPanelOpen ? 'rotate(180deg)' : 'rotate(0deg)';
    }

    // Close panel when clicking outside
    document.addEventListener('click', (e) => {
      const wrapper = document.getElementById('apiStatusWrapper');
      if (wrapper && !wrapper.contains(e.target) && _apiPanelOpen) {
        _apiPanelOpen = false;
        document.getElementById('apiStatusPanel').style.display = 'none';
        const chevron = document.getElementById('apiStatusChevron');
        if (chevron) chevron.style.transform = 'rotate(0deg)';
      }
    });

    function updateApiSummaryDot(data) {
      const apis = Object.values(data);
      const total  = apis.length;
      const online = apis.filter(a => a.status === 'ok').length;
      const hasError = apis.some(a => a.status === 'error' || a.status === 'unconfigured');
      const allOk   = online === total;
      const dot  = document.getElementById('apiOverallDot');
      const count = document.getElementById('apiOnlineCount');
      if (dot) dot.style.background = allOk ? '#10b981' : hasError ? '#ef4444' : '#f59e0b';
      if (count) count.textContent = `${online}/${total} online`;
    }

    // ── API Health Check ───────────────────────────────────────────────────────
    async function checkHealth() {
      const btn = document.getElementById('btnRefresh');
      if (btn) btn.classList.add('spinning');

      // Reset all cards to "checking"
      ['gemini','apollo','apify','explorium','zerobounce'].forEach(api => {
        const dot = document.getElementById(`hdot-${api}`);
        const msg = document.getElementById(`hmsg-${api}`);
        const lat = document.getElementById(`hlat-${api}`);
        const card = document.getElementById(`hcard-${api}`);
        if (!dot) return;
        dot.className  = 'health-dot checking';
        msg.textContent = 'Checking…';
        lat.textContent = '—';
        lat.className   = 'health-latency';
        card.className  = 'health-card';
      });

      try {
        const res  = await fetch('/api/health');
        const data = await res.json();

        Object.entries(data).forEach(([api, info]) => {
          const dot  = document.getElementById(`hdot-${api}`);
          const msg  = document.getElementById(`hmsg-${api}`);
          const lat  = document.getElementById(`hlat-${api}`);
          const card = document.getElementById(`hcard-${api}`);
          if (!dot) return;

          const s = info.status;   // ok | error | warning | unconfigured
          const normalizedStatus = s === 'unconfigured' ? 'error' : s;

          dot.className  = `health-dot ${normalizedStatus}`;
          msg.textContent = info.message || '';
          card.className  = `health-card ${normalizedStatus}`;

          // Dynamically update card title
          const nameContainer = card.querySelector('.health-name');
          if (nameContainer) {
            const displayName = info.name || (api === 'zerobounce' ? 'Email Verifier' : api.charAt(0).toUpperCase() + api.slice(1));
            nameContainer.innerHTML = `<div class="health-dot ${normalizedStatus}" id="hdot-${api}"></div> ${displayName}`;
          }

          if (info.latency_ms > 0) {
            const ms    = info.latency_ms;
            const speed = ms < 500 ? 'fast' : ms < 1500 ? 'medium' : 'slow';
            lat.textContent = `${ms}ms`;
            lat.className   = `health-latency ${speed}`;
          }
        });

        updateApiSummaryDot(data);

        const now = new Date();
        const el = document.getElementById('healthLastCheck');
        if (el) el.textContent = `Last checked ${now.toLocaleTimeString()}`;

      } catch (err) {
        ['gemini','apollo','apify','explorium','zerobounce'].forEach(api => {
          const dot = document.getElementById(`hdot-${api}`);
          const msg = document.getElementById(`hmsg-${api}`);
          if (dot) dot.className = 'health-dot error';
          if (msg) msg.textContent = 'Could not reach server';
        });
        const dot = document.getElementById('apiOverallDot');
        if (dot) dot.style.background = '#ef4444';
      } finally {
        if (btn) btn.classList.remove('spinning');
      }
    }

    // "Validate ICP" is a frontend-only checkpoint row (no backend stage
    // number of its own — see pipelineStepValidateICP) that visually proves
    // the finalized ICP is locked before Search Leads calls Apollo/Apify/
    // Explorium. It's driven by two real signals instead of a stage number:
    // the SSE 'icp' event (-> active) and the Search Leads stage starting
    // (-> done). 'pending' | 'active' | 'done'.
    let icpValidationState = 'pending';

    function markICPGenerated() {
      if (icpValidationState === 'pending') icpValidationState = 'active';
      reapplyICPValidationState();
    }

    function reapplyICPValidationState() {
      // The 'icp' SSE event fires while the backend is still logically on
      // stage 1 (it hasn't called push_stage(2, ...) yet), so the generic
      // data-stage comparison in updateSidebarStages() still shows Generate
      // ICP as merely "active". Once icpValidationState leaves 'pending' we
      // know generation is actually finished, so force that row to done too.
      if (icpValidationState !== 'pending') {
        const generateRow = document.querySelector('.pipeline-step[data-stage="0"]');
        if (generateRow && !generateRow.classList.contains('done')) {
          generateRow.classList.remove('active');
          generateRow.classList.add('done');
          const genNumEl = generateRow.querySelector('.step-num');
          if (genNumEl) genNumEl.innerHTML = '✓';
        }
      }

      const row = document.getElementById('pipelineStepValidateICP');
      if (!row) return;
      const numEl = row.querySelector('.step-num');
      row.classList.remove('active', 'done');
      if (icpValidationState === 'done') {
        row.classList.add('done');
        if (numEl) numEl.innerHTML = '✓';
      } else if (icpValidationState === 'active') {
        row.classList.add('active');
      }
      updatePipelineOverallProgress(document.querySelectorAll('.pipeline-step.done').length, document.querySelectorAll('.pipeline-step').length);
    }

    function renderStages() {
      // New sidebar pipeline steps are already in HTML; just do initial state
      icpValidationState = 'pending';
      updateSidebarStages(-1);
      for (let i = 1; i <= STAGES.length; i++) {
        const bar = document.getElementById(`stepProgress-${i}`);
        if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
      }
    }

    function updateSidebarStages(activeStageNum) {
      const steps = document.querySelectorAll('.pipeline-step');
      steps.forEach((step, idx) => {
        const stageNum = parseInt(step.getAttribute('data-stage'), 10);
        step.classList.remove('active', 'done');
        const numEl = step.querySelector('.step-num');
        if (numEl) numEl.textContent = (idx + 1).toString();

        if (Number.isNaN(stageNum)) return; // checkpoint row — see reapplyICPValidationState()

        if (stageNum < activeStageNum) {
          step.classList.add('done');
          if (numEl) numEl.innerHTML = '✓';
        }
        if (stageNum === activeStageNum) {
          step.classList.add('active');
        }
      });
      // The forEach above unconditionally wiped the checkpoint row's classes
      // too (it shares .pipeline-step); restore its real, event-driven state.
      reapplyICPValidationState();
    }

    function updatePipelineOverallProgress(doneCount, total) {
      const badge = document.getElementById('pipelineOverallBadge');
      const fill = document.getElementById('pipelineOverallFill');
      if (badge) {
        badge.textContent = `${doneCount}/${total}`;
        badge.classList.toggle('all-done', total > 0 && doneCount === total);
      }
      if (fill) fill.style.width = total ? `${Math.round((doneCount / total) * 100)}%` : '0%';
    }

    function setStage(stageId) {
      // Discover Companies (stage 2) starting is the real proof that ICP
      // validation finished — every downstream call, starting with company
      // discovery, only happens with a finalized ICP in hand (see
      // build_icp_fit_scorer()/discover_companies()/scrape_* in
      // pipeline.py, none of which ever receive raw inquiry text).
      if (stageId >= 2) icpValidationState = 'done';
      updateSidebarStages(stageId - 1);
      STAGES.forEach(s => {
        const el = document.getElementById(`stage-${s.id}`);
        if (!el) return;
        el.classList.remove('active', 'done');
        if (s.id < stageId)  el.classList.add('done');
        if (s.id === stageId) el.classList.add('active');
      });
      // A new stage started — hide every step's mini progress bar; the
      // active one gets it back only once real stage_progress events arrive.
      for (let i = 1; i <= STAGES.length; i++) {
        const bar = document.getElementById(`stepProgress-${i}`);
        if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
      }
    }

    function updateStepProgress(stage, done, total) {
      const bar = document.getElementById(`stepProgress-${stage}`);
      if (!bar || !total) return;
      const pct = Math.min(100, Math.round((done / total) * 100));
      bar.style.display = 'flex';
      bar.innerHTML = `
        <div class="step-progress-track"><div class="step-progress-fill" style="width:${pct}%"></div></div>
        <span class="step-progress-label">${done}/${total}</span>
      `;
    }

    function completeAllStages() {
      icpValidationState = 'done';
      const steps = document.querySelectorAll('.pipeline-step');
      steps.forEach(step => {
        step.classList.remove('active');
        step.classList.add('done');
        const numEl = step.querySelector('.step-num');
        if (numEl) numEl.innerHTML = '✓';
      });
      updatePipelineOverallProgress(steps.length, steps.length);
      for (let i = 1; i <= STAGES.length; i++) {
        const bar = document.getElementById(`stepProgress-${i}`);
        if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
      }
      STAGES.forEach(s => {
        const el = document.getElementById(`stage-${s.id}`);
        if (!el) return;
        el.classList.remove('active');
        el.classList.add('done');
      });
    }


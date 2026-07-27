    // ── Run Pipeline ───────────────────────────────────────────────────────────
    function connectSSE(runId) {
      currentRunId = runId;
      document.getElementById('runIdLabel').textContent = `#${runId}`;

      // Connect SSE
      const es = new EventSource(`/api/stream/${runId}`);
      const accumStats = {};

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data);

        if (msg.type === 'done') { es.close(); return; }

        if (msg.type === 'log') {
          appendLog(msg.level, msg.message);
        }

        if (msg.type === 'stage') {
          setStage(msg.stage);
          appendLog('INFO', `▶ Stage ${msg.stage}: ${msg.label}`);
        }

        if (msg.type === 'stage_progress') {
          updateStepProgress(msg.stage, msg.done, msg.total);
        }

        if (msg.type === 'icp') {
          renderICP(msg.data);
          markICPGenerated();
        }

        if (msg.type === 'search_plan') {
          renderSearchPlan(msg.data);
        }

        if (msg.type === 'stats') {
          Object.assign(accumStats, msg);
          updateStatCards(accumStats);
          updateCharts(accumStats);
        }

        if (msg.type === 'progress') {
          const pct = Math.min(100, Math.round(msg.collected / msg.target * 100));
          appendLog('INFO',
            `📊 Page ${msg.page} done — ${msg.collected}/${msg.target} verified leads (${pct}%)`
          );
          // Update verified stat card live
          updateStatCards({ verified: msg.collected });
        }

        if (msg.type === 'complete') {
          stopPipelineTimer();
          completeAllStages();
          setRunStatus('complete', 'Complete');
          resetButton();
          allLeads = msg.data.sample || [];
          renderCRMContent();
          document.getElementById('crmPlaceholder').style.display = 'none';
          document.getElementById('panelTable').style.display = 'flex';
          switchTab('tab-crm');
          document.getElementById('tableCount').textContent = `(${allLeads.length} leads)`;
          updateCharts(msg.data.stats);
          updateStatCards(msg.data.stats);
          const pages = msg.data.stats.pages || 1;
          if (msg.data.stats.sample < 25) {
            showAlert('warning', `⚠ Only ${msg.data.stats.sample} verified leads collected after ${pages} page(s). Try broadening your ICP or upgrading your Apify plan.`);
          }
          appendLog('INFO', `✅ Pipeline complete — ${allLeads.length} verified leads exported (${pages} page(s) scraped).`);
          loadHistory();
          if (typeof onRunComplete === 'function') onRunComplete(msg.data.stats);
        }

        if (msg.type === 'error') {
          stopPipelineTimer();
          setRunStatus('error', 'Error');
          resetButton();
          showAlert('error', `Pipeline error: ${msg.message}`);
          appendLog('ERROR', msg.message);
          if (typeof onRunError === 'function') onRunError(msg.message);
          es.close();
        }

        if (msg.type === 'cancelled') {
          stopPipelineTimer();
          setRunStatus('idle', 'Cancelled');
          resetButton();
          appendLog('INFO', '■ Pipeline cancelled by user.');
          if (typeof onRunCancelled === 'function') onRunCancelled();
          es.close();
        }
      };

      es.onerror = () => {
        stopPipelineTimer();
        if (currentRunId === runId) {
          es.close();
        }
      };
    }

    // Reconnects to an already-running pipeline's live progress — used by
    // Home's "View Live" (renderHomePipelineStatus(), home-dashboard.js),
    // which only has a run_id from GET /api/runs, not a fresh SSE
    // connection. If this same page/tab already opened the connection
    // (currentRunId matches — set by connectSSE() above), reuse it instead
    // of opening a second EventSource: /api/stream/<run_id> hands each
    // message to whichever consumer calls next on the run's plain
    // queue.Queue first, so two live connections to the same run would
    // silently split its messages between them rather than both seeing
    // everything.
    function viewLiveRun(runId) {
      openSearchDrawer();
      if (typeof enterProgressView === 'function') enterProgressView();
      if (currentRunId === runId) return;
      connectSSE(runId);
    }

    // POSTs a cancel request for the run currently shown in the progress
    // view. Cooperative, not instant — see app/state.py's
    // RunRegistry.request_cancel() docstring: the backend thread only
    // checks this at its own checkpoints, so the 'cancelled' SSE message
    // above may arrive anywhere from immediately to ~90s later (e.g. mid
    // gmail_bounce wait) depending which stage is running.
    async function stopRun() {
      if (!currentRunId) return;
      const btn = document.getElementById('btnStopRun');
      if (btn) { btn.disabled = true; btn.textContent = 'Stopping…'; }
      try {
        await fetch(`/api/run/${currentRunId}/cancel`, { method: 'POST' });
      } catch (err) {
        showAlert('error', `Failed to request cancellation: ${err.message}`);
        if (btn) { btn.disabled = false; btn.textContent = 'Stop Pipeline'; }
      }
    }

    // ── Pipeline Settings Toggle ────────────────────────────────────────────────
    function toggleSettings() {
      const content = document.getElementById('settingsContent');
      const icon = document.getElementById('settingsToggleIcon');
      content.classList.toggle('show');
      if (!icon) return;   // dashboard redesign dropped this chevron; nothing left to rotate
      if (content.classList.contains('show')) {
        icon.style.transform = 'rotate(0deg)';
      } else {
        icon.style.transform = 'rotate(-90deg)';
      }
    }

    // ── Pipeline Progress Toggle — collapsed by default (nothing actionable
    // to show before a run starts), auto-expanded by resetUI() once one does ──
    function togglePipelineProgress(forceOpen) {
      const content = document.getElementById('pipelineProgressContent');
      const icon = document.getElementById('pipelineProgressToggleIcon');
      if (!content || !icon) return;
      const shouldOpen = forceOpen === true ? true : forceOpen === false ? false : !content.classList.contains('show');
      content.classList.toggle('show', shouldOpen);
      icon.style.transform = shouldOpen ? 'rotate(0deg)' : 'rotate(-90deg)';
    }

    function setTargetLeads(value) {
      document.getElementById('targetLeads').value = value;
      document.querySelectorAll('#targetLeadsQuickPicks .quick-pick-btn').forEach(btn => {
        btn.classList.toggle('active', parseInt(btn.textContent, 10) === value);
      });
    }

    // Typing a custom value manually deselects every preset button, unless
    // it happens to match one of them exactly.
    function onTargetLeadsInput() {
      const value = parseInt(document.getElementById('targetLeads').value, 10);
      document.querySelectorAll('#targetLeadsQuickPicks .quick-pick-btn').forEach(btn => {
        btn.classList.toggle('active', parseInt(btn.textContent, 10) === value);
      });
    }

    // ── Source Strategy (backend Source Orchestrator profile) ───────────────────
    let selectedSourceProfile = 'balanced';
    function setSourceProfile(name) {
      selectedSourceProfile = name;
      document.querySelectorAll('#sourceProfileOptions .profile-option').forEach(el => {
        el.classList.toggle('active', el.dataset.profile === name);
      });
    }


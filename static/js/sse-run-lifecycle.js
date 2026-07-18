    // ── Run Pipeline ───────────────────────────────────────────────────────────
    async function startRun() {
      let inquiry = document.getElementById('inquiry').value.trim();

      // If the box holds text that was never actually sent to "Send to AI"
      // (typed fresh, then Run Pipeline clicked directly), any lastParsedICP
      // still in memory belongs to an earlier, unrelated conversation —
      // discard it so the backend parses THIS inquiry instead of silently
      // reusing a stale ICP.
      const wasChattedAbout = inquiry && chatHistory.some(m => m.role === 'user' && m.content === inquiry);
      if (inquiry && !wasChattedAbout) {
        lastParsedICP = null;
      }

      // If there is a chat history, we can use the first message as the inquiry summary
      if (!inquiry && chatHistory.length > 0) {
        const firstUserMsg = chatHistory.find(m => m.role === 'user');
        if (firstUserMsg) inquiry = firstUserMsg.content;
      }
      
      if (!inquiry && !lastParsedICP) {
        showAlert('warning', 'Please enter a target description or message the AI strategist before running the pipeline.');
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
        showAlert('warning', 'Please enable at least one scraper (Apollo, Apify, or Explorium) to search for leads.');
        return;
      }

      if (enable_explorium && !explorium_api_key) {
        showAlert('warning', 'Please enter your Explorium API Key to use Explorium search.');
        return;
      }

      hideAlert();
      resetUI();
      startPipelineTimer();
      setRunStatus('running', 'Running…');
      document.getElementById('globalProgressLine').style.display = 'block';

      const btn = document.getElementById('btnRun');
      const lbl = document.getElementById('btnLabel');
      btn.disabled = true;
      btn.classList.add('loading');
      lbl.textContent = 'Running pipeline…';

      const btnRefine = document.getElementById('btnRefineRun');
      const spinnerRefine = document.getElementById('spinnerRefine');
      if (btnRefine) btnRefine.disabled = true;
      if (spinnerRefine) spinnerRefine.classList.add('loading');


      let runId;
      try {
        const res  = await fetch('/api/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            inquiry: inquiry || 'Custom Chat Run',
            target,
            max_pages,
            enable_apollo,
            enable_apify,
            enable_explorium,
            explorium_api_key,
            verifier_provider,
            profile: selectedSourceProfile,
            apify_actor: getSelectedApifyActor(),
            icp: lastParsedICP
          })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        runId = data.run_id;
        connectSSE(runId);
      } catch (err) {
        showAlert('error', `Failed to start run: ${err.message}`);
        resetButton();
      }
    }

    function updateFileName() {
      const fileInput = document.getElementById('importFile');
      const fileNameDiv = document.getElementById('fileName');
      if (fileInput.files.length > 0) {
        fileNameDiv.textContent = `Selected: ${fileInput.files[0].name}`;
      } else {
        fileNameDiv.textContent = '';
      }
    }

    async function startImportRun() {
      const inquiry = document.getElementById('inquiry').value.trim();
      if (!inquiry) {
        showAlert('warning', 'Please enter a customer inquiry before running the pipeline.');
        return;
      }

      const fileInput = document.getElementById('importFile');
      if (fileInput.files.length === 0) {
        showAlert('warning', 'Please select a JSON or CSV file to import.');
        return;
      }

      const target = parseInt(document.getElementById('targetLeads').value) || 25;
      const verifier_provider = document.querySelector('input[name="verifierProvider"]:checked').value;

      hideAlert();
      resetUI();
      startPipelineTimer();
      setRunStatus('running', 'Running…');
      document.getElementById('globalProgressLine').style.display = 'block';

      const btn = document.getElementById('btnImportRun');
      btn.disabled = true;
      btn.textContent = 'Uploading & running…';

      const formData = new FormData();
      formData.append('inquiry', inquiry);
      formData.append('file', fileInput.files[0]);
      formData.append('target', target);
      formData.append('verifier_provider', verifier_provider);

      let runId;
      try {
        const res = await fetch('/api/run-imported', {
          method: 'POST',
          body: formData
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        runId = data.run_id;
        connectSSE(runId);
      } catch (err) {
        showAlert('error', `Failed to start import run: ${err.message}`);
        resetButton();
      }
    }

    // ── Log Console ────────────────────────────────────────────────────────────
    function appendLog(level, message) {
      const console_ = document.getElementById('logConsole');
      const empty = console_.querySelector('.log-empty');
      if (empty) empty.remove();

      const now  = new Date();
      const time = now.toTimeString().slice(0, 8);

      const line = document.createElement('div');
      line.className = 'log-line';
      line.innerHTML = `
        <span class="log-time">${time}</span>
        <span class="log-level ${level}">${level.padEnd(7)}</span>
        <span class="log-msg">${escapeHtml(message)}</span>
      `;
      console_.appendChild(line);
      console_.scrollTop = console_.scrollHeight;
    }

    function escapeHtml(text) {
      if (text === null || text === undefined) return '';
      return String(text)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    }

    // ── Stat Cards ─────────────────────────────────────────────────────────────
    let lastStats = {};
    function updateStatCards(stats) {
      if (!stats) return;
      lastStats = { ...lastStats, ...stats };

      const totalRaw = lastStats.raw || 0;
      animateCount('statRaw', totalRaw);

      // Verified percentage calculation
      const verifiedCount = lastStats.verified || 0;
      const verifiedPercent = totalRaw > 0 ? Math.round((verifiedCount / totalRaw) * 100) : 0;
      animateCount('statVerified', verifiedPercent, '%');

      // Duplicates count calculation
      const dedupedCount = lastStats.deduped || 0;
      const duplicatesCount = totalRaw > dedupedCount ? (totalRaw - dedupedCount) : 0;
      animateCount('statDeduped', duplicatesCount);

      if (lastStats.apollo !== undefined || lastStats.apify !== undefined || lastStats.explorium !== undefined) {
        const apollo = lastStats.apollo || 0;
        const apify = lastStats.apify || 0;
        const exp = lastStats.explorium || 0;
        document.getElementById('statSources').textContent =
          `Apollo: ${apollo}  ·  Apify: ${apify}  ·  Explorium: ${exp}`;
      }
    }

    function animateCount(id, target, suffix = '') {
      if (target === undefined || target === null) return;
      const el = document.getElementById(id);
      if (!el) return;
      
      let currentValText = el.textContent;
      if (suffix && currentValText.endsWith(suffix)) {
        currentValText = currentValText.slice(0, -suffix.length);
      }
      const current = parseInt(currentValText) || 0;
      const start = performance.now();
      const duration = 600;
      const step = (now) => {
        const p = Math.min((now - start) / duration, 1);
        const val = Math.round(current + (target - current) * ease(p));
        el.textContent = val + suffix;
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
    function ease(t) { return t < .5 ? 2*t*t : -1+(4-2*t)*t; }

    // Maps containerId -> onRemove(index) callback, set fresh on every sync
    // so removeRefinementTag (called from a plain onclick string) can look
    // up the right handler for whichever underlying data structure that
    // row is bound to (a flat array or a derived view of nested objects).
    let _refinementRemoveHandlers = {};

    function renderRefinementTags(containerId, displayList, onAdd, onRemove) {
      const container = document.getElementById(containerId);
      if (!container) return;
      _refinementRemoveHandlers[containerId] = onRemove;
      container.innerHTML = '';

      if (displayList && displayList.length) {
        displayList.forEach((item, index) => {
          const tag = document.createElement('span');
          tag.className = 'tag-removable';
          tag.innerHTML = `${escapeHtml(String(item))} <span class="tag-remove-btn" onclick="removeRefinementTag('${containerId}', ${index})">&times;</span>`;
          container.appendChild(tag);
        });
      }

      const input = document.createElement('input');
      input.className = 'tag-input-inline';
      input.placeholder = '+ Add';

      input.onkeydown = function(e) {
        if (e.key === 'Enter' || e.key === ',') {
          e.preventDefault();
          const val = input.value.trim().replace(/,$/, '');
          if (val) {
            onAdd(val);
          }
          input.value = '';
        }
      };
      container.appendChild(input);
    }

    function removeRefinementTag(containerId, index) {
      const handler = _refinementRemoveHandlers[containerId];
      if (handler) handler(index);
    }

    function updateRefinementPanel() {
      syncRefinementPanelOnly();
    }


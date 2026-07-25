    // ── Tabs Navigation Toggle ──────────────────────────────────────────────────
    const TAB_LABELS = {
      'tab-home': 'Home', 'tab-crm': 'Leads', 'tab-icp': 'ICP', 'tab-search': 'Search Filters',
      'tab-charts': 'Analytics', 'tab-history': 'Datasets', 'tab-log': 'Console',
      'tab-csvmap': 'CSV Mapper', 'tab-buyer': 'Buyer ICP', 'tab-allleads': 'All Leads'
    };

    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.getElementById(`btn-${tabId}`).classList.add('active');

      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      document.getElementById(tabId).classList.add('active');

      const crumb = document.getElementById('breadcrumbSection');
      if (crumb && TAB_LABELS[tabId]) crumb.textContent = TAB_LABELS[tabId];

      // tab-home has its own independent, all-time stat tiles inside its own
      // tab content (querySelector('.stats-bar') below still resolves to the
      // PERSISTENT per-run bar — it appears earlier in DOM order, outside
      // .tabs-container — so this correctly hides only that one, not Home's).
      const isFullBleed = tabId === 'tab-allleads' || tabId === 'tab-home';
      document.querySelector('.welcome-banner').style.display = isFullBleed ? 'none' : '';
      document.querySelector('.promo-banner').style.display = isFullBleed ? 'none' : '';
      document.querySelector('.stats-bar').style.display = isFullBleed ? 'none' : '';

      if (tabId === 'tab-history') {
        loadHistory();
      }
      if (tabId === 'tab-home') {
        loadHomeDashboard();
      }
    }

    // ── Search drawer (AI Strategist / ICP refinement / settings / import / progress) ──
    function openSearchDrawer() {
      document.getElementById('searchDrawer').classList.add('open');
      document.getElementById('searchDrawerBackdrop').classList.add('open');
    }
    function closeSearchDrawer() {
      document.getElementById('searchDrawer').classList.remove('open');
      document.getElementById('searchDrawerBackdrop').classList.remove('open');
    }

    // ── Search drawer resize (drag handle) + maximize toggle ──────────────
    // Default width is wider than before; a drag handle on the left edge
    // allows free-form resize, and a maximize button snaps to near-full-
    // width and back. Both the width and the maximized flag persist across
    // sessions via localStorage, same convention as the theme toggle.
    const DRAWER_DEFAULT_WIDTH = 560;
    const DRAWER_MIN_WIDTH = 400;
    const DRAWER_MAXIMIZED_FRACTION = 0.96;
    let drawerIsMaximized = false;
    let drawerPreMaximizeWidth = DRAWER_DEFAULT_WIDTH;

    function drawerMaxWidth() {
      return window.innerWidth * DRAWER_MAXIMIZED_FRACTION;
    }

    function updateDrawerMaximizeIcon() {
      const btn = document.getElementById('drawerMaximizeBtn');
      if (!btn) return;
      btn.classList.toggle('maximized', drawerIsMaximized);
      btn.title = drawerIsMaximized ? 'Restore' : 'Expand';
    }

    function initDrawerWidth() {
      const drawer = document.getElementById('searchDrawer');
      if (!drawer) return;

      const savedWidth = parseInt(localStorage.getItem('drawerWidth'), 10);
      const savedMaximized = localStorage.getItem('drawerMaximized') === 'true';

      drawerPreMaximizeWidth = (savedWidth && savedWidth >= DRAWER_MIN_WIDTH) ? savedWidth : DRAWER_DEFAULT_WIDTH;
      drawerIsMaximized = savedMaximized;

      drawer.style.width = (drawerIsMaximized ? drawerMaxWidth() : drawerPreMaximizeWidth) + 'px';
      updateDrawerMaximizeIcon();

      // If the window shrinks below the current width (maximized or not),
      // re-clamp so the drawer never overflows the viewport.
      window.addEventListener('resize', () => {
        const maxW = drawerMaxWidth();
        const current = drawer.getBoundingClientRect().width;
        if (drawerIsMaximized) {
          drawer.style.width = maxW + 'px';
        } else if (current > maxW) {
          drawer.style.width = maxW + 'px';
        }
      });

      // Drag-to-resize
      const handle = document.getElementById('drawerResizeHandle');
      if (!handle) return;
      let dragging = false;
      let startX = 0;
      let startWidth = 0;

      handle.addEventListener('mousedown', (e) => {
        dragging = true;
        if (drawerIsMaximized) {
          drawerIsMaximized = false;
          localStorage.setItem('drawerMaximized', 'false');
          updateDrawerMaximizeIcon();
        }
        startX = e.clientX;
        startWidth = drawer.getBoundingClientRect().width;
        drawer.classList.add('resizing');
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';
        e.preventDefault();
      });

      window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        // Drawer is anchored to the right edge, so dragging left (negative
        // clientX delta) grows it — width = startWidth + (startX - clientX).
        let newWidth = startWidth + (startX - e.clientX);
        newWidth = Math.max(DRAWER_MIN_WIDTH, Math.min(newWidth, drawerMaxWidth()));
        drawer.style.width = newWidth + 'px';
      });

      window.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        drawer.classList.remove('resizing');
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
        drawerPreMaximizeWidth = drawer.getBoundingClientRect().width;
        localStorage.setItem('drawerWidth', Math.round(drawerPreMaximizeWidth));
      });
    }

    function toggleDrawerMaximize() {
      const drawer = document.getElementById('searchDrawer');
      if (!drawer) return;
      drawerIsMaximized = !drawerIsMaximized;
      if (drawerIsMaximized) {
        drawerPreMaximizeWidth = drawer.getBoundingClientRect().width;
        drawer.style.width = drawerMaxWidth() + 'px';
      } else {
        drawer.style.width = drawerPreMaximizeWidth + 'px';
      }
      localStorage.setItem('drawerMaximized', drawerIsMaximized);
      localStorage.setItem('drawerWidth', Math.round(drawerPreMaximizeWidth));
      updateDrawerMaximizeIcon();
    }
    function openSettingsDrawer() {
      openSearchDrawer();
      const content = document.getElementById('settingsContent');
      if (content && !content.classList.contains('show')) toggleSettings();
      const section = document.getElementById('pipelineSettingsSection');
      if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    // ── Sidebar search (filters the Leads board/table from the nav rail) ──
    function sidebarSearchInput(value) {
      const ts = document.getElementById('tableSearch');
      if (!ts) return;
      switchTab('tab-crm');
      ts.value = value;
      filterTable();
    }

    async function loadHistory() {
      const container = document.getElementById('historyList');
      if (!container) return;
      try {
        const res = await fetch('/api/history');
        const data = await res.json();
        container.innerHTML = '';
        if (!data.length) {
          container.innerHTML = `
            <div class="placeholder" style="height:auto; padding:48px 24px;">
              <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
              <div>
                <div class="placeholder-title">No saved datasets yet</div>
                <div class="placeholder-sub">Run the pipeline and results will be saved here automatically.</div>
              </div>
            </div>
          `;
          return;
        }
        data.forEach(item => {
          let dateStr = escapeHtml(item.timestamp);
          try {
            const parts = item.timestamp.split(' ');
            if (parts.length >= 2) {
              const dParts = parts[0].split('-');
              const tParts = parts[1].split(':');
              if (dParts.length === 3 && tParts.length >= 2) {
                const date = new Date(dParts[0], dParts[1] - 1, dParts[2], tParts[0], tParts[1], tParts[2] || 0);
                dateStr = date.toLocaleString();
              }
            }
          } catch(e) {}

          const card = document.createElement('div');
          card.className = 'history-card';
          card.innerHTML = `
            <div class="history-card-header">
              <span class="history-card-time">${dateStr}</span>
              <span class="history-card-size">${escapeHtml(item.size)}</span>
            </div>
            <div class="history-card-query">"${escapeHtml(item.inquiry || 'Manual Import')}"</div>
            <div class="history-card-footer">
              <div class="history-card-stats">
                <span class="badge badge-valid">${item.leads} Leads</span>
              </div>
              <div style="display: flex; gap: 8px;">
                <button class="history-btn-load" onclick="viewHistoryRun('${escapeHtml(item.filename)}')">Load Dataset &rarr;</button>
                <a class="history-btn-download" href="/api/download/${encodeURIComponent(item.filename)}" target="_blank" title="Download CSV">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 3v12"/></svg>
                </a>
              </div>
            </div>
          `;
          container.appendChild(card);
        });
      } catch (err) {
        container.innerHTML = `<div style="color: var(--red); padding: 20px; text-align: center;">Error loading history: ${escapeHtml(err.message)}</div>`;
      }
    }

    async function viewHistoryRun(filename) {
      try {
        const res = await fetch(`/api/history/${encodeURIComponent(filename)}`);
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        selectedLeadKeys.clear();

        // 1. Populate inquiry text
        document.getElementById('inquiry').value = data.inquiry || '';
        
        // 2. Render ICP attributes
        if (data.icp && Object.keys(data.icp).length > 0) {
          renderICP(data.icp);
        } else {
          document.getElementById('icpCard').style.display = 'none';
          document.getElementById('icpPlaceholder').style.display = 'flex';
        }
        
        // 3. Render leads table
        allLeads = data.sample || [];
        renderCRMContent();
        document.getElementById('crmPlaceholder').style.display = 'none';
        document.getElementById('panelTable').style.display = 'flex';
        document.getElementById('tableCount').textContent = `(${allLeads.length} leads)`;
        
        // 4. Update Stats cards & Charts
        if (data.stats) {
          updateStatCards(data.stats);
          updateCharts(data.stats);
        }
        
        // 5. Switch to CRM tab
        switchTab('tab-crm');
        showAlert('success', `Loaded historic dataset for: "${data.inquiry}"`);
        
      } catch (err) {
        showAlert('error', `Failed to load dataset: ${err.message}`);
      }
    }

    async function sendChatMessage() {
      const inputEl = document.getElementById('inquiry');
      const message = inputEl.value.trim();
      if (!message) return;

      const chatLog = document.getElementById('chatLog');
      hideAlert();

      // Append user message
      const userMsg = document.createElement('div');
      userMsg.className = 'chat-msg chat-user';
      userMsg.innerHTML = `<strong>You:</strong> ${escapeHtml(message)}`;
      chatLog.appendChild(userMsg);
      chatLog.scrollTop = chatLog.scrollHeight;

      // Clear input
      inputEl.value = '';

      // Add to local history state
      chatHistory.push({ role: 'user', content: message });

      // Append typing indicator
      const typingIndicator = document.createElement('div');
      typingIndicator.id = 'chatTyping';
      typingIndicator.className = 'chat-msg chat-ai chat-typing';
      typingIndicator.innerHTML = `<em>AI Strategist is analyzing…</em>`;
      chatLog.appendChild(typingIndicator);
      chatLog.scrollTop = chatLog.scrollHeight;

      // Disable buttons
      const btnSend = document.getElementById('btnICPOnly');
      const lblSend = document.getElementById('btnICPLabel');
      const btnRun  = document.getElementById('btnRun');
      
      btnSend.disabled = true;
      btnRun.disabled = true;
      btnSend.classList.add('loading');
      lblSend.textContent = 'Analyzing…';

      try {
        const res = await fetch('/api/chat-icp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: message,
            history: chatHistory,
            icp: lastParsedICP
          })
        });
        const data = await res.json();
        
        // Remove typing indicator
        const typingEl = document.getElementById('chatTyping');
        if (typingEl) typingEl.remove();

        if (data.error || data.status === 'error') {
          throw new Error(data.error || data.message || 'Failed to analyze request');
        }

        // Append assistant message
        const assistantMsg = document.createElement('div');
        assistantMsg.className = 'chat-msg chat-ai';
        
        let repliesHtml = '';
        if (data.suggested_replies && data.suggested_replies.length) {
          repliesHtml = `<div class="chips-container">
            ${data.suggested_replies.map(r => `<span class="chip-suggest" onclick="selectSuggestedReply(this)">${escapeHtml(r)}</span>`).join('')}
          </div>`;
        }

        assistantMsg.innerHTML = `<strong>AI Strategist</strong> — ${escapeHtml(data.chat_response)}${repliesHtml}`;
        chatLog.appendChild(assistantMsg);
        chatLog.scrollTop = chatLog.scrollHeight;

        // Push assistant response to local history state
        chatHistory.push({ role: 'assistant', content: data.chat_response });

        // Update parsed ICP
        lastParsedICP = data.icp;
        renderICP(lastParsedICP);

        showAlert('success', 'Ideal Customer Profile updated successfully.');
      } catch (err) {
        // Remove typing indicator
        const typingEl = document.getElementById('chatTyping');
        if (typingEl) typingEl.remove();

        // Append error bubble
        const errMsg = document.createElement('div');
        errMsg.className = 'chat-msg chat-err';
        errMsg.innerHTML = `<strong>Error:</strong> ${escapeHtml(err.message)}`;
        chatLog.appendChild(errMsg);
        chatLog.scrollTop = chatLog.scrollHeight;

        showAlert('error', `Failed to generate ICP: ${err.message}`);
      } finally {
        btnSend.disabled = false;
        btnRun.disabled = false;
        btnSend.classList.remove('loading');
        lblSend.textContent = 'Send to AI';
      }
    }

    function handleChatKey(event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        sendChatMessage();
      }
    }

    function handleWebsiteIcpKey(event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        generateIcpFromWebsite();
      }
    }

    async function generateIcpFromWebsite() {
      const inputEl = document.getElementById('websiteUrl');
      const website = inputEl.value.trim();
      if (!website) {
        showAlert('warning', 'Please enter a website URL first.');
        return;
      }

      const chatLog = document.getElementById('chatLog');
      hideAlert();

      const userMsg = document.createElement('div');
      userMsg.className = 'chat-msg chat-user';
      userMsg.innerHTML = `<strong>You:</strong> Build an ICP from ${escapeHtml(website)}`;
      chatLog.appendChild(userMsg);
      chatLog.scrollTop = chatLog.scrollHeight;

      const typingIndicator = document.createElement('div');
      typingIndicator.id = 'websiteIcpTyping';
      typingIndicator.className = 'chat-msg chat-ai chat-typing';
      typingIndicator.innerHTML = `<em>AI Strategist is reading the website…</em>`;
      chatLog.appendChild(typingIndicator);
      chatLog.scrollTop = chatLog.scrollHeight;

      const btn = document.getElementById('btnWebsiteICP');
      const lbl = document.getElementById('btnWebsiteICPLabel');
      const btnRun = document.getElementById('btnRun');

      btn.disabled = true;
      btnRun.disabled = true;
      btn.classList.add('loading');
      lbl.textContent = 'Analyzing…';

      try {
        const res = await fetch('/api/generate-icp-from-website', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ website: website })
        });
        const data = await res.json();

        const typingEl = document.getElementById('websiteIcpTyping');
        if (typingEl) typingEl.remove();

        if (data.error || data.status === 'error') {
          throw new Error(data.error || data.message || 'Failed to analyze that website');
        }

        const assistantMsg = document.createElement('div');
        assistantMsg.className = 'chat-msg chat-ai';
        const summary = (data.icp && data.icp.icp_summary) || 'ICP generated from the website.';
        assistantMsg.innerHTML = `<strong>AI Strategist</strong> — Read ${escapeHtml(data.site_title || data.source_url || website)}. ${escapeHtml(summary)}`;
        chatLog.appendChild(assistantMsg);
        chatLog.scrollTop = chatLog.scrollHeight;

        chatHistory.push({ role: 'user', content: `Build an ICP from ${website}` });
        chatHistory.push({ role: 'assistant', content: summary });

        inputEl.value = '';
        lastParsedICP = data.icp;
        renderICP(lastParsedICP);

        showAlert('success', 'Ideal Customer Profile generated from website.');
      } catch (err) {
        const typingEl = document.getElementById('websiteIcpTyping');
        if (typingEl) typingEl.remove();

        const errMsg = document.createElement('div');
        errMsg.className = 'chat-msg chat-err';
        errMsg.innerHTML = `<strong>Error:</strong> ${escapeHtml(err.message)}`;
        chatLog.appendChild(errMsg);
        chatLog.scrollTop = chatLog.scrollHeight;

        showAlert('error', `Failed to generate ICP: ${err.message}`);
      } finally {
        btn.disabled = false;
        btnRun.disabled = false;
        btn.classList.remove('loading');
        lbl.textContent = 'Analyze Website';
      }
    }

    function selectSuggestedReply(el) {
      const message = el.textContent.trim();
      const inputEl = document.getElementById('inquiry');
      inputEl.value = message;
      sendChatMessage();
    }



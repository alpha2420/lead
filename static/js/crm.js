    // ── Leads Table ────────────────────────────────────────────────────────────
    function renderTable(leads) {
      const tbody = document.getElementById('leadsBody');
      tbody.innerHTML = '';
      leads.forEach((l, index) => {
        const scoreW = Math.round(l.score || 0);
        const initials = (l.name || '').split(' ').map(n => n[0]).join('').slice(0, 2).toUpperCase() || '?';
        const key = getNotesKey(l);
        const isSelected = selectedLeadKeys.has(key);
        const tr = document.createElement('tr');
        if (isSelected) tr.classList.add('row-selected');
        tr.innerHTML = `
          <td class="td-check"><input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleLeadSelection('${key.replace(/'/g, "\\'")}', this.checked)" /></td>
          <td class="td-name">${escapeHtml(l.company||'—')}</td>
          <td><div class="td-name-cell"><span class="td-avatar">${initials}</span><span class="td-name">${escapeHtml(l.name||'—')}</span></div></td>
          <td>${escapeHtml(l.title||'—')}</td>
          <td class="td-email">${escapeHtml(l.email||'—')}</td>
          <td>${escapeHtml(l.phone||'—')}</td>
          <td>${escapeHtml(l.city||'—')}</td>
          <td>${escapeHtml(l.state||'—')}</td>
          <td>${escapeHtml(l.country||'—')}</td>
          <td>${escapeHtml(l.zip_code||'—')}</td>
          <td>${escapeHtml(l.employee_count||'—')}</td>
          <td>${escapeHtml(l.level||'—')}</td>
          <td>${escapeHtml(l.phone2||'—')}</td>
          <td class="td-email">${escapeHtml(l.email2||'—')}</td>
          <td>${escapeHtml(l.biz_address||'—')}</td>
          <td>${escapeHtml(l.location||'—')}</td>
          <td>${escapeHtml(l.market_cap||'—')}</td>
          <td>${escapeHtml(l.industry||'—')}</td>
          <td>${escapeHtml(l.biz_category||'—')}</td>
          <td>${escapeHtml(l.biz_description||'—')}</td>
          <td>${escapeHtml(l.technology||'—')}</td>
          <td>${l.linkedin_url ? `<a class="td-link" href="${escapeHtml(l.linkedin_url)}" target="_blank">↗ View</a>` : '—'}</td>
          <td>${statusBadge(l.status)}</td>
          <td>
            <div class="score-bar-wrap">
              <div class="score-bar"><div class="score-bar-fill" style="width:${scoreW}%"></div></div>
              <div class="score-num">${scoreW}</div>
            </div>
          </td>
          <td>${sourceBadge(l.source)}</td>
        `;
        tr.dataset.search = `${l.name} ${l.title} ${l.company} ${l.location} ${l.email} ${l.city||''} ${l.state||''} ${l.country||''} ${l.industry||''} ${l.technology||''}`.toLowerCase();
        tr.onclick = (e) => {
          if (e.target.tagName === 'A' || e.target.closest('a')) return;
          if (e.target.tagName === 'INPUT' || e.target.closest('.td-check')) return;
          openLeadDrawer(l, index);
        };
        tbody.appendChild(tr);
      });
      filterTable();
      updateBulkBar();
    }

    // ── Row selection & bulk actions ──────────────────────────────────────────
    function toggleLeadSelection(key, checked) {
      if (checked) selectedLeadKeys.add(key);
      else selectedLeadKeys.delete(key);
      renderCRMContent();
    }

    function toggleSelectAll(checked) {
      document.querySelectorAll('#leadsBody tr').forEach((tr, i) => {
        if (tr.style.display === 'none') return;
        const lead = allLeads[i];
        if (!lead) return;
        const key = getNotesKey(lead);
        if (checked) selectedLeadKeys.add(key);
        else selectedLeadKeys.delete(key);
      });
      renderCRMContent();
    }

    function clearSelection() {
      selectedLeadKeys.clear();
      renderCRMContent();
    }

    function updateBulkBar() {
      const bar = document.getElementById('bulkActionBar');
      const countEl = document.getElementById('bulkCount');
      if (!bar || !countEl) return;
      const count = selectedLeadKeys.size;
      countEl.textContent = count;
      bar.style.display = count > 0 ? 'flex' : 'none';

      const selectAll = document.getElementById('selectAllLeads');
      if (selectAll) {
        const visibleRows = Array.from(document.querySelectorAll('#leadsBody tr')).filter(tr => tr.style.display !== 'none');
        const visibleSelected = visibleRows.filter(tr => tr.querySelector('.td-check input')?.checked).length;
        selectAll.checked = visibleRows.length > 0 && visibleSelected === visibleRows.length;
        selectAll.indeterminate = visibleSelected > 0 && visibleSelected < visibleRows.length;
      }
    }

    function exportSelectedCSV() {
      const selected = allLeads.filter(l => selectedLeadKeys.has(getNotesKey(l)));
      if (!selected.length) return;
      downloadLeadsCSV(selected, `leads_selected_${new Date().toISOString().slice(0,10)}.csv`);
    }

    function removeSelectedLeads() {
      if (!selectedLeadKeys.size) return;
      const removedCount = selectedLeadKeys.size;
      allLeads = allLeads.filter(l => !selectedLeadKeys.has(getNotesKey(l)));
      selectedLeadKeys.clear();
      renderCRMContent();
      document.getElementById('tableCount').textContent = `(${allLeads.length} leads)`;
      showAlert('success', `Removed ${removedCount} lead${removedCount === 1 ? '' : 's'} from the list.`);
    }

    function statusBadge(status) {
      const map = { valid:'badge-valid', invalid:'badge-invalid', 'catch-all':'badge-catchall' };
      const labels = { valid:'Verified', invalid:'Invalid', 'catch-all':'Pending' };
      const cls = map[status] || 'badge-other';
      const label = labels[status] || status || 'Unknown';
      return `<span class="badge ${cls}">${label}</span>`;
    }
    function sourceBadge(src) {
      const cls = src === 'apollo' ? 'badge-apollo' : src === 'apify' ? 'badge-apify' : src === 'explorium' ? 'badge-explorium' : 'badge-other';
      return `<span class="badge ${cls}">${src||'—'}</span>`;
    }

    function filterTable() {
      const searchInput = document.getElementById('tableSearch');
      if (!searchInput) return;
      const q = searchInput.value.toLowerCase();
      const source = document.getElementById('filterSource').value;
      const status = document.getElementById('filterStatus').value;
      const minScore = parseInt(document.getElementById('filterMinScore').value) || 0;

      if (viewMode === 'board') {
        renderKanban(allLeads);
        return;
      }

      let visibleCount = 0;
      let totalCount = 0;

      document.querySelectorAll('#leadsBody tr').forEach((tr, index) => {
        totalCount++;
        const lead = allLeads[index];
        if (!lead) return;

        // 1. Text search match
        const matchesQuery = !q || tr.dataset.search.includes(q);

        // 2. Source match
        const matchesSource = source === 'all' || String(lead.source).toLowerCase() === source;

        // 3. Status match
        const matchesStatus = status === 'all' || String(lead.status).toLowerCase() === status;

        // 4. Score match
        const scoreVal = Math.round(lead.score || 0);
        const matchesScore = scoreVal >= minScore;

        if (matchesQuery && matchesSource && matchesStatus && matchesScore) {
          tr.style.display = '';
          visibleCount++;
        } else {
          tr.style.display = 'none';
        }
      });

      const indicator = document.getElementById('crmStatsIndicator');
      if (indicator) {
        indicator.innerHTML = `Showing <span>${visibleCount}</span> of <span>${totalCount}</span> leads`;
      }
    }

    // ── CRM Segmented View Controller ──────────────────────────────────────────
    function setViewMode(mode) {
      viewMode = mode;
      
      document.getElementById('btn-view-table').classList.toggle('active', mode === 'table');
      document.getElementById('btn-view-board').classList.toggle('active', mode === 'board');
      
      document.querySelector('.table-wrap').style.display = mode === 'table' ? 'block' : 'none';
      document.getElementById('kanbanBoard').style.display = mode === 'board' ? 'block' : 'none';
      
      renderCRMContent();
    }

    function renderCRMContent() {
      if (viewMode === 'table') {
        renderTable(allLeads);
      } else {
        renderKanban(allLeads);
      }
    }

    // ── CRM Kanban Board Rendering ─────────────────────────────────────────────
    function renderKanban(leads) {
      const cols = {
        scraped:      document.getElementById('cards-scraped'),
        verifying:    document.getElementById('cards-verifying'),
        qualified:    document.getElementById('cards-qualified'),
        contacted:    document.getElementById('cards-contacted'),
        disqualified: document.getElementById('cards-disqualified')
      };
      
      // Clear columns
      Object.values(cols).forEach(el => { if (el) el.innerHTML = ''; });
      
      const q = document.getElementById('tableSearch').value.toLowerCase();
      const source = document.getElementById('filterSource').value;
      const status = document.getElementById('filterStatus').value;
      const minScore = parseInt(document.getElementById('filterMinScore').value) || 0;
      
      let counts = { scraped: 0, verifying: 0, qualified: 0, contacted: 0, disqualified: 0 };
      let visibleCount = 0;
      
      leads.forEach((l, index) => {
        const matchesQuery = !q || `${l.name} ${l.title} ${l.company} ${l.location} ${l.email}`.toLowerCase().includes(q);
        const matchesSource = source === 'all' || String(l.source).toLowerCase() === source;
        const matchesStatus = status === 'all' || String(l.status).toLowerCase() === status;
        const scoreVal = Math.round(l.score || 0);
        const matchesScore = scoreVal >= minScore;
        
        if (!matchesQuery || !matchesSource || !matchesStatus || !matchesScore) return;
        
        visibleCount++;
        
        // Determine column
        let colKey = 'scraped';
        if (l.status === 'valid') colKey = 'qualified';
        else if (l.status === 'catch-all') colKey = 'contacted';
        else if (l.status === 'invalid') colKey = 'disqualified';
        else if (l.status === 'verifying') colKey = 'verifying';
        
        counts[colKey]++;
        
        const card = document.createElement('div');
        card.className = 'kanban-card';
        card.draggable = true;
        card.ondragstart = (e) => dragLead(e, index);
        card.onclick = (e) => {
          if (e.target.closest('button')) return;
          openLeadDrawer(l, index);
        };
        
        const scoreW = Math.round(l.score || 0);
        const initials = (l.name || '—').trim().split(/\s+/).map(p => p[0]).slice(0, 2).join('').toUpperCase() || '—';

        card.innerHTML = `
          <div class="card-tag-row">
            <span class="card-id-tag">L-${index + 1}</span>
            ${sourceBadge(l.source)}
          </div>
          <div class="card-lead-name">${escapeHtml(l.name || '—')}</div>
          <div class="card-lead-details">
            <strong>${escapeHtml(l.company || '—')}</strong><br>
            ${escapeHtml(l.title || '—')}
          </div>
          <div style="font-size: 0.65rem; color: var(--text-muted); display: flex; align-items: center; gap: 4px;">
            <span>📍 ${escapeHtml(l.location || 'Not Specified')}</span>
          </div>
          <div class="score-bar-wrap" style="margin-bottom:0;">
            <div class="score-bar"><div class="score-bar-fill" style="width:${scoreW}%"></div></div>
            <div class="score-num">${scoreW}%</div>
          </div>
          <div class="card-lead-footer">
            <div class="card-avatar-circle" title="${escapeHtml(l.name || '')}">${escapeHtml(initials)}</div>
            <button class="card-action-btn">Edit &rarr;</button>
          </div>
        `;
        if (cols[colKey]) {
          cols[colKey].appendChild(card);
        }
      });
      
      // Update badges
      if (document.getElementById('badge-scraped')) document.getElementById('badge-scraped').textContent = counts.scraped;
      if (document.getElementById('badge-verifying')) document.getElementById('badge-verifying').textContent = counts.verifying;
      if (document.getElementById('badge-qualified')) document.getElementById('badge-qualified').textContent = counts.qualified;
      if (document.getElementById('badge-contacted')) document.getElementById('badge-contacted').textContent = counts.contacted;
      if (document.getElementById('badge-disqualified')) document.getElementById('badge-disqualified').textContent = counts.disqualified;
      
      const indicator = document.getElementById('crmStatsIndicator');
      if (indicator) {
        indicator.innerHTML = `Showing <span>${visibleCount}</span> of <span>${leads.length}</span> leads`;
      }
    }

    // ── Kanban Drag & Drop Handlers ──────────────────────────────────────────
    function dragLead(e, index) {
      e.dataTransfer.setData('text/plain', index);
    }
    
    function allowDrop(e) {
      e.preventDefault();
      e.currentTarget.classList.add('drag-over');
    }
    
    // Add dragleave handler on document
    document.addEventListener('dragover', (e) => {
      e.preventDefault();
    });
    
    function dropLead(e, targetStatus) {
      e.preventDefault();
      e.currentTarget.classList.remove('drag-over');
      
      const indexStr = e.dataTransfer.getData('text/plain');
      const index = parseInt(indexStr);
      if (isNaN(index)) return;
      
      const lead = allLeads[index];
      if (!lead) return;
      
      lead.status = targetStatus;
      
      if (selectedLeadIndex === index) {
        document.getElementById('editStatus').value = targetStatus;
      }
      
      renderCRMContent();
    }

    // ── CRM Lead Drawer Interactive Functions ──────────────────────────────────
    function openLeadDrawer(lead, index) {
      selectedLeadIndex = index;
      
      document.getElementById('drawerLeadName').textContent = lead.name || '—';
      document.getElementById('editName').value = lead.name || '';
      document.getElementById('editTitle').value = lead.title || '';
      document.getElementById('editCompany').value = lead.company || '';
      document.getElementById('editLocation').value = lead.location || '';
      document.getElementById('editEmail').value = lead.email || '';
      document.getElementById('editLinkedin').value = lead.linkedin_url || '';
      
      // Avatar initials
      const initials = (lead.name || '')
        .split(' ')
        .map(n => n[0])
        .join('')
        .slice(0, 2)
        .toUpperCase() || '?';
      document.getElementById('drawerAvatar').textContent = initials;
      
      // Source badge
      const srcBadge = document.getElementById('drawerSourceBadge');
      if (srcBadge) {
        srcBadge.textContent = lead.source || 'imported';
        srcBadge.className = 'drawer-source-badge ' + (lead.source === 'apollo' ? 'badge-apollo' : lead.source === 'apify' ? 'badge-apify' : lead.source === 'explorium' ? 'badge-explorium' : 'badge-other');
      }
      
      // Status drop down
      document.getElementById('editStatus').value = lead.status || 'unknown';
      
      // Composite score
      const scoreW = Math.round(lead.score || 0);
      document.getElementById('drawerScoreFill').style.width = `${scoreW}%`;
      document.getElementById('drawerScoreNum').textContent = `${scoreW}%`;

      // Score breakdown — raw email confidence + domain match signal
      const emailConf = document.getElementById('drawerEmailConfidence');
      if (emailConf) {
        emailConf.textContent = lead.email_confidence !== undefined ? `${Math.round(lead.email_confidence)}%` : '—';
      }
      const domainEl = document.getElementById('drawerDomainSignal');
      if (domainEl) {
        const signal = (lead.domain_signal || 'UNKNOWN').toUpperCase();
        const domainClass = { EXACT: 'badge-valid', PERSONAL: 'badge-other', MISMATCH: 'badge-invalid', UNKNOWN: 'badge-other' }[signal] || 'badge-other';
        domainEl.textContent = signal;
        domainEl.className = `badge ${domainClass}`;
      }

      // LinkedIn cross-verify breakdown
      const liMatch = document.getElementById('drawerLinkedinMatch');
      if (liMatch) {
        const hasMatch = lead.linkedin_company_match !== undefined && lead.linkedin_company_match !== null;
        liMatch.textContent = hasMatch
          ? `${Math.round(lead.linkedin_company_match)}% / ${Math.round(lead.linkedin_title_match || 0)}%`
          : '—';
      }
      const liEmployee = document.getElementById('drawerLinkedinEmployee');
      if (liEmployee) {
        const current = lead.linkedin_current_employee;
        const label = current === true ? 'YES' : current === false ? 'FORMER' : 'UNKNOWN';
        const liClass = { YES: 'badge-valid', FORMER: 'badge-invalid', UNKNOWN: 'badge-other' }[label];
        liEmployee.textContent = label;
        liEmployee.className = `badge ${liClass}`;
      }

      // Notes
      const notesKey = getNotesKey(lead);
      document.getElementById('editNotes').value = leadNotes[notesKey] || '';
      
      // Link btn
      const linkBtn = document.getElementById('drawerLinkedinLink');
      if (lead.linkedin_url) {
        linkBtn.href = lead.linkedin_url;
        linkBtn.style.display = 'flex';
      } else {
        linkBtn.style.display = 'none';
      }
      
      // Copy reset
      const copyBtn = document.getElementById('btnCopyEmail');
      copyBtn.className = 'btn-copy';
      copyBtn.querySelector('span').textContent = 'Copy';
      
      // Populate Raw Metadata
      const rawMetadataGroup = document.getElementById('rawMetadataGroup');
      const rawMetadataContent = document.getElementById('rawMetadataContent');
      if (rawMetadataGroup && rawMetadataContent) {
        const standardFields = ['name', 'title', 'company', 'location', 'email', 'linkedin_url', 'status', 'score', 'source'];
        const additionalData = {};
        let hasAdditional = false;
        for (const k in lead) {
          if (!standardFields.includes(k) && lead[k] !== null && lead[k] !== undefined && lead[k] !== '') {
            additionalData[k] = lead[k];
            hasAdditional = true;
          }
        }
        if (hasAdditional) {
          rawMetadataGroup.style.display = 'block';
          rawMetadataContent.textContent = JSON.stringify(additionalData, null, 2);
        } else {
          rawMetadataGroup.style.display = 'none';
        }
      }
      
      // Trigger Open
      document.getElementById('crmDrawer').classList.add('open');
      document.getElementById('drawerBackdrop').classList.add('open');
    }

    function closeDrawer() {
      document.getElementById('crmDrawer').classList.remove('open');
      document.getElementById('drawerBackdrop').classList.remove('open');
      selectedLeadIndex = null;
    }

    function getNotesKey(lead) {
      return (lead.email || (lead.name + '_' + lead.company)).toLowerCase().trim();
    }

    function saveLeadNotes() {
      if (selectedLeadIndex === null) return;
      const lead = allLeads[selectedLeadIndex];
      if (!lead) return;
      
      const notesKey = getNotesKey(lead);
      leadNotes[notesKey] = document.getElementById('editNotes').value;
      saveNotesToStorage();
    }

    function saveNotesToStorage() {
      try {
        localStorage.setItem('leadNotes', JSON.stringify(leadNotes));
      } catch (e) {
        console.error("Could not save lead notes", e);
      }
    }

    function copyEmailToClipboard() {
      const emailField = document.getElementById('editEmail');
      if (!emailField || !emailField.value) return;
      
      navigator.clipboard.writeText(emailField.value).then(() => {
        const copyBtn = document.getElementById('btnCopyEmail');
        copyBtn.classList.add('copied');
        copyBtn.querySelector('span').textContent = 'Copied!';
        setTimeout(() => {
          copyBtn.classList.remove('copied');
          copyBtn.querySelector('span').textContent = 'Copy';
        }, 2000);
      });
    }

    function updateLeadField(field) {
      if (selectedLeadIndex === null) return;
      const lead = allLeads[selectedLeadIndex];
      if (!lead) return;
      
      const inputVal = document.getElementById(
        field === 'name' ? 'editName' :
        field === 'title' ? 'editTitle' :
        field === 'company' ? 'editCompany' :
        field === 'location' ? 'editLocation' :
        field === 'email' ? 'editEmail' : 'editLinkedin'
      ).value;
      
      lead[field] = inputVal;
      
      if (field === 'name') {
        document.getElementById('drawerLeadName').textContent = inputVal || '—';
      }
      
      renderCRMContent();
    }

    function updateLeadStatus(status) {
      if (selectedLeadIndex === null) return;
      const lead = allLeads[selectedLeadIndex];
      if (!lead) return;
      
      lead.status = status;
      renderCRMContent();
    }

    function sortTable(key) {
      if (sortKey === key) sortDir *= -1;
      else { sortKey = key; sortDir = 1; }

      document.querySelectorAll('thead th').forEach(th => th.classList.remove('sorted'));
      const headers = ['company','name','title','email','phone','city','state','country','zip_code','employee_count','level','phone2','email2','biz_address','location','market_cap','industry','biz_category','biz_description','technology','','status','score','source'];
      const idx = headers.indexOf(key);
      if (idx >= 0) document.querySelectorAll('thead th')[idx].classList.add('sorted');

      const numericKeys = new Set(['score', 'employee_count']);
      allLeads.sort((a, b) => {
        if (numericKeys.has(key)) return ((Number(a[key]) || 0) - (Number(b[key]) || 0)) * sortDir;
        const av = String(a[key] ?? '').toLowerCase();
        const bv = String(b[key] ?? '').toLowerCase();
        return av < bv ? -sortDir : av > bv ? sortDir : 0;
      });
      renderCRMContent();
    }


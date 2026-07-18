    // ── CSV Download ───────────────────────────────────────────────────────────
    function downloadLeadsCSV(leads, filename) {
      if (!leads.length) return;
      const cols = ['Company','Person Name','Title','Email Id','Phone','City','State','Country','Zip Code','Employee Count','Level','Phone 2','Email 2','Biz Address','Location','Market Cap','Industry','Biz Category','Biz Description','Technology','LinkedIn URL','Verification Status','Composite Score','Email Confidence','Domain Match','LinkedIn Company Match','LinkedIn Title Match','LinkedIn Current Employer','Source'];
      const rows = leads.map(l => [
        l.company, l.name, l.title, l.email, l.phone, l.city, l.state, l.country, l.zip_code,
        l.employee_count, l.level, l.phone2, l.email2, l.biz_address, l.location, l.market_cap,
        l.industry, l.biz_category, l.biz_description, l.technology,
        l.linkedin_url, l.status, l.score, l.email_confidence, l.domain_signal,
        l.linkedin_company_match, l.linkedin_title_match, l.linkedin_current_employee, l.source
      ].map(v => `"${String(v||'').replace(/"/g,'""')}"`).join(','));
      const csv = [cols.join(','), ...rows].join('\n');
      const blob = new Blob([csv], { type: 'text/csv' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
    }

    function downloadCSV() {
      downloadLeadsCSV(allLeads, `leads_sample_${new Date().toISOString().slice(0,10)}.csv`);
    }

    // ── CSV Structure Mapper (standalone personal utility — no Leads/Datasets integration) ──
    let csvMapperData = null; // { headers, rows, mapping, ai_used_for, canonical_fields, keepOverrides, cleanedResult }
    let csvMapperTemplates = [];

    function updateCsvMapperFileName() {
      const fileInput = document.getElementById('csvMapperFile');
      const nameDiv = document.getElementById('csvMapperFileName');
      const btn = document.getElementById('btnAnalyzeCsvMapping');
      if (fileInput.files.length > 0) {
        nameDiv.textContent = fileInput.files[0].name;
        btn.disabled = false;
      } else {
        nameDiv.textContent = '';
        btn.disabled = true;
      }
      document.getElementById('csvMapperResults').style.display = 'none';
    }

    function toggleCsvMapperSettings() {
      const content = document.getElementById('csvMapperSettingsContent');
      const icon = document.getElementById('csvMapperSettingsToggleIcon');
      content.classList.toggle('show');
      icon.style.transform = content.classList.contains('show') ? 'rotate(0deg)' : 'rotate(-90deg)';
    }

    async function analyzeCsvMapping() {
      const fileInput = document.getElementById('csvMapperFile');
      if (!fileInput.files.length) return;

      const btn = document.getElementById('btnAnalyzeCsvMapping');
      const originalLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Analyzing…';

      try {
        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        const res = await fetch('/api/csv-mapper/analyze', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        csvMapperData = data;
        csvMapperData.keepOverrides = {};
        csvMapperData.cleanedResult = null;
        renderCsvMapperResults(data);
        document.getElementById('csvMapperProcessedResults').style.display = 'none';
        document.getElementById('btnDownloadNormalizedCsv').disabled = true;
      } catch (err) {
        showAlert('error', `Failed to analyze CSV: ${err.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    }

    function renderCsvMapperResults(data) {
      const { headers, rows, mapping, ai_used_for, canonical_fields } = data;

      const mappedCount = headers.filter(h => mapping[h]).length;
      document.getElementById('csvMapperSummary').textContent =
        `${mappedCount} of ${headers.length} columns mapped automatically` +
        (ai_used_for.length ? ` (${ai_used_for.length} via AI — please double-check those)` : '') +
        `. ${rows.length} rows detected.`;

      const tbody = document.getElementById('csvMapperTableBody');
      tbody.innerHTML = headers.map(h => {
        const sample = escapeHtml(String((rows[0] && rows[0][h]) || ''));
        const aiTag = ai_used_for.includes(h) ? '<span class="csvmap-ai-badge">✨ AI</span>' : '';
        const options = ['<option value="">— Ignore (keep as extra column) —</option>']
          .concat(canonical_fields.map(([key, label]) =>
            `<option value="${key}" ${mapping[h] === key ? 'selected' : ''}>${escapeHtml(label)}</option>`
          )).join('');
        return `<tr>
          <td class="csvmap-source-header">${escapeHtml(h)}${aiTag}</td>
          <td class="csvmap-sample-value" title="${sample}">${sample}</td>
          <td><select class="csvmap-map-select" data-source-header="${escapeHtml(h)}">${options}</select></td>
        </tr>`;
      }).join('');

      const previewHead = document.getElementById('csvMapperPreviewHead');
      previewHead.innerHTML = '<th class="csvmap-row-num">#</th>' + headers.map(h => `<th>${escapeHtml(h)}</th>`).join('');
      const previewBody = document.getElementById('csvMapperPreviewBody');
      previewBody.innerHTML = rows.slice(0, 5).map((row, i) =>
        `<tr><td class="csvmap-row-num">${i + 1}</td>${headers.map(h => `<td>${escapeHtml(String(row[h] || ''))}</td>`).join('')}</tr>`
      ).join('');

      document.getElementById('csvMapperResults').style.display = 'block';
    }

    // ── CSV Mapper — saved downloads (server-persisted normalized CSVs) ─────
    async function loadCsvMapperDownloads() {
      const container = document.getElementById('csvMapperDownloadsList');
      if (!container) return;
      try {
        const res = await fetch('/api/csv-mapper/downloads');
        const data = await res.json();
        if (!data.length) {
          container.innerHTML = `
            <div class="placeholder" style="height:auto; padding:24px;">
              <div>
                <div class="placeholder-title">No saved exports yet</div>
                <div class="placeholder-sub">Run "Clean &amp; Validate" and normalized CSVs will be saved here automatically.</div>
              </div>
            </div>`;
          return;
        }
        container.innerHTML = data.map(item => {
          const panelId = 'csvmapPreview_' + item.filename.replace(/[^a-zA-Z0-9]/g, '_');
          return `
          <div class="history-card" style="margin-bottom:8px;">
            <div class="history-card-header">
              <span class="history-card-time">${escapeHtml(item.timestamp)}</span>
              <span class="history-card-size">${escapeHtml(item.size)}</span>
            </div>
            <div class="history-card-query">${item.rows} rows — ${escapeHtml(item.filename)}</div>
            <div class="history-card-footer">
              <div class="history-card-stats"></div>
              <div style="display:flex; gap:8px;">
                <button class="csvmap-btn-sm" onclick="toggleCsvMapperDownloadPreview('${escapeHtml(item.filename)}', ${item.rows}, this)">Open Preview</button>
                <a class="history-btn-download" href="/api/csv-mapper/download/${encodeURIComponent(item.filename)}" target="_blank" title="Download CSV">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 3v12"/></svg>
                </a>
              </div>
            </div>
            <div class="table-wrap" id="${panelId}" style="display:none; max-height:280px; margin-top:10px; border:1px solid var(--border); border-radius:var(--radius-xs);"></div>
          </div>`;
        }).join('');
      } catch (err) {
        container.innerHTML = `<div style="color: var(--red); padding: 12px; text-align: center; font-size:0.72rem;">Error loading downloads: ${escapeHtml(err.message)}</div>`;
      }
    }

    // ── CSV Mapper — "All Leads" combined CRM-style view ────────────────────
    let csvMapperAllLeadsData = { headers: [], rows: [] };
    let csvMapperAllLeadsSort = { column: null, dir: 1 };
    let csvMapperAllLeadsFiltered = [];
    let csvMapperAllLeadsPage = 1;
    let csvMapperAllLeadsPageSize = 50;

    const CSVMAP_ALLLEADS_FACETS = [
      { field: 'Title',    listId: 'csvMapperTitleList' },
      { field: 'Location', listId: 'csvMapperLocationList' },
      { field: 'Company',  listId: 'csvMapperCompanyList' },
      { field: 'Industry', listId: 'csvMapperIndustryList' },
    ];
    const CSVMAP_ALLLEADS_FILTER_INPUT_IDS = {
      search: 'csvMapperLeadsSearch',
      title: 'csvMapperFilterTitle',
      location: 'csvMapperFilterLocation',
      company: 'csvMapperFilterCompany',
      industry: 'csvMapperFilterIndustry',
      country: 'csvMapperFilterCountry',
    };

    function csvmapDisplayHeader(h) {
      return h === '_source_file' ? 'Source File' : h;
    }

    async function loadCsvMapperAllLeads() {
      const countEl = document.getElementById('csvMapperAllLeadsCount');
      try {
        const res = await fetch('/api/csv-mapper/leads');
        const data = await res.json();
        csvMapperAllLeadsData = { headers: data.headers, rows: data.rows };
        populateCsvMapperAllLeadsFacets();
        csvMapperAllLeadsPage = 1;
        renderCsvMapperAllLeadsTable();
        countEl.textContent = `${data.rows.length} unique lead(s) across ${data.total_files} file(s)` +
          (data.total_raw_rows > data.rows.length ? ` (${data.total_raw_rows - data.rows.length} duplicates merged)` : '');
      } catch (err) {
        if (countEl) countEl.textContent = 'Failed to load';
        console.error('Failed to load CSV mapper all-leads:', err);
      }
    }

    function populateCsvMapperAllLeadsFacets() {
      const { rows } = csvMapperAllLeadsData;
      CSVMAP_ALLLEADS_FACETS.forEach(({ field, listId }) => {
        const values = [...new Set(rows.map(r => (r[field] || '').trim()).filter(Boolean))]
          .sort((a, b) => a.localeCompare(b)).slice(0, 500);
        const list = document.getElementById(listId);
        if (list) list.innerHTML = values.map(v => `<option value="${escapeHtml(v)}"></option>`).join('');
      });

      const countrySelect = document.getElementById('csvMapperFilterCountry');
      if (countrySelect) {
        const current = countrySelect.value;
        const countries = [...new Set(rows.map(r => (r['Country'] || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
        countrySelect.innerHTML = '<option value="">All Countries</option>' + countries.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
        if (countries.includes(current)) countrySelect.value = current;
      }
    }

    function csvMapperAllLeadsFilterChanged() {
      csvMapperAllLeadsPage = 1;
      renderCsvMapperAllLeadsTable();
    }

    function getCsvMapperAllLeadsFilterState() {
      const val = id => (document.getElementById(id)?.value || '').trim();
      return {
        search: val('csvMapperLeadsSearch').toLowerCase(),
        title: val('csvMapperFilterTitle').toLowerCase(),
        location: val('csvMapperFilterLocation').toLowerCase(),
        company: val('csvMapperFilterCompany').toLowerCase(),
        industry: val('csvMapperFilterIndustry').toLowerCase(),
        country: val('csvMapperFilterCountry'),
      };
    }

    function renderCsvMapperAllLeadsChips(f) {
      const container = document.getElementById('csvMapperAllLeadsChips');
      const clearBtn = document.getElementById('btnClearAllLeadsFilters');
      const chips = [];
      if (f.search) chips.push({ key: 'search', label: `Search: "${f.search}"` });
      if (f.title) chips.push({ key: 'title', label: `Title: ${f.title}` });
      if (f.location) chips.push({ key: 'location', label: `Location: ${f.location}` });
      if (f.company) chips.push({ key: 'company', label: `Company: ${f.company}` });
      if (f.industry) chips.push({ key: 'industry', label: `Industry: ${f.industry}` });
      if (f.country) chips.push({ key: 'country', label: `Country: ${f.country}` });

      const anyActive = chips.length > 0;
      clearBtn.style.display = anyActive ? '' : 'none';
      container.style.display = anyActive ? 'flex' : 'none';
      container.innerHTML = anyActive
        ? `<span style="font-size:0.62rem; color:var(--text-dim); font-weight:700; text-transform:uppercase; letter-spacing:0.05em;">Filters</span>` +
          chips.map(c => `<span class="tag">${escapeHtml(c.label)} <span class="tag-remove-btn" onclick="clearCsvMapperAllLeadsFilter('${c.key}')">&times;</span></span>`).join('')
        : '';
    }

    function clearCsvMapperAllLeadsFilter(key) {
      const el = document.getElementById(CSVMAP_ALLLEADS_FILTER_INPUT_IDS[key]);
      if (el) el.value = '';
      csvMapperAllLeadsFilterChanged();
    }

    function clearCsvMapperAllLeadsFilters() {
      Object.values(CSVMAP_ALLLEADS_FILTER_INPUT_IDS).forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      csvMapperAllLeadsFilterChanged();
    }

    function csvMapperAllLeadsGoToPage(page) {
      csvMapperAllLeadsPage = page;
      renderCsvMapperAllLeadsTable();
    }

    function csvMapperAllLeadsChangePageSize(value) {
      csvMapperAllLeadsPageSize = value === 'all' ? Infinity : parseInt(value, 10);
      csvMapperAllLeadsPage = 1;
      renderCsvMapperAllLeadsTable();
    }

    function csvmapAllLeadsCell(h, raw) {
      const pad = 'padding:8px 10px; border-bottom:1px solid var(--border); white-space:nowrap;';
      if (h === 'Person Name') {
        const initials = raw.split(' ').map(n => n[0]).filter(Boolean).join('').slice(0, 2).toUpperCase() || '?';
        return `<td style="${pad}"><div class="td-name-cell"><span class="td-avatar">${initials}</span><span class="td-name">${escapeHtml(raw || '—')}</span></div></td>`;
      }
      if (h === 'Company') return `<td class="td-name" style="${pad}">${escapeHtml(raw || '—')}</td>`;
      if (h === 'Email Id' || h === 'Email 2') {
        return `<td class="td-email" style="${pad}">${raw ? `<a class="td-link" href="mailto:${escapeHtml(raw)}">${escapeHtml(raw)}</a>` : '—'}</td>`;
      }
      if (h === 'Phone' || h === 'Phone 2') {
        return `<td style="${pad}">${raw ? `<a class="td-link" href="tel:${escapeHtml(raw)}">${escapeHtml(raw)}</a>` : '—'}</td>`;
      }
      if (h === 'LinkedIn URL') {
        return `<td style="${pad}">${raw ? `<a class="td-link" href="${escapeHtml(raw)}" target="_blank" rel="noopener">↗ View</a>` : '—'}</td>`;
      }
      if (h === 'Website') {
        const href = raw ? (/^https?:\/\//i.test(raw) ? raw : 'https://' + raw) : '';
        return `<td style="${pad}">${raw ? `<a class="td-link" href="${escapeHtml(href)}" target="_blank" rel="noopener">↗ ${escapeHtml(raw)}</a>` : '—'}</td>`;
      }
      return `<td style="${pad} color:var(--text-muted);">${escapeHtml(raw || '—')}</td>`;
    }

    function renderCsvMapperAllLeadsTable() {
      const { headers, rows } = csvMapperAllLeadsData;
      const head = document.getElementById('csvMapperAllLeadsHead');
      head.innerHTML = '<th class="csvmap-row-num" style="padding:8px 10px; top:0; border-bottom:1px solid var(--border);">#</th>' + headers.map(h =>
        `<th onclick="sortCsvMapperAllLeads('${h}')" class="${csvMapperAllLeadsSort.column === h ? 'sorted' : ''}" style="padding:8px 10px; cursor:pointer; white-space:nowrap; background:var(--well); color:var(--text-dim); font-weight:700; text-transform:uppercase; font-size:0.6rem; letter-spacing:0.04em; position:sticky; top:0; border-bottom:1px solid var(--border);">${escapeHtml(csvmapDisplayHeader(h))} <span class="sort-icon">↕</span></th>`
      ).join('');

      const f = getCsvMapperAllLeadsFilterState();
      let filtered = rows.filter(r => {
        if (f.search && !Object.values(r).some(v => String(v || '').toLowerCase().includes(f.search))) return false;
        if (f.title && !String(r['Title'] || '').toLowerCase().includes(f.title)) return false;
        if (f.location && !String(r['Location'] || '').toLowerCase().includes(f.location)) return false;
        if (f.company && !String(r['Company'] || '').toLowerCase().includes(f.company)) return false;
        if (f.industry && !String(r['Industry'] || '').toLowerCase().includes(f.industry)) return false;
        if (f.country && String(r['Country'] || '') !== f.country) return false;
        return true;
      });

      if (csvMapperAllLeadsSort.column) {
        const { column, dir } = csvMapperAllLeadsSort;
        filtered = [...filtered].sort((a, b) => String(a[column] || '').localeCompare(String(b[column] || '')) * dir);
      }

      csvMapperAllLeadsFiltered = filtered;
      renderCsvMapperAllLeadsChips(f);
      document.getElementById('btnExportCsvMapperAllLeads').disabled = filtered.length === 0;
      const exportLabel = document.getElementById('btnExportCsvMapperAllLeadsLabel');
      if (exportLabel) exportLabel.textContent = filtered.length !== rows.length ? `Export Filtered (${filtered.length})` : 'Export All';

      const pageSize = csvMapperAllLeadsPageSize;
      const totalPages = pageSize === Infinity ? 1 : Math.max(1, Math.ceil(filtered.length / pageSize));
      csvMapperAllLeadsPage = Math.min(Math.max(1, csvMapperAllLeadsPage), totalPages);
      const startIdx = pageSize === Infinity ? 0 : (csvMapperAllLeadsPage - 1) * pageSize;
      const endIdx = pageSize === Infinity ? filtered.length : Math.min(startIdx + pageSize, filtered.length);
      const pageRows = filtered.slice(startIdx, endIdx);

      const statsEl = document.getElementById('csvMapperAllLeadsStats');
      if (statsEl) statsEl.innerHTML = filtered.length
        ? `Showing <span>${startIdx + 1}–${endIdx}</span> of <span>${filtered.length}</span>`
        : `Showing <span>0</span> of <span>${rows.length}</span>`;
      document.getElementById('csvMapperAllLeadsPageLabel').textContent = `Page ${csvMapperAllLeadsPage} of ${totalPages}`;
      document.getElementById('btnAllLeadsPrevPage').disabled = csvMapperAllLeadsPage <= 1;
      document.getElementById('btnAllLeadsNextPage').disabled = csvMapperAllLeadsPage >= totalPages;

      const body = document.getElementById('csvMapperAllLeadsBody');
      if (!rows.length) {
        body.innerHTML = `<tr><td colspan="${headers.length + 1 || 1}" style="padding:32px; text-align:center; color:var(--text-dim); font-size:0.72rem;">No leads yet — run "Clean &amp; Validate" with "Save a copy on the server" checked.</td></tr>`;
        return;
      }
      if (!filtered.length) {
        body.innerHTML = `<tr><td colspan="${headers.length + 1}" style="padding:32px; text-align:center; color:var(--text-dim); font-size:0.72rem;">No leads match these filters. <button type="button" class="csvmap-btn-sm" style="margin-left:6px;" onclick="clearCsvMapperAllLeadsFilters()">Clear filters</button></td></tr>`;
        return;
      }

      body.innerHTML = pageRows.map((row, i) => {
        const cells = headers.map(h => csvmapAllLeadsCell(h, String(row[h] || ''))).join('');
        return `<tr><td class="csvmap-row-num" style="padding:8px 10px; border-bottom:1px solid var(--border); color:var(--text-dim);">${startIdx + i + 1}</td>${cells}</tr>`;
      }).join('');
    }

    function sortCsvMapperAllLeads(column) {
      csvMapperAllLeadsSort = csvMapperAllLeadsSort.column === column
        ? { column, dir: csvMapperAllLeadsSort.dir * -1 }
        : { column, dir: 1 };
      renderCsvMapperAllLeadsTable();
    }

    function exportCsvMapperAllLeads() {
      const { headers } = csvMapperAllLeadsData;
      const rows = csvMapperAllLeadsFiltered;
      if (!rows.length) return;
      const csvRows = rows.map(row => headers.map(h => `"${String(row[h] || '').replace(/"/g, '""')}"`).join(','));
      const csv = [headers.map(h => `"${csvmapDisplayHeader(h).replace(/"/g, '""')}"`).join(','), ...csvRows].join('\n');
      const blob = new Blob([csv], { type: 'text/csv' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `all_leads_${new Date().toISOString().slice(0,10)}.csv`;
      a.click();
    }

    async function toggleCsvMapperDownloadPreview(filename, totalRows, btn) {
      const panelId = 'csvmapPreview_' + filename.replace(/[^a-zA-Z0-9]/g, '_');
      const panel = document.getElementById(panelId);
      if (!panel) return;

      if (panel.style.display === 'block') {
        panel.style.display = 'none';
        btn.textContent = 'Open Preview';
        return;
      }
      panel.style.display = 'block';
      btn.textContent = 'Hide Preview';

      if (panel.dataset.loaded === '1') return;
      panel.innerHTML = `<div style="padding:12px; font-size:0.7rem; color:var(--text-dim);">Loading preview…</div>`;
      try {
        const res = await fetch(`/api/csv-mapper/preview/${encodeURIComponent(filename)}`);
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        const shownCount = data.rows.length;
        panel.innerHTML = `
          <div style="padding:6px 8px; font-size:0.6rem; color:var(--text-dim); background:var(--bg-card); border-bottom:1px solid var(--border); position:sticky; top:0;">
            Showing ${shownCount} of ${totalRows} rows
          </div>
          <table style="width:100%; border-collapse:collapse; font-size:0.7rem; text-align:left;">
            <thead><tr>${data.headers.map(h =>
              `<th style="padding:6px 8px; background:var(--bg-card); color:var(--text-dim); font-weight:700; text-transform:uppercase; font-size:0.58rem; letter-spacing:0.04em; position:sticky; top:22px; border-bottom:1px solid var(--border); white-space:nowrap;">${escapeHtml(h)}</th>`
            ).join('')}</tr></thead>
            <tbody>${data.rows.map(row =>
              `<tr>${row.map(cell => `<td style="padding:6px 8px; border-bottom:1px solid var(--border); color:var(--text); white-space:nowrap;">${escapeHtml(cell)}</td>`).join('')}</tr>`
            ).join('')}</tbody>
          </table>`;
        panel.dataset.loaded = '1';
      } catch (err) {
        panel.innerHTML = `<div style="padding:12px; color:var(--red); font-size:0.72rem;">Failed to load preview: ${escapeHtml(err.message)}</div>`;
      }
    }

    // ── CSV Mapper — templates ───────────────────────────────────────────────
    async function loadCsvMapperTemplateList() {
      try {
        const res = await fetch('/api/csv-mapper/templates');
        csvMapperTemplates = await res.json();
        const sel = document.getElementById('csvMapperTemplateSelect');
        if (!sel) return;
        const current = sel.value;
        sel.innerHTML = '<option value="">— Select a saved mapping template —</option>' +
          csvMapperTemplates.map(t => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)}</option>`).join('');
        sel.value = current;
      } catch (err) {
        console.error('Failed to load CSV mapper templates:', err);
      }
    }

    function applyCsvMapperTemplate() {
      const name = document.getElementById('csvMapperTemplateSelect').value;
      if (!name) return;
      if (!csvMapperData) {
        showAlert('warning', 'Analyze a CSV first, then load a template to apply its mapping.');
        return;
      }
      const template = csvMapperTemplates.find(t => t.name === name);
      if (!template) return;

      document.querySelectorAll('#csvMapperTableBody select.csvmap-map-select').forEach(sel => {
        const header = sel.dataset.sourceHeader;
        if (header in (template.mapping || {})) sel.value = template.mapping[header] || '';
      });
      applyCsvMapperSettingsToUI(template.settings || {});
      showAlert('success', `Loaded mapping template "${name}".`);
    }

    async function saveCsvMapperTemplate() {
      if (!csvMapperData) {
        showAlert('warning', 'Analyze a CSV first, then save its mapping as a template.');
        return;
      }
      const name = (prompt('Save mapping as template — enter a name:') || '').trim();
      if (!name) return;

      const currentMapping = {};
      document.querySelectorAll('#csvMapperTableBody select.csvmap-map-select').forEach(sel => {
        currentMapping[sel.dataset.sourceHeader] = sel.value || null;
      });

      try {
        const res = await fetch('/api/csv-mapper/templates', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, mapping: currentMapping, settings: readCsvMapperSettings() }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        showAlert('success', `Template "${name}" saved.`);
        loadCsvMapperTemplateList();
      } catch (err) {
        showAlert('error', `Failed to save template: ${err.message}`);
      }
    }

    async function deleteCsvMapperTemplate() {
      const name = document.getElementById('csvMapperTemplateSelect').value;
      if (!name) {
        showAlert('warning', 'Select a saved template to delete.');
        return;
      }
      if (!confirm(`Delete template "${name}"?`)) return;
      try {
        await fetch(`/api/csv-mapper/templates/${encodeURIComponent(name)}`, { method: 'DELETE' });
        showAlert('success', `Template "${name}" deleted.`);
        loadCsvMapperTemplateList();
      } catch (err) {
        showAlert('error', `Failed to delete template: ${err.message}`);
      }
    }

    // ── CSV Mapper — settings panel ──────────────────────────────────────────
    function readCsvMapperSettings() {
      const dedupKeys = ['email', 'linkedin', 'phone', 'company_domain', 'company_name']
        .filter(k => document.getElementById(`dedupKey_${k}`)?.checked);
      return {
        dedup_keys: dedupKeys,
        fuzzy: !!document.getElementById('csvMapperFuzzy')?.checked,
        skip_duplicates: !!document.getElementById('csvMapperSkipDuplicates')?.checked,
        enrich: !!document.getElementById('csvMapperEnrich')?.checked,
        save: !!document.getElementById('csvMapperSaveServer')?.checked,
        keep_overrides: (csvMapperData && csvMapperData.keepOverrides) || {},
      };
    }

    function applyCsvMapperSettingsToUI(settings) {
      ['email', 'linkedin', 'phone', 'company_domain', 'company_name'].forEach(k => {
        const el = document.getElementById(`dedupKey_${k}`);
        if (el) el.checked = (settings.dedup_keys || []).includes(k);
      });
      const map = {
        csvMapperFuzzy: !!settings.fuzzy,
        csvMapperSkipDuplicates: settings.skip_duplicates !== false,
        csvMapperEnrich: !!settings.enrich,
        csvMapperSaveServer: settings.save !== false,
      };
      Object.entries(map).forEach(([id, checked]) => {
        const el = document.getElementById(id);
        if (el) el.checked = checked;
      });
    }

    // ── CSV Mapper — clean, validate, dedup ──────────────────────────────────
    async function processCsvMapping() {
      if (!csvMapperData) return;

      const currentMapping = {};
      document.querySelectorAll('#csvMapperTableBody select.csvmap-map-select').forEach(sel => {
        currentMapping[sel.dataset.sourceHeader] = sel.value || null;
      });
      csvMapperData.mapping = currentMapping;

      const btn = document.getElementById('btnProcessCsvMapping');
      const originalLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Processing…';

      try {
        const settings = readCsvMapperSettings();
        const res = await fetch('/api/csv-mapper/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rows: csvMapperData.rows, mapping: currentMapping, settings }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        csvMapperData.cleanedResult = data;
        csvMapperData.settings = settings;
        renderCsvMapperProcessedResults(data);
        document.getElementById('btnDownloadNormalizedCsv').disabled = false;
        if (data.saved_path) { loadCsvMapperDownloads(); loadCsvMapperAllLeads(); }
        showAlert('success',
          `Processed ${data.validation.total} rows — ${data.duplicate_groups.length} duplicate group(s), ` +
          `quality score ${data.quality_score}%.` + (data.saved_path ? ` Saved to ${data.saved_path}.` : ''));
      } catch (err) {
        showAlert('error', `Failed to process CSV: ${err.message}`);
      } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    }

    function setCsvMapperKeepOverride(groupIndex, rowIndex) {
      if (!csvMapperData.keepOverrides) csvMapperData.keepOverrides = {};
      csvMapperData.keepOverrides[groupIndex] = rowIndex;
    }

    function csvmapRowLabel(rowIndex) {
      const cleaned = csvMapperData.cleanedResult.cleaned_rows[rowIndex].cleaned;
      const bits = [cleaned.company, cleaned.name, cleaned.email].filter(Boolean);
      return escapeHtml(bits.join(' — ') || `Row ${rowIndex + 1}`);
    }

    function renderCsvMapperProcessedResults(data) {
      document.getElementById('csvmapStatTotal').textContent = data.validation.total;
      document.getElementById('csvmapStatValid').textContent = data.validation.valid;
      document.getElementById('csvmapStatDuplicates').textContent = data.validation.duplicates;
      document.getElementById('csvmapStatInvalid').textContent = data.validation.invalid;
      document.getElementById('csvmapQualityFill').style.width = `${data.quality_score}%`;
      document.getElementById('csvmapQualityNum').textContent = `${data.quality_score}%`;

      const dupeSection = document.getElementById('csvMapperDupeSection');
      const dupeGroups = document.getElementById('csvMapperDupeGroups');
      if (data.duplicate_groups.length) {
        dupeGroups.innerHTML = data.duplicate_groups.map((group, gi) => {
          const kept = (csvMapperData.keepOverrides && csvMapperData.keepOverrides[gi] !== undefined)
            ? csvMapperData.keepOverrides[gi] : group[0];
          const members = group.map(rowIndex => `
            <label class="csvmap-dedup-member">
              <input type="radio" name="csvmapDupeGroup${gi}" value="${rowIndex}" ${rowIndex === kept ? 'checked' : ''}
                onchange="setCsvMapperKeepOverride(${gi}, ${rowIndex})" />
              ${csvmapRowLabel(rowIndex)}
            </label>`).join('');
          return `<div class="csvmap-dedup-group-row"><strong style="font-size:0.68rem; color:var(--text-dim);">Group ${gi + 1} (${group.length} records)</strong>${members}</div>`;
        }).join('');
        dupeSection.style.display = 'block';
      } else {
        dupeGroups.innerHTML = '';
        dupeSection.style.display = 'none';
      }

      const issuesSection = document.getElementById('csvMapperIssuesSection');
      const issuesBody = document.getElementById('csvMapperIssuesBody');
      const rowsWithIssues = data.cleaned_rows
        .map((r, i) => ({ r, i }))
        .filter(({ r }) => r.issues.length > 0);
      if (rowsWithIssues.length) {
        issuesBody.innerHTML = rowsWithIssues.map(({ r, i }) => `
          <tr>
            <td style="padding:6px 8px;">${i + 1}</td>
            <td style="padding:6px 8px;">${escapeHtml(r.cleaned.company || '—')}</td>
            <td style="padding:6px 8px;">${r.issues.map(iss => `<span class="csvmap-issue-tag">${escapeHtml(iss.field)}: ${escapeHtml(iss.problem.replace(/_/g, ' '))}</span>`).join('')}</td>
          </tr>`).join('');
        issuesSection.style.display = 'block';
      } else {
        issuesBody.innerHTML = '';
        issuesSection.style.display = 'none';
      }

      const report = data.import_report;
      document.getElementById('csvMapperImportReport').textContent =
        `Import report — ${report.imported} to import, ${report.skipped_duplicates} duplicates skipped, ${report.failed} with validation issues.`;

      document.getElementById('csvMapperProcessedResults').style.display = 'block';
    }

    function downloadNormalizedCsv() {
      const result = csvMapperData && csvMapperData.cleanedResult;
      if (!result) return;

      const canonicalOrder = csvMapperData.canonical_fields.map(([key]) => key);
      const canonicalLabels = Object.fromEntries(csvMapperData.canonical_fields);
      const extraColumns = csvMapperData.settings.enrich
        ? [...new Set(result.cleaned_rows.flatMap(r => Object.keys(r.extra)))].sort()
        : [];

      const headers = canonicalOrder.map(k => canonicalLabels[k])
        .concat(extraColumns.map(c => c.replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())));

      const exportSet = new Set(result.export_indices);
      const csvRows = result.cleaned_rows
        .map((r, i) => ({ r, i }))
        .filter(({ i }) => exportSet.has(i))
        .map(({ r }) => {
          const canonicalVals = canonicalOrder.map(k => r.cleaned[k] || '');
          const extraVals = extraColumns.map(c => r.extra[c] || '');
          return canonicalVals.concat(extraVals).map(v => `"${String(v).replace(/"/g, '""')}"`).join(',');
        });

      const csv = [headers.map(h => `"${h.replace(/"/g, '""')}"`).join(','), ...csvRows].join('\n');
      const blob = new Blob([csv], { type: 'text/csv' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `normalized_leads_${new Date().toISOString().slice(0,10)}.csv`;
      a.click();
      showAlert('success', `Downloaded ${csvRows.length} cleaned rows.` + (result.saved_path ? ` A copy was also saved server-side at ${result.saved_path}.` : ''));
    }


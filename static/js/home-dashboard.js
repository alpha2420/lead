// ── Home Dashboard ───────────────────────────────────────────────────────
// Populates #tab-home (templates/index.html) from GET /api/history/aggregate
// — durable all-time totals read from output/metadata_*.json, never the
// ephemeral in-memory GET /api/runs (that registry is wiped on every server
// restart, so a "recent activity"/"all-time" view built on it would go
// blank right after a restart). GET /api/runs is used only for the one
// thing it's actually right for: whether a pipeline is live RIGHT NOW.
// See app/blueprints/history.py's aggregate_stats().

async function loadHomeDashboard() {
  let agg = {};
  let liveRuns = [];
  try {
    const aggRes = await fetch('/api/history/aggregate');
    agg = await aggRes.json();
  } catch (err) {
    console.error('Failed to load /api/history/aggregate', err);
  }
  try {
    const runsRes = await fetch('/api/runs');
    if (runsRes.ok) liveRuns = await runsRes.json();
  } catch (err) {
    console.error('Failed to load /api/runs', err);
  }

  renderHomeStatTiles(agg);
  renderHomePipelineStatus(liveRuns);
  renderRecentSearches(agg.recent_runs || []);
  renderActivityFeed(agg.recent_runs || []);
  renderRecentLeadsPreview(agg.recent_sample || []);
  renderDatasetsOverview(agg);
  if (typeof updateHomeCharts === 'function') updateHomeCharts(agg.outcome_totals || {});
}

function renderHomeStatTiles(agg) {
  const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  setVal('statHomeTotal', (agg.total_leads || 0).toLocaleString());
  setVal('statHomeVerified', (agg.total_verified || 0).toLocaleString());
  setVal('statHomeDuplicates', (agg.total_duplicates || 0).toLocaleString());
  setVal('statHomeSuccessRate', `${(agg.success_rate || 0).toFixed(1)}%`);
  setVal('statHomeAvgRuntime', agg.avg_runtime_seconds != null ? `${agg.avg_runtime_seconds.toFixed(1)}s` : '—');
}

// Deliberately NOT a live mirror of the search drawer's 15-stage stepper
// (static/js/core.js's setStage()/updateStepProgress() key off unique
// stage-N/stepProgress-N ids — a second live copy on Home would collide
// with those ids and mis-number both). This is a lightweight status
// summary only: "something is running, go watch it" or "nothing is
// running right now" — GET /api/runs is the right (if ephemeral) source
// for that one specific fact.
function renderHomePipelineStatus(liveRuns) {
  const container = document.getElementById('homePipelineStatus');
  if (!container) return;
  const active = (liveRuns || []).find(r => r.status === 'running' || r.status === 'pending');
  if (active) {
    container.innerHTML = `
      <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;">
        <div style="display:flex; align-items:center; gap:8px; min-width:0;">
          <span class="status-dot running"></span>
          <span style="font-size:0.8rem; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">Pipeline running — "${escapeHtml(active.inquiry || '')}"</span>
        </div>
        <button class="stat-card-link" onclick="openSearchDrawer()">View Live <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="9 18 15 12 9 6"/></svg></button>
      </div>
    `;
  } else {
    container.innerHTML = `<span style="color: var(--text-dim); font-size: 0.8rem;">No pipeline currently running. Start a new search to see live progress here.</span>`;
  }
}

// Reuses loadHistory()'s exact .history-card markup (static/js/ui-shell.js)
// so Home and the Datasets tab stay visually identical for the same data.
function renderRecentSearches(runs) {
  const container = document.getElementById('homeRecentSearches');
  if (!container) return;
  if (!runs.length) {
    container.innerHTML = `<span style="color: var(--text-dim); font-size: 0.78rem;">No searches yet — run your first search to see it here.</span>`;
    return;
  }
  container.innerHTML = runs.slice(0, 5).map(r => `
    <div class="history-card" style="margin: 0;">
      <div class="history-card-header">
        <span class="history-card-time">${escapeHtml(r.timestamp || '')}</span>
        <span class="history-card-size">${r.verified_pct != null ? r.verified_pct + '% verified' : ''}</span>
      </div>
      <div class="history-card-query">"${escapeHtml(r.inquiry || 'Manual Import')}"</div>
      <div class="history-card-footer">
        <div class="history-card-stats">
          <span class="badge badge-valid">${r.leads || 0} Leads</span>
        </div>
        <button class="history-btn-load" onclick="viewHistoryRun('${escapeHtml(r.filename)}')">Load Dataset &rarr;</button>
      </div>
    </div>
  `).join('');
}

// Same underlying data as Recent Searches (both come from
// GET /api/history/aggregate's recent_runs) by design — the user asked to
// keep both, differentiated by presentation: cards above, a denser log here.
function renderActivityFeed(runs) {
  const container = document.getElementById('homeActivityFeed');
  if (!container) return;
  if (!runs.length) {
    container.innerHTML = `<span style="color: var(--text-dim); font-size: 0.78rem;">No activity yet.</span>`;
    return;
  }
  container.innerHTML = runs.slice(0, 8).map(r => `
    <div style="display:flex; align-items:flex-start; gap:8px; font-size:0.75rem; padding:6px 0; border-bottom:1px solid var(--border);">
      <span style="color: var(--green); flex-shrink:0; line-height:1.4;">&#9679;</span>
      <div style="flex:1; min-width:0;">
        <div style="color: var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">Run completed — "${escapeHtml(r.inquiry || 'Manual Import')}"</div>
        <div style="color: var(--text-dim); font-size:0.68rem;">${escapeHtml(r.timestamp || '')} &middot; ${r.leads || 0} leads</div>
      </div>
    </div>
  `).join('');
}

function renderRecentLeadsPreview(sample) {
  const tbody = document.getElementById('homeRecentLeadsBody');
  if (!tbody) return;
  if (!sample.length) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; color: var(--text-dim); padding: 16px;">No leads yet.</td></tr>`;
    return;
  }
  const badgeMap = { valid: 'badge-valid', invalid: 'badge-invalid', 'catch-all': 'badge-catchall' };
  tbody.innerHTML = sample.slice(0, 8).map(l => {
    const status = l.status || l._verification_status || 'unverified';
    const badgeClass = badgeMap[status] || 'badge-other';
    return `
      <tr>
        <td>${escapeHtml(l.name || '')}</td>
        <td>${escapeHtml(l.title || '')}</td>
        <td>${escapeHtml(l.company || '')}</td>
        <td><span class="badge ${badgeClass}">${escapeHtml(status)}</span></td>
      </tr>
    `;
  }).join('');
}

function renderDatasetsOverview(agg) {
  const container = document.getElementById('homeDatasetsOverview');
  if (!container) return;
  const items = [
    { label: 'Total Searches Run', value: agg.run_count || 0 },
    { label: 'Data Stored', value: `${agg.disk_size_kb || 0} KB` },
    { label: 'Most Recent', value: (agg.recent_runs && agg.recent_runs[0]) ? agg.recent_runs[0].timestamp : '—' },
  ];
  container.innerHTML = items.map(i => `
    <div style="display:flex; justify-content:space-between; align-items:center; font-size:0.78rem; padding:6px 0; border-bottom:1px solid var(--border);">
      <span style="color: var(--text-dim);">${escapeHtml(i.label)}</span>
      <span style="color: var(--text); font-weight:600;">${escapeHtml(String(i.value))}</span>
    </div>
  `).join('');
}

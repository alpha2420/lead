// ── Search Drawer Wizard ─────────────────────────────────────────────────
// Turns the "New Search" drawer from one long always-visible scroll into
// three progressive-disclosure sections — Describe -> Refine ICP ->
// Configure & Run — plus a separate Progress view once a run starts.
//
// Deliberately NOT a rigid one-panel-at-a-time wizard: sections are
// .wizard-section accordions (locked until the previous one is finished,
// same mechanic this codebase's .strategy-module already uses), not
// mutually-exclusive hidden panels. This matters because the AI Strategist
// chat is conversational — a user may send several messages back and
// forth, well after Section 2's accordions are already visible, and
// expects them to keep updating live. Since renderICP() (icp.js) already
// re-syncs every accordion whenever lastParsedICP changes, and Section 1
// is only ever collapsed (never destroyed/hidden), re-opening it to send
// another message just works, with zero new plumbing and no step reset.
//
// Website ICP generation (generateIcpFromWebsite(), ui-shell.js) shares
// the exact same #chatLog as AI chat — it's a secondary input method
// *within* Section 1, not a separate top-level mode, since splitting it
// out would mean either duplicating #chatLog (invalid: duplicate DOM ids)
// or reparenting it between panels on every mode switch. Manual Search and
// Import ARE separate top-level modes (setWizardMode('manual'|'import')),
// since neither touches the chat log at all.
//
// Import is structurally shorter: its parsed ICP is never used to plan a
// search (app/pipeline_runner.py's run_pipeline_imported_thread skips
// Company Discovery/Validation/Search Planning as instant no-ops), so it
// skips Section 2 entirely — continueFromImport() jumps straight to
// Section 3, which also hides Target Leads/Execution Profile there since
// neither controls anything on the import path.

let wizardMode = 'ai';

function setWizardMode(mode) {
  wizardMode = mode;
  document.getElementById('aiSearchPanel').style.display = mode === 'ai' ? 'flex' : 'none';
  document.getElementById('manualSearchPanel').style.display = mode === 'manual' ? 'flex' : 'none';
  document.getElementById('importSearchPanel').style.display = mode === 'import' ? 'flex' : 'none';
  document.getElementById('btn-mode-ai').classList.toggle('active', mode === 'ai');
  document.getElementById('btn-mode-manual').classList.toggle('active', mode === 'manual');
  document.getElementById('btn-mode-import').classList.toggle('active', mode === 'import');
  if (mode === 'manual' && typeof renderManualSearchTags === 'function') renderManualSearchTags();

  const nonImportFields = document.getElementById('nonImportSettingsFields');
  if (nonImportFields) nonImportFields.style.display = mode === 'import' ? 'none' : '';
  const btnRun = document.getElementById('btnRun');
  const btnImportRun = document.getElementById('btnImportRun');
  if (btnRun) btnRun.style.display = mode === 'import' ? 'none' : '';
  if (btnImportRun) btnImportRun.style.display = mode === 'import' ? '' : 'none';

  renderWizardProgress();
}

// ── Section lock/open primitives — mirrors toggleStrategyModule()'s
// existing open/closed mechanic (icp.js). "locked" is visual-only (dims a
// section that hasn't been touched yet, in .wizard-section.locked's CSS) —
// it never blocks a click. Any user can freely open Refine ICP or Configure
// & Run right away without describing a target first: Configure & Run's
// fields (target lead count, execution profile, Run Pipeline) don't need an
// ICP to be useful, and Refine ICP shows its own explanatory empty state
// when there isn't one yet (see syncRefinementPanelOnly(), icp.js). ──────
// Configure & Run's body reuses the sidebar's #settingsContent panel
// (collapsed by default — max-height:0 until toggleSettings() adds .show,
// see static/css/layout.css). The wizard accordion has no toggle control of
// its own for it, so without this it stays visibly empty (Target Leads,
// Execution Profile, even the Run Pipeline button all live inside it) no
// matter which of the wizard's several open-this-section paths got used.
function _ensureConfigureContentExpanded(id) {
  if (id !== 'wizardSectionConfigure') return;
  const settingsContent = document.getElementById('settingsContent');
  if (settingsContent) settingsContent.classList.add('show');
}

function toggleWizardSection(id) {
  const el = document.getElementById(id);
  if (!el) return;

  // Sections toggle independently (see module docstring — not a rigid
  // one-panel-at-a-time wizard), but collapsing the one currently-open
  // section down to zero leaves every row shut with no visual cue for
  // which header to click next. Refuse that specific transition — the
  // user can still switch which section is open by opening a different
  // one, just not close the last one down to nothing.
  const isOpen = document.querySelectorAll('.wizard-section.open').length;
  if (el.classList.contains('open') && isOpen <= 1) return;

  el.classList.remove('locked');   // first click clears the dimmed "not yet touched" look
  el.classList.toggle('open');
  if (el.classList.contains('open')) _ensureConfigureContentExpanded(id);
  renderWizardProgress();
}

function unlockWizardSection(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('locked');
}

function openWizardSection(id) {
  const el = document.getElementById(id);
  if (el) { el.classList.remove('locked'); el.classList.add('open'); }
  _ensureConfigureContentExpanded(id);
}

function collapseWizardSection(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}

// ── Section transitions ──────────────────────────────────────────────────
function continueToRefine() {
  if (!lastParsedICP) return;
  collapseWizardSection('wizardSectionDescribe');
  openWizardSection('wizardSectionRefine');
}

function continueFromManual() {
  if (typeof buildICPFromManualFields === 'function' && buildICPFromManualFields()) {
    continueToRefine();
  }
}

function continueFromImport() {
  const fileInput = document.getElementById('importFile');
  if (!fileInput || fileInput.files.length === 0) {
    showAlert('warning', 'Please select a JSON or CSV file to import.');
    return;
  }
  hideAlert();
  collapseWizardSection('wizardSectionDescribe');
  openWizardSection('wizardSectionConfigure');
}

function continueToConfigure() {
  collapseWizardSection('wizardSectionRefine');
  openWizardSection('wizardSectionConfigure');
}

// Called from icp.js's renderICP() — the single function every
// ICP-producing path (chat, website, manual, history reload) already
// funnels through, so hooking the unlock here can't drift out of sync
// with any one of them.
function onICPReady() {
  if (wizardMode === 'import') return; // import never unlocks Section 2
  unlockWizardSection('wizardSectionRefine');
  const btn = document.getElementById('btnContinueToRefine');
  if (btn) btn.disabled = false;
  const summary = document.getElementById('describeSummary');
  if (summary && lastParsedICP && lastParsedICP.icp_summary) {
    const text = lastParsedICP.icp_summary;
    summary.textContent = text.length > 60 ? text.slice(0, 60) + '…' : text;
  }
  renderWizardProgress();
}

// ── Step indicator — purely visual; reflects each section's real
// locked/open state rather than tracking a separate "current step"
// number, so it can never drift out of sync with what's actually usable. ──
function renderWizardProgress() {
  const el = document.getElementById('wizardProgress');
  if (!el) return;

  const steps = wizardMode === 'import'
    ? [{ n: 1, label: 'Import', section: 'wizardSectionDescribe' },
       { n: 2, label: 'Configure & Run', section: 'wizardSectionConfigure' }]
    : [{ n: 1, label: 'Describe', section: 'wizardSectionDescribe' },
       { n: 2, label: 'Refine ICP', section: 'wizardSectionRefine' },
       { n: 3, label: 'Configure & Run', section: 'wizardSectionConfigure' }];

  el.innerHTML = steps.map((s, i) => {
    const secEl = document.getElementById(s.section);
    const locked = secEl && secEl.classList.contains('locked');
    const open = secEl && secEl.classList.contains('open');
    const cls = locked ? '' : (open ? 'active' : 'done');
    const sep = i < steps.length - 1 ? '<span class="wizard-progress-sep"></span>' : '';
    return `<span class="wizard-progress-step ${cls}">${s.n}. ${s.label}</span>${sep}`;
  }).join('');
}

// ── Progress view swap — mutually exclusive with #searchWizardSections,
// entered the moment a run's SSE connection opens (sse-run-lifecycle.js),
// so live progress replaces the input controls instead of sitting below
// them. ───────────────────────────────────────────────────────────────
function enterProgressView() {
  const sections = document.getElementById('searchWizardSections');
  const wizardProgress = document.getElementById('wizardProgress');
  const progress = document.getElementById('pipelineProgressView');
  if (sections) sections.style.display = 'none';
  if (wizardProgress) wizardProgress.style.display = 'none';
  if (progress) progress.style.display = 'block';
  const banner = document.getElementById('progressBanner');
  if (banner) banner.innerHTML = '';
  const stopBtn = document.getElementById('btnStopRun');
  if (stopBtn) { stopBtn.style.display = ''; stopBtn.disabled = false; stopBtn.textContent = 'Stop Pipeline'; }
}

function exitProgressView() {
  const sections = document.getElementById('searchWizardSections');
  const wizardProgress = document.getElementById('wizardProgress');
  const progress = document.getElementById('pipelineProgressView');
  if (sections) sections.style.display = '';
  if (wizardProgress) wizardProgress.style.display = '';
  if (progress) progress.style.display = 'none';
}

// Called from pipeline-run.js's SSE 'complete' handler. Deliberately does
// NOT auto-close the drawer or reset the wizard — the existing "only N
// leads collected" warning (showAlert('warning', ...), fired separately by
// the same handler) needs to stay visible rather than get hidden by an
// automatic close, matching this codebase's existing "warn, don't silently
// minimize" philosophy (e.g. score_floor_underfilled's handling server-side).
function onRunComplete(stats) {
  const sample = (stats && stats.sample) || 0;
  const banner = document.getElementById('progressBanner');
  if (banner) {
    banner.innerHTML = `
      <div class="alert success show" style="margin-bottom:10px;">✓ Complete — ${sample} lead${sample === 1 ? '' : 's'} exported.</div>
      <div class="btn-pair">
        <button class="btn-run" onclick="switchTab('tab-crm'); closeSearchDrawer();">View Leads</button>
        <button class="btn-dashed" onclick="resetWizard();">New Search</button>
      </div>
    `;
  }
  const stopBtn = document.getElementById('btnStopRun');
  if (stopBtn) stopBtn.style.display = 'none';
}

// Called from pipeline-run.js's SSE 'error' handler. Stays in the progress
// view with explicit backtrack options rather than forcing a full restart
// — same lossless-backtrack principle as Section 1's "always reopenable"
// chat accordion.
function onRunError(message) {
  const banner = document.getElementById('progressBanner');
  if (banner) {
    banner.innerHTML = `
      <div class="alert error show" style="margin-bottom:10px;">✕ ${escapeHtml(message || 'Something went wrong.')}</div>
      <div class="btn-pair">
        <button class="btn-dashed" onclick="exitProgressView(); openWizardSection('wizardSectionConfigure'); renderWizardProgress();">&larr; Back to Configure</button>
        <button class="btn-dashed" onclick="exitProgressView(); openWizardSection('wizardSectionRefine'); renderWizardProgress();">&larr; Back to Refine ICP</button>
      </div>
    `;
  }
  const stopBtn = document.getElementById('btnStopRun');
  if (stopBtn) stopBtn.style.display = 'none';
}

// Called from pipeline-run.js's SSE 'cancelled' handler (user clicked Stop
// Pipeline). Same backtrack options as onRunError, since the sample so far
// was discarded server-side (app/pipeline_runner.py never reaches export_csv
// on a cancelled run) — there's nothing to view, only somewhere to go back to.
function onRunCancelled() {
  const banner = document.getElementById('progressBanner');
  if (banner) {
    banner.innerHTML = `
      <div class="alert warning show" style="margin-bottom:10px;">■ Pipeline stopped.</div>
      <div class="btn-pair">
        <button class="btn-dashed" onclick="exitProgressView(); openWizardSection('wizardSectionConfigure'); renderWizardProgress();">&larr; Back to Configure</button>
        <button class="btn-dashed" onclick="resetWizard();">New Search</button>
      </div>
    `;
  }
  const stopBtn = document.getElementById('btnStopRun');
  if (stopBtn) stopBtn.style.display = 'none';
}

// Clears wizard + chat state and returns to Section 1 — called from the
// completion banner's "New Search" button.
function resetWizard() {
  lastParsedICP = null;
  if (typeof syncRefinementPanelOnly === 'function') syncRefinementPanelOnly();   // clear stale ICP content, restore the empty state
  if (typeof chatHistory !== 'undefined') chatHistory.length = 0;

  const chatLog = document.getElementById('chatLog');
  if (chatLog) {
    chatLog.innerHTML = `<div class="chat-msg chat-ai"><strong>AI Strategist</strong> — Describe your target audience or company profile and I&rsquo;ll build an Ideal Customer Profile with ready-to-run search queries.</div>`;
  }
  const inquiry = document.getElementById('inquiry');
  if (inquiry) inquiry.value = '';
  const importInquiry = document.getElementById('importInquiry');
  if (importInquiry) importInquiry.value = '';
  const fileName = document.getElementById('fileName');
  if (fileName) fileName.textContent = '';
  const importFile = document.getElementById('importFile');
  if (importFile) importFile.value = '';

  collapseWizardSection('wizardSectionRefine');
  collapseWizardSection('wizardSectionConfigure');
  document.getElementById('wizardSectionRefine').classList.add('locked');
  document.getElementById('wizardSectionConfigure').classList.add('locked');
  openWizardSection('wizardSectionDescribe');

  const btn = document.getElementById('btnContinueToRefine');
  if (btn) btn.disabled = true;
  const summary = document.getElementById('describeSummary');
  if (summary) summary.textContent = '';

  setWizardMode('ai');
  exitProgressView();
  renderWizardProgress();
}

'use strict';

let _importCount = 0;   // tracks how many times Import has been successfully used

/* ── Freemium tier helpers ────────────────────────────────── */

const FREE_HAND_LIMIT   = 30;
const FREE_EXPORT_LIMIT = 5;   // single-hand exports per calendar day (free tier)
const _SESSION_KEY      = 'pppha_session_id';

// ── Tier-gated UI element lists ────────────────────────────────────────────────
// Add/remove IDs here to control which elements are shown per tier.
// FREE_ONLY_ELS  → visible to non-Pro users; hidden for Pro.
// PRO_ONLY_ELS   → visible to Pro users; hidden for non-Pro (note: sections managed
//                  by _loadHistory() are NOT listed here — they handle their own state).
const FREE_ONLY_ELS = [
  'tier-compare',       // Free vs Pro marketing cards
];
const PRO_ONLY_ELS = [
  // (tournament sections managed separately by _loadHistory)
];

// Firebase handles — populated by _initFirebase()
let _analytics   = null;
let _db          = null;
let _auth        = null;
let _currentUser = null;  // firebase.User or null

// In-memory freemium state — loaded from Firestore on auth change
// Defaults to free tier until Firestore responds (safe fallback)
let _userState = { is_pro: false, exports_today: 0, last_export_date: '' };

function getSessionId() {
  let id = localStorage.getItem(_SESSION_KEY);
  if (!id) {
    id = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
          const r = Math.random() * 16 | 0;
          return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    localStorage.setItem(_SESSION_KEY, id);
  }
  return id;
}

function isPro() {
  return _userState.is_pro === true;
}

function _todayStr() {
  return new Date().toISOString().slice(0, 10); // 'YYYY-MM-DD'
}

function checkExportQuota() {
  if (isPro()) return true;
  const today = _todayStr();
  if (_userState.last_export_date !== today) return true;
  return (_userState.exports_today || 0) < FREE_EXPORT_LIMIT;
}

function consumeExportQuota() {
  if (isPro()) return;
  const today = _todayStr();
  // Update in-memory state synchronously so subsequent gate checks are correct
  if (_userState.last_export_date !== today) {
    _userState.exports_today    = 0;
    _userState.last_export_date = today;
  }
  _userState.exports_today = (_userState.exports_today || 0) + 1;
  // Persist to Firestore async (non-blocking)
  _trackEvent('export_clicked', { allowed: true });
  if (_db) {
    const ref = _getUserDocRef();
    if (ref) ref.set({
      exports_today:    _userState.exports_today,
      last_export_date: today,
      last_seen:        firebase.firestore.FieldValue.serverTimestamp(),
    }, { merge: true }).catch(e => console.warn('Firestore quota update failed:', e));
  }
  _renderExportCounter();
}

function showUpgradeModal(reason) {
  const reasonEl = document.getElementById('pro-modal-reason');
  if (reasonEl) {
    const msgs = {
      export: "You've used your free export for today. Upgrade to Pro for unlimited daily exports.",
      hands:  "Free accounts see only the last 30 hands. Upgrade to Pro for unlimited history.",
      tourney:"Tournament exports are a Pro feature.",
    };
    reasonEl.textContent = msgs[reason] || '';
  }
  // Reset coming-soon banner and button
  const cs  = document.getElementById('pro-coming-soon');
  const btn = document.getElementById('pro-upgrade-btn');
  if (cs)  cs.classList.add('d-none');
  if (btn) btn.disabled = false;
  // Show dev hatch only when ?dev=1
  const devHatch = document.getElementById('pro-dev-hatch');
  if (devHatch) {
    devHatch.classList.toggle('d-none', new URLSearchParams(location.search).get('dev') !== '1');
  }
  _trackEvent('pro_modal_shown', { reason });
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('pro-upgrade-modal'));
  modal.show();
}

function handleUpgradeClick(tier = 'pro') {
  _trackEvent('pro_upgrade_clicked');
  const btn = document.getElementById('pro-upgrade-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Redirecting…'; }
  const fail = (msg) => {
    // Must match the button's initial label in index.html, or a failed checkout
    // silently relabels the CTA and drops the price from it.
    if (btn) { btn.disabled = false; btn.textContent = 'Get Early Access — A$7.99/month'; }
    const cs = document.getElementById('pro-coming-soon');
    if (cs) { cs.textContent = msg; cs.classList.remove('d-none'); }
  };
  // uid/email are derived server-side from this token and are no longer sent in
  // the body — the server refuses the call without it.
  if (!_currentUser) { fail('Please sign in before upgrading.'); return; }
  _currentUser.getIdToken()
    .then(token => fetch('/api/create-checkout-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify({ tier }),
    }))
    .then(r => r.json())
    .then(d => {
      if (d.url) { window.location.href = d.url; }
      else throw new Error(d.error || 'Could not start checkout');
    })
    .catch(err => fail(err.message));
}

// activateProDev()/deactivateProDev() removed: they wrote is_pro straight from
// the client, which the Firestore rules now reject (see firestore.rules — a
// client-writable is_pro meant any signed-in user could self-grant Pro).

/* ── Helpers ─────────────────────────────────────────────── */

function fmtChips(n) {
  if (n == null) return 'N/A';
  return Math.abs(n).toLocaleString();
}

function fmtProfitHtml(n) {
  if (n === 0) return '<span class="profit-zero">0</span>';
  const abs = Math.abs(n).toLocaleString();
  return n > 0
    ? `<span class="profit-pos">+${abs}</span>`
    : `<span class="profit-neg">−${abs}</span>`;
}

function fmtProfitPlain(n) {
  if (n === 0) return '0';
  const abs = Math.abs(n).toLocaleString();
  return n > 0 ? `+${abs}` : `−${abs}`;
}

function renderCard(card) {
  return `<span class="playing-card ${card.suit_class}">
    <span class="card-rank">${card.rank}</span><span class="card-suit">${card.suit}</span>
  </span>`;
}

function resultBadge(result) {
  if (result === 'Won')  return '<span class="badge-won">Won</span>';
  if (result === 'Lost') return '<span class="badge-lost">Lost</span>';
  return '<span class="badge-break">Break even</span>';
}

function posBadge(pos) {
  if (!pos || pos === '?') return '<span class="pos-badge pos-bl">?</span>';
  const cls = pos === 'BTN' ? 'pos-btn' : (pos === 'SB' || pos === 'BB') ? 'pos-bl' : '';
  return `<span class="pos-badge ${cls}">${pos}</span>`;
}

function streetBadge(s) {
  if (s === 'Pre')      return `<span class="pos-badge pos-bl">Pre</span>`;
  if (s === 'Pre VPIP') return `<span class="pos-badge" style="color:var(--yellow);border-color:rgba(227,179,65,.3);background:rgba(227,179,65,.12)">Pre VPIP</span>`;
  if (s === 'Flop')  return `<span class="pos-badge" style="color:var(--blue);border-color:rgba(88,166,255,.3);background:rgba(88,166,255,.12)">Flop</span>`;
  if (s === 'Turn')  return `<span class="pos-badge" style="color:var(--yellow);border-color:rgba(227,179,65,.3);background:rgba(227,179,65,.12)">Turn</span>`;
  if (s === 'River') return `<span class="pos-badge" style="color:var(--green);border-color:rgba(63,185,80,.3);background:rgba(63,185,80,.12)">River</span>`;
  if (s === 'SD')    return `<span class="pos-badge" style="color:#a371f7;border-color:rgba(163,113,247,.3);background:rgba(163,113,247,.12)">Showdown</span>`;
  return `<span class="pos-badge pos-bl">${s || '—'}</span>`;
}

function fmtProfitBB(profit, bigBlind) {
  if (!bigBlind || profit == null) return '<span class="profit-zero">—</span>';
  const bb = profit / bigBlind;
  if (bb === 0) return '<span class="profit-zero">0</span>';
  const abs = Math.abs(bb).toFixed(2);
  return bb > 0
    ? `<span class="profit-pos">+${abs}</span>`
    : `<span class="profit-neg">−${abs}</span>`;
}

function shortHandNum(gameid) {
  const parts = (gameid || '').split('-');
  return parts[2] ? String(parseInt(parts[2], 10)) : (gameid || '—');
}

/* ── Timezone helpers ────────────────────────────────────── */

function currentTz() {
  const sel = document.getElementById('tz-select');
  return sel ? sel.value : 'Australia/Adelaide';
}

/** Format parts of a date/time into a plain object keyed by part type. */
function _tzParts(ts, tz, opts) {
  const out = {};
  new Intl.DateTimeFormat('en-AU', Object.assign({ timeZone: tz }, opts))
    .formatToParts(new Date(ts * 1000))
    .forEach(function (p) { out[p.type] = p.value; });
  return out;
}

/** "8 Jun 26, 14:30" — on mobile collapses to "8 Jun, 14:30" */
let _pendingHandId  = '';
let _exportHandCb   = null;

function copyHandId(btn) {
  const handNum = btn.dataset.handNum;
  _pendingHandId = handNum;
  navigator.clipboard.writeText(handNum).catch(() => {});
  btn.innerHTML = `<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>`;
  btn.classList.add('copied');
  setTimeout(() => {
    btn.classList.remove('copied');
    btn.innerHTML = `<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  }, 1500);
}

function _openExportHandModal(cb) {
  _exportHandCb = cb;
  const input  = document.getElementById('hand-id-input');
  const status = document.getElementById('hand-id-status');
  input.value  = _pendingHandId;
  status.innerHTML = '';
  _pendingHandId = '';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('modal-export-hand')).show();
  // Focus + trigger validation if pre-filled
  setTimeout(() => {
    input.focus();
    if (input.value) _validateExportHandInput(input.value);
  }, 300);
}

function _validateExportHandInput(val) {
  const status = document.getElementById('hand-id-status');
  const okBtn  = document.getElementById('hand-id-ok-btn');
  const trimmed = val.trim();
  if (!trimmed) {
    status.innerHTML = '';
    if (okBtn) okBtn.disabled = true;
    return;
  }
  if (/^\d{12}(-\w+)+$/.test(trimmed)) {
    status.innerHTML = `<span style="color:var(--green)">✓ Valid hand ID</span>`;
    if (okBtn) okBtn.disabled = false;
  } else {
    const hint = /^\d{12}/.test(trimmed) ? 'Missing segments after timestamp'
                                          : 'Must start with a 12-digit timestamp (e.g. 260611124411-…)';
    status.innerHTML = `<span style="color:var(--red)">✗ ${hint}</span>`;
    if (okBtn) okBtn.disabled = true;
  }
}

function _confirmExportHand() {
  const input  = document.getElementById('hand-id-input');
  const status = document.getElementById('hand-id-status');
  const okBtn  = document.getElementById('hand-id-ok-btn');
  const val    = input.value.trim();
  if (!val || !/^[\w-]{4,}$/.test(val) || !_exportHandCb) return;

  const cb = _exportHandCb;
  _exportHandCb = null;
  if (okBtn) okBtn.disabled = true;

  status.innerHTML = `<span style="color:var(--muted)">Exporting hand <strong>${val}</strong>…</span>`;

  const onDone = () => {
    status.innerHTML = `<span style="color:var(--green)">✓ Export completed successfully</span>`;
    setTimeout(() => bootstrap.Modal.getOrCreateInstance(
      document.getElementById('modal-export-hand')).hide(), 1500);
  };
  const onFail = (msg) => {
    status.innerHTML = `<span style="color:var(--red)">✗ ${msg || 'Export failed'}</span>`;
    _exportHandCb = cb;
    if (okBtn) okBtn.disabled = false;
  };

  const result = cb(val);
  if (result && typeof result.then === 'function') {
    result.then(onDone).catch(err => onFail(err.message));
  } else {
    onDone();
  }
}

function fmtHandDateTime(ts, tz) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const dateFull = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, day: 'numeric', month: 'short', year: '2-digit',
  }).format(d);
  const dateShort = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, day: 'numeric', month: 'short',
  }).format(d);
  const time = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(d);
  return `<span class="d-none d-md-inline">${dateFull}, ${time}</span>`
       + `<span class="d-md-none">${dateShort}<br>${time}</span>`;
}

/** "8 Jun 26" */
function fmtDate(ts, tz) {
  if (!ts) return '—';
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, day: 'numeric', month: 'short', year: '2-digit',
  }).format(new Date(ts * 1000));
}

/** "2:30 PM" */
function fmtTime(ts, tz) {
  if (!ts) return '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour: 'numeric', minute: '2-digit', hour12: true,
  }).format(new Date(ts * 1000));
}

/** "ACDT", "UTC", "EST", etc. */
function getTzAbbr(ts, tz) {
  const parts = new Intl.DateTimeFormat('en-AU', {
    timeZone: tz, timeZoneName: 'short',
  }).formatToParts(new Date(ts * 1000));
  return (parts.find(function (p) { return p.type === 'timeZoneName'; }) || {}).value || tz;
}

/** Refresh every [data-tz-label] column header to show the current abbreviation. */
function updateTzHeaders() {
  const data  = window._lastData;
  const refTs = (data && data.tournaments && data.tournaments[0] &&
                 data.tournaments[0].earliest_ts)
                 || Date.now() / 1000;
  const abbr = getTzAbbr(refTs, currentTz());
  document.querySelectorAll('[data-tz-label]').forEach(function (th) {
    th.textContent = th.dataset.tzLabel + ' (' + abbr + ')';
  });
}

/* ── UI helpers ──────────────────────────────────────────── */

function showError(msg) {
  const el = document.getElementById('error-msg');
  el.textContent = msg;
  el.classList.remove('d-none');
}

function clearError() {
  const el = document.getElementById('error-msg');
  el.classList.add('d-none');
  el.textContent = '';
}

function setLoading(on) {
  const box      = document.getElementById('loading-msg');
  const spinner  = document.getElementById('loading-spinner');
  const text     = document.getElementById('loading-text');
  if (on) {
    spinner.classList.remove('d-none');
    text.style.color = 'var(--green)';
    text.textContent = 'Fetching hand history… this may take a few seconds';
    box.classList.remove('d-none');
  } else {
    box.classList.add('d-none');
  }
  document.getElementById('import-btn').disabled = on;
}

function showImportSuccess(data) {
  const box     = document.getElementById('loading-msg');
  const spinner = document.getElementById('loading-spinner');
  const text    = document.getElementById('loading-text');
  const name    = data.player?.name || 'Player';
  const hands   = data.new_hands ?? 0;
  const tours   = data.new_tourney_count ?? 0;
  spinner.classList.add('d-none');
  text.style.color = 'var(--green)';
  _importCount++;
  const greeting = _importCount > 1 ? 'Welcome back' : 'Welcome';
  text.innerHTML =
    `✓ ${greeting}, <strong>${name}</strong>! ` +
    `<strong>${hands}</strong> hands loaded` +
    (tours ? ` across <strong>${tours}</strong> tournament${tours !== 1 ? 's' : ''}` : '') +
    `.`;
  box.classList.remove('d-none');
}

/* ── Import handler ──────────────────────────────────────── */

function handleImport() {
  const url = (document.getElementById('url-input').value || '').trim();
  clearError();
  document.getElementById('results-section').classList.add('d-none');

  if (!url) {
    showError('Please enter a PPPoker Hand Review URL.');
    return;
  }

  setLoading(true);

  const _doFetch = (idToken) => {
    const headers = { 'Content-Type': 'application/json' };
    if (idToken) headers['Authorization'] = `Bearer ${idToken}`;
    fetch('/api/analyze', {
      method: 'POST',
      headers,
      body: JSON.stringify({ url, session_id: getSessionId() }),
    })
      .then(r => r.json())
      .then(data => {
        setLoading(false);
        if (data.error) { showError(data.error); return; }
        renderResults(data);
        showImportSuccess(data);
        if (data.saved) {
          _startImportHighlights((data.tournaments || []).map(t => t.tourney_id).filter(Boolean));
          _loadHistory();
        }
      })
      .catch(err => {
        setLoading(false);
        showError('Network error: ' + err.message);
      });
  };

  if (_currentUser) {
    _currentUser.getIdToken().then(_doFetch).catch(() => _doFetch(null));
  } else {
    _doFetch(null);
  }
}

/* ── Render all ──────────────────────────────────────────── */

function renderResults(data) {
  window._lastData = data;   // persisted so tz changes can re-render
  _trackEvent('hands_imported', { total: data.total_fetched || 0 });

  // Derive date span from newly-imported hands only
  const _spanStr = (() => {
    if (!data.new_ts_min) return null;
    const tz   = currentTz();
    const opts = { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz };
    const dMin = new Date(data.new_ts_min * 1000).toLocaleDateString('en-GB', opts);
    const dMax = new Date(data.new_ts_max * 1000).toLocaleDateString('en-GB', opts);
    return dMin === dMax ? dMax : `${dMin} – ${dMax}`;
  })();

  // Player avatar initials
  const _initials = (data.player.name || '').replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase() || '??';
  const _avatarEl = document.getElementById('player-avatar-text');
  if (_avatarEl) _avatarEl.textContent = _initials;

  document.getElementById('player-info').innerHTML =
    `<strong>${data.player.name}</strong>` +
    `<span style="color:var(--muted);font-size:.8rem">&nbsp;&nbsp;UID: ${data.player.uid}</span>` +
    (_spanStr ? `<span style="color:var(--muted);font-size:.78rem">&nbsp;&nbsp;${_spanStr}</span>` : '') +
    (data.total_fetched < data.total_available
      ? `&nbsp;&nbsp;<span class="text-warning" style="font-size:.8rem">(${data.total_available - data.total_fetched} hands failed to load)</span>`
      : '');

  renderHandStats(data);
  renderRecentHands(data.recent_hands || []);
  renderRecentWonHands(data.recent_won_hands || []);
  // Pro users get the persisted cross-session Tournament History from the
  // independent top-level section below (populated by _loadHistory(), which
  // runs whether or not an import happened this session). The free-tier card
  // inside #results-section only shows the current import's tournaments.
  const freeCard = document.getElementById('free-tournament-history-card');
  if (data.saved) {
    if (freeCard) freeCard.classList.add('d-none');
  } else {
    if (freeCard) freeCard.classList.remove('d-none');
    renderTournaments(data.tournaments || []);
  }
  updateTzHeaders();
  _updateExportGates();
  _renderPlayerExportAll();

  document.getElementById('results-section').classList.remove('d-none');
  document.getElementById('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ── Shared hand table renderer ──────────────────────────── */

function renderHandsTable(hands, tbodyId, options = {}) {
  if (!isPro() && hands.length > FREE_HAND_LIMIT) {
    hands = hands.slice(-FREE_HAND_LIMIT);
  }
  const tbody = document.getElementById(tbodyId);
  if (!hands.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No hands available</td></tr>';
    return;
  }

  const tz = currentTz();
  const showExport = !!options.showExport;
  const exportTid  = options.exportTid || '';   // if set, route export through storage endpoints

  tbody.innerHTML = hands.map(h => {
    const cards = (h.hole_cards || []).map(renderCard).join('');
    const copyBtn = h.hand_num
      ? `<button class="copy-hand-btn" onclick="copyHandId(this)" data-hand-num="${h.hand_num}" title="Hand ID: ${h.hand_num}">
           <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
         </button>`
      : '';

    // Cols 5+6 differ between normal tables (Result, Net P/L) and export tables (Net P/L, Export)
    let cols56;
    if (showExport) {
      const hn = h.hand_num;
      const tid = exportTid;
      const exportBtns = hn
        ? `<div class="d-flex gap-1 flex-wrap justify-content-center">
            <button class="btn export-icon-btn" data-platform="PokerTracker" title="Export PT4" onclick="exportHandFromRow('${hn}','PokerTracker',this,'${tid}')">
              <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="16" height="16" alt="PT">
            </button>
            <button class="btn export-icon-btn" data-platform="DriveHUD" title="Export DriveHUD" onclick="exportHandFromRow('${hn}','DriveHUD',this,'${tid}')">
              <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="16" height="16" alt="DH">
            </button>
            <button class="btn export-icon-btn" data-platform="GTOWizard" title="Export GTO Wizard" onclick="exportHandFromRow('${hn}','GTOWizard',this,'${tid}')">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
            </button>
            <button class="btn export-icon-btn" title="Export JSON" onclick="exportHandFromRow('${hn}','',this,'${tid}')">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
            </button>
          </div>`
        : '<span class="text-muted">—</span>';
      // Export mode: Net P/L (BB) | Export
      cols56 = `<td>${fmtProfitBB(h.profit, h.big_blind)}</td>
      <td class="text-center d-none d-lg-table-cell">${exportBtns}</td>`;
    } else {
      // Normal mode: Result | Net P/L (BB)
      cols56 = `<td class="d-none d-lg-table-cell">${resultBadge(h.result)}</td>
      <td>${fmtProfitBB(h.profit, h.big_blind)}</td>`;
    }

    // Column visibility mirrors the header priority in index.html: cards and
    // net p/l always survive, then position and street, and when/export/replay
    // drop off as the viewport narrows.
    return `<tr>
      <td class="d-none d-md-table-cell"><span class="hand-when-cell">${fmtHandDateTime(h.ts, tz)}${copyBtn}</span></td>
      <td class="no-wrap">${cards || '—'}</td>
      <td>${posBadge(h.position)}</td>
      <td>${streetBadge(h.last_street)}</td>
      ${cols56}
      <td class="d-none d-xl-table-cell">
        ${h.replay_url && h.replay_url !== '#'
          ? `<a class="replay-link" href="${h.replay_url}" target="_blank" rel="noopener" title="Watch replay">▶</a>`
          : '<span class="text-muted">—</span>'}
      </td>
    </tr>`;
  }).join('');
}

function renderRecentHands(hands)    { renderHandsTable(hands, 'recent-hands-tbody',  { showExport: true }); }
function renderRecentWonHands(hands) { renderHandsTable(hands, 'recent-won-tbody',     { showExport: true }); }

/* ── Table 2: Stats summary (preserved, not called by default) ── */

function statCard(label, value, colorClass) {
  return `<div class="col-6 col-sm-4 col-md-3 col-xl-2">
    <div class="stat-card">
      <div class="stat-value ${colorClass || ''}">${value}</div>
      <div class="stat-label">${label}</div>
    </div>
  </div>`;
}

function renderStats(s) {
  const grid = document.getElementById('stats-grid');
  const net  = s.net_profit   || 0;
  const bb   = s.bb_100       || 0;
  const win  = s.biggest_win  || 0;
  const loss = s.biggest_loss || 0;

  grid.innerHTML = [
    statCard('Hands Played',  s.total_hands || 0),
    statCard('VPIP',          (s.vpip_pct || 0) + '%'),
    statCard('PFR',           (s.pfr_pct  || 0) + '%'),
    statCard('Aggr. Factor',  s.af        || 0),
    statCard('WTSD',          (s.wtsd_pct || 0) + '%'),
    statCard('W$SD',          (s.wsd_pct  || 0) + '%'),
    statCard('BB / 100',      bb.toFixed(2),              bb  < 0 ? 'neg' : ''),
    statCard('Net Profit',    fmtProfitPlain(net),        net < 0 ? 'neg' : ''),
    statCard('Biggest Win',   '+' + win.toLocaleString(), ''),
    statCard('Biggest Loss',  '−' + Math.abs(loss).toLocaleString(), 'neg'),
  ].join('');
}

/* ── Table 3: Tournaments ────────────────────────────────── */

function renderTournaments(tournaments) {
  const tbody = document.getElementById('tournaments-tbody');

  // Populate tourney strip
  const strip = document.getElementById('tourney-strip');
  if (strip) {
    const mttCount = tournaments.filter(t => t.is_mtt).length;
    const satCount = tournaments.filter(t => (t.room_name || '').toLowerCase().includes('sat')).length;
    const wonCount = tournaments.filter(t => (t.net || 0) > 0).length;
    const items = [
      ['Tourneys',  tournaments.length],
      ['MTT',       mttCount],
      ['Satellite', satCount],
      ['Won',       wonCount],
    ];
    strip.innerHTML = items.map(([label, value]) =>
      `<span class="val-pill"><strong>${value}</strong><span class="val-pill-label">${label}</span></span>`
    ).join('<span class="val-sep">·</span>');
  }

  if (!tournaments.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No tournaments detected</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = tournaments.map(t => {
    const typeBadge = t.is_mtt
      ? '<span class="badge bg-primary">MTT</span>'
      : '<span class="badge bg-secondary">Cash</span>';

    return `<tr>
      <td style="white-space:nowrap"><small>${fmtDate(t.earliest_ts, tz)}</small></td>
      <td class="d-none d-sm-table-cell"><small>${t.room_name || '—'}</small></td>
      <td class="d-none d-lg-table-cell"><small class="text-muted">${fmtTime(t.earliest_ts, tz)}</small></td>
      <td class="d-none d-lg-table-cell"><small class="text-muted">${t.time_played || '—'}</small></td>
      <td class="d-none">${typeBadge}</td>
      <td class="text-center">${t.hands}</td>
      <td class="text-center export-col" style="vertical-align:middle">
        ${isPro()
          ? `<div class="d-flex gap-2 flex-wrap justify-content-center">
              <button class="btn export-icon-btn" data-platform="PokerTracker" title="Export for PokerTracker" onclick="exportTournament('${t.tourney_id}', this)">
                <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="22" height="22" alt="PT">
              </button>
              <button class="btn export-icon-btn" data-platform="DriveHUD" title="Export for DriveHUD" onclick="exportTournament('${t.tourney_id}', this)">
                <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="22" height="22" alt="DH">
              </button>
              <button class="btn export-icon-btn" data-platform="GTOWizard" title="Export for GTO Wizard" onclick="exportTournament('${t.tourney_id}', this)">
                <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
              </button>
              <button class="btn export-icon-btn" title="Export as JSON file" onclick="exportTournamentJson('${t.tourney_id}', this)">
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              </button>
            </div>`
          : `<div class="tourney-gate-wrap">
              <div class="tourney-gate-blur" aria-hidden="true">
                <div class="d-flex gap-2 flex-wrap justify-content-center">
                  <button class="btn export-icon-btn" tabindex="-1" disabled>
                    <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="22" height="22" alt="">
                  </button>
                  <button class="btn export-icon-btn" tabindex="-1" disabled>
                    <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="22" height="22" alt="">
                  </button>
                  <button class="btn export-icon-btn" tabindex="-1" disabled>
                    <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
                  </button>
                  <button class="btn export-icon-btn" tabindex="-1" disabled>
                    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                  </button>
                </div>
              </div>
              <div class="tourney-gate-overlay">
                <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--yellow)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                <span class="tourney-gate-label">Pro only</span>
                <button class="tourney-gate-btn" onclick="showUpgradeModal('tourney')">Early Access · A$7.99/mo</button>
              </div>
            </div>`
        }
      </td>
    </tr>`;
  }).join('');

}

/* ── Export gate renderers ───────────────────────────────── */

const _EXPORT_ALL_BTNS_HTML =
  `<div class="export-btn-grid">` +
  `<button class="export-grid-btn" data-platform="PokerTracker" title="Export for PokerTracker" onclick="exportAllHands(this)">` +
  `<img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="28" height="28" alt="PT"><span>Poker Tracker</span></button>` +
  `<button class="export-grid-btn" data-platform="DriveHUD" title="Export for DriveHUD" onclick="exportAllHands(this)">` +
  `<img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="28" height="28" alt="DH"><span>DriveHUD</span></button>` +
  `<button class="export-grid-btn" data-platform="GTOWizard" title="Export for GTO Wizard" onclick="exportAllHands(this)">` +
  `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>` +
  `<span>GTO Wizard</span></button>` +
  `<button class="export-grid-btn" title="Export as JSON" onclick="exportAllHandsJson(this)">` +
  `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>` +
  `<span>JSON File</span></button>` +
  `</div>`;

const _LOCK_ICON_SVG =
  `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" ` +
  `stroke="var(--yellow)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">` +
  `<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;

/** Renders the Export All Hands container — real buttons for Pro, blurred gate for free. */
function _renderExportAllSection() {
  const el = document.getElementById('export-all-container');
  if (!el) return;
  if (isPro()) {
    el.innerHTML = _EXPORT_ALL_BTNS_HTML;
  } else {
    el.innerHTML =
      `<div class="export-gate-wrap">` +
      `<div class="tourney-gate-blur" aria-hidden="true">${_EXPORT_ALL_BTNS_HTML}</div>` +
      `<div class="tourney-gate-overlay">` +
      _LOCK_ICON_SVG +
      `<span class="tourney-gate-label">Export All Hands — Pro only</span>` +
      `<button class="tourney-gate-btn" onclick="showUpgradeModal('export')">Early Access · A$7.99/mo</button>` +
      `</div></div>`;
  }
}

/** Updates the per-hand export counter and enables/disables the hand export buttons. */
function _renderExportCounter() {
  const el = document.getElementById('export-hand-counter');
  if (!el) return;
  if (isPro()) {
    el.classList.add('d-none');
    document.querySelectorAll('#export-hand-grid .export-grid-btn').forEach(b => { b.disabled = false; });
    return;
  }
  const today = _todayStr();
  const used  = (_userState.last_export_date === today) ? (_userState.exports_today || 0) : 0;
  const atLimit = used >= FREE_EXPORT_LIMIT;
  el.classList.remove('d-none');
  if (atLimit) {
    el.innerHTML =
      `<span class="export-counter-limit">Daily limit reached (${used}/${FREE_EXPORT_LIMIT}) — ` +
      `<button class="btn-link-inline" onclick="showUpgradeModal('export')">upgrade to Pro</button>` +
      ` for unlimited exports</span>`;
    document.querySelectorAll('#export-hand-grid .export-grid-btn').forEach(b => { b.disabled = true; });
  } else {
    el.innerHTML = `<strong>${used}</strong> of <strong>${FREE_EXPORT_LIMIT}</strong> exports used today`;
    document.querySelectorAll('#export-hand-grid .export-grid-btn').forEach(b => { b.disabled = false; });
  }
}

/** Show/hide tier-gated elements based on current Pro status. */
function _applyTierVisibility() {
  const pro = isPro();
  FREE_ONLY_ELS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = pro ? 'none' : '';
  });
  PRO_ONLY_ELS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = pro ? '' : 'none';
  });
}

/** Call after any state change that could affect export UI. */
function _updateExportGates() {
  _applyTierVisibility();
  _renderPlayerExportAll();
}

/* ── Tournament Summary ──────────────────────────────────── */

// tourney_ids touched by the most recent import — used to briefly highlight
// the corresponding rows in Tournament Summary / History after _loadHistory() renders.
let _justImportedTourneyIds = new Set();
let _importHighlightTimer = null;
const IMPORT_HIGHLIGHT_MS = 60000;

function _startImportHighlights(tourneyIds) {
  _justImportedTourneyIds = new Set(tourneyIds || []);
  if (_importHighlightTimer) clearTimeout(_importHighlightTimer);
  _importHighlightTimer = setTimeout(() => {
    _justImportedTourneyIds = new Set();
    _importHighlightTimer = null;
    document.querySelectorAll('.row-flash, .card-flash').forEach(el => {
      el.classList.remove('row-flash', 'card-flash');
    });
  }, IMPORT_HIGHLIGHT_MS);
}

function _dismissImportHighlight(el) {
  if (!el) return;
  el.classList.remove('row-flash', 'card-flash');
}

// ── TS / CGS filter + sort state ─────────────────────────────────────────────
let _allTournaments     = [];          // cached from _loadHistory
let _tsFilter           = 'week';      // 'all' | 'today' | 'week' | 'month'
let _tsSortCol          = 'last';      // null = default order
let _tsSortDir          = 'asc';

let _cgsFilter          = 'all';
let _cgsSortCol         = null;
let _cgsSortDir         = 'desc';

// ── Date-filter helper ────────────────────────────────────────────────────────
function _filterTournamentsByDate(tournaments, filter) {
  if (filter === 'all') return tournaments;
  const tz  = currentTz();
  const now = new Date();
  const todayStr = now.toLocaleDateString('en-CA', { timeZone: tz });

  if (filter === 'today') {
    return tournaments.filter(t => {
      if (!t.earliest_ts) return false;
      return new Date(t.earliest_ts * 1000).toLocaleDateString('en-CA', { timeZone: tz }) === todayStr;
    });
  }
  if (filter === 'week') {
    const cutoff = (now.getTime() - 7 * 86400000) / 1000;
    return tournaments.filter(t => (t.earliest_ts || 0) >= cutoff);
  }
  if (filter === 'month') {
    const cutoff = (now.getTime() - 30 * 86400000) / 1000;
    return tournaments.filter(t => (t.earliest_ts || 0) >= cutoff);
  }
  return tournaments;
}

// ── TS filter / sort ──────────────────────────────────────────────────────────
function _setTsFilter(f) {
  _tsFilter = f;
  const sel = document.getElementById('ts-filter-select');
  if (sel) sel.value = f;
  _renderTournamentSummary(_allTournaments);
}

function _sortTs(col) {
  if (_tsSortCol === col) {
    _tsSortDir = _tsSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _tsSortCol = col;
    _tsSortDir = 'desc';
  }
  _renderTournamentSummary(_allTournaments);
}

function _updateTsSortIcons(col, dir) {
  document.querySelectorAll('#tournament-summary-section .sort-th').forEach(th => {
    const icon = th.querySelector('.sort-icon');
    if (!icon) return;
    icon.textContent = th.dataset.col === col ? (dir === 'asc' ? ' ▲' : ' ▼') : '';
  });
}

// ── CGS filter / sort ─────────────────────────────────────────────────────────
function _setCgsFilter(f) {
  _cgsFilter = f;
  const sel = document.getElementById('cgs-filter-select');
  if (sel) sel.value = f;
  _renderCashGamesSummary(_allTournaments);
}

function _sortCgs(col) {
  if (_cgsSortCol === col) {
    _cgsSortDir = _cgsSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _cgsSortCol = col;
    _cgsSortDir = 'desc';
  }
  _renderCashGamesSummary(_allTournaments);
}

function _updateCgsSortIcons(col, dir) {
  document.querySelectorAll('#cash-games-summary-section .sort-th').forEach(th => {
    const icon = th.querySelector('.sort-icon');
    if (!icon) return;
    icon.textContent = th.dataset.col === col ? (dir === 'asc' ? ' ▲' : ' ▼') : '';
  });
}


// ── Group sort helper ─────────────────────────────────────────────────────────
function _sortGroups(rows, col, dir) {
  if (!col) return rows;
  const sign = dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    let av, bv;
    switch (col) {
      case 'name':     av = a[0].toLowerCase(); bv = b[0].toLowerCase(); break;
      case 'events':   av = a[1].length; bv = b[1].length; break;
      case 'type':     av = a[1].some(t => t.is_mtt) ? 1 : 0; bv = b[1].some(t => t.is_mtt) ? 1 : 0; break;
      case 'avgHands': av = a[1].reduce((s, t) => s + (t.hands || 0), 0) / a[1].length;
                       bv = b[1].reduce((s, t) => s + (t.hands || 0), 0) / b[1].length; break;
      case 'avgDur':   av = a[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / a[1].length;
                       bv = b[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / b[1].length; break;
      case 'handsHr': {
        const aDurHr = a[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
        const bDurHr = b[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
        av = aDurHr > 0 ? a[1].reduce((s, t) => s + (t.hands || 0), 0) / aDurHr : 0;
        bv = bDurHr > 0 ? b[1].reduce((s, t) => s + (t.hands || 0), 0) / bDurHr : 0;
        break;
      }
      case 'last':     av = Math.max(...a[1].map(t => t.earliest_ts || 0));
                       bv = Math.max(...b[1].map(t => t.earliest_ts || 0)); break;
      default: return 0;
    }
    if (av < bv) return -sign;
    if (av > bv) return  sign;
    return 0;
  });
}

// ── Cash Games Summary ────────────────────────────────────────────────────────
function _renderCashGamesSummary(tournaments) {
  const cashOnly = (tournaments || []).filter(t => !t.is_mtt);
  const filtered = _filterTournamentsByDate(cashOnly, _cgsFilter);

  const cgsSection = document.getElementById('cash-games-summary-section');
  if (cgsSection) cgsSection.classList.remove('d-none');
  const cgsdSection = document.getElementById('cash-game-detail-section');
  // Hide session detail when filter produces no results (no sessions to click)
  if (cgsdSection) cgsdSection.classList.toggle('d-none', !filtered.length);

  // Stats strip
  const strip = document.getElementById('cgs-strip');
  if (strip) {
    const items = [['Sessions', filtered.length], ['Total Hands', filtered.reduce((s, t) => s + (t.hands || 0), 0)]];
    strip.innerHTML = items.map(([label, value]) =>
      `<span class="val-pill"><strong>${value}</strong><span class="val-pill-label">${label}</span></span>`
    ).join('<span class="val-sep">·</span>');
  }

  const tbody = document.getElementById('cgs-summary-tbody');
  if (!tbody) return;

  const byRoom = {};
  for (const t of filtered) {
    const key = t.room_name || '(Unknown)';
    if (!byRoom[key]) byRoom[key] = [];
    byRoom[key].push(t);
  }

  let rows = Object.entries(byRoom).sort((a, b) => b[1].length - a[1].length);
  rows = _sortGroups(rows, _cgsSortCol, _cgsSortDir);
  _updateCgsSortIcons(_cgsSortCol, _cgsSortDir);

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No cash game data in selected range.</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = rows.map(([name, entries], idx) => {
    const rowId       = `cgs-${idx}`;
    const count       = entries.length;
    const totalHands  = entries.reduce((s, t) => s + (t.hands || 0), 0);
    const totalDurHr  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
    const handsPerHr  = totalDurHr > 0 ? (totalHands / totalDurHr) : 0;
    const avgHands    = Math.round(totalHands / count);
    const avgDurSecs  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / count;
    const avgVpip     = (entries.reduce((s, t) => s + (t.vpip_pct || 0), 0) / count).toFixed(1);
    const avgPfr      = (entries.reduce((s, t) => s + (t.pfr_pct  || 0), 0) / count).toFixed(1);
    const lastTs      = Math.max(...entries.map(t => t.earliest_ts || 0));
    const lastDate    = lastTs ? new Date(lastTs * 1000).toLocaleDateString('en-GB',
        { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';

    const sortedEntries = [...entries].sort((a, b) => (b.earliest_ts || 0) - (a.earliest_ts || 0));
    const isRowNew = entries.some(t => _justImportedTourneyIds.has(t.tourney_id));
    const eventCards = sortedEntries.map(t => {
      const d = t.earliest_ts ? new Date(t.earliest_ts * 1000).toLocaleDateString('en-GB',
          { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';
      const evDurHr = (t.duration_secs || 0) / 3600;
      const evPerHr = evDurHr > 0 ? (t.hands || 0) / evDurHr : 0;
      const isCardNew = _justImportedTourneyIds.has(t.tourney_id);
      return `<div class="tsum-event-card${isCardNew ? ' card-flash' : ''}" data-tid="${t.tourney_id}" onclick="event.stopPropagation();_dismissImportHighlight(this);_selectCgsdDetail('${t.tourney_id}', this)">
        <div class="tsum-event-top">
          <span class="tsum-event-date">${d}</span>
          <span class="tsum-stat-pill">${fmtTime(t.earliest_ts, tz)}</span>
          <span class="tsum-stat-pill">${_fmtDuration(t.duration_secs)}</span>
        </div>
        <div class="tsum-event-stats">${_tsumStatPills(t.hands || 0, t.vpip_pct || 0, t.pfr_pct || 0, evPerHr)}</div>
        <div class="tsum-event-actions">${_TSUM_EXPORT_ICONS(t.tourney_id)}</div>
      </div>`;
    }).join('');

    return `<tr class="tsum-summary-row${isRowNew ? ' row-flash' : ''}" onclick="_dismissImportHighlight(this);_toggleCgsDetail('${rowId}')">
      <td>
        <svg class="tsum-chevron" id="${rowId}-chevron" xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        <small>${name}</small>
      </td>
      <td class="text-center">${count}</td>
      <td class="text-center d-none d-md-table-cell">${Math.max(...entries.map(t => t.max_players || 0)) || '—'}</td>
      <td class="text-center d-none d-md-table-cell">${avgHands}</td>
      <td class="text-center d-none d-md-table-cell">${_fmtDuration(avgDurSecs)}</td>
      <td class="text-center d-none d-lg-table-cell">${handsPerHr.toFixed(1)}</td>
      <td class="text-center"><small>${lastDate}</small></td>
    </tr>
    <tr class="tsum-detail-row">
      <td colspan="7">
        <div class="tsum-detail-wrap" id="${rowId}-detail">
          <div class="tsum-event-grid">${eventCards}</div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function _toggleCgsDetail(rowId) {
  _toggleTsumDetail('cgs-summary-tbody', rowId);
}

// ── Cash Game Session Details ─────────────────────────────────────────────────
let _selectedCgsId = null;

function _resetCgsd() {
  const tbody = document.getElementById('cgsd-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Select a cash game session above to view its hands.</td></tr>';
  const hint = document.getElementById('cgsd-hint');
  if (hint) hint.textContent = '';
  document.querySelectorAll('.tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  _selectedCgsId = null;
}

async function _selectCgsdDetail(tid, cardEl) {
  if (cardEl && cardEl.classList.contains('selected')) {
    _resetCgsd();
    return;
  }
  document.querySelectorAll('#cash-games-summary-section .tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  if (cardEl) cardEl.classList.add('selected');
  _selectedCgsId = tid;

  const tbody   = document.getElementById('cgsd-tbody');
  const hint    = document.getElementById('cgsd-hint');
  const section = document.getElementById('cash-game-detail-section');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Loading hands…</td></tr>';
  if (section) section.classList.remove('d-none');

  if (!_currentUser) return;
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) { _resetCgsd(); return; }

  try {
    const r = await fetch(`/api/tournaments/${tid}/hands`, { headers: { 'Authorization': `Bearer ${token}` } });
    if (_selectedCgsId !== tid) return;
    if (!r.ok) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
      return;
    }
    const data  = await r.json();
    const hands = data.hands || [];
    renderHandsTable(hands, 'cgsd-tbody', { showExport: true, exportTid: tid });
    if (hint) {
      const dateLabel = cardEl ? (cardEl.querySelector('.tsum-event-date')?.textContent || '') : '';
      hint.textContent = `${hands.length} hand${hands.length !== 1 ? 's' : ''}${dateLabel ? ' · ' + dateLabel : ''}`;
    }
    if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
  }
}

// ── Per-row hand export (TD + CGSD) ──────────────────────────────────────────
async function exportHandFromRow(handNum, platform, btn, tid) {
  if (!_currentUser) { showUpgradeModal('tourney'); return; }
  _rowExportStatus(btn, 'loading', 'Exporting…');
  const token = await _currentUser.getIdToken().catch(() => null);
  const isJson = !platform;
  // If a tournament ID is provided, use storage-backed endpoints (work without a live import session)
  const endpoint = tid
    ? (isJson ? `/api/tournaments/${tid}/export/json/hand` : `/api/tournaments/${tid}/export/hand`)
    : (isJson ? '/api/export/json/hand' : '/api/export/hand');
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  try {
    const r = await fetch(endpoint, {
      method:  'POST',
      headers,
      body: JSON.stringify({ hand_id: handNum, platform, session_id: getSessionId() }),
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || 'Export failed'); }
    const cd = r.headers.get('Content-Disposition') || '';
    const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
    const filename = m ? m[1].replace(/['"]/g, '').trim() : `hand_export.${isJson ? 'json' : 'txt'}`;
    const blob = await r.blob();
    _triggerDownload(blob, filename);
    _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
  } catch (err) {
    _rowExportStatus(btn, 'err', err.message, 6000);
  }
}

// ── Player badge: Export All (moves here from removed section) ────────────────
function _renderPlayerExportAll() {
  const wrap = document.getElementById('player-export-all');
  const btns = document.getElementById('player-export-all-btns');
  if (!wrap || !btns) return;
  if (!window._lastData) { wrap.classList.add('d-none'); return; }
  if (isPro()) {
    btns.innerHTML = _EXPORT_ALL_BTNS_HTML;
    wrap.classList.remove('d-none');
  } else {
    btns.innerHTML =
      `<div class="export-gate-wrap" style="min-height:auto">` +
      `<div class="tourney-gate-blur" aria-hidden="true">${_EXPORT_ALL_BTNS_HTML}</div>` +
      `<div class="tourney-gate-overlay">` +
      _LOCK_ICON_SVG +
      `<span class="tourney-gate-label">Pro only</span>` +
      `<button class="tourney-gate-btn" onclick="showUpgradeModal('export')">Early Access · A$7.99/mo</button>` +
      `</div></div>`;
    wrap.classList.remove('d-none');
  }
}

function _fmtDuration(secs) {
  if (!secs || secs < 0) return '—';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

async function _loadHistory() {
  if (!isPro() || !_currentUser) return;
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) return;
  const tsSection  = document.getElementById('tournament-summary-section');
  const tdSection  = document.getElementById('tournament-history-pro-section');
  const cgsSection = document.getElementById('cash-games-summary-section');
  const cgsdSection= document.getElementById('cash-game-detail-section');
  try {
    const r = await fetch('/api/tournaments', { headers: { 'Authorization': `Bearer ${token}` } });
    if (!r.ok) return;
    const data = await r.json();
    const tournaments = data.tournaments || [];
    _allTournaments = tournaments;

    if (!tournaments.length) {
      [tsSection, tdSection, cgsSection, cgsdSection].forEach(el => el && el.classList.add('d-none'));
      return;
    }

    const hasMtt  = tournaments.some(t =>  t.is_mtt);
    const hasCash = tournaments.some(t => !t.is_mtt);

    if (hasMtt) {
      _renderTournamentSummary(tournaments);
      _resetTournamentDetails();
      if (tsSection) tsSection.classList.remove('d-none');
      if (tdSection) tdSection.classList.remove('d-none');
    } else {
      if (tsSection) tsSection.classList.add('d-none');
      if (tdSection) tdSection.classList.add('d-none');
    }

    if (hasCash) {
      _renderCashGamesSummary(tournaments);
      _resetCgsd();
    } else {
      if (cgsSection) cgsSection.classList.add('d-none');
      if (cgsdSection) cgsdSection.classList.add('d-none');
    }
  } catch (e) { console.warn('History load failed:', e); }
}

const _TSUM_ICON_HANDS   = `<svg class="tsum-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="11" height="15" rx="1.5" transform="rotate(-12 8.5 13.5)"/><rect x="10" y="4" width="11" height="15" rx="1.5"/></svg>`;
const _TSUM_ICON_PERCENT = `<svg class="tsum-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><line x1="19" y1="5" x2="5" y2="19"/></svg>`;
const _TSUM_ICON_CLOCK    = `<svg class="tsum-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>`;

function _tsumStatPills(hands, vpip, pfr, perHr) {
  const vpipR = Math.round(vpip);
  const pfrR  = Math.round(pfr);
  return `
    <span class="tsum-stat-item" title="Total hands">
      ${_TSUM_ICON_HANDS}
      <span class="tsum-stat-pill tsum-stat-stacked">
        <span class="tsum-stat-full">${hands} hands</span>
        <span class="tsum-stat-short">${hands}</span>
      </span>
    </span>
    <span class="tsum-stat-item" title="VPIP / PFR">
      ${_TSUM_ICON_PERCENT}
      <span class="tsum-stat-pill tsum-stat-stacked">
        <span class="tsum-stat-full">${vpip.toFixed(1)}% / ${pfr.toFixed(1)}%</span>
        <span class="tsum-stat-short">${vpipR}/${pfrR}</span>
      </span>
    </span>
    <span class="tsum-stat-item" title="Hands per hour">
      ${_TSUM_ICON_CLOCK}
      <span class="tsum-stat-pill tsum-stat-stacked">
        <span class="tsum-stat-full">${perHr.toFixed(1)}/hr</span>
        <span class="tsum-stat-short">${perHr.toFixed(1)}</span>
      </span>
    </span>`;
}

const _TSUM_EXPORT_ICONS = (tourneyId) => `
  <button class="btn export-icon-btn" data-platform="PokerTracker" title="Export for PokerTracker" onclick="event.stopPropagation();exportPersistedTournament('${tourneyId}', this)">
    <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="16" height="16" alt="PT">
  </button>
  <button class="btn export-icon-btn" data-platform="DriveHUD" title="Export for DriveHUD" onclick="event.stopPropagation();exportPersistedTournament('${tourneyId}', this)">
    <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="16" height="16" alt="DH">
  </button>
  <button class="btn export-icon-btn" data-platform="GTOWizard" title="Export for GTO Wizard" onclick="event.stopPropagation();exportPersistedTournament('${tourneyId}', this)">
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
  </button>
  <button class="btn export-icon-btn" title="Export as JSON file" onclick="event.stopPropagation();exportPersistedTournamentJson('${tourneyId}', this)">
    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
  </button>`;

function _renderTournamentSummary(tournaments) {
  const tbody = document.getElementById('tourney-summary-tbody');
  if (!tbody) return;

  // Exclude Cash games, then apply date filter
  const mttOnly = (tournaments || []).filter(t => t.is_mtt);
  const filtered = _filterTournamentsByDate(mttOnly, _tsFilter);

  // Pills strip
  const strip = document.getElementById('tourney-strip-pro');
  if (strip) {
    const satCount = filtered.filter(t => (t.room_name || '').toLowerCase().includes('sat')).length;
    const pkoCount = filtered.filter(t => { const r = (t.room_name || '').toLowerCase(); return (r.includes('pko') || r.includes('mko')) && !r.includes('sat'); }).length;
    const items = [['Tourneys', filtered.length], ['Satellite', satCount], ['PKO', pkoCount]];
    strip.innerHTML = items.map(([label, value]) =>
      `<span class="val-pill"><strong>${value}</strong><span class="val-pill-label">${label}</span></span>`
    ).join('<span class="val-sep">·</span>');
  }

  const byRoom = {};
  for (const t of filtered) {
    const key = t.room_name || '(Unknown)';
    if (!byRoom[key]) byRoom[key] = [];
    byRoom[key].push(t);
  }

  let rows = Object.entries(byRoom).sort((a, b) => b[1].length - a[1].length);
  rows = _sortGroups(rows, _tsSortCol, _tsSortDir);
  _updateTsSortIcons(_tsSortCol, _tsSortDir);

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-4 text-muted">No tournament data in selected range.</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = rows.map(([name, entries], idx) => {
    const rowId       = `tsum-${idx}`;
    const count       = entries.length;
    const isMtt       = entries.some(t => t.is_mtt);
    const totalHands  = entries.reduce((s, t) => s + (t.hands || 0), 0);
    const totalDurHr  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
    const handsPerHr  = totalDurHr > 0 ? (totalHands / totalDurHr) : 0;
    const avgHands    = Math.round(totalHands / count);
    const avgDurSecs  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / count;
    const avgVpip     = (entries.reduce((s, t) => s + (t.vpip_pct || 0), 0) / count).toFixed(1);
    const avgPfr      = (entries.reduce((s, t) => s + (t.pfr_pct  || 0), 0) / count).toFixed(1);
    const lastTs      = Math.max(...entries.map(t => t.earliest_ts || 0));
    const lastDate    = lastTs ? new Date(lastTs * 1000).toLocaleDateString('en-GB',
        { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';

    const sortedEntries = [...entries].sort((a, b) => (a.earliest_ts || 0) - (b.earliest_ts || 0));
    const isRowNew = entries.some(t => _justImportedTourneyIds.has(t.tourney_id));
    const eventCards = sortedEntries.map(t => {
      const d = t.earliest_ts ? new Date(t.earliest_ts * 1000).toLocaleDateString('en-GB',
          { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';
      const evDurHr = (t.duration_secs || 0) / 3600;
      const evPerHr = evDurHr > 0 ? (t.hands || 0) / evDurHr : 0;
      const isCardNew = _justImportedTourneyIds.has(t.tourney_id);
      return `<div class="tsum-event-card${isCardNew ? ' card-flash' : ''}" data-tid="${t.tourney_id}" onclick="event.stopPropagation();_dismissImportHighlight(this);_selectTourneyDetail('${t.tourney_id}', this)">
        <div class="tsum-event-top">
          <span class="tsum-event-date">${d}</span>
          <span class="tsum-stat-pill" title="Sit down time">${fmtTime(t.earliest_ts, tz)}</span>
          <span class="tsum-stat-pill" title="Time played">${_fmtDuration(t.duration_secs)}</span>
        </div>
        <div class="tsum-event-stats">${_tsumStatPills(t.hands || 0, t.vpip_pct || 0, t.pfr_pct || 0, evPerHr)}</div>
        <div class="tsum-event-actions">${_TSUM_EXPORT_ICONS(t.tourney_id)}</div>
      </div>`;
    }).join('');

    return `<tr class="tsum-summary-row${isRowNew ? ' row-flash' : ''}" onclick="_dismissImportHighlight(this);_toggleTourneyDetail('${rowId}')">
      <td>
        <svg class="tsum-chevron" id="${rowId}-chevron" xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        <small>${name}</small>
      </td>
      <td class="text-center">${count}</td>
      <td class="text-center d-none">${isMtt ? '<span class="badge bg-primary">MTT</span>' : '<span class="badge bg-secondary">MTT</span>'}</td>
      <td class="text-center d-none d-md-table-cell">${Math.max(...entries.map(t => t.max_players || 0)) || '—'}</td>
      <td class="text-center d-none d-md-table-cell">${avgHands}</td>
      <td class="text-center d-none d-md-table-cell">${_fmtDuration(avgDurSecs)}</td>
      <td class="text-center d-none d-lg-table-cell">${handsPerHr.toFixed(1)}</td>
      <td class="text-center"><small>${lastDate}</small></td>
    </tr>
    <tr class="tsum-detail-row">
      <td colspan="8">
        <div class="tsum-detail-wrap" id="${rowId}-detail">
          <div class="tsum-event-grid">${eventCards}</div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function _toggleTourneyDetail(rowId) {
  _toggleTsumDetail('tourney-summary-tbody', rowId);
}

// Accordion toggle shared by the Tournament Summary and Cash Game Summary
// tables: opening a row's detail closes any other open row in the same tbody.
function _toggleTsumDetail(tbodyId, rowId) {
  const tbody   = document.getElementById(tbodyId);
  const detail  = document.getElementById(`${rowId}-detail`);
  const chevron = document.getElementById(`${rowId}-chevron`);
  if (!detail) return;
  const opening = !detail.classList.contains('tsum-detail-open');
  if (tbody && opening) {
    tbody.querySelectorAll('.tsum-detail-wrap.tsum-detail-open').forEach(el => {
      if (el !== detail) el.classList.remove('tsum-detail-open');
    });
    tbody.querySelectorAll('.tsum-chevron-open').forEach(el => {
      if (el !== chevron) el.classList.remove('tsum-chevron-open');
    });
  }
  detail.classList.toggle('tsum-detail-open');
  if (chevron) chevron.classList.toggle('tsum-chevron-open');
}

/* ── Tournament Progress Graph ──────────────────────────────────────────── */

let _tgChart      = null;
let _tgYMode      = 'both';   // 'both' | 'chips' | 'bb'
let _tgXMode      = 'time';   // 'time' | 'level'
let _tgPlayedOnly = false;    // when true, crop to played hands only
let _tgState      = null;     // computed chart state shared across toggle updates

// Tournament configs: ITM bubble / expected-end hours from tournament start
const _TG_CFGS = {
  'DEEP FREEZE':  { itmH: 4.0, endH: 5.5, lateRegLevels: 14, levelDurRebuyMin: null, levelDurMin: 12 },
  'CRAZY 2':      { itmH: 3.0, endH: 4.0, lateRegLevels: 12, levelDurRebuyMin: 12,   levelDurMin: 10 },
  'LUCKY DAY':    { itmH: 3.0, endH: 4.0, lateRegLevels: 11, levelDurRebuyMin: 10,   levelDurMin:  8 },
  'EAST PKO SAT': { itmH: 3.0, endH: 4.0, lateRegLevels: 10, levelDurRebuyMin:  6,   levelDurMin:  5 },
  'TEXAS':        { itmH: 3.0, endH: 4.0, lateRegLevels: 11, levelDurRebuyMin: 10,   levelDurMin: 10 },
  'MINI':         { itmH: 4.0, endH: 5.5, lateRegLevels: 12, levelDurRebuyMin:  6,   levelDurMin:  6 },
};
const _TG_CFG_DEFAULT = { itmH: 4.0, endH: 5.5, lateRegLevels: 12, levelDurRebuyMin: 12, levelDurMin: 10 };

// BB → Level lookup (base levels 1-51 + Crazy 2 / Deep Freeze extra 52-68)
const _TG_BB_LVL = (() => {
  const pairs = [
    [1,50],[2,100],[3,200],[4,300],[5,400],[6,500],[7,600],[8,800],
    [9,1000],[10,1200],[11,1600],[12,2000],[13,2400],[14,3000],[15,4000],
    [16,5000],[17,6000],[18,8000],[19,10000],[20,12000],[21,16000],[22,20000],
    [23,24000],[24,30000],[25,40000],[26,50000],[27,60000],[28,80000],
    [29,100000],[30,120000],[31,160000],[32,200000],[33,240000],[34,300000],
    [35,400000],[36,500000],[37,600000],[38,800000],[39,1000000],[40,1200000],
    [41,1600000],[42,2000000],[43,3000000],[44,4000000],[45,5000000],
    [46,6000000],[47,8000000],[48,10000000],[49,12000000],[50,16000000],
    [51,20000000],
    [52,30000000],[53,40000000],[54,60000000],[55,80000000],[56,100000000],
    [57,120000000],[58,160000000],[59,200000000],[60,300000000],[61,400000000],
    [62,600000000],[63,800000000],[64,1000000000],[65,1200000000],
    [66,1600000000],[67,2000000000],[68,3000000000],
  ];
  const m = {};
  for (const [lvl, bb] of pairs) m[bb] = lvl;
  return m;
})();

// Lucky Day BB → Level (60-level override structure)
const _TG_LUCKY_BB_LVL = (() => {
  const bbs = [
    100,200,300,400,600,800,1000,1200,1600,2000,2400,3000,4000,5000,
    6000,8000,10000,12000,16000,20000,24000,30000,40000,50000,60000,
    80000,100000,120000,160000,200000,240000,300000,400000,500000,
    600000,800000,1000000,1200000,1600000,2000000,3000000,4000000,
    5000000,6000000,8000000,10000000,12000000,16000000,20000000,
    24000000,30000000,40000000,50000000,60000000,80000000,100000000,
    120000000,160000000,200000000,240000000,
  ];
  const m = {};
  bbs.forEach((bb, i) => { m[bb] = i + 1; });
  return m;
})();

function _tgGetCfg(roomName) {
  const up = (roomName || '').toUpperCase();
  for (const [key, cfg] of Object.entries(_TG_CFGS)) {
    if (up.includes(key)) return cfg;
  }
  return _TG_CFG_DEFAULT;
}

function _tgInferLevel(bigBlind, roomName, chipStack) {
  if (!bigBlind) return null;
  const isLucky = (roomName || '').toUpperCase().includes('LUCKY DAY');
  const map = isLucky ? _TG_LUCKY_BB_LVL : _TG_BB_LVL;
  const direct = map[bigBlind] || null;
  const scaled = map[Math.round(bigBlind / 100)] || null;
  // Both candidates can exist (the table contains values 100x apart); disambiguate
  // using the BB count implied by the hero's stack — a real tournament BB count
  // is almost always in the 1-300 range, so whichever candidate lands there wins.
  if (direct && scaled && chipStack) {
    const bbDirect = chipStack / bigBlind;
    const bbScaled = chipStack / (bigBlind / 100);
    const plausible = v => v >= 1 && v <= 300;
    if (plausible(bbDirect) && !plausible(bbScaled)) return direct;
    if (plausible(bbScaled) && !plausible(bbDirect)) return scaled;
  }
  return direct || scaled || null;
}

function _tgFmtElapsed(secs) {
  if (secs < 0) return '-' + _tgFmtElapsed(-secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}

function _tgFmtK(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e9)  return (n / 1e9).toFixed(2).replace(/\.?0+$/, '') + 'B';
  if (a >= 1e6)  return (n / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (a >= 1000) return (n / 1000).toFixed(1).replace(/\.?0+$/, '') + 'K';
  return n.toLocaleString();
}

// Custom Chart.js plugin — draws dashed vertical reference lines; labels show on hover
const _TG_REFLINES_PLUGIN = {
  id: 'tgRefLines',
  // Track cursor position so labels only render for the line being hovered.
  afterEvent(chart, args) {
    const e = args.event;
    const prev = chart._tgHoverX;
    if (e.type === 'mousemove')      chart._tgHoverX = e.x;
    else if (e.type === 'mouseout')  chart._tgHoverX = null;
    if (chart._tgHoverX !== prev) args.changed = true;
  },
  afterDraw(chart) {
    const lines = chart.config.options.tgRefLines;
    if (!lines || !lines.length) return;
    const { ctx, chartArea: ca, scales: { x } } = chart;
    if (!x) return;
    const hoverX = chart._tgHoverX;
    ctx.save();
    ctx.font = "10px 'Exo 2', sans-serif";
    for (const { secs, color, label } of lines) {
      const px = x.getPixelForValue(secs);
      if (px < ca.left || px > ca.right) continue;
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.moveTo(px, ca.top);
      ctx.lineTo(px, ca.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      // Only draw the label box when the cursor is near this line.
      if (hoverX == null || Math.abs(hoverX - px) > 8) continue;
      const parts = label.split('\n');
      const tw = Math.max(...parts.map(l => ctx.measureText(l).width));
      const bh = parts.length * 14 + 6;
      const bw = tw + 10;
      const bx = Math.min(px + 4, ca.right - bw - 2);
      const by = ca.top + 5;
      ctx.fillStyle = 'rgba(13,17,23,0.88)';
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 3); ctx.fill(); }
      else { ctx.fillRect(bx, by, bw, bh); }
      ctx.fillStyle = color;
      parts.forEach((l, i) => ctx.fillText(l, bx + 5, by + 13 + i * 14));
    }
    ctx.restore();
  },
};

// Custom Chart.js plugin — draws ring + icon markers for biggest win/loss/bust
const _TG_MARKERS_PLUGIN = {
  id: 'tgMarkers',
  afterDatasetsDraw(chart) {
    const markers = chart.config.options.tgMarkers;
    if (!markers || !markers.length) return;
    const { ctx, scales: { x, yLeft, yRight } } = chart;
    ctx.save();
    for (const { secs, chipY, bbY, color, icon } of markers) {
      const px = x.getPixelForValue(secs);
      let py;
      if (yLeft && yLeft.display && chipY != null) py = yLeft.getPixelForValue(chipY);
      else if (yRight && yRight.display && bbY != null) py = yRight.getPixelForValue(bbY);
      if (py == null) continue;
      ctx.fillStyle   = color + '22';
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2;
      ctx.beginPath();
      ctx.arc(px, py, 9, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle     = color;
      ctx.font          = 'bold 9px sans-serif';
      ctx.textAlign     = 'center';
      ctx.textBaseline  = 'middle';
      ctx.fillText(icon, px, py);
    }
    ctx.textAlign    = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.restore();
  },
};

function _tgLevelFromElapsed(elapsedSecs, cfg) {
  const rebuyDur = (cfg.levelDurRebuyMin || cfg.levelDurMin) * 60;
  const mainDur  = cfg.levelDurMin * 60;
  const rebuyEnd = cfg.lateRegLevels * rebuyDur;
  let lvl;
  if (elapsedSecs <= 0) lvl = 1;
  else if (elapsedSecs <= rebuyEnd) lvl = Math.ceil(elapsedSecs / rebuyDur);
  else lvl = cfg.lateRegLevels + Math.ceil((elapsedSecs - rebuyEnd) / mainDur);
  return cfg.maxBlinds ? Math.min(lvl, cfg.maxBlinds) : lvl;
}

// Inverse of _tgLevelFromElapsed: seconds from tournament start to the START of
// a given level. Used to anchor a hero who late-registers at (say) level 6 at
// the real tournament time that level began, instead of at t=0 — so the data
// lines up with the fixed Late Reg / ITM / End reference lines.
function _tgLevelStartSecs(level, cfg) {
  if (!level || level <= 1) return 0;
  const rebuyDur = (cfg.levelDurRebuyMin || cfg.levelDurMin) * 60;
  const mainDur  = cfg.levelDurMin * 60;
  const lr       = cfg.lateRegLevels;
  const completed = level - 1;            // levels fully elapsed before this one
  if (completed <= lr) return completed * rebuyDur;
  return lr * rebuyDur + (completed - lr) * mainDur;
}

function _renderTournamentChart(hands, meta) {
  const wrap = document.getElementById('tourney-graph-wrap');
  if (!wrap) return;
  if (_tgChart) { _tgChart.destroy(); _tgChart = null; }
  _tgState = null;
  _tgSetGraphWarning('');

  if (!hands || !hands.length) { wrap.classList.add('d-none'); return; }

  const sorted = [...hands].filter(h => h.ts).sort((a, b) => a.ts - b.ts);
  if (!sorted.length) { wrap.classList.add('d-none'); return; }

  if (meta && meta.graph_ready === false) {
    wrap.classList.add('d-none');
    _tgSetGraphWarning(meta.graph_warning || 'Graph cannot be displayed because this tournament configuration is incomplete.');
    return;
  }

  const roomName       = (meta && meta.room_name) || '';
  const isFinishBusted = !!(meta && meta.finish_busted);
  // Prefer DB-resolved config from the backend (meta); the hardcoded table is
  // only a fallback for fields the API didn't provide.
  const cfg            = { ..._tgGetCfg(roomName) };
  if (meta) {
    if (meta.itm_h != null)              cfg.itmH          = meta.itm_h;
    if (meta.end_h != null)              cfg.endH          = meta.end_h;
    if (meta.ft_h  != null)              cfg.ftH           = meta.ft_h;
    if (meta.late_reg_level != null)     cfg.lateRegLevels = meta.late_reg_level;
    if (meta.level_duration_min != null) cfg.levelDurMin   = meta.level_duration_min;
    if (meta.max_blinds != null)         cfg.maxBlinds     = meta.max_blinds;
    // rebuy-phase duration: DB truth wins, including a null "no rebuy period"
    if ('level_duration_rebuy_min' in meta) cfg.levelDurRebuyMin = meta.level_duration_rebuy_min;
  }
  if (cfg.ftH == null) cfg.ftH = 3.5;  // final-table default so the ref line still draws

  // Anchor x=0 at the REAL tournament start. The backend reconciles each hand's
  // actual blind level from the DB ladder, so a hero who late-registers at
  // (say) level 6 gets placed at the real time that level began rather than at
  // t=0 — keeping the data aligned with the fixed Late Reg / ITM / End lines.
  // `entryOffset` is the seconds from tournament start to the first hand's
  // level; hero wall-clock deltas are added on top. Falls back to 0 when the
  // level is unknown (behaves like the old first-hand anchoring).
  const firstHand   = sorted[0];
  const tournStart  = firstHand.ts;
  const entryOffset = _tgLevelStartSecs(firstHand.level, cfg);
  const elapsedOf   = ts => (ts - tournStart) + entryOffset;

  const titleEl = document.getElementById('tourney-graph-title');
  if (titleEl) {
    const dateStr = new Date(tournStart * 1000).toLocaleDateString('en-GB',
        { day: 'numeric', month: 'short', year: 'numeric', timeZone: currentTz() });
    titleEl.textContent = roomName ? `${roomName} — ${dateStr}` : dateStr;
  }

  // Seconds from tournament start to key milestones
  const lateRegSecs = (cfg.levelDurRebuyMin || cfg.levelDurMin) * cfg.lateRegLevels * 60;
  const itmSecs     = cfg.itmH * 3600;
  const endSecs     = cfg.endH * 3600;
  const ftSecs      = cfg.ftH * 3600;
  const lastHandElapsed = elapsedOf(sorted[sorted.length - 1].ts);
  const axisSecs    = Math.max(endSecs + 30 * 60, lastHandElapsed + 15 * 60);

  // Build {x: elapsedSecs, y: value} datasets. When two consecutive hands are
  // separated by a long real-time pause (busted & later re-entered), insert a
  // null point so Chart.js breaks the line instead of drawing a diagonal
  // connector across the gap.
  const GAP_BREAK_SECS = 15 * 60;
  const rebuyTsSet = new Set(((meta && meta.rebuys) || []).map(r => r.ts));
  const chipDataset = [];
  const bbDataset   = [];
  const pointList   = [];  // parallel to dataset for tooltip lookup

  for (let i = 0; i < sorted.length; i++) {
    const h = sorted[i];
    const elapsed = elapsedOf(h.ts);

    if (i > 0 && rebuyTsSet.has(h.ts)) {
      // Backend detected a bust-and-rebuy right before this hand: the prior
      // hand's stack actually went to zero, not just to whatever its
      // pre-hand chip_stack shows. Plant an explicit zero point there so the
      // line visibly busts before the break, instead of just jumping to the
      // new buy-in's stack.
      const prevElapsed = elapsedOf(sorted[i - 1].ts);
      chipDataset.push({ x: prevElapsed + 1, y: 0 });
      bbDataset.push({ x: prevElapsed + 1, y: 0 });
      pointList.push(null);
      const midX = (prevElapsed + elapsed) / 2;
      chipDataset.push({ x: midX, y: null });
      bbDataset.push({ x: midX, y: null });
      pointList.push(null);
    } else if (i > 0 && (h.ts - sorted[i - 1].ts) > GAP_BREAK_SECS) {
      const midX = (elapsedOf(sorted[i - 1].ts) + elapsed) / 2;
      chipDataset.push({ x: midX, y: null });
      bbDataset.push({ x: midX, y: null });
      pointList.push(null);
    }

    const chips = h.chip_stack != null ? h.chip_stack : null;
    const bb    = chips != null && h.big_blind ? parseFloat((chips / h.big_blind).toFixed(1)) : null;
    chipDataset.push({ x: elapsed, y: chips });
    bbDataset.push({ x: elapsed, y: bb });
    pointList.push(h);
  }

  // If the hero busted out on the final hand, add a terminal point at y=0
  // so the line visibly touches bottom instead of stopping mid-air.
  let bustPoint = null;
  if (isFinishBusted) {
    const lastHand = sorted[sorted.length - 1];
    bustPoint = { x: elapsedOf(lastHand.ts) + 5, y: 0 };
    chipDataset.push(bustPoint);
    bbDataset.push({ x: bustPoint.x, y: 0 });
    pointList.push(null);
  }

  // Track played range for "played only" zoom
  const playedMinSecs = elapsedOf(sorted[0].ts);
  const playedMaxSecs = bustPoint ? bustPoint.x : elapsedOf(sorted[sorted.length - 1].ts);

  // Reference lines at fixed second positions
  const refLines = [
    { secs: lateRegSecs, color: 'rgba(204,204,0,0.9)',   label: `Late Reg\nL${cfg.lateRegLevels}` },
    { secs: ftSecs,      color: 'rgba(179,136,255,0.9)', label: `Final Table\n${cfg.ftH}h`         },
    { secs: itmSecs,     color: 'rgba(255,152,0,0.9)',   label: `ITM Bubble\n${cfg.itmH}h`         },
    { secs: endSecs,     color: 'rgba(255,82,82,0.9)',   label: `Exp. End\n${cfg.endH}h`            },
  ];

  // Critical point markers — store secs + y-values for both axes
  let bigWinH = null, bigWinVal = -Infinity;
  let bigLossH = null, bigLossVal = Infinity;
  let lastH = null;
  for (const h of sorted) {
    if ((h.profit || 0) > bigWinVal)  { bigWinVal  = h.profit; bigWinH  = h; }
    if ((h.profit || 0) < bigLossVal) { bigLossVal = h.profit; bigLossH = h; }
    lastH = h;
  }
  function _mkMarker(h, color, icon) {
    const secs  = elapsedOf(h.ts);
    const chips = h.chip_stack != null ? h.chip_stack : null;
    const bb    = chips != null && h.big_blind ? parseFloat((chips / h.big_blind).toFixed(1)) : null;
    return { secs, chipY: chips, bbY: bb, color, icon };
  }
  const markers = [];
  if (bigWinH  && bigWinVal  > 0) markers.push(_mkMarker(bigWinH,  '#00e676', '▲'));
  if (bigLossH && bigLossVal < 0) markers.push(_mkMarker(bigLossH, '#ff5252', '▼'));
  if (bustPoint) markers.push({ secs: bustPoint.x, chipY: 0, bbY: 0, color: '#ff5252', icon: '✕' });

  // Rebuy / add-on spots detected by the backend analyser (marked at the hand
  // where the chip injection was observed).
  const _findHandByTs = ts => sorted.find(x => x.ts === ts);
  for (const rb of (meta && meta.rebuys) || []) {
    const h = _findHandByTs(rb.ts);
    if (!h) continue;
    markers.push(_mkMarker(h, '#ffb300', 'R'));
    // Also flag the bust that preceded this rebuy, at the zero point planted
    // in the dataset above.
    const idx = sorted.indexOf(h);
    if (idx > 0) {
      const prevElapsed = elapsedOf(sorted[idx - 1].ts);
      markers.push({ secs: prevElapsed + 1, chipY: 0, bbY: 0, color: '#ff5252', icon: '✕' });
    }
  }
  for (const ad of (meta && meta.addons) || []) {
    const h = _findHandByTs(ad.ts);
    if (h) markers.push(_mkMarker(h, '#40c4ff', 'A'));
  }

  _tgState = {
    chipDataset, bbDataset, pointList,
    refLines, markers,
    tournStart, entryOffset, axisSecs,
    playedMinSecs, playedMaxSecs,
    roomName, cfg,
  };

  _tgBuildChart();
  wrap.classList.remove('d-none');
}

function _tgSetGraphWarning(message) {
  const el = document.getElementById('tourney-graph-warning');
  if (!el) return;
  el.textContent = message || '';
  el.classList.toggle('d-none', !message);
}

function _tgBuildChart() {
  if (!_tgState) return;
  const s = _tgState;
  const { chipDataset, bbDataset, pointList, refLines, markers, tournStart, entryOffset, axisSecs,
          playedMinSecs, playedMaxSecs, roomName, cfg } = s;
  const canvas = document.getElementById('tourney-graph-canvas');
  if (!canvas) return;

  const chipColor = 'rgba(64,196,255,1)';
  const bbColor   = 'rgba(0,230,118,1)';
  const showLeft  = _tgYMode !== 'bb';
  const showRight = _tgYMode !== 'chips';

  if (_tgChart) { _tgChart.destroy(); _tgChart = null; }

  if (typeof Chart === 'undefined') {
    console.warn('Chart.js not loaded yet');
    return;
  }

  // x-axis range: full view = 0..axisSecs; played only = zoom to played range
  const xMin = _tgPlayedOnly ? Math.max(0, playedMinSecs - 60) : 0;
  const xMax = _tgPlayedOnly ? playedMaxSecs + 120 : axisSecs;

  // Tick callback: Time mode = elapsed hh:mm, Level mode = "L#"
  function xTickCb(v) {
    if (_tgXMode === 'level') {
      const lvl = _tgLevelFromElapsed(v, cfg);
      return `L${lvl}`;
    }
    return _tgFmtElapsed(v);
  }

  // Tick step: aim for ~10 ticks across the visible range
  const visibleRange = xMax - xMin;
  const niceSteps = [5*60, 10*60, 15*60, 20*60, 30*60, 45*60, 60*60];
  const stepSize = niceSteps.find(s => visibleRange / s <= 12) || 30*60;

  Chart.register(_TG_REFLINES_PLUGIN, _TG_MARKERS_PLUGIN);

  _tgChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'Chip Stack',
          data: chipDataset,
          borderColor: chipColor,
          backgroundColor: 'rgba(64,196,255,0.07)',
          fill: true,
          borderWidth: 1.5,
          tension: 0.3,
          pointRadius: 1,
          pointHoverRadius: 6,
          pointBackgroundColor: chipColor,
          pointBorderColor: chipColor,
          yAxisID: 'yLeft',
          spanGaps: false,
          hidden: _tgYMode === 'bb',
          order: 1,
        },
        {
          label: 'BB Count',
          data: bbDataset,
          borderColor: bbColor,
          backgroundColor: 'rgba(0,230,118,0.05)',
          fill: true,
          borderWidth: 1.5,
          tension: 0.3,
          pointRadius: 1,
          pointHoverRadius: 6,
          pointBackgroundColor: bbColor,
          pointBorderColor: bbColor,
          yAxisID: 'yRight',
          spanGaps: false,
          hidden: _tgYMode === 'chips',
          order: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: 'index', intersect: false },
      tgRefLines: refLines,
      tgMarkers:  markers,
      plugins: {
        legend: {
          display: true,
          labels: {
            color: '#d0ddd0',
            font: { family: "'Exo 2', sans-serif", size: 12 },
            boxWidth: 12,
            filter: item => !item.hidden,
          },
        },
        tooltip: {
          backgroundColor: 'rgba(13,17,23,0.95)',
          borderColor: '#1e2d1e',
          borderWidth: 1,
          titleColor: '#d0ddd0',
          bodyColor: '#8aaa8a',
          padding: 10,
          displayColors: true,
          boxWidth: 8,
          boxHeight: 8,
          caretPadding: 14,
          yAlign: 'bottom',
          filter: item => !item.dataset.hidden,
          callbacks: {
            title(items) {
              if (!items.length) return '';
              const i = items[0].dataIndex;
              const h = pointList[i];
              if (!h) return '';
              const elapsed = _tgFmtElapsed((h.ts - tournStart) + entryOffset);
              const lvl     = (h.level != null)
                ? h.level
                : _tgInferLevel(h.big_blind, roomName, h.chip_stack);
              let hn = 0;
              for (let j = 0; j <= i; j++) if (pointList[j]) hn++;
              return `Hand #${hn} · +${elapsed}${lvl ? ' · L' + lvl : ''}`;
            },
            label(item) {
              const i = item.dataIndex;
              const h = pointList[i];
              if (!h) return null;
              if (item.datasetIndex === 0) {
                return `  Chips: ${_tgFmtK(h.chip_stack)}`;
              } else {
                const bb = h.chip_stack && h.big_blind
                  ? (h.chip_stack / h.big_blind).toFixed(1) : '—';
                return `  BBs:   ${bb}`;
              }
            },
            afterBody(items) {
              if (!items.length) return [];
              const i = items[0].dataIndex;
              const h = pointList[i];
              if (!h) return [];
              const lines = [];
              if (h.profit) {
                const sign = h.profit > 0 ? '+' : '';
                lines.push(`  P/L:   ${sign}${_tgFmtK(h.profit)}`);
              }
              if (h.position)                              lines.push(`  Pos:   ${h.position}`);
              if (h.last_street && h.last_street !== 'Pre') lines.push(`  Street: ${h.last_street}`);
              if (h.hole_cards?.length) {
                const cards = h.hole_cards.map(c => c.display || '?').join(' ');
                lines.push(`  Cards: ${cards}`);
              }
              return lines;
            },
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          min: xMin,
          max: xMax,
          ticks: {
            color: '#6b8c6b',
            font: { size: 10, family: "'Exo 2', sans-serif" },
            stepSize,
            maxRotation: 0,
            callback: xTickCb,
          },
          grid: { color: 'rgba(30,45,30,0.4)' },
        },
        yLeft: {
          type: 'linear',
          position: 'left',
          display: showLeft,
          ticks: {
            color: chipColor,
            font: { size: 10, family: "'Exo 2', sans-serif" },
            callback: v => _tgFmtK(v),
          },
          grid: { color: 'rgba(30,45,30,0.4)' },
        },
        yRight: {
          type: 'linear',
          position: 'right',
          display: showRight,
          ticks: {
            color: bbColor,
            font: { size: 10, family: "'Exo 2', sans-serif" },
            callback: v => `${v}BB`,
          },
          grid: { drawOnChartArea: false },
        },
      },
    },
  });
}

let _tgShowChips = true;
let _tgShowBB = true;

function _tgToggleY(which) {
  if (which === 'chips') _tgShowChips = !_tgShowChips;
  else _tgShowBB = !_tgShowBB;
  if (!_tgShowChips && !_tgShowBB) {
    if (which === 'chips') _tgShowBB = true; else _tgShowChips = true;
  }
  document.getElementById('tg-y-chips')?.classList.toggle('active', _tgShowChips);
  document.getElementById('tg-y-bb')?.classList.toggle('active', _tgShowBB);
  _tgYMode = _tgShowChips && _tgShowBB ? 'both' : _tgShowChips ? 'chips' : 'bb';
  if (!_tgChart) return;
  _tgChart.data.datasets[0].hidden = !_tgShowChips;
  _tgChart.data.datasets[1].hidden = !_tgShowBB;
  _tgChart.options.scales.yLeft.display  = _tgShowChips;
  _tgChart.options.scales.yRight.display = _tgShowBB;
  _tgChart.update();
}

function _tgToggleX() {
  _tgXMode = _tgXMode === 'time' ? 'level' : 'time';
  const pill = document.getElementById('tg-x-pill');
  if (pill) { pill.textContent = _tgXMode === 'time' ? 'Time' : 'Level'; pill.classList.toggle('active', _tgXMode === 'time'); }
  if (!_tgChart || !_tgState) return;
  _tgChart.update();
}

function _tgTogglePlayed() {
  _tgPlayedOnly = !_tgPlayedOnly;
  const pill = document.getElementById('tg-played-only-pill');
  if (pill) pill.classList.toggle('active', _tgPlayedOnly);
  if (!_tgChart || !_tgState) return;
  const s = _tgState;
  if (_tgPlayedOnly) {
    _tgChart.options.scales.x.min = Math.max(0, s.playedMinSecs - 60);
    _tgChart.options.scales.x.max = s.playedMaxSecs + 120;
  } else {
    _tgChart.options.scales.x.min = 0;
    _tgChart.options.scales.x.max = s.axisSecs;
  }
  _tgChart.update();
}

function _tgDestroy() {
  if (_tgChart) { _tgChart.destroy(); _tgChart = null; }
  _tgState = null;
  _tgPlayedOnly = false;
  _tgShowChips = true;
  _tgShowBB = true;
  _tgYMode = 'both';
  _tgXMode = 'time';
  const chipPill = document.getElementById('tg-y-chips');
  const bbPill = document.getElementById('tg-y-bb');
  const xPill = document.getElementById('tg-x-pill');
  const phPill = document.getElementById('tg-played-only-pill');
  if (chipPill) chipPill.classList.add('active');
  if (bbPill) bbPill.classList.add('active');
  if (xPill) { xPill.textContent = 'Time'; xPill.classList.add('active'); }
  if (phPill) phPill.classList.remove('active');
  _tgSetGraphWarning('');
  const wrap = document.getElementById('tourney-graph-wrap');
  if (wrap) wrap.classList.add('d-none');
}

/* ── Tournament Details (Pro): hands of the event selected in the Summary ── */

let _selectedTourneyId = null;

/** Clears the Tournament Details table back to its empty placeholder and drops any selection. */
function _resetTournamentDetails() {
  const tbody = document.getElementById('tourney-detail-tbody');
  if (tbody) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Select a tournament above to view its hands.</td></tr>';
  }
  const hint = document.getElementById('tourney-detail-hint');
  if (hint) hint.textContent = '';
  document.querySelectorAll('.tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  _selectedTourneyId = null;
  _tgDestroy();
}

/** Loads one tournament's hands into the Tournament Details table; clicking the same card again clears it. */
async function _selectTourneyDetail(tid, cardEl) {
  // Toggle off when the already-selected card is clicked again.
  if (cardEl && cardEl.classList.contains('selected')) {
    _resetTournamentDetails();
    return;
  }
  document.querySelectorAll('.tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  if (cardEl) cardEl.classList.add('selected');
  _selectedTourneyId = tid;

  const tbody   = document.getElementById('tourney-detail-tbody');
  const hint    = document.getElementById('tourney-detail-hint');
  const section = document.getElementById('tournament-history-pro-section');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Loading hands…</td></tr>';
  _tgDestroy();

  if (!_currentUser) return;
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) { _resetTournamentDetails(); return; }

  try {
    const r = await fetch(`/api/tournaments/${tid}/hands`, { headers: { 'Authorization': `Bearer ${token}` } });
    if (_selectedTourneyId !== tid) return;  // selection changed mid-fetch — drop stale response
    if (!r.ok) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
      return;
    }
    const data  = await r.json();
    const hands = data.hands || [];
    const meta  = data.meta  || {};
    // Fall back to _allTournaments for fields the API might not yet return
    if (!meta.room_name || !meta.earliest_ts) {
      const cached = (_allTournaments || []).find(t => t.tourney_id === tid) || {};
      meta.room_name    = meta.room_name    || cached.room_name    || '';
      meta.earliest_ts  = meta.earliest_ts  || cached.earliest_ts  || null;
      meta.finish_busted= meta.finish_busted != null ? meta.finish_busted : (cached.finish_busted || false);
    }
    renderHandsTable(hands, 'tourney-detail-tbody', { showExport: true, exportTid: tid });
    _renderTournamentChart(hands, meta);
    if (hint) {
      const dateLabel = cardEl ? (cardEl.querySelector('.tsum-event-date')?.textContent || '') : '';
      hint.textContent = `${hands.length} hand${hands.length === 1 ? '' : 's'}${dateLabel ? ' · ' + dateLabel : ''}`;
    }
    if (section) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const card = section.querySelector('.section-card');
      if (card) {
        card.classList.remove('td-flash');
        void card.offsetWidth;          // restart the highlight animation
        card.classList.add('td-flash');
      }
    }
  } catch (e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
  }
}

async function exportPersistedTournament(tourneyId, btn) {
  if (!_currentUser) return;
  const platform = (btn && btn.dataset.platform) || '';
  _rowExportStatus(btn, 'loading');
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) { _rowExportStatus(btn, 'err', 'Not signed in', 5000); return; }
  try {
    const r = await fetch(`/api/tournaments/${tourneyId}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify({ platform }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || 'Export failed');
    }
    const cd = r.headers.get('Content-Disposition') || '';
    const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
    const filename = m ? m[1].replace(/['"]/g, '').trim() : `pppoker_${tourneyId}.txt`;
    const blob = await r.blob();
    _triggerDownload(blob, filename);
    _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
  } catch (err) {
    _rowExportStatus(btn, 'err', err.message, 6000);
  }
}

async function exportPersistedTournamentJson(tourneyId, btn) {
  if (!_currentUser) return;
  _rowExportStatus(btn, 'loading');
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) { _rowExportStatus(btn, 'err', 'Not signed in', 5000); return; }
  try {
    const r = await fetch(`/api/tournaments/${tourneyId}/export/json`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || 'Export failed');
    }
    const cd = r.headers.get('Content-Disposition') || '';
    const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
    const filename = m ? m[1].replace(/['"]/g, '').trim() : `pppoker_${tourneyId}.json`;
    const blob = await r.blob();
    _triggerDownload(blob, filename);
    _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
  } catch (err) {
    _rowExportStatus(btn, 'err', err.message, 6000);
  }
}

/* ── Export Panel ────────────────────────────────────────── */

function renderHandStats(data) {
  const newHands = data.new_hands ?? 0;
  // Only use new_stats/new_validation — never fall back to all-time stats for pills
  const v = (newHands > 0 ? data.new_validation : null) || {};
  const s = (newHands > 0 ? data.new_stats      : null) || {};
  const _set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; };
  _set('hs-hands', newHands);
  _set('hs-flop',  newHands > 0 ? s.hands_hero_saw_flop  : 0);
  _set('hs-won',   newHands > 0 ? v.hands_won             : 0);
  _set('hs-turn',  newHands > 0 ? s.hands_hero_saw_turn  : 0);
  _set('hs-river', newHands > 0 ? s.hands_hero_saw_river : 0);
  _set('hs-sd',    newHands > 0 ? s.hands_at_showdown    : 0);
  const row = document.getElementById('loaded-hands-row');
  if (row) row.classList.remove('d-none');
}


/* ── Shared export helpers ───────────────────────────────── */

function _triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a   = Object.assign(document.createElement('a'), { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function exportRawJson() {
  const data = window._lastData;
  if (!data) return;
  try {
    const blob     = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const ts       = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const filename = `pppoker_raw_${ts}.json`;
    _triggerDownload(blob, filename);
    _rowExportStatus(null, 'ok', `Saved as ${filename}`, 5000);
  } catch (e) {
    _rowExportStatus(null, 'err', `JSON export error: ${e.message}`, 6000);
  }
}

function exportSpecificHandJson(btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  _openExportHandModal(handId => {
    consumeExportQuota();
    _rowExportStatus(btn, 'loading', 'Building JSON…');
    return fetch('/api/export/json/hand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hand_id: handId, session_id: getSessionId() }),
    })
      .then(r => {
        if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
        const cd = r.headers.get('Content-Disposition') || '';
        const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
        const filename = m ? m[1].replace(/['"]/g, '').trim() : 'hand.json';
        return r.blob().then(blob => ({ blob, filename }));
      })
      .then(({ blob, filename }) => {
        _triggerDownload(blob, filename);
        _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
      })
      .catch(err => { _rowExportStatus(btn, 'err', err.message, 6000); throw err; });
  });
}

function exportAllHandsJson(btn) {
  if (!isPro()) { showUpgradeModal('export'); return; }
  _rowExportStatus(btn, 'loading', 'Building JSON…');
  fetch('/api/export/json/all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: getSessionId() }),
  })
    .then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
      const cd = r.headers.get('Content-Disposition') || '';
      const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
      const filename = m ? m[1].replace(/['"]/g, '').trim() : 'pppoker_all.json';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      _triggerDownload(blob, filename);
      _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    })
    .catch(err => _rowExportStatus(btn, 'err', err.message, 6000));
}

function exportTournamentJson(tourneyId, btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  consumeExportQuota();
  _rowExportStatus(btn, 'loading');
  fetch('/api/export/json/tournament', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tourney_id: tourneyId, session_id: getSessionId() }),
  })
    .then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
      const cd = r.headers.get('Content-Disposition') || '';
      const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
      const filename = m ? m[1].replace(/['"]/g, '').trim() : 'tournament.json';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      _triggerDownload(blob, filename);
      _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    })
    .catch(err => _rowExportStatus(btn, 'err', err.message, 6000));
}

function exportSpecificHand(btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  const platform = (btn && btn.dataset.platform) || '';
  _openExportHandModal(handId => {
    consumeExportQuota();
    _rowExportStatus(btn, 'loading', 'Looking up hand…');
    return fetch('/api/export/hand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hand_id: handId, platform, session_id: getSessionId() }),
    })
      .then(r => {
        if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
        const cd = r.headers.get('Content-Disposition') || '';
        const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
        const filename = m ? m[1].replace(/['"]/g, '').trim() : 'hand_export.txt';
        return r.blob().then(blob => ({ blob, filename }));
      })
      .then(({ blob, filename }) => {
        _triggerDownload(blob, filename);
        _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
      })
      .catch(err => { _rowExportStatus(btn, 'err', err.message, 6000); throw err; });
  });
}

function exportAllHands(btn) {
  if (!isPro()) { showUpgradeModal('export'); return; }
  _rowExportStatus(btn, 'loading', 'Generating export…');

  const platform = (btn && btn.dataset.platform) || '';
  fetch('/api/export/pokerstars', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform, session_id: getSessionId() }),
  })
    .then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
      const cd = r.headers.get('Content-Disposition') || '';
      const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
      const filename = m ? m[1].replace(/['"]/g, '').trim() : 'pppoker_export.txt';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      _triggerDownload(blob, filename);
      _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    })
    .catch(err => {
      _rowExportStatus(btn, 'err', err.message, 6000);
    });
}

function _rowExportStatus(btn, state, text, autoClear) {
  const toast = document.getElementById('export-toast');
  if (!toast) return;
  clearTimeout(toast._hideTimer);
  if (state === 'loading') {
    toast.innerHTML = `<span style="color:var(--muted)">${text || 'Exporting…'}</span>`;
  } else if (state === 'ok') {
    toast.innerHTML = `<span style="color:var(--green)">✓ ${text}</span>`;
  } else {
    toast.innerHTML = `<span style="color:var(--red)">${text}</span>`;
  }
  toast.classList.add('et-visible');
  if (autoClear) {
    toast._hideTimer = setTimeout(() => toast.classList.remove('et-visible'), autoClear);
  }
}

function _doExportTournament(tourneyId, btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  consumeExportQuota();
  _rowExportStatus(btn, 'loading');

  const platform = (btn && btn.dataset.platform) || '';
  fetch('/api/export/tournament', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tourney_id: tourneyId, platform, session_id: getSessionId() }),
  })
    .then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
      const cd = r.headers.get('Content-Disposition') || '';
      const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
      const filename = m ? m[1].replace(/['"]/g, '').trim() : 'tournament_export.txt';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      const url = URL.createObjectURL(blob);
      const a   = Object.assign(document.createElement('a'), { href: url, download: filename });
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    })
    .catch(err => {
      _rowExportStatus(btn, 'err', err.message, 6000);
    });
}

function exportTournament(tourneyId, btn) {
  // Skip modal if user previously suppressed it.
  if (localStorage.getItem('exportWarningSuppressed') === '1') {
    _doExportTournament(tourneyId, btn);
    return;
  }

  const modal      = new bootstrap.Modal(document.getElementById('exportWarningModal'));
  const suppress   = document.getElementById('export-warning-suppress');
  suppress.checked = false;

  const confirmBtn = document.getElementById('export-confirm-btn');
  // Replace any previous listener to avoid stacking handlers
  const newBtn = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
  newBtn.addEventListener('click', () => {
    if (suppress.checked) localStorage.setItem('exportWarningSuppressed', '1');
    modal.hide();
    _doExportTournament(tourneyId, btn);
  });
  modal.show();
}

/* ── Init ────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleImport();
  });

  // Bootstrap tooltips (used for info ⓘ buttons that don't need a modal)
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    new bootstrap.Tooltip(el, { trigger: 'hover focus' });
  });

  // Auth modal: send link on Enter, and clear state each time modal opens
  const authModal = document.getElementById('modal-auth');
  if (authModal) {
    authModal.addEventListener('shown.bs.modal', () => {
      const inp = document.getElementById('auth-email-input');
      const msg = document.getElementById('auth-msg');
      if (inp) inp.value = '';
      if (msg) { msg.className = 'mt-2 d-none'; msg.innerHTML = ''; }
      const btn = document.getElementById('auth-send-btn');
      if (btn) btn.disabled = false;
      if (inp) inp.focus();
    });
    document.getElementById('auth-email-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') sendMagicLink();
    });
  }

  // Export hand modal — validate on input, auto-proceed on valid ID
  const exportHandModal = document.getElementById('modal-export-hand');
  const handIdInput = document.getElementById('hand-id-input');
  if (exportHandModal && handIdInput) {
    handIdInput.addEventListener('input', () => _validateExportHandInput(handIdInput.value));
    handIdInput.addEventListener('keydown', e => { if (e.key === 'Enter') _confirmExportHand(); });
    exportHandModal.addEventListener('hidden.bs.modal', () => {
      handIdInput.value = '';
      document.getElementById('hand-id-status').innerHTML = '';
      const okBtn = document.getElementById('hand-id-ok-btn');
      if (okBtn) okBtn.disabled = true;
      _exportHandCb = null;
    });
  }

  document.getElementById('tz-select').addEventListener('change', () => {
    if (window._lastData) {
      renderRecentHands(window._lastData.recent_hands || []);
      renderRecentWonHands(window._lastData.recent_won_hands || []);
      renderTournaments(window._lastData.tournaments || []);
      updateTzHeaders();
    }
  });
});

/* ── Auth helpers ────────────────────────────────────────── */

/** Returns the Firestore doc ref for the current user (auth) or guest (session). */
function _getUserDocRef() {
  if (!_db) return null;
  if (_currentUser) return _db.collection('users').doc(_currentUser.uid);
  return _db.collection('guests').doc(getSessionId());
}

/**
 * Load (or create) the Firestore user/guest doc and populate _userState.
 * Called whenever auth state changes.
 */
async function _loadUserState() {
  if (!_db) return;
  const ref = _getUserDocRef();
  if (!ref) return;
  try {
    const snap = await ref.get();
    if (snap.exists) {
      const d = snap.data();
      _userState = {
        is_pro:           d.is_pro           || false,
        exports_today:    d.exports_today    || 0,
        last_export_date: d.last_export_date || '',
      };
    } else {
      // First visit — create doc with defaults
      const base = {
        is_pro:           false,
        exports_today:    0,
        last_export_date: _todayStr(),
        first_seen:       firebase.firestore.FieldValue.serverTimestamp(),
        last_seen:        firebase.firestore.FieldValue.serverTimestamp(),
      };
      if (_currentUser) {
        base.uid   = _currentUser.uid;
        base.email = _currentUser.email;
      } else {
        base.session_id = getSessionId();
      }
      await ref.set(base);
      _userState = { is_pro: false, exports_today: 0, last_export_date: _todayStr() };
    }
    _updateExportGates(); // Refresh gate UI whenever state loads/reloads
  } catch (e) { console.warn('Firestore user state load failed:', e); }
}

/** Re-render the auth bar based on current sign-in state. */
function _renderAuthBar(email) {
  const bar = document.getElementById('auth-bar');
  if (!bar) return;
  if (email) {
    bar.innerHTML =
      `<span class="auth-chip">` +
      `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>` +
      `<span class="auth-email">${email}</span>` +
      `</span>` +
      `<button class="auth-signout-btn auth-signout-standalone" onclick="signOutUser()">Sign out</button>`;
  } else {
    bar.innerHTML =
      `<button class="auth-chip auth-signin-btn" data-bs-toggle="modal" data-bs-target="#modal-auth">` +
      `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>` +
      `<span>Sign in</span>` +
      `</button>`;
  }
}

/**
 * Send a Firebase magic link to the given email.
 * NOTE: For this to work in Firebase Console you must:
 *   1. Enable "Email/Password" provider → enable "Email link (passwordless sign-in)"
 *   2. Add your app's domain to the Authorized Domains list (localhost is pre-authorized)
 */
function sendMagicLink() {
  if (!_auth) {
    const msgEl = document.getElementById('auth-msg');
    if (msgEl) { msgEl.className = 'mt-2'; msgEl.innerHTML = '<span style="color:var(--red)">Auth service not available. Check Firebase config.</span>'; msgEl.classList.remove('d-none'); }
    return;
  }
  const emailInput = document.getElementById('auth-email-input');
  const msgEl      = document.getElementById('auth-msg');
  const btn        = document.getElementById('auth-send-btn');
  const email      = (emailInput ? emailInput.value : '').trim();
  if (!email) {
    if (msgEl) { msgEl.className = 'mt-2'; msgEl.innerHTML = '<span style="color:var(--yellow)">Please enter your email address.</span>'; msgEl.classList.remove('d-none'); }
    return;
  }
  if (btn) btn.disabled = true;
  if (msgEl) { msgEl.className = 'mt-2'; msgEl.innerHTML = '<span style="color:var(--muted)">Sending…</span>'; msgEl.classList.remove('d-none'); }

  _auth.sendSignInLinkToEmail(email, {
    url: window.location.origin + '/',
    handleCodeInApp: true,
  }).then(() => {
    localStorage.setItem('emailForSignIn', email);
    if (msgEl) msgEl.innerHTML = `<span style="color:var(--green)">✓ Link sent to <strong>${email}</strong> — check your inbox.</span>`;
    if (btn) btn.disabled = false;
  }).catch(err => {
    if (msgEl) msgEl.innerHTML = `<span style="color:var(--red)">${err.message || 'Failed to send link.'}</span>`;
    if (btn) btn.disabled = false;
  });
}

/** Sign out the current user. */
function signOutUser() {
  if (!_auth) return;
  _auth.signOut().then(() => {
    _currentUser = null;
    _userState   = { is_pro: false, exports_today: 0, last_export_date: '' };
    window._lastData = null;

    // Reset UI to blank-slate state
    const urlInput = document.getElementById('url-input');
    if (urlInput) urlInput.value = '';
    const results = document.getElementById('results-section');
    if (results) results.classList.add('d-none');
    const loadingMsg = document.getElementById('loading-msg');
    if (loadingMsg) loadingMsg.classList.add('d-none');

    const ts = document.getElementById('tournament-summary-section');
    if (ts) ts.classList.add('d-none');
    const th = document.getElementById('tournament-history-pro-section');
    if (th) th.classList.add('d-none');
    const cgs = document.getElementById('cash-games-summary-section');
    if (cgs) cgs.classList.add('d-none');
    const cgsd = document.getElementById('cash-game-detail-section');
    if (cgsd) cgsd.classList.add('d-none');
    _renderAuthBar(null);
    _updateExportGates();
    _loadUserState();
  }).catch(e => console.warn('Sign out failed:', e));
}

/** Sign in with Google popup. onAuthStateChanged handles the rest. */
function signInWithGoogle() {
  const msgEl = document.getElementById('auth-msg');
  if (!_auth) {
    if (msgEl) {
      msgEl.className = 'mt-2';
      msgEl.innerHTML = `<span style="color:var(--red,#f85149)">Auth not ready — please wait a moment and try again.</span>`;
      msgEl.classList.remove('d-none');
    }
    return;
  }
  const provider = new firebase.auth.GoogleAuthProvider();
  _auth.signInWithPopup(provider)
    .then(() => {
      const modal = bootstrap.Modal.getInstance(document.getElementById('modal-auth'));
      if (modal) modal.hide();
    })
    .catch(err => {
      if (msgEl) {
        msgEl.className = 'mt-2';
        msgEl.innerHTML = `<span style="color:var(--red,#f85149)">${err.message || 'Google sign-in failed.'}</span>`;
        msgEl.classList.remove('d-none');
      }
    });
}

/* ── Firebase ─────────────────────────────────────────────── */

function _trackEvent(name, params) {
  try {
    if (_analytics) _analytics.logEvent(name, params || {});
  } catch {}
}

async function _initFirebase() {
  _renderAuthBar(null);   // show Sign in immediately; overwritten once auth state resolves
  try {
    const res = await fetch('/api/firebase-config');
    if (!res.ok) return;
    const cfg = await res.json();
    if (!cfg.FIREBASE_API_KEY) return;
    if (typeof firebase === 'undefined') return;

    firebase.initializeApp({
      apiKey:            cfg.FIREBASE_API_KEY,
      authDomain:        cfg.FIREBASE_AUTH_DOMAIN,
      projectId:         cfg.FIREBASE_PROJECT_ID,
      storageBucket:     cfg.FIREBASE_STORAGE_BUCKET,
      messagingSenderId: cfg.FIREBASE_MESSAGING_SENDER_ID,
      appId:             cfg.FIREBASE_APP_ID,
      measurementId:     cfg.FIREBASE_MEASUREMENT_ID,
    });

    _analytics = firebase.analytics();
    _db        = firebase.firestore();
    _auth      = firebase.auth ? firebase.auth() : null;

    // Unlock auth buttons now that Firebase is ready
    ['btn-google-signin', 'auth-send-btn'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = false;
    });

    // ── Handle magic-link redirect (must run before onAuthStateChanged) ──
    if (_auth && _auth.isSignInWithEmailLink(window.location.href)) {
      let email = localStorage.getItem('emailForSignIn');
      if (!email) email = window.prompt('Please confirm your email to complete sign-in:') || '';
      if (email) {
        try {
          await _auth.signInWithEmailLink(email, window.location.href);
          localStorage.removeItem('emailForSignIn');
          window.history.replaceState({}, document.title, '/');
        } catch (e) { console.warn('Magic link sign-in failed:', e); }
      }
    }

    // ── Auth state listener — fires immediately with current user (or null) ──
    if (_auth) {
      _auth.onAuthStateChanged(async (user) => {
        _currentUser = user;
        await _loadUserState();
        _renderAuthBar(user ? user.email : null);
        if (user) {
          _getUserDocRef().set({
            email:     user.email,
            last_seen: firebase.firestore.FieldValue.serverTimestamp(),
          }, { merge: true }).catch(() => {});
          if (isPro()) _loadHistory();
        }
      });
    } else {
      // Auth SDK not available — load guest state directly
      await _loadUserState();
      _renderAuthBar(null);
    }

    _trackEvent('app_open');
  } catch (e) { console.warn('Firebase init failed:', e); }
}

// Kick off Firebase after the page is interactive (non-blocking)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initFirebase);
} else {
  _initFirebase();
}

'use strict';

let _importCount = 0;   // tracks how many times Import has been successfully used

/* ── Freemium tier helpers ────────────────────────────────── */

const FREE_HAND_LIMIT        = 30;
const FREE_EXPORT_LIMIT      = 5;   // single-hand exports per calendar day (free tier)
const _SESSION_KEY      = 'pppha_session_id';

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
  const uid   = _currentUser ? _currentUser.uid   : '';
  const email = _currentUser ? _currentUser.email : '';
  fetch('/api/create-checkout-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ uid, email, tier }),
  })
    .then(r => r.json())
    .then(d => {
      if (d.url) { window.location.href = d.url; }
      else throw new Error(d.error || 'Could not start checkout');
    })
    .catch(err => {
      if (btn) { btn.disabled = false; btn.textContent = 'Upgrade to Pro'; }
      const cs = document.getElementById('pro-coming-soon');
      if (cs) { cs.textContent = err.message; cs.classList.remove('d-none'); }
    });
}

function activateProDev() {
  const ref = _getUserDocRef();
  if (ref) {
    ref.set({ is_pro: true }, { merge: true })
      .then(() => location.reload())
      .catch(() => location.reload());
  } else {
    _userState.is_pro = true;
    location.reload();
  }
}

function deactivateProDev() {
  const ref = _getUserDocRef();
  if (ref) {
    ref.set({ is_pro: false }, { merge: true })
      .then(() => location.reload())
      .catch(() => location.reload());
  } else {
    _userState.is_pro = false;
    location.reload();
  }
}

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
  const hands   = data.total_fetched || 0;
  const tours   = (data.tournaments || []).length;
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
  // Clear any stale export status from a previous session
  const _es = document.getElementById('export-status');
  if (_es) { _es.classList.add('d-none'); _es.innerHTML = ''; }

  if (!url) {
    showError('Please enter a PPPoker Hand Review URL.');
    return;
  }

  setLoading(true);

  fetch('/api/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  })
    .then(r => r.json())
    .then(data => {
      setLoading(false);
      if (data.error) { showError(data.error); return; }
      renderResults(data);
      showImportSuccess(data);
    })
    .catch(err => {
      setLoading(false);
      showError('Network error: ' + err.message);
    });
}

/* ── Render all ──────────────────────────────────────────── */

function renderResults(data) {
  window._lastData = data;   // persisted so tz changes can re-render
  _trackEvent('hands_imported', { total: data.total_fetched || 0 });

  // Derive session date span from tournament timestamps
  const _ts = (data.tournaments || []).map(t => t.earliest_ts).filter(Boolean);
  const _spanStr = (() => {
    if (!_ts.length) return null;
    const tz   = currentTz();
    const opts = { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz };
    const min  = new Date(Math.min(..._ts) * 1000);
    const max  = new Date(Math.max(..._ts) * 1000);
    const dMin = min.toLocaleDateString('en-GB', opts);
    const dMax = max.toLocaleDateString('en-GB', opts);
    return dMin === dMax.replace(/\s\d{4}$/, '')
      ? dMax                        // same day — show once with year
      : `${dMin} – ${dMax}`;       // range
  })();

  // Player avatar initials
  const _initials = (data.player.name || '').replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase() || '??';
  const _avatarEl = document.getElementById('player-avatar-text');
  if (_avatarEl) _avatarEl.textContent = _initials;

  document.getElementById('player-info').innerHTML =
    `<strong>${data.player.name}</strong>` +
    `<span style="color:var(--muted);font-size:.8rem">&nbsp;&nbsp;UID: ${data.player.uid}</span>` +
    (_spanStr ? `<br><span style="color:var(--muted);font-size:.78rem">${_spanStr}</span>` : '') +
    (data.total_fetched < data.total_available
      ? `&nbsp;&nbsp;<span class="text-warning" style="font-size:.8rem">(${data.total_available - data.total_fetched} hands failed to load)</span>`
      : '');

  renderHandStats(data.validation || {}, data.stats || {});
  renderRecentHands(data.recent_hands || []);
  renderRecentWonHands(data.recent_won_hands || []);
  renderTournaments(data.tournaments || []);
  updateTzHeaders();
  _updateExportGates();

  document.getElementById('results-section').classList.remove('d-none');
  document.getElementById('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ── Shared hand table renderer ──────────────────────────── */

function renderHandsTable(hands, tbodyId) {
  if (!isPro() && hands.length > FREE_HAND_LIMIT) {
    hands = hands.slice(-FREE_HAND_LIMIT);
  }
  const tbody = document.getElementById(tbodyId);
  if (!hands.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No hands available</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = hands.map(h => {
    const cards = (h.hole_cards || []).map(renderCard).join('');
    const copyBtn = h.hand_num
      ? `<button class="copy-hand-btn" onclick="copyHandId(this)" data-hand-num="${h.hand_num}" title="Hand ID: ${h.hand_num}">
           <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
         </button>`
      : '';
    return `<tr>
      <td><span class="hand-when-cell">${fmtHandDateTime(h.ts, tz)}${copyBtn}</span></td>
      <td class="no-wrap">${cards || '—'}</td>
      <td class="d-none d-md-table-cell">${posBadge(h.position)}</td>
      <td>${streetBadge(h.last_street)}</td>
      <td class="d-none d-md-table-cell">${resultBadge(h.result)}</td>
      <td class="d-none d-lg-table-cell">${fmtProfitBB(h.profit, h.big_blind)}</td>
      <td>
        ${h.replay_url && h.replay_url !== '#'
          ? `<a class="replay-link" href="${h.replay_url}" target="_blank" rel="noopener" title="Watch replay">▶</a>`
          : '<span class="text-muted">—</span>'}
      </td>
    </tr>`;
  }).join('');
}

function renderRecentHands(hands)    { renderHandsTable(hands, 'recent-hands-tbody'); }
function renderRecentWonHands(hands) { renderHandsTable(hands, 'recent-won-tbody'); }

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
      <td class="d-none d-md-table-cell">${typeBadge}</td>
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
                <button class="tourney-gate-btn" onclick="showUpgradeModal('tourney')">Upgrade · $7.99/mo</button>
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
      `<button class="tourney-gate-btn" onclick="showUpgradeModal('export')">Upgrade · $7.99/mo</button>` +
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

/** Call after any state change that could affect export UI. */
function _updateExportGates() {
  _renderExportAllSection();
  _renderExportCounter();
  const tierCompare = document.getElementById('tier-compare');
  if (tierCompare) tierCompare.style.display = isPro() ? 'none' : '';
}

/* ── Export Panel ────────────────────────────────────────── */

function renderHandStats(v, s) {
  const _set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; };
  _set('hs-hands', v.hands_imported);
  _set('hs-flop',  s.hands_hero_saw_flop);
  _set('hs-won',   v.hands_won);
  _set('hs-turn',  s.hands_hero_saw_turn);
  _set('hs-river', s.hands_hero_saw_river);
  _set('hs-sd',    s.hands_at_showdown);
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

/**
 * Show a status message in the export panel's #export-status div.
 * btn is passed for context (reserved for future per-row status in panel rows).
 */
function _panelExportStatus(btn, state, text, autoClear) {
  const el = document.getElementById('export-status');
  if (!el) return;
  el.classList.remove('d-none');
  if (state === 'loading') {
    el.innerHTML = `<span style="color:var(--muted)">${text}</span>`;
  } else if (state === 'ok') {
    el.innerHTML = `<span class="profit-pos">&#10003; ${text}</span>`;
  } else {
    el.innerHTML = `<span class="profit-neg">${text}</span>`;
  }
  if (autoClear) setTimeout(() => { el.classList.add('d-none'); el.innerHTML = ''; }, autoClear);
}

function exportRawJson() {
  const data = window._lastData;
  if (!data) return;
  const status = document.getElementById('export-status');
  status.classList.remove('d-none');
  try {
    const blob     = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const ts       = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const filename = `pppoker_raw_${ts}.json`;
    const url      = URL.createObjectURL(blob);
    const a        = Object.assign(document.createElement('a'), { href: url, download: filename });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    status.innerHTML = `<span class="profit-pos">✓ Saved as ${filename}</span>`;
  } catch (e) {
    status.innerHTML = `<span class="profit-neg">JSON export error: ${e.message}</span>`;
  }
}

function exportSpecificHandJson(btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  _openExportHandModal(handId => {
    consumeExportQuota();
    _panelExportStatus(btn, 'loading', 'Building JSON…');
    return fetch('/api/export/json/hand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hand_id: handId }),
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
        _panelExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
      })
      .catch(err => { _panelExportStatus(btn, 'err', err.message, 6000); throw err; });
  });
}

function exportAllHandsJson(btn) {
  if (!isPro()) { showUpgradeModal('export'); return; }
  _panelExportStatus(btn, 'loading', 'Building JSON…');
  fetch('/api/export/json/all', { method: 'POST' })
    .then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Export failed'); });
      const cd = r.headers.get('Content-Disposition') || '';
      const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
      const filename = m ? m[1].replace(/['"]/g, '').trim() : 'pppoker_all.json';
      return r.blob().then(blob => ({ blob, filename }));
    })
    .then(({ blob, filename }) => {
      _triggerDownload(blob, filename);
      _panelExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    })
    .catch(err => _panelExportStatus(btn, 'err', err.message, 6000));
}

function exportTournamentJson(tourneyId, btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  consumeExportQuota();
  _rowExportStatus(btn, 'loading');
  fetch('/api/export/json/tournament', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tourney_id: tourneyId }),
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
    _panelExportStatus(btn, 'loading', 'Looking up hand…');
    return fetch('/api/export/hand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hand_id: handId, platform }),
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
        _panelExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
      })
      .catch(err => { _panelExportStatus(btn, 'err', err.message, 6000); throw err; });
  });
}

function exportAllHands(btn) {
  if (!isPro()) { showUpgradeModal('export'); return; }
  _panelExportStatus(btn, 'loading', 'Generating export…');

  const platform = (btn && btn.dataset.platform) || '';
  fetch('/api/export/pokerstars', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform }),
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
      _panelExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    })
    .catch(err => {
      _panelExportStatus(btn, 'err', err.message, 6000);
    });
}

function _rowExportStatus(btn, state, text, autoClear) {
  // Show a temporary inline status message below the button group in the same cell.
  const td = btn ? btn.closest('td') : null;
  if (!td) return;
  let el = td.querySelector('.row-export-status');
  if (!el) {
    el = document.createElement('div');
    el.className = 'row-export-status';
    el.style.cssText = 'font-size:.72rem;margin-top:5px;min-height:1em';
    td.appendChild(el);
  }
  if (state === 'loading') {
    el.innerHTML = `<span style="color:var(--muted)">Exporting…</span>`;
  } else if (state === 'ok') {
    el.innerHTML = `<span style="color:var(--green)">✓ ${text}</span>`;
  } else {
    el.innerHTML = `<span style="color:var(--red)">${text}</span>`;
  }
  if (autoClear) setTimeout(() => { el.innerHTML = ''; }, autoClear);
}

function _doExportTournament(tourneyId, btn) {
  if (!checkExportQuota()) { showUpgradeModal('export'); return; }
  consumeExportQuota();
  _rowExportStatus(btn, 'loading');

  const platform = (btn && btn.dataset.platform) || '';
  fetch('/api/export/tournament', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tourney_id: tourneyId, platform }),
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
    if (!res.ok) { console.error('[Firebase] config fetch failed:', res.status); return; }
    const cfg = await res.json();
    if (!cfg.FIREBASE_API_KEY) { console.error('[Firebase] FIREBASE_API_KEY missing from config'); return; }
    if (typeof firebase === 'undefined') { console.error('[Firebase] SDK not loaded (blocked?)'); return; }

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
          // Keep email + last_seen fresh on the auth user doc
          _getUserDocRef().set({
            email:     user.email,
            last_seen: firebase.firestore.FieldValue.serverTimestamp(),
          }, { merge: true }).catch(() => {});
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

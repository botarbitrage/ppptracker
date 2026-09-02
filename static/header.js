/* ── Shared page header ───────────────────────────────────────────────────────
 * The round "User" button and its dropdown, shared by the main page (through
 * app.js's _renderAuthBar) and by every sub-page's own auth bootstrap. Only the
 * main page loads app.js, so the markup lives here rather than there — this is
 * the one definition of what the header looks like.
 *
 * The dropdown is toggled by this file rather than by Bootstrap's JS: /admin and
 * /leaks load Bootstrap's CSS but not its bundle, and pulling the bundle into
 * them just for a menu isn't worth it. The .show class it toggles is the same
 * one Bootstrap's CSS styles, and .user-dropdown-menu positions itself in
 * style.css, so the menu behaves the same on all five pages.
 */
(function (global) {
  'use strict';

  const ICON = {
    account: '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
    admin: '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    signOut: '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
    logIn: '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  };

  /** 2-char initials from an email's local-part, e.g. "jane.doe@x.com" -> "JD". */
  function initials(email) {
    const local = (email || '').split('@')[0] || '';
    const parts = local.split(/[.\-_+]+/).filter(Boolean);
    const out = parts.length > 1 ? (parts[0][0] + parts[1][0]) : local.slice(0, 2);
    return (out || '??').toUpperCase();
  }

  /**
   * Render the signed-in user menu into `el`.
   *
   * opts:
   *   email       - signed-in address; drives the initials and the hover title.
   *   accountHref - where "My Account" goes. Omit on the main page, which opens
   *                 the #modal-account modal in place instead.
   *   admin       - 'no' (omit the item), 'yes' (show it), or 'deferred' (render
   *                 it hidden as #admin-board-item for a later admin check).
   *   onSignOut   - name of a global function to call on Sign out.
   *   labels      - { myAccount, adminBoard, signOut }, already translated.
   */
  function renderUserMenu(el, opts) {
    if (!el) return;
    const o = opts || {};
    const l = o.labels || {};
    const myAccount = l.myAccount || 'My Account';
    const adminBoard = l.adminBoard || 'Admin Board';
    const signOut = l.signOut || 'Sign out';
    const onSignOut = o.onSignOut || 'signOutUser';

    const accountItem = o.accountHref
      ? `<li><a href="${o.accountHref}" class="user-dropdown-item">${ICON.account}<span></span></a></li>`
      : `<li><button type="button" class="user-dropdown-item" data-bs-toggle="modal" data-bs-target="#modal-account">${ICON.account}<span></span></button></li>`;

    const adminItem = o.admin && o.admin !== 'no'
      ? `<li><a href="/admin" id="admin-board-item" class="user-dropdown-item${o.admin === 'deferred' ? ' d-none' : ''}">${ICON.admin}<span></span></a></li>`
      : '';

    el.innerHTML =
      `<div class="user-menu">` +
      `<button class="user-btn" type="button" aria-haspopup="true" aria-expanded="false"></button>` +
      `<ul class="dropdown-menu user-dropdown-menu">` +
      accountItem +
      adminItem +
      `<li><button type="button" class="user-dropdown-item user-dropdown-item-danger" onclick="${onSignOut}()">${ICON.signOut}<span></span></button></li>` +
      `</ul></div>`;

    // Text goes in as text, never as markup — the email is user-controlled.
    const btn = el.querySelector('.user-btn');
    btn.textContent = initials(o.email);
    btn.title = o.email || '';
    btn.setAttribute('aria-label', myAccount);

    const spans = el.querySelectorAll('.user-dropdown-item span');
    const labels = adminItem ? [myAccount, adminBoard, signOut] : [myAccount, signOut];
    spans.forEach((s, i) => { s.textContent = labels[i]; });

    _wireToggle(el.querySelector('.user-menu'));
  }

  /** Render the signed-out "Log In" chip. `target` is the sign-in modal selector. */
  function renderLogIn(el, label, target) {
    if (!el) return;
    el.innerHTML =
      `<button class="auth-chip auth-signin-btn" data-bs-toggle="modal" data-bs-target="${target || '#modal-auth'}">` +
      ICON.logIn + `<span></span></button>`;
    el.querySelector('span').textContent = label || 'Log In';
  }

  /** Click to open, click-outside or Escape to close. */
  function _wireToggle(menu) {
    if (!menu) return;
    const btn = menu.querySelector('.user-btn');
    const list = menu.querySelector('.user-dropdown-menu');
    const close = () => {
      list.classList.remove('show');
      btn.setAttribute('aria-expanded', 'false');
    };
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = list.classList.toggle('show');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', (e) => {
      if (!menu.contains(e.target)) close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') close();
    });
  }

  global.PPPHeader = { initials, renderUserMenu, renderLogIn };
})(window);

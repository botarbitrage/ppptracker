// test_leaks_compare.js — logic test for the Compare-mode functions in
// templates/leaks.html (Phase 6).
//
// tierOf, progressOf, buildPositionsCombined, sortedRowsCompare, and the
// Rec. Action message builders are pure — no DOM access — so they run
// directly under Node against synthetic /api/leaks-shaped data. This is the
// part of Compare mode with real branching logic (classifying a stat's
// movement between two ranges, tail-pinning "neh"/"no target" rows in both
// sort directions, zipping two responses that may not cover the same
// positions); the DOM wiring around it (filter-bar cloning, checkbox state)
// is comparatively low-risk and is exercised by hand in the browser instead.
//
// The inline <script> is extracted from the real template file, so this
// tests the code that actually ships, not a copy of it.
//
//     node test_leaks_compare.js

const fs = require('fs');
const vm = require('vm');

class FakeClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach(x => this.set.add(x)); }
  remove(...c) { c.forEach(x => this.set.delete(x)); }
  toggle(c, force) {
    if (force === undefined) { this.set.has(c) ? this.set.delete(c) : this.set.add(c); }
    else if (force) this.set.add(c); else this.set.delete(c);
  }
  contains(c) { return this.set.has(c); }
}
function fakeEl() {
  return { textContent: '', innerHTML: '', value: '', min: '', max: '',
           classList: new FakeClassList(), checked: false,
           querySelector: () => fakeEl(), addEventListener: () => {} };
}

function loadFunctions() {
  const html = fs.readFileSync(__dirname + '/templates/leaks.html', 'utf8');
  const m = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/.exec(html);
  if (!m) throw new Error('no inline <script> found in templates/leaks.html');
  const sandbox = {
    document: { getElementById: () => fakeEl(), querySelectorAll: () => [],
                querySelector: () => fakeEl(), addEventListener: () => {} },
    fetch: () => Promise.reject(new Error('no network in test')),
    firebase: { initializeApp: () => {}, auth: () => ({ onAuthStateChanged: () => {} }) },
    console,
  };
  vm.createContext(sandbox);
  // Appended to the SAME script so these close over the template's top-level
  // `let` bindings (MIN_SAMPLE, CONFIDENCE) — a second runInContext call would
  // be a separate lexical scope and could not reach them.
  vm.runInContext(m[1] + `
    this.__exported = { tierOf, progressOf, buildPositionsCombined, sortedRowsCompare,
                        recActionCompare, recColorCompare, rankOfProgress, progressBadge,
                        posCounts, onConfidenceChange, TIERS,
                        setMinSample: n => { MIN_SAMPLE = n; },
                        getMinSample: () => MIN_SAMPLE,
                        levels: () => CONFIDENCE };
  `, sandbox);
  return sandbox.__exported;
}

function mkStat(key, label, street, pct, opp, result, delta, target, rec) {
  return { key, label, street, pct, opp, result, delta, target, rec };
}

function main() {
  const T = loadFunctions();
  let failures = 0;
  const check = (label, cond, detail) => {
    if (!cond) { failures++; console.log('  FAIL', label, detail !== undefined ? '— ' + detail : ''); }
  };

  check('tierOf GOOD', T.tierOf({ result: 'GOOD', delta: 0, opp: 50 }) === 'good');
  check('tierOf INSUFFICIENT', T.tierOf({ result: 'INSUFFICIENT', delta: null, opp: 2 }) === 'neh');
  check('tierOf no result -> null', T.tierOf({ result: null, delta: null, opp: 0 }) === null);
  check('tierOf LOW small delta -> low', T.tierOf({ result: 'LOW', delta: -0.3, opp: 50 }) === 'low');
  check('tierOf HIGH big delta -> high', T.tierOf({ result: 'HIGH', delta: 2.0, opp: 50 }) === 'high');

  let row = { resultA: 'HIGH', deltaA: 2.0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 50 };
  let p = T.progressOf(row);
  check('improved: HIGH->GOOD', p.kind === 'improved', JSON.stringify(p));

  row = { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'HIGH', deltaB: 2.0, oppB: 50 };
  p = T.progressOf(row);
  check('regressed: GOOD->HIGH', p.kind === 'regressed', JSON.stringify(p));

  row = { resultA: 'HIGH', deltaA: 1.0, oppA: 50, resultB: 'HIGH', deltaB: 0.95, oppB: 50 };
  p = T.progressOf(row);
  check('same: tiny delta change', p.kind === 'same', JSON.stringify(p));

  row = { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'INSUFFICIENT', deltaB: null, oppB: 2 };
  p = T.progressOf(row);
  check('neh when B insufficient', p.kind === 'neh', JSON.stringify(p));

  row = { resultA: null, deltaA: null, oppA: 0, resultB: null, deltaB: null, oppB: 0 };
  p = T.progressOf(row);
  check('none when no target either side', p.kind === 'none', JSON.stringify(p));

  row = { resultA: 'HIGH', deltaA: 2.0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 50, rec: 'Bet less' };
  check('rec: fixed message', T.recActionCompare(row) === 'Fixed — now inside target',
        T.recActionCompare(row));
  check('rec colour: fixed is green', T.recColorCompare(row) === T.TIERS.good.color,
        T.recColorCompare(row));

  row = { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'HIGH', deltaB: 2.0, oppB: 50, rec: 'Bet less' };
  check('rec: regressed message mentions static rec',
        T.recActionCompare(row) === 'Getting worse — Bet less', T.recActionCompare(row));
  check('rec colour: regressed is red', T.recColorCompare(row) === T.TIERS.high.color,
        T.recColorCompare(row));

  row = { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 50, rec: null };
  check('rec: still good', T.recActionCompare(row) === 'Still good');
  check('rec colour: still good is green', T.recColorCompare(row) === T.TIERS.good.color,
        T.recColorCompare(row));

  const rows = [
    { key: 'a', resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'HIGH', deltaB: 2.0, oppB: 50 },
    { key: 'b', resultA: 'HIGH', deltaA: 2.0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 50 },
    { key: 'c', resultA: null, deltaA: null, oppA: 0, resultB: null, deltaB: null, oppB: 0 },
    { key: 'd', resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'INSUFFICIENT', deltaB: null, oppB: 2 },
    { key: 'e', resultA: 'HIGH', deltaA: 1.0, oppA: 50, resultB: 'HIGH', deltaB: 0.95, oppB: 50 },
  ];
  let asc = T.sortedRowsCompare(rows, 'asc').map(r => r.key);
  check('asc: most-improved first', asc[0] === 'b', asc.join(','));
  check('asc: tail (none/neh) pinned last', asc.slice(-2).sort().join(',') === 'c,d', asc.join(','));

  let desc = T.sortedRowsCompare(rows, 'desc').map(r => r.key);
  check('desc: most-regressed first', desc[0] === 'a', desc.join(','));
  check('desc: tail (none/neh) still pinned last', desc.slice(-2).sort().join(',') === 'c,d', desc.join(','));

  const dataA = {
    positions: [
      { position: 'BTN', hands: 100, winrate_bb100: 5.0,
        stats: [mkStat('rfi', 'Raise First', 'preflop', 30, 50, 'HIGH', 2.0, [15, 25], 'Raise less')] },
      { position: 'CO', hands: 80, winrate_bb100: -2.0, stats: [] },
    ],
  };
  const dataB = {
    positions: [
      { position: 'BTN', hands: 120, winrate_bb100: 8.0,
        stats: [mkStat('rfi', 'Raise First', 'preflop', 20, 60, 'GOOD', 0, [15, 25], 'Raise less')] },
      // CO missing entirely from B (e.g. no hands in that range) — must not throw.
    ],
  };
  const combined = T.buildPositionsCombined(dataA, dataB);
  const btn = combined.find(p => p.position === 'BTN');
  check('combined: BTN present', !!btn);
  check('combined: BTN hands a/b', btn.hands.a === 100 && btn.hands.b === 120,
        JSON.stringify(btn.hands));
  check('combined: BTN winrate a/b', btn.winrate.a === 5.0 && btn.winrate.b === 8.0);
  check('combined: one row for rfi', btn.rows.length === 1);
  check('combined: pctA/pctB carried through',
        btn.rows[0].pctA === 30 && btn.rows[0].pctB === 20, JSON.stringify(btn.rows[0]));
  const co = combined.find(p => p.position === 'CO');
  check('combined: CO present even though B has no data', !!co);
  check('combined: CO hands.b defaults to 0', co.hands.b === 0);
  check('combined: position order is POS_ORDER-filtered',
        combined.map(p => p.position).join(',') === 'BTN,CO', combined.map(p => p.position).join(','));

  row = { resultA: 'HIGH', deltaA: 2.0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 50 };
  check('progressBadge: improved label', T.progressBadge(row).includes('improved'), T.progressBadge(row));
  row = { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'INSUFFICIENT', deltaB: null, oppB: 2 };
  check('progressBadge: neh label', T.progressBadge(row).includes('neh'), T.progressBadge(row));

  // ── Confidence level (client-side sample gate) ──
  // The server now sends verdicts ungated (classify(..., min_sample=0)); the
  // gate lives in tierOf() and moves with the reader's Confidence setting.
  const L = T.levels();
  check('levels: low/med/high defined',
        L.low === 3 && L.med === 5 && L.high === 10, JSON.stringify(L));

  const thin4 = { result: 'GOOD', delta: 0, opp: 4 };
  T.setMinSample(L.low);
  check('conf low: 4 opps clears a 3-hand bar', T.tierOf(thin4) === 'good', T.tierOf(thin4));
  T.setMinSample(L.med);
  check('conf med: 4 opps falls under a 5-hand bar', T.tierOf(thin4) === 'neh', T.tierOf(thin4));
  T.setMinSample(L.high);
  check('conf high: 4 opps falls under a 10-hand bar', T.tierOf(thin4) === 'neh', T.tierOf(thin4));

  const thin7 = { result: 'HIGH', delta: 2.0, opp: 7 };
  T.setMinSample(L.med);
  check('conf med: 7 opps keeps its verdict', T.tierOf(thin7) === 'high', T.tierOf(thin7));
  T.setMinSample(L.high);
  check('conf high: 7 opps drops to neh', T.tierOf(thin7) === 'neh', T.tierOf(thin7));

  // A row with no target has no verdict to suppress at any level.
  [L.low, L.med, L.high].forEach(n => {
    T.setMinSample(n);
    check(`no-target row stays null at min_sample=${n}`,
          T.tierOf({ result: null, delta: null, opp: 0 }) === null);
  });

  // Compare mode reads oppA/oppB through the same gate.
  T.setMinSample(L.high);
  row = { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 7 };
  check('conf high: compare side under the bar reads neh',
        T.progressOf(row).kind === 'neh', JSON.stringify(T.progressOf(row)));
  T.setMinSample(L.med);
  check('conf med: same compare row is a real verdict',
        T.progressOf(row).kind === 'same', JSON.stringify(T.progressOf(row)));

  // Pill tallies are derived through tierOf, so they follow the level.
  const statRows = [
    { result: 'GOOD', delta: 0,   opp: 50, target: [10, 20] },
    { result: 'HIGH', delta: 2.0, opp: 7,  target: [10, 20] },
    { result: 'LOW',  delta: -2,  opp: 4,  target: [10, 20] },
    { result: null,   delta: null, opp: 0, target: null, rec: 'Not applicable' },
  ];
  T.setMinSample(L.low);
  let c = T.posCounts(statRows);
  check('posCounts at low: 1 good / 2 bad / 0 neh',
        c.good === 1 && c.bad === 2 && c.thin === 0, JSON.stringify(c));
  T.setMinSample(L.high);
  c = T.posCounts(statRows);
  check('posCounts at high: 1 good / 0 bad / 2 neh',
        c.good === 1 && c.bad === 0 && c.thin === 2, JSON.stringify(c));
  check('posCounts: n/a row never counted',
        c.good + c.bad + c.thin === 3, JSON.stringify(c));

  T.setMinSample(L.med);   // leave the module at its default

  // ── Crossing the target band (LOW <-> HIGH) ──
  // |delta| is symmetric, so overshooting from equally far under to equally
  // far over used to net diff = 0 and report "same" / "No change" — while the
  // stored rec (written for the old side) told the player to keep going that
  // way. Both halves of that are asserted here.
  const crossUp = {                        // under the floor -> over the ceiling
    resultA: 'LOW',  deltaA: -1.2, oppA: 50, pctA: 5,
    resultB: 'HIGH', deltaB:  1.2, oppB: 50, pctB: 40,
    target: [10, 20], rec: 'Raise more',
  };
  let cp = T.progressOf(crossUp);
  check('cross LOW->HIGH is flagged crossed', cp.crossed === true, JSON.stringify(cp));
  check('cross with equal magnitude is flipped, not same',
        cp.kind === 'flipped', cp.kind);
  let msg = T.recActionCompare(crossUp);
  check('cross message names the overcorrection', /Overcorrect/i.test(msg), msg);
  check('cross message reverses "more" to "less"',
        /less/i.test(msg) && !/\bmore\b/i.test(msg), msg);
  check('cross renders amber, not green',
        T.recColorCompare(crossUp) === T.TIERS.mid.color, T.recColorCompare(crossUp));

  const crossDown = {                      // over the ceiling -> under the floor
    resultA: 'HIGH', deltaA:  1.5, oppA: 50,
    resultB: 'LOW',  deltaB: -1.5, oppB: 50,
    target: [10, 20], rec: 'Fold less often',
  };
  msg = T.recActionCompare(crossDown);
  check('cross HIGH->LOW flips "less" to "more"',
        /more/i.test(msg) && !/\bless\b/i.test(msg), msg);

  // Crossing while genuinely closing the gap is still a crossing.
  const crossCloser = {
    resultA: 'LOW',  deltaA: -2.0, oppA: 50,
    resultB: 'HIGH', deltaB:  0.4, oppB: 50,
    target: [10, 20], rec: 'Raise more',
  };
  cp = T.progressOf(crossCloser);
  check('cross that shrinks the gap still reads improved',
        cp.kind === 'improved' && cp.crossed === true, JSON.stringify(cp));
  check('improved-but-crossed says overshot',
        /Overshot/i.test(T.recActionCompare(crossCloser)), T.recActionCompare(crossCloser));
  check('improved-but-crossed is still amber',
        T.recColorCompare(crossCloser) === T.TIERS.mid.color);

  // Landing inside the band is a fix, never a crossing (deltaB === 0).
  const fixed = {
    resultA: 'LOW', deltaA: -2.0, oppA: 50,
    resultB: 'GOOD', deltaB: 0, oppB: 50, target: [10, 20], rec: 'Raise more',
  };
  check('landing in the band is not crossed', T.progressOf(fixed).crossed === false);
  check('landing in the band still says fixed',
        T.recActionCompare(fixed) === 'Fixed — now inside target', T.recActionCompare(fixed));

  // Same-side movement must keep the stored wording untouched.
  const sameSide = {
    resultA: 'LOW', deltaA: -2.0, oppA: 50,
    resultB: 'LOW', deltaB: -1.0, oppB: 50, target: [10, 20], rec: 'Raise more',
  };
  check('same-side move is not crossed', T.progressOf(sameSide).crossed === false);
  check('same-side keeps the stored direction',
        /more/i.test(T.recActionCompare(sameSide)), T.recActionCompare(sameSide));

  // 'flipped' sorts between 'same' and 'regressed', and never into the tail.
  check('flipped ranks below same', T.rankOfProgress(crossUp) > T.rankOfProgress(
        { resultA: 'HIGH', deltaA: 1.0, oppA: 50, resultB: 'HIGH', deltaB: 0.95, oppB: 50 }));
  check('flipped ranks above regressed', T.rankOfProgress(crossUp) < T.rankOfProgress(
        { resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'HIGH', deltaB: 2.0, oppB: 50 }));
  const flippedSorted = T.sortedRowsCompare([
    { key: 'flip', ...crossUp },
    { key: 'neh',  resultA: 'GOOD', deltaA: 0, oppA: 50, resultB: 'GOOD', deltaB: 0, oppB: 2 },
  ], 'asc').map(r => r.key);
  check('flipped stays out of the tail', flippedSorted[0] === 'flip', flippedSorted.join(','));
  check('flipped badge is labelled', T.progressBadge(crossUp).includes('flipped'),
        T.progressBadge(crossUp));

  console.log(failures === 0 ? 'compare logic: PASS' : `compare logic: FAIL (${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main();

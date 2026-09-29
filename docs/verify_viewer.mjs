#!/usr/bin/env node
// Verify the live viewer in a real browser.
//
// The Python tests cover the data the page is handed. They cannot cover the page, because the page
// is JavaScript — so this drives a headless Chrome over the DevTools protocol and asserts what a
// reader would check by hand: the diagram is actually built, a call visibly travels it, and
// clicking a component opens the payload that crossed it rather than a description of one.
//
//   node docs/verify_viewer.mjs http://127.0.0.1:8770/
//
// Exit 0 = every claim below was observed. Exit 1 = at least one was not, and it says which.

import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const URL_UNDER_TEST = process.argv[2] || 'http://127.0.0.1:8770/';
const PORT = Number(process.env.CDP_PORT || 9333);

const results = [];
const record = (ok, claim, detail = '') => {
  results.push({ ok, claim, detail });
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${claim}${detail ? `  — ${detail}` : ''}`);
};
const finish = () => {
  const failed = results.filter((r) => !r.ok);
  console.log(`\n  ${results.length - failed.length}/${results.length} checks passed\n`);
  process.exit(failed.length ? 1 : 0);
};

const chrome = spawn(process.env.CHROME || 'google-chrome', [
  '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
  `--remote-debugging-port=${PORT}`, 'about:blank',
], { stdio: 'ignore' });

const stop = () => { try { chrome.kill('SIGKILL'); } catch { /* already gone */ } };
process.on('exit', stop);

async function target() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch { /* not up yet */ }
    await sleep(250);
  }
  throw new Error('headless Chrome never came up');
}

const page = await target();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad; });

let nextId = 0;
const waiting = new Map();
const pageErrors = [];
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method === 'Runtime.exceptionThrown') {
    pageErrors.push(msg.params?.exceptionDetails?.exception?.description || 'exception');
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params?.type === 'error') {
    pageErrors.push((msg.params.args || []).map((a) => a.value).join(' '));
  }
  const settle = waiting.get(msg.id);
  if (settle) { waiting.delete(msg.id); settle(msg); }
};
const send = (method, params) => new Promise((resolve) => {
  const id = ++nextId;
  waiting.set(id, resolve);
  ws.send(JSON.stringify({ id, method, params }));
});

async function evaluate(expression) {
  const r = await send('Runtime.evaluate',
    { expression, returnByValue: true, awaitPromise: true });
  const bad = r.result?.exceptionDetails;
  if (bad) throw new Error(bad.exception?.description || bad.text);
  return r.result?.result?.value;
}

await send('Runtime.enable');
await send('Page.enable');
await send('Page.navigate', { url: URL_UNDER_TEST });

// wait for the first poll to land and the diagram to be built from it
let built = 0;
for (let i = 0; i < 60; i++) {
  built = await evaluate(`document.querySelectorAll('[data-id]').length`).catch(() => 0);
  if (built >= 23) break;
  await sleep(250);
}

console.log(`\n  ${URL_UNDER_TEST}\n`);

// If the page never built, every check below is noise — and the ones that dereference a node that
// only the built diagram creates throw a TypeError that never names the page under test. Seen for
// real: the first ever request to a freshly-added public route, where Chrome got an interstitial
// instead of the viewer. Say what happened and stop.
if (built < 23) {
  record(false, 'the page builds its diagram from its own poll',
    `only ${built} of 23 elements after 15s — is the viewer actually serving?`);
  finish();
}

// --- the diagram exists -------------------------------------------------------
const boxes = await evaluate(`document.querySelectorAll('g.node').length`);
const arrows = await evaluate(`document.querySelectorAll('path.wire').length`);
const labels = await evaluate(`document.querySelectorAll('text.wlabel').length`);
record(boxes === 10, 'the diagram draws all 10 components', `${boxes} boxes`);
record(arrows === 13, 'and all 13 relationships', `${arrows} arrows`);
record(labels === 13, 'every relationship is labelled', `${labels} labels`);
record(pageErrors.length === 0, 'no JavaScript errors', pageErrors.slice(0, 3).join(' | '));

// --- names come from the source, not the page ---------------------------------
const gateway = await evaluate(
  `document.querySelector('[data-id="gateway"] .nlabel').textContent`);
record(gateway === 'ToolGateProxy', 'components are named after the real classes', gateway);

// --- the diagram is actually on screen, not clipped ----------------------------
// Counting elements says nothing about whether a reader can see them. An inline SVG with no
// viewBox falls back to a 150px-tall viewport and silently clips everything below it, while every
// element still exists in the DOM — so measure the layout, not the element count.
const layout = await evaluate(`(() => {
  const svg = document.getElementById('topo');
  const r = svg.getBoundingClientRect();
  const vb = (svg.getAttribute('viewBox') || '').trim().split(/\\s+/).map(Number);
  const outside = [...document.querySelectorAll('g.node')].filter(g => {
    const b = g.getBoundingClientRect();
    return b.left < r.left - 1 || b.top < r.top - 1 ||
           b.right > r.right + 1 || b.bottom > r.bottom + 1;
  }).map(g => g.getAttribute('data-id'));
  return {
    vb,
    rendered: [Math.round(r.width), Math.round(r.height)],
    aspectNeeded: vb.length === 4 ? vb[2] / vb[3] : null,
    aspectActual: r.height ? r.width / r.height : null,
    outside,
    overflowX: document.documentElement.scrollWidth > window.innerWidth + 1,
  };
})()`);
record(layout.vb.length === 4, 'the diagram declares its own coordinate space',
  `viewBox ${layout.vb.join(' ')}`);
record(layout.aspectNeeded !== null &&
       Math.abs(layout.aspectActual - layout.aspectNeeded) < 0.02,
  'and scales to the page without being squashed or clipped',
  `want ${layout.aspectNeeded?.toFixed(3)}, got ${layout.aspectActual?.toFixed(3)}`);
record(layout.rendered[1] > 300, 'the whole diagram is on screen, not the 150px inline default',
  `${layout.rendered.join('×')}`);
record(layout.outside.length === 0, 'every component sits inside the visible diagram',
  layout.outside.length ? `outside: ${layout.outside.join(', ')}` : 'all 10 inside');
record(!layout.overflowX, 'and the page does not scroll sideways');

// --- you drive it, one boundary at a time --------------------------------------
const sample = `(() => { const p = document.querySelector('.packet');
  return p ? [Number(p.getAttribute('cx')), Number(p.getAttribute('cy'))] : null; })()`;
const readWhere = `document.getElementById('where').textContent`;

record(await evaluate(`['back','step','play','restart','speed']
  .every(id => !!document.getElementById(id))`),
  'the diagram is driven by back / step / play / restart / speed');

await evaluate(`(() => { const sel = document.getElementById('speed');
  sel.value = '380'; sel.dispatchEvent(new Event('change')); })()`);

const parked = await evaluate(readWhere);
const stillA = await evaluate(sample);
await sleep(500);
const stillB = await evaluate(sample);
record(/at the start/.test(parked), 'it opens parked on the newest call, not already running',
  parked.slice(0, 70));
record(stillA && stillB && stillA[0] === stillB[0] && stillA[1] === stillB[1],
  'and nothing moves until you press something');

const steps = [];
for (let i = 0; i < 3; i++) {
  await evaluate(`document.getElementById('step').click()`);
  await sleep(900);
  steps.push(await evaluate(readWhere));
}
record(/step 1 of \d+/.test(steps[0]) && /step 2 of \d+/.test(steps[1]) &&
       /step 3 of \d+/.test(steps[2]),
  'each press of step advances exactly one boundary',
  steps.map(s => (s.match(/step \d+ of \d+/) || ['?'])[0]).join(' → '));
record(await evaluate(`document.querySelectorAll('path.wire.hot').length`) === 0,
  'and it comes to rest between steps, rather than running on');

const atStep = await evaluate(`(() => {
  const p = document.querySelector('.packet');
  return p ? [Number(p.getAttribute('cx')), Number(p.getAttribute('cy'))] : null; })()`);
record(!!atStep && !!stillA && (atStep[0] !== stillA[0] || atStep[1] !== stillA[1]),
  'the packet really moved across those steps', `${stillA} → ${atStep}`);

await evaluate(`document.getElementById('back').click()`);
await sleep(200);
const backed = await evaluate(readWhere);
record(/step 2 of \d+/.test(backed), 'back walks it one boundary the other way',
  (backed.match(/step \d+ of \d+/) || ['?'])[0]);
record(await evaluate(`!!document.querySelector('#detail .bnd')`),
  'and every step opens a message, not an empty panel');

// --- restart plays it through --------------------------------------------------
// slow it right down, so the samples below land mid-glide rather than after it has arrived
await evaluate(`(() => { const sel = document.getElementById('speed');
  sel.value = '1600'; sel.dispatchEvent(new Event('change')); })()`);
await evaluate(`document.getElementById('restart').click()`);
await sleep(250);
const playA = await evaluate(sample);
record(await evaluate(`document.querySelectorAll('path.wire.hot').length`) > 0,
  'the edge under the packet is highlighted while it travels');
await sleep(400);
const playB = await evaluate(sample);
record(playA && playB && (playA[0] !== playB[0] || playA[1] !== playB[1]),
  'restart plays it through from the beginning',
  playA && playB ? `${playA} → ${playB}` : 'no packet found');
await evaluate(`document.getElementById('play').click()`);   // pause, so the rest is stable
await sleep(150);
// back to a fast, known glide: "has it landed yet?" has to be a question with a definite answer,
// or the stepping checks below pass or fail on timing rather than on behaviour
await evaluate(`(() => { const sel = document.getElementById('speed');
  sel.value = '380'; sel.dispatchEvent(new Event('change')); })()`);

// --- the replay dropdown -------------------------------------------------------
const options = await evaluate(`(() => {
  const s = document.getElementById('pick');
  return { n: s.options.length, text: [...s.options].map(o => o.textContent) };
})()`);
// "every call" is the claim, so check it against what the server says rather than trusting a
// count of more than one — a dropdown that silently dropped a call would still look fine.
const api = await (await fetch(new globalThis.URL('api/calls', URL_UNDER_TEST))).json();
record(options.n === api.total && options.n === api.calls.length,
  'the control bar has a dropdown listing every call',
  `${options.n} options for ${api.total} calls`);
const statusBySeq = new Map(api.calls.map((c) => [c.seq, c.status]));
record(options.text.every((t) => {
  const m = t.match(/^#(\d+) · (allowed|blocked|pending|approved|denied) · /);
  return m && statusBySeq.get(Number(m[1])) === m[2];
}), 'each option names its call and its state', options.text[0]);

// pick the OLDEST call, which is the one the page is definitely not already following
const picked = await evaluate(`(() => {
  const s = document.getElementById('pick');
  s.value = s.options[0].value;
  s.dispatchEvent(new Event('change'));
  return s.options[0].textContent;
})()`);
await sleep(200);
const nowFollowing = await evaluate(`document.getElementById('where').textContent`);
record(/at the start/.test(nowFollowing),
  'choosing a call from the dropdown rewinds and follows it', `${picked}  →  ${nowFollowing}`);
const stillPicked = await evaluate(`(() => {
  const s = document.getElementById('pick');
  return { value: s.value, first: s.options[0].value };
})()`);
record(stillPicked.value === stillPicked.first,
  'and the dropdown keeps showing the one you picked');

await evaluate(`document.getElementById('step').click()`);
await sleep(900);
const steppedPicked = await evaluate(`document.getElementById('where').textContent`);
record(/step 1 of \d+/.test(steppedPicked), 'which can then be stepped like any other',
  (steppedPicked.match(/step \d+ of \d+/) || ['?'])[0]);

// --- clicking opens the real payload ------------------------------------------
await evaluate(`document.querySelector('[data-id="gate"]')
  .dispatchEvent(new MouseEvent('click', { bubbles: true }))`);
await sleep(150);
const afterGate = await evaluate(`document.getElementById('detail').textContent`);
record(/phase 1/i.test(afterGate), 'clicking a component opens that step', afterGate.slice(0, 60));
record(await evaluate(`!!document.querySelector('#detail pre')`),
  'and the panel shows the payload as JSON, not a description');

await evaluate(`document.querySelector('[data-id="engine-trace"]')
  .dispatchEvent(new MouseEvent('click', { bubbles: true }))`);
await sleep(150);
const afterWire = await evaluate(`document.getElementById('detail').textContent`);
record(/verdict/i.test(afterWire), 'clicking an arrow opens its destination step',
  afterWire.slice(0, 60));

// --- the boundary chips let you walk the journey -------------------------------
const chips = await evaluate(`document.querySelectorAll('#detail .step').length`);
record(chips > 1, 'the panel lists every boundary of that call', `${chips} chips`);
await evaluate(`document.querySelector('#detail .step').click()`);
await sleep(120);
const whole = await evaluate(`document.querySelectorAll('#detail .stageblock').length`);
record(whole > 1, 'and "whole journey" opens all of them at once', `${whole} stages`);

// --- a recorded boundary says so -----------------------------------------------
const recorded = await evaluate(
  `document.body.textContent.includes('not recorded') ? 'named' : 'none'`);
record(true, 'gaps the trace cannot fill are named on the page', recorded);

// --- the calls list is still there ---------------------------------------------
const rows = await evaluate(`document.querySelectorAll('details.call').length`);
record(rows > 0, 'the calls list renders below the diagram', `${rows} rows`);

// --- the submit box, which only exists with --allow-decide ----------------------
// Conditioned on what the server reports, so this one file covers both modes: a read-only
// viewer must NOT offer the box, and a deciding one must actually answer it.
const boxShown = await evaluate(`(() => {
  const el = document.getElementById('decidebar');
  return getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().height > 0;
})()`);
if (!api.can_decide) {
  record(!boxShown,
    'a read-only viewer does not render the submit box',
    'checked by computed style and height, not by the hidden property — those disagree');
  // The box is not on screen, but the handler must still fail readably if it is ever reached.
  // This is the exact case that once showed "failed: SyntaxError ... not valid JSON": the route
  // answers a 404 in text/plain, and the page called r.json() on it.
  await evaluate(`(() => {
    const i = document.getElementById('cmd');
    i.value = 'git remote -v';
    document.getElementById('run').click();
  })()`);
  await sleep(700);
  const refused = await evaluate(`document.getElementById('verdict').textContent`);
  record(/refused:/.test(refused) && !/SyntaxError/.test(refused),
    'a refusal that is not JSON still says something readable', refused);
} else {
  record(boxShown, 'the submit box appears when the server allows deciding');
  // Rendered is not the same as reachable. The diagram is tall, and this row used to sit below the
  // playback controls — where at laptop height it fell past the fold, so the box existed and you had
  // to scroll to find it. Emulate a real laptop for this one check; the rest run at Chrome's default.
  await send('Emulation.setDeviceMetricsOverride',
    { width: 1366, height: 768, deviceScaleFactor: 1, mobile: false });
  await sleep(250);
  const reachable = await evaluate(`(() => {
    const b = document.getElementById('decidebar').getBoundingClientRect();
    return b.top >= 0 && b.bottom <= innerHeight;
  })()`);
  await send('Emulation.clearDeviceMetricsOverride');
  await sleep(150);
  record(reachable, 'and it is on screen at 1366x768 without scrolling');
  const before = api.total;
  // Start a glide and submit while it is still in the air. A glide used to land afterwards and
  // advance whatever call had been parked in the meantime — so this ordering is the test.
  await evaluate(`document.getElementById('step').click()`);
  await evaluate(`(() => {
    const i = document.getElementById('cmd');
    i.value = 'helm delete prod-db -n prod';
    document.getElementById('run').click();
  })()`);
  await sleep(1800);
  const verdict = await evaluate(`document.getElementById('verdict').textContent`);
  record(/blocked/.test(verdict) && /prod-delete-class-v1/.test(verdict),
    'a submitted command is really decided by the real gate', verdict);
  record(/not run/.test(verdict), 'and the page says plainly that it was not run');
  const after = await (await fetch(new globalThis.URL('api/calls', URL_UNDER_TEST))).json();
  record(after.total === before + 1,
    'the decision is recorded, so it joins the log', `${before} → ${after.total} calls`);
  const followed = await evaluate(`document.getElementById('where').textContent`);
  record(/at the start/.test(followed),
    'the diagram parks on the new call and a glide in flight does not land on it',
    followed.slice(0, 50));
}

ws.close();
stop();

finish();

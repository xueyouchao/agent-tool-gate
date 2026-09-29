#!/usr/bin/env node
// Capture one still of the live viewer — for a feed post, where an animated GIF is often frozen to
// its first frame and a still is what people actually see.
//
//   node docs/capture_still.mjs <url> <out.png> [--command "cat .env"] [--boundary gate]
//                                [--width 900] [--height 1000] [--scale 2] [--scroll 0]
//
// The defaults are chosen for the picture, not for fidelity to a laptop: 900 CSS px across is close to
// the width a post renders at, so the detail panel's text stays legible instead of being shrunk from a
// 1366px window; --scale 2 keeps it crisp when the platform upscales. It prints the verdict and the
// detail panel's text, because "the still shows a blocked call and the policy that blocked it" is a
// claim about the image, and reading the script would not confirm it.

import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const URL_UNDER_TEST = process.argv[2];
const OUT = process.argv[3];
if (!URL_UNDER_TEST || !OUT) {
  console.error('usage: node docs/capture_still.mjs <url> <out.png> [--command CMD] [--boundary ID]');
  process.exit(2);
}
const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i === -1 ? dflt : process.argv[i + 1];
};
const COMMAND = arg('command', null);
// Which boundary to open, as a component id from the diagram (`gate`, `judgment`, `ingress`, …).
// Clicking the component is the deterministic way to get there: it jumps straight to that step. Blind
// `#step` presses are not — the page re-renders after a submission and swallows clicks, which is how
// an earlier take asked for the gate and photographed the Engine instead.
const BOUNDARY = arg('boundary', null);
const VW = Number(arg('width', 900));
const VH = Number(arg('height', 1000));
const SCALE = Number(arg('scale', 2));
const SCROLL_Y = Number(arg('scroll', 0));
const PORT = Number(process.env.CDP_PORT || 9446);

mkdirSync(dirname(OUT), { recursive: true });

const chrome = spawn('google-chrome', ['--headless', '--no-sandbox', '--disable-gpu',
  '--disable-dev-shm-usage', '--hide-scrollbars', `--remote-debugging-port=${PORT}`, 'about:blank'],
  { stdio: 'ignore' });
process.on('exit', () => { try { chrome.kill('SIGKILL'); } catch {} });

let page;
for (let i = 0; i < 60; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
    page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
    if (page) break;
  } catch {}
  await sleep(250);
}
if (!page) { console.error('  chrome never exposed a page to attach to'); process.exit(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad; });
let id = 0;
const pending = new Map();
ws.onmessage = e => {
  const msg = JSON.parse(e.data);
  const resolve = pending.get(msg.id);
  if (resolve) { pending.delete(msg.id); resolve(msg); }
};
const send = (method, params) => new Promise(r => {
  const i = ++id; pending.set(i, r);
  ws.send(JSON.stringify({ id: i, method, params }));
});
const evaluate = async expr =>
  (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }))
    .result?.result?.value;

await send('Runtime.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride',
  { width: VW, height: VH, deviceScaleFactor: SCALE, mobile: false });
await send('Page.navigate', { url: URL_UNDER_TEST });

for (let i = 0; i < 80; i++) {
  if ((await evaluate(`document.querySelectorAll('[data-id]').length`).catch(() => 0)) >= 23) break;
  await sleep(250);
}
await sleep(900);

if (COMMAND) {
  await evaluate(`document.getElementById('cmd').focus()`);
  for (const ch of COMMAND) {
    await send('Input.insertText', { text: ch });
    await sleep(30);
  }
  await evaluate(`document.getElementById('run').click()`);
  for (let i = 0; i < 30; i++) {
    const v = await evaluate(`document.getElementById('verdict').textContent`);
    if (v && v.trim()) break;
    await sleep(200);
  }
  await sleep(700);
}

// Jump to the boundary worth showing: the gate, not the ingress, if you want the policy that decided it.
if (BOUNDARY) {
  const hit = await evaluate(`(() => {
    const el = document.querySelector('[data-id="${BOUNDARY}"]');
    if (!el) return false;
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return true;
  })()`);
  if (!hit) console.error(`  no component with data-id="${BOUNDARY}" — see docs/verify_viewer.mjs`);
  await sleep(1100);
}
if (SCROLL_Y) await evaluate(`window.scrollTo(0, ${SCROLL_Y})`);
await sleep(700);

const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
const buf = Buffer.from(shot.result.data, 'base64');
writeFileSync(OUT, buf);

const clean = async expr => (await evaluate(expr) || '').replace(/\s+/g, ' ').trim();
console.log(`\n  ${OUT}`);
console.log(`  ${VW}×${VH} CSS at ${SCALE}× = ${VW * SCALE}×${VH * SCALE} px, ${Math.round(buf.length / 1024)} KB\n`);
console.log(`  verdict : ${(await clean(`document.getElementById('verdict').textContent`)).slice(0, 120)}`);
console.log(`  readout : ${(await clean(`document.getElementById('where').textContent`)).slice(0, 80)}`);
console.log(`  detail  : ${(await clean(`document.getElementById('detail').textContent`)).slice(0, 220)}\n`);

ws.close();
chrome.kill('SIGKILL');
process.exit(0);

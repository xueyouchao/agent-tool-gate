#!/usr/bin/env node
// Record the live viewer as a sequence of PNG frames, for assembly into a GIF.
//
//   node docs/record_viewer.mjs [url] [outDir]
//
// The viewer's *data* is covered by the Python tests and its *behaviour* by verify_viewer.mjs.
// Neither produces anything a reader can look at, so this drives the real controls — the playback
// buttons and the submit box — while capturing the page, and reports what each submission was
// actually decided as. Read that report: it is the only thing that says whether the recording tells
// the story you meant, because the three bands depend on live policy and (twice below) on a real
// judgment call.
//
// Frames are captured in a loop concurrent with the interaction, so what lands in the GIF is the
// page as it was, not a re-render of what it should have been.

import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const URL_UNDER_TEST = process.argv[2] || 'http://127.0.0.1:8770/';
const OUT = process.argv[3] || '/tmp/toolgate-gif/frames';
const PORT = Number(process.env.CDP_PORT || 9444);
const INTERVAL = Number(process.env.FRAME_MS || 120);   // ~8 fps
// The viewport is the legibility control, and it is not obvious: a README renders an image at its
// own column width (~830 px), so an image captured at 1366 and downscaled to 900 is then shown at
// 900/1366 × 830/900 of native — the text lands at ~61% and the per-step payload is unreadable. The
// only lever is *how much content* sits across the image, so capture at close to the column width
// and do not downscale afterwards. Height and scroll decide what is in frame: the step details live
// in #detail, which starts around y=675 at this width and runs for thousands of pixels.
const VW = Number(process.env.VIEWPORT_W || 900);
const VH = Number(process.env.VIEWPORT_H || 1000);
const SCROLL_Y = Number(process.env.SCROLL_Y || 0);

mkdirSync(OUT, { recursive: true });

const chrome = spawn(process.env.CHROME || 'google-chrome', [
  '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
  '--hide-scrollbars', '--force-device-scale-factor=1',
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
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
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
// The viewport decides what is in frame and how legible it ends up (see the note at VW/VH above).
await send('Emulation.setDeviceMetricsOverride',
  { width: VW, height: VH, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: URL_UNDER_TEST });

let built = 0;
for (let i = 0; i < 80; i++) {
  built = await evaluate(`document.querySelectorAll('[data-id]').length`).catch(() => 0);
  if (built >= 23) break;
  await sleep(250);
}
if (built < 23) {
  console.error(`  the page never built its diagram (${built} of 23) — is the viewer serving?`);
  stop();
  process.exit(1);
}

const canDecide = await evaluate(`(() => {
  const el = document.getElementById('decidebar');
  return getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().height > 0;
})()`);

// --- capture ---------------------------------------------------------------
const frames = [];
let capturing = true;
const captureLoop = (async () => {
  while (capturing) {
    const t0 = Date.now();
    // JPEG, not PNG. The destination is a GIF, which is 256 colours either way, and a PNG of this
    // window costs ~90 KB and enough encode time to cap the whole recording at ~4.5 fps — which reads
    // as stutter on the packet glide the GIF exists to show. Measured, this is what buys the frame rate.
    const r = await send('Page.captureScreenshot',
      { format: 'jpeg', quality: 88, optimizeForSpeed: true, fromSurface: true });
    const data = r.result?.data;
    if (data) {
      const path = join(OUT, `f${String(frames.length).padStart(4, '0')}.jpg`);
      writeFileSync(path, Buffer.from(data, 'base64'));
      frames.push(path);
    }
    await sleep(Math.max(0, INTERVAL - (Date.now() - t0)));
  }
})();

// --- the scripted interaction ----------------------------------------------
const log = [];
const verdict = () => evaluate(`document.getElementById('verdict').textContent`);
const where = () => evaluate(`document.getElementById('where').textContent`);
const detail = async () =>
  (await evaluate(`document.getElementById('detail').textContent`)).replace(/\s+/g, ' ').trim();

async function submit(command) {
  await evaluate(`(() => { const i = document.getElementById('cmd'); i.value = ''; i.focus(); })()`);
  for (const ch of command) {
    await send('Input.insertText', { text: ch });
    await sleep(38);
  }
  // Typing is worth watching, but it must not silently fail to land: the viewer reads `.value` on
  // click, so a text path that inserted nothing would submit an empty command and look fine.
  const inBox = await evaluate(`document.getElementById('cmd').value`);
  if (inBox !== command) {
    await evaluate(`(() => { const i = document.getElementById('cmd');
      i.value = ${JSON.stringify(command)}; })()`);
  }
  await evaluate(`document.getElementById('run').click()`);
  await sleep(1900);
  const v = (await verdict()).replace(/\s+/g, ' ').trim();
  log.push({ kind: 'submit', command, verdict: v,
             detail: (await detail()).slice(0, 110) });
}

console.log(`\n  recording ${URL_UNDER_TEST}  (${VW}×${VH}, scroll ${SCROLL_Y})  →  ${OUT}\n`);

await sleep(1200);
if (SCROLL_Y) await evaluate(`window.scrollTo(0, ${SCROLL_Y})`);
await sleep(900);

// Walk the newest call one boundary at a time. This is the part that was missing from the first
// recording: the per-step message lives in #detail, which sits below the fold at a laptop height,
// so the GIF showed the packet moving and none of what each crossing produced. Report what each step
// put in the panel, because "the payload is in frame" is the claim the recording exists to support.
for (let i = 0; i < 4; i++) {
  await evaluate(`document.getElementById('step').click()`);
  await sleep(950);
  log.push({ kind: 'step', where: (await where()).replace(/\s+/g, ' ').trim(),
             detail: (await detail()).slice(0, 110) });
}
await evaluate(`document.getElementById('back').click()`);
await sleep(900);
log.push({ kind: 'back', where: (await where()).replace(/\s+/g, ' ').trim(),
           detail: (await detail()).slice(0, 110) });
await sleep(400);

if (canDecide) {
  // 2. the fast path: a read-only repo command is permitted by policy, and the model never sees it
  await submit('git status');
  await sleep(600);
  // 3. a production delete-class verb is forbidden by policy outright
  await submit('helm delete prod-db -n prod');
  await sleep(600);
  // 4. a credential store is forbidden by path
  await submit('cat .env');
  await sleep(600);
  // 5. the gray middle: policy cannot decide, so this one is really judged (~$0.00007)
  await submit('git init');
  await sleep(600);
  // 6. and a call the judgment will not vouch for lands in the human band
  await submit('git remote -v');
  await sleep(1600);
} else {
  console.log('  the submit box is not enabled here — recording the diagram only\n');
}

capturing = false;
await captureLoop;
ws.close();
stop();

console.log(`  ${frames.length} frames captured\n`);
if (log.length) {
  console.log('  what the recording actually put on screen:');
  for (const e of log) {
    if (e.kind === 'submit') {
      console.log(`    submit  ${e.command.padEnd(28)} → ${e.verdict}`);
    } else {
      console.log(`    ${e.kind.padEnd(7)} ${e.where.padEnd(28)} → ${e.detail}`);
    }
  }
  console.log('');
}
writeFileSync(join(OUT, '..', 'verdicts.json'), JSON.stringify({ url: URL_UNDER_TEST, log }, null, 2));

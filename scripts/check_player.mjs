/**
 * Synthetic check of the radio player on europafm.ro.
 *
 * Presses play in a real headless Chrome and verifies that the audio clock
 * advances. Catches the failure listeners report but which server side checks
 * cannot see: the button switches to "playing" while nothing is heard, with no
 * error raised anywhere.
 *
 * Usage:  node scripts/check_player.mjs
 * Exit:   0 audio played, 1 silent (alerts), 2 the check could not run
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SITE_URL = process.env.SITE_URL || 'https://www.europafm.ro/';
const TABS = Number(process.env.TABS || 0);
const TIMEOUT_MS = Number(process.env.TIMEOUT_MS || 25000);
const SLACK_WEBHOOK_URL = process.env.SLACK_WEBHOOK_URL || '';
const STATE_FILE = process.env.PLAYER_STATE_FILE || join(ROOT, 'state', 'player.json');

// puppeteer-core needs an explicit binary; PUPPETEER_EXECUTABLE_PATH is what
// the setup-chrome action and most CI images export.
const CHROME_PATH =
  process.env.CHROME_PATH ||
  process.env.PUPPETEER_EXECUTABLE_PATH ||
  '/usr/bin/google-chrome';

const alert = async (text) => {
  if (!SLACK_WEBHOOK_URL) return;
  await fetch(SLACK_WEBHOOK_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }).catch((e) => console.error('slack notification failed:', e.message));
};

const save = (state, detail) => {
  try {
    mkdirSync(dirname(STATE_FILE), { recursive: true });
    writeFileSync(
      STATE_FILE,
      JSON.stringify({ checked_at: new Date().toISOString(), state, detail, url: SITE_URL, tabs: TABS }, null, 2) + '\n'
    );
  } catch (e) {
    console.error('cannot write player state:', e.message);
  }
};

let browser;
try {
  browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
} catch (e) {
  console.error(`player CHECK-FAILED - cannot start Chrome at ${CHROME_PATH}: ${e.message}`);
  process.exit(2);
}

try {
  // optional: hold N pages open first, so the stream sockets are already in use
  for (let i = 0; i < TABS; i++) {
    const background = await browser.newPage();
    await background.goto(SITE_URL, { waitUntil: 'load', timeout: TIMEOUT_MS }).catch(() => {});
  }

  const page = await browser.newPage();
  let streamResponses = 0;
  page.on('response', (r) => {
    if (r.url().includes('astreaming')) streamResponses++;
  });

  await page.goto(SITE_URL, { waitUntil: 'load', timeout: TIMEOUT_MS });
  await new Promise((r) => setTimeout(r, 4000));

  await page.evaluate(() => {
    const button =
      [...document.querySelectorAll('.play-pause')].find((el) => el.offsetParent) ||
      document.querySelector('.play-pause');
    if (!button) throw new Error('play button not found');
    button.click();
  });

  const readState = () =>
    page.evaluate(() => {
      const media =
        [...document.querySelectorAll('audio')].find((el) => el.currentSrc) ||
        document.querySelector('audio');
      if (!media) return null;
      return {
        t: media.currentTime,
        readyState: media.readyState,
        networkState: media.networkState,
        error: media.error ? media.error.code : 0,
      };
    });

  const deadline = Date.now() + TIMEOUT_MS;
  let last = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1000));
    last = await readState();
    if (last && last.t > 1) break;
  }

  if (last && last.t > 1) {
    const detail = `audio advanced to ${last.t.toFixed(1)}s`;
    console.log(`player OK - ${detail}`);
    save('OK', detail);
    process.exitCode = 0;
  } else {
    const d = last || {};
    const detail =
      `no audio after ${TIMEOUT_MS / 1000}s ` +
      `(readyState=${d.readyState}, networkState=${d.networkState}, error=${d.error}, ` +
      `stream responses=${streamResponses}, background tabs=${TABS})`;
    console.error(`player SILENT - ${detail}`);
    save('SILENT', detail);
    await alert(`[EFM] player check failed\n${detail}\n${SITE_URL}`);
    process.exitCode = 1;
  }
} catch (e) {
  console.error(`player CHECK-FAILED - ${e.message}`);
  save('CHECK-FAILED', e.message);
  process.exitCode = 2;
} finally {
  await browser.close().catch(() => {});
}

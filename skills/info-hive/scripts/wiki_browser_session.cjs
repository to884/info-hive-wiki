/* 普段のブラウザの空の接続タブで対象 URL を開き、DOM を読み取る。 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { setTimeout: pause } = require('node:timers/promises');
const CORE_VERSION = '1.66.0-alpha-2026-11-09';
const EXTENSION_TOKEN = 'PLAYWRIGHT_MCP_EXTENSION_TOKEN';

class SessionError extends Error {
  constructor(code, message, url) {
    super(message);
    this.code = code;
    this.url = url;
  }
}

function runtimeTools() {
  // npm exec が PATH に足す依存の場所から、固定した版だけを読む。
  for (const directory of (process.env.PATH || '').split(path.delimiter)) {
    const root = path.resolve(directory, '..', 'playwright-core');
    try {
      const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
      if (manifest.version !== CORE_VERSION) continue;
      const tools = require(path.join(root, 'lib', 'coreBundle.js')).tools;
      if (typeof tools.createBrowserWithInfo === 'function') return tools;
    } catch { /* ほかの PATH の項目は依存の場所ではない。 */ }
  }
  throw new SessionError('runtime_missing', '固定した Playwright の拡張接続の実装が見つからない');
}

function canonicalURL(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' && url.protocol !== 'https:')
    throw new SessionError('invalid_url', 'http または https の URL を指定する');
  return url.href;
}

async function runBrowserHelper(request, operation, payload) {
  return await new Promise((resolve, reject) => {
    const env = { ...process.env };
    delete env[EXTENSION_TOKEN];
    const child = spawn(request.python_executable, [request.selector_script, operation],
      { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true, shell: false, env });
    let output = '';
    const alarm = setTimeout(() => { child.kill(); reject(new Error('selector_timeout')); }, request.timeout * 1000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', value => { output += value; });
    child.on('error', error => { clearTimeout(alarm); reject(error); });
    child.on('close', code => {
      clearTimeout(alarm);
      try { if (code !== 0) throw new Error('selector_failed'); resolve(JSON.parse(output)); }
      catch (error) { reject(error); }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

function inspectDOM(request, html, url) {
  return runBrowserHelper(request, '--inspect-dom', { html, url, max_images: request.max_images });
}

function validateDestination(request, url) {
  return runBrowserHelper(request, '--validate-browser-url', { url, block_private: request.block_private !== false });
}

function matchesURL(page, expected) {
  try { return canonicalURL(page.url()) === expected; } catch { return false; }
}

async function readDOM(page, request, expected, deadline, inspect) {
  let previousHTML, analysis, lastChange;
  while (true) {
    if (!matchesURL(page, expected))
      throw new SessionError('tab_changed', '対象タブの URL が変わった。取り込むページのアドレスを確かめてやり直す');
    const html = await page.content();
    if (Buffer.byteLength(html, 'utf8') > request.max_bytes)
      throw new SessionError('too_large', '対象タブの DOM が大きさの上限を超えた');
    if ((deadline || request.max_images) && html !== previousHTML) {
      const next = await inspect(request, html, expected);
      if (next.body_sha256 !== analysis?.body_sha256) lastChange = performance.now();
      analysis = next;
      previousHTML = html;
    }
    const now = performance.now();
    // 新しく開いたページでは、load 後に JavaScript が描く本文も待つ。
    if (!deadline || now >= deadline || (!analysis.short && now - lastChange >= 1000))
      return { html, targets: analysis?.images ?? [] };
    await pause(Math.min(250, deadline - now));
  }
}

async function readPage(browser, request, inspect = inspectDOM, validate = validateDestination) {
  const expected = canonicalURL(request.url);
  const timeout = request.timeout * 1000;
  const pages = browser.contexts().flatMap(context => context.pages());
  const matching = pages.filter(page => matchesURL(page, expected));
  let page, deadline, finalURL = expected;
  if (matching.length === 1) {
    page = matching[0];
    await page.waitForLoadState('domcontentloaded', { timeout });
  } else if (pages.length === 1 && pages[0].url() === 'about:blank') {
    // 拡張はトークン認証時、またはタブを選ばずに許可したときに空のタブを割り当てる。
    page = pages[0];
    deadline = performance.now() + timeout;
    let response;
    try {
      response = await page.goto(expected, { waitUntil: 'domcontentloaded', timeout });
    } catch {
      throw new SessionError('navigation_failed', '取り込み URL をブラウザで開けなかった。ブラウザの表示を確かめる');
    }
    if (response && response.status() >= 400)
      throw new SessionError('http_error', `ブラウザでも HTTP ${response.status()} を受け取った`);
    finalURL = canonicalURL(page.url());
    // HTTP の応答 URL にフラグメントは含まれない。表示中の URL がこの移動の応答に対応することを確かめる。
    const responseURL = response ? canonicalURL(response.url()) : expected;
    if (response ? finalURL.split('#', 1)[0] !== responseURL.split('#', 1)[0] : finalURL !== expected)
      throw new SessionError('tab_changed', '対象タブの URL が変わった。取り込むページのアドレスを確かめてやり直す');
    if (finalURL !== expected) {
      const refusal = await validate(request, finalURL);
      if (refusal.error) throw new SessionError(refusal.kind, refusal.error, refusal.url);
    }
  } else {
    throw new SessionError('tab_not_unique', 'Playwright の接続画面でタブを選ばずに許可するか、対象 URL のタブを 1 つだけ選ぶ');
  }
  const { html, targets } = await readDOM(page, request, finalURL, deadline, inspect);
  // 本文内で既に読み込まれた画像を使う。fetch、画像の再読み込み、Cookie の読み取りは行わない。
  const capture = targets.length ? await page.evaluate(({ maxBytes, targets }) => {
    const images = [];
    const warnings = [];
    let total = 0;
    for (const source of targets) {
      const image = Array.from(document.images).find(image => (image.currentSrc || image.src) === source);
      if (!image) continue;
      if (!source || !image.complete || image.naturalWidth <= 32 || image.naturalHeight <= 32) continue;
      try {
        if (new URL(source).origin !== location.origin) continue;
        if (image.naturalWidth * image.naturalHeight > 4194304) {
          warnings.push('大きすぎる画像は読み取らなかった');
          continue;
        }
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        canvas.getContext('2d').drawImage(image, 0, 0);
        const base64 = canvas.toDataURL('image/png').split(',')[1];
        const bytes = Math.floor(base64.length * 3 / 4);
        if (bytes > maxBytes || total + bytes > maxBytes) {
          warnings.push('画像の大きさの上限を超えるため読み取らなかった');
          continue;
        }
        images.push({ url: source, base64 });
        total += bytes;
      } catch { warnings.push('読み込み済みの画像を PNG にできなかった'); }
    }
    return { images, warnings };
  }, { maxBytes: request.max_bytes, targets }) : { images: [], warnings: [] };
  if (!matchesURL(page, finalURL))
    throw new SessionError('tab_changed', '読み取り中に対象タブの URL が変わった。目的のページを表示してやり直す');
  return { ...capture, html, final_url: finalURL, request_url: request.url, matched_url: finalURL,
    browser: request.channel, browser_session: 'existing' };
}

async function connectAndRead(request, tools = runtimeTools()) {
  let connection;
  const config = { extension: true, background: true, tabGroup: false, timeouts: {},
    browser: { browserName: 'chromium', launchOptions: { channel: request.channel,
      ...(request.executable_path ? { executablePath: request.executable_path } : {}) } } };
  try {
    // Backend/Tab を作らず接続だけを使う。MCP の console ログ、snapshot、trace を開始しない。
    connection = await tools.createBrowserWithInfo(config, { clientName: 'info-hive ingest' },
      { extension: true, browser: request.channel },
      { title: 'info-hive-ingest-' + crypto.randomUUID(), workspaceDir: process.cwd() });
    if (connection.ownership !== 'attached')
      throw new SessionError('not_attached', '普段のブラウザへの接続ではないため読み取りを中止した');
    try {
      return await readPage(connection.browser, request);
    } catch (error) {
      if (error instanceof SessionError) throw error;
      throw new SessionError('read_failed', '対象タブを読み取れなかった。ページの読み込みが終わってからやり直す');
    }
  } finally {
    if (connection) {
      await connection.browser.unbind().catch(() => {});
      // 接続した Browser の close は切断であり、ブラウザ本体や既存タブを閉じない。
      await connection.browser.close().catch(() => {});
    }
  }
}

async function main() {
  const input = await new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', value => { data += value; if (data.length > 65536) reject(new Error('input')); });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
  const request = JSON.parse(input);
  const alarm = setTimeout(async () => {
    await writeResult({ error: 'ブラウザの接続確認が終わらなかった。Playwright の接続画面で許可してやり直す', kind: 'connection_timeout' });
    process.exit(2);
  }, (request.timeout + 120) * 1000);
  try {
    const result = await connectAndRead(request);
    await writeResult(result);
  } finally { clearTimeout(alarm); }
}

async function writeResult(result) {
  await new Promise((resolve, reject) => process.stdout.write(JSON.stringify(result) + '\n', error => error ? reject(error) : resolve()));
}

module.exports = { CORE_VERSION, canonicalURL, readPage, connectAndRead, writeResult };
if (require.main === module) {
  main().then(() => process.exit(0), async error => {
    const message = error instanceof SessionError ? error.message
      : process.env[EXTENSION_TOKEN]
        ? 'Playwright の公式拡張に接続できなかった。拡張の導入と PLAYWRIGHT_MCP_EXTENSION_TOKEN がブラウザのプロファイルに対応しているかを確かめる'
        : 'Playwright の公式拡張に接続できなかった。ブラウザで拡張の導入と接続を確かめる';
    await writeResult({ error: message, kind: error.code || 'connection_failed', url: error instanceof SessionError ? error.url : undefined }).catch(() => {});
    process.exit(2);
  });
}

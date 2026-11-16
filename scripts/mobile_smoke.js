#!/usr/bin/env node
/**
 * Run a dependency-free Chromium viewport smoke test for the local web pages.
 *
 * This is a browser-size regression check, not a real-device certification.
 * It uses Chrome DevTools Protocol directly so the repository does not need a
 * Playwright/Puppeteer dependency just for two deterministic viewport checks.
 */

'use strict';

const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {URL} = require('node:url');

const ROOT = path.resolve(__dirname, '..');
const VIEWPORTS = [
  {name: 'small', width: 320, height: 568},
  {name: 'large', width: 390, height: 844},
];
const DEFAULT_TIMEOUT_MS = 15000;
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

function parseArgs(argv) {
  const options = {
    chromium: process.env.CHROMIUM_BIN || '',
    output: process.env.MOBILE_SMOKE_OUTPUT || '',
    timeout: DEFAULT_TIMEOUT_MS,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--chromium') options.chromium = argv[++index] || '';
    else if (argument === '--output') options.output = argv[++index] || '';
    else if (argument === '--timeout') options.timeout = Number(argv[++index]) || DEFAULT_TIMEOUT_MS;
    else if (argument === '--help' || argument === '-h') {
      console.log('Usage: node scripts/mobile_smoke.js [--chromium PATH] [--output DIR] [--timeout MS]');
      console.log('Checks the unauthenticated Dashboard at 320x568 and 390x844.');
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }
  return options;
}

function findChromium(explicit) {
  if (explicit) return explicit;
  const candidates = ['chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable', 'chrome'];
  const searchPath = String(process.env.PATH || '').split(path.delimiter);
  for (const candidate of candidates) {
    for (const directory of searchPath) {
      const executable = path.join(directory, candidate);
      try {
        fs.accessSync(executable, fs.constants.X_OK);
        return executable;
      } catch (_) {
        // Continue through the platform-specific executable names.
      }
    }
  }
  throw new Error('未找到 Chromium；请安装 chromium-browser，或通过 --chromium 指定路径');
}

function freePort(host = '127.0.0.1') {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, host, () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function contentType(file) {
  return {
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.ico': 'image/x-icon',
  }[path.extname(file).toLowerCase()] || 'application/octet-stream';
}

function serveFile(response, file) {
  fs.readFile(file, (error, body) => {
    if (error) {
      response.writeHead(error.code === 'ENOENT' ? 404 : 500, {'Content-Type': 'text/plain; charset=utf-8'});
      response.end(error.code === 'ENOENT' ? 'Not found' : 'Read failed');
      return;
    }
    response.writeHead(200, {'Content-Type': contentType(file), 'Cache-Control': 'no-store'});
    response.end(body);
  });
}

async function startDashboardServer() {
  const port = await freePort();
  const server = http.createServer((request, response) => {
    let requestPath;
    try {
      requestPath = decodeURIComponent(new URL(request.url, `http://${request.headers.host}`).pathname);
    } catch (_) {
      response.writeHead(400);
      response.end('Bad request');
      return;
    }
    // The host normally injects this bridge script. A no-op response keeps
    // standalone smoke pages on the same unauthenticated code path without a
    // misleading network 404.
    if (requestPath === '/api/plugin/page/bridge-sdk.js') {
      response.writeHead(200, {'Content-Type': 'text/javascript; charset=utf-8', 'Cache-Control': 'no-store'});
      response.end('// standalone mobile smoke: no host bridge');
      return;
    }
    const relative = requestPath === '/' || requestPath === '/dashboard' || requestPath === '/dashboard/'
      ? 'pages/dashboard/index.html'
      : requestPath === '/dashboard/logo.png'
        ? 'logo.png'
        : requestPath.startsWith('/dashboard/')
          ? `pages/dashboard/${requestPath.slice('/dashboard/'.length)}`
          : requestPath.replace(/^\/+/, '');
    const file = path.resolve(ROOT, relative);
    if (!file.startsWith(`${ROOT}${path.sep}`) && file !== ROOT) {
      response.writeHead(403);
      response.end('Forbidden');
      return;
    }
    serveFile(response, file);
  });
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');
  return {server, url: `http://127.0.0.1:${port}/dashboard/`};
}

async function stopProcess(child, signal = 'SIGTERM') {
  if (!child || child.exitCode !== null || child.signalCode) return;
  child.kill(signal);
  await Promise.race([
    once(child, 'exit'),
    new Promise(resolve => setTimeout(resolve, 2000)),
  ]);
}

async function waitForCdp(port, timeout) {
  const targetsUrl = `http://127.0.0.1:${port}/json/list`;
  const started = Date.now();
  let lastError;
  while (Date.now() - started < timeout) {
    try {
      const response = await fetch(targetsUrl);
      if (response.ok) {
        const targets = await response.json();
        const page = targets.find(target => target.type === 'page' && target.webSocketDebuggerUrl);
        if (page) return page.webSocketDebuggerUrl;
        lastError = new Error('没有可用的 page target');
      } else {
        lastError = new Error(`HTTP ${response.status}`);
      }
    } catch (error) {
      lastError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`等待 Chromium CDP 超时：${lastError?.message || '调试端口未就绪'}`);
}

class CdpSession {
  constructor(webSocketUrl, timeout) {
    this.timeout = timeout;
    this.nextId = 0;
    this.pending = new Map();
    this.events = new Map();
    this.socket = new WebSocket(webSocketUrl);
    this.open = once(this.socket, 'open');
    this.socket.addEventListener('message', event => this.receive(event.data));
    this.socket.addEventListener('close', () => {
      for (const {reject} of this.pending.values()) reject(new Error('CDP 连接已关闭'));
      this.pending.clear();
    });
  }

  receive(raw) {
    let message;
    try { message = JSON.parse(raw); } catch (_) { return; }
    if (message.id && this.pending.has(message.id)) {
      const {resolve, reject, timer} = this.pending.get(message.id);
      clearTimeout(timer);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(`${message.error.code}: ${message.error.message}`));
      else resolve(message.result || {});
      return;
    }
    if (message.method) {
      for (const listener of this.events.get(message.method) || []) listener(message.params || {});
    }
  }

  async send(method, params = {}) {
    await this.open;
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP ${method} 超时`));
      }, this.timeout);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params}));
    });
  }

  on(method, listener) {
    const listeners = this.events.get(method) || [];
    listeners.push(listener);
    this.events.set(method, listeners);
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      const description = result.exceptionDetails.exception?.description || '页面脚本异常';
      throw new Error(description);
    }
    return result.result?.value;
  }

  close() {
    try { this.socket.close(); } catch (_) { /* already closed */ }
  }
}

async function capturePage(cdp, url, viewport, outputFile) {
  const errors = [];
  cdp.on('Runtime.exceptionThrown', event => errors.push(event.exceptionDetails?.text || '页面异常'));
  cdp.on('Log.entryAdded', event => {
    if (event.entry?.level === 'error') errors.push(`${event.entry.source || 'console'} ${event.entry.url || ''} ${event.entry.text || '控制台错误'}`.trim());
  });
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Log.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: true,
    screenWidth: viewport.width,
    screenHeight: viewport.height,
  });
  const navigation = await cdp.send('Page.navigate', {url});
  if (navigation.errorText) throw new Error(`${url}: ${navigation.errorText}`);
  await cdp.evaluate(`new Promise(resolve => {
    if (document.readyState === 'complete') resolve();
    else window.addEventListener('load', resolve, {once: true});
  })`);
  await new Promise(resolve => setTimeout(resolve, 250));
  const metrics = await cdp.evaluate(`(() => {
    const body = document.body;
    const root = document.documentElement;
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      clientWidth: root.clientWidth,
      scrollWidth: Math.max(root.scrollWidth, body ? body.scrollWidth : 0),
      bodyText: body ? body.innerText.slice(0, 500) : '',
      login: Boolean(document.querySelector('#loginPage')),
      loginBox: Boolean(document.querySelector('#loginPage .login-box')),
      authed: document.body.classList.contains('authed'),
    };
  })()`);
  const screenshot = await cdp.send('Page.captureScreenshot', {format: 'png', fromSurface: true});
  const bytes = Buffer.from(screenshot.data, 'base64');
  if (!bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) throw new Error(`${url}: 截图不是 PNG`);
  const pngWidth = bytes.readUInt32BE(16);
  const pngHeight = bytes.readUInt32BE(20);
  if (!bytes.length || pngWidth !== viewport.width || pngHeight !== viewport.height) {
    throw new Error(`${url}: 截图尺寸 ${pngWidth}x${pngHeight}，期望 ${viewport.width}x${viewport.height}`);
  }
  if (bytes.length < 512) throw new Error(`${url}: 截图内容过小，疑似空白渲染`);
  if (metrics.innerWidth !== viewport.width || metrics.innerHeight !== viewport.height) {
    throw new Error(`${url}: 页面 viewport ${metrics.innerWidth}x${metrics.innerHeight}，期望 ${viewport.width}x${viewport.height}`);
  }
  if (metrics.scrollWidth > metrics.clientWidth + 1) {
    throw new Error(`${url}: 存在水平溢出 ${metrics.scrollWidth}px > ${metrics.clientWidth}px`);
  }
  if (!metrics.login || !metrics.loginBox || metrics.authed) {
    throw new Error(`${url}: 未认证 Dashboard 登录入口状态异常`);
  }
  if (errors.length) throw new Error(`${url}: 浏览器错误：${errors.slice(0, 3).join(' | ')}`);
  if (outputFile) fs.writeFileSync(outputFile, bytes);
  return {metrics, screenshotBytes: bytes.length, screenshotWidth: pngWidth, screenshotHeight: pngHeight};
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const chromium = findChromium(options.chromium);
  const output = options.output ? path.resolve(options.output) : '';
  if (output) fs.mkdirSync(output, {recursive: true});

  const dashboard = await startDashboardServer();
  let browser;
  let cdp;
  const tempProfile = fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-mobile-smoke-'));
  try {
    const cdpPort = await freePort();
    browser = spawn(chromium, [
      '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
      '--hide-scrollbars', '--remote-allow-origins=*', `--remote-debugging-port=${cdpPort}`,
      `--user-data-dir=${tempProfile}`, 'about:blank',
    ], {stdio: ['ignore', 'ignore', 'pipe']});
    const cdpUrl = await waitForCdp(cdpPort, options.timeout);
    cdp = new CdpSession(cdpUrl, options.timeout);
    for (const viewport of VIEWPORTS) {
      const dashboardUrl = `${dashboard.url}?mobile-smoke=${viewport.name}`;
      const dashboardOutput = output ? path.join(output, `dashboard-${viewport.name}.png`) : '';
      const dashboardResult = await capturePage(cdp, dashboardUrl, viewport, dashboardOutput);
      console.log(`[mobile-smoke] dashboard unauthenticated ${viewport.name} ${viewport.width}x${viewport.height}: ok (${dashboardResult.screenshotBytes} bytes)`);
    }
    console.log('[mobile-smoke] Chromium viewport smoke passed; this does not certify a real device.');
  } finally {
    if (cdp) cdp.close();
    await stopProcess(browser);
    await new Promise(resolve => dashboard.server.close(resolve));
    try {
      fs.rmSync(tempProfile, {recursive: true, force: true});
    } catch (error) {
      // A browser lock can outlive the short shutdown grace period on Android.
      // The profile is temporary and must not turn a completed smoke test into
      // a false negative; leave it for the OS temporary-file cleanup instead.
      if (error.code !== 'ENOTEMPTY' && error.code !== 'EBUSY') throw error;
    }
  }
}

main().catch(error => {
  console.error(`[mobile-smoke] failed: ${error.message}`);
  process.exitCode = 1;
});

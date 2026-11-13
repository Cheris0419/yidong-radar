#!/usr/bin/env node
/**
 * 异动雷达 · GitHub Pages 在线版独立验收（QA）
 * ==========================================
 * 用 Chrome DevTools Protocol 驱动 headless Chrome 打开公网站点：
 *   - 截图（移动 390x844 / 桌面 1280x900 / 日间主题 / 错误态 / 搜索联想）
 *   - 采集 console error、未捕获异常、4xx/5xx 网络请求、请求主机清单
 *   - 在页面上下文内调用被 static-api.js 拦截的 fetch('/api/*') 拿到「静态接口」返回的
 *     原始 JSON（与 docs/BACKEND-CONTRACT.md 同构），写入 qa/pages_observed.json，
 *     供 qa/online_accept.py 与 QA 独立引擎逐字段比对
 *
 * 用法：
 *   node qa/pages_check.mjs [--base https://cheris0419.github.io/yidong-radar/] [--out qa/pages_observed.json]
 * 环境变量：CHROME_BIN 可指定 chrome/chromium 路径（默认自动探测）。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const OUT_DIR = path.join(__dirname, 'screens');
const DEBUG_PORT = 9400 + (process.pid % 200);

function argVal(flag, dft) {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? (process.argv[i + 1] || dft) : dft;
}
const BASE = argVal('--base', 'https://cheris0419.github.io/yidong-radar/').replace(/\/?$/, '/');
const OUT_JSON = argVal('--out', path.join(__dirname, 'pages_observed.json'));

function findChrome() {
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN;
  const home = process.env.HOME || '';
  const candidates = [
    path.join(home, 'Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
    path.join(home, 'Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
    'chrome-headless-shell', 'chromium',
  ];
  for (const c of candidates) {
    try { if (c.indexOf('/') < 0 || fs.existsSync(c)) return c; } catch { /* ignore */ }
  }
  throw new Error('未找到 Chrome，请设置 CHROME_BIN');
}

// ------------------------------ 断言 ------------------------------ //
let pass = 0, fail = 0;
const failures = [];
function ok(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; failures.push(name + (detail ? ' :: ' + detail : '')); console.log(`  ✗ ${name}${detail ? ' :: ' + detail : ''}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------ CDP ------------------------------ //
class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    this.consoleLog = []; this.exceptions = []; this.logEntries = [];
    this.netErrors = []; this.responses = [];
    this.allResponses = [];   // 全局累计（不随 resetLogs 清空，用于主机/状态汇总）
  }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => c._on(JSON.parse(ev.data));
    return c;
  }
  _on(m) {
    if (m.id && this.pending.has(m.id)) {
      const { resolve, reject } = this.pending.get(m.id);
      this.pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result || {});
      return;
    }
    const p = m.params || {};
    switch (m.method) {
      case 'Runtime.consoleAPICalled':
        this.consoleLog.push({ type: p.type, text: (p.args || []).map((a) =>
          a.value !== undefined ? String(a.value) : (a.description || a.type || '?')).join(' ') });
        break;
      case 'Runtime.exceptionThrown':
        this.exceptions.push({ text: p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || 'unknown' });
        break;
      case 'Log.entryAdded':
        this.logEntries.push({ level: p.entry?.level, text: p.entry?.text });
        break;
      case 'Network.responseReceived':
        this.responses.push({ url: p.response?.url, status: p.response?.status, type: p.type });
        this.allResponses.push({ url: p.response?.url, status: p.response?.status, type: p.type });
        if (p.response?.status >= 400) this.netErrors.push({ url: p.response.url, status: p.response.status });
        break;
      case 'Network.loadingFailed':
        this.netErrors.push({ error: p.errorText, canceled: !!p.canceled, type: p.type });
        break;
      default: break;
    }
  }
  send(method, params = {}, timeoutMs = 30000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      try { this.ws.send(JSON.stringify({ id, method, params })); }
      catch (e) { this.pending.delete(id); clearTimeout(timer); reject(e); }
    });
  }
  resetLogs() { this.consoleLog = []; this.exceptions = []; this.logEntries = []; this.netErrors = []; this.responses = []; }
  errors() {
    return {
      consoleErrors: this.consoleLog.filter((x) => x.type === 'error').map((x) => x.text),
      logErrors: this.logEntries.filter((x) => x.level === 'error').map((x) => x.text),
      exceptions: this.exceptions.map((x) => x.text),
      netErrors: this.netErrors.slice(),
    };
  }
  async eval(expression, awaitPromise = false) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise, userGesture: true });
    if (r.exceptionDetails) throw new Error('eval: ' + JSON.stringify(r.exceptionDetails).slice(0, 500));
    return r.result?.value;
  }
  async waitFor(expression, timeout = 25000) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (await this.eval(expression)) return true; } catch { /* 导航中 */ }
      await sleep(150);
    }
    return false;
  }
  async navigate(url) {
    await this.send('Page.navigate', { url });
    await this.waitFor('document.readyState === "complete"', 30000);
    await sleep(300);
  }
  async setViewport(width, height, mobile = false) {
    await this.send('Emulation.setDeviceMetricsOverride', {
      width, height, deviceScaleFactor: 2, mobile,
      screenWidth: width, screenHeight: height,
    });
    if (mobile) await this.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  }
  async screenshot(file, fullPage = true) {
    const params = { format: 'png', captureBeyondViewport: !!fullPage };
    if (fullPage) {
      const lm = await this.send('Page.getLayoutMetrics');
      const cs = lm.cssContentSize || lm.contentSize;
      params.clip = { x: 0, y: 0, width: Math.ceil(cs.width), height: Math.ceil(cs.height), scale: 1 };
    }
    const r = await this.send('Page.captureScreenshot', params);
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
    return fs.statSync(file).size;
  }
}

// ------------------------------ 页面数据提取 ------------------------------ //
const ROWS_READY = `document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`;

async function pagePayload(cdp, code) {
  return await cdp.eval(
    `fetch('/api/triggers?code=${encodeURIComponent(code)}').then(function(r){return r.json();})`, true);
}
/** 宽容版：把响应按 text 抓回来，JSON 解析失败时保留状态码/内容片段（用于排查在线版错误路径） */
async function pageRaw(cdp, code) {
  return await cdp.eval(`fetch('/api/triggers?code=${encodeURIComponent(code)}')
    .then(function(r){ return r.text().then(function(t){
      var j=null; try{ j=JSON.parse(t); }catch(e){}
      return {status:r.status, contentType:r.headers.get('content-type'), json:j, bodyHead:t.slice(0,160)};
    }); })
    .catch(function(e){ return {error:String(e)}; })`, true);
}
function apiNetworkRequests(cdp) {
  return cdp.responses
    .filter((r) => { try { return new URL(r.url).pathname.indexOf('/api/') >= 0; } catch (e) { return false; } })
    .map((r) => ({ path: new URL(r.url).pathname, status: r.status }));
}
async function pageSearch(cdp, q) {
  return await cdp.eval(
    `fetch('/api/search?q=${encodeURIComponent(q)}').then(function(r){return r.json();})`, true);
}

async function extractDom(cdp) {
  return await cdp.eval(`(() => {
    const txt = (id) => { const el = document.getElementById(id); return el ? el.textContent.replace(/\\s+/g,' ').trim() : ''; };
    const rows = [...document.querySelectorAll('#triggerRows tr.trigger-row')].map((tr) => ({
      key: (tr.querySelector('.day-key')||{}).textContent || '',
      date: (tr.querySelector('.day-date')||{}).textContent || '',
      pct: (tr.querySelector('.trigger-pct')||{}).textContent || '',
      price: (tr.querySelector('.trigger-price')||{}).textContent || '',
      over: tr.classList.contains('is-over'),
      todayChip: (tr.querySelector('.today-chip') || {}).textContent || '',
      rules: [...tr.querySelectorAll('.rule')].map((li) => ({
        text: li.textContent.replace(/\\s+/g,' ').trim(),
        binding: li.classList.contains('is-binding'),
      })),
    }));
    const rt = (root, sel) => { const el = root.querySelector(sel); return el && el.textContent ? el.textContent.replace(/\\s+/g,' ').trim() : ''; };
    const readouts = [...document.querySelectorAll('#readoutList .readout')].map((el) => ({
      title: rt(el, '.readout-title'),
      value: rt(el, '.readout-value'),
      remaining: rt(el, '.readout-remaining'),
      rule: rt(el, '.readout-rule'),
      event: rt(el, '.readout-event'),
      level: el.getAttribute('data-level') || '',
    }));
    return {
      title: document.title,
      htmlTitle: window.__ydrHtmlTitle || '',
      dataView: document.getElementById('main')?.dataset.view || '',
      htmlTheme: document.documentElement.dataset.theme || '',
      stockName: txt('stockName'), stockCode: txt('stockCode'), stockBoard: txt('stockBoard'),
      price: txt('price'), pct: txt('pct'),
      indexName: txt('indexName'), indexPrice: txt('indexPrice'), indexPct: txt('indexPct'),
      risk: txt('riskBadge'), riskReasons: txt('riskReasons'),
      rows, readouts,
      closeChip: { hidden: document.getElementById('closeChip')?.hidden, text: txt('closeChip') },
      noticeMain: txt('noticeMain'), noticeSub: txt('noticeSub'),
      tableFoot: { hidden: document.getElementById('tableFoot')?.hidden, text: txt('tableFoot') },
      dataSource: txt('dataSource'), footerUpdate: txt('footerUpdate'),
      errorViewHidden: document.getElementById('errorView')?.hidden,
      errorTitle: txt('errorTitle'), errorText: txt('errorText'), errorCode: txt('errorCode'),
      emptyViewHidden: document.getElementById('emptyView')?.hidden,
      searchOptions: [...document.querySelectorAll('#searchList .search-option')].map((el) => el.textContent.replace(/\\s+/g,' ').trim()),
      searchPanelHidden: document.getElementById('searchPanel')?.hidden,
      overflow: { scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth },
      staticMarker: !!window.__YDR_STATIC__,
    };
  })()`);
}

async function assertClean(cdp, tag, { allowApi4xx = false } = {}) {
  const e = cdp.errors();
  let net = e.netErrors.slice();
  if (allowApi4xx) net = net.filter((x) => !(String(x.url || '').includes('/api/') && (x.status === 400 || x.status === 404)));
  ok(`[${tag}] 无 console error`, e.consoleErrors.length === 0, JSON.stringify(e.consoleErrors).slice(0, 400));
  ok(`[${tag}] 无未捕获异常`, e.exceptions.length === 0, JSON.stringify(e.exceptions).slice(0, 400));
  const logErrors = allowApi4xx
    ? e.logErrors.filter((t) => !/status of (400|404)/.test(String(t)))
    : e.logErrors;
  ok(`[${tag}] 无 log error`, logErrors.length === 0, JSON.stringify(logErrors).slice(0, 400));
  ok(`[${tag}] 无 4xx/5xx`, net.length === 0, JSON.stringify(net).slice(0, 400));
}

// ------------------------------ 用例 ------------------------------ //
const observed = { base: BASE, generatedAt: new Date().toISOString(), cases: {}, search: {}, console: {}, network: {} };

async function caseCode(cdp, code, { mobile = false, label = '' } = {}) {
  console.log(`\n[公网] ?code=${code} ${label}`);
  cdp.resetLogs();
  await cdp.navigate(`${BASE}?code=${code}`);
  const loaded = await cdp.waitFor(ROWS_READY, 30000);
  ok(`[${code}] 触发表 10 行加载完成`, loaded);
  const payload = await pagePayload(cdp, code);
  await cdp.eval(`fetch(location.pathname + location.search, {cache:'no-store'}).then(function(r){return r.text();}).then(function(t){ window.__ydrHtmlTitle = (t.match(/<title>([^<]*)<\\/title>/i)||[])[1] || ''; });`, true);
  const dom = await extractDom(cdp);
  ok(`[${code}] 动态标题含「异动雷达」`, /异动雷达/.test(dom.title), dom.title);
  ok(`[${code}] HTML 源 <title> 含「（在线版）」`, /（在线版）/.test(dom.htmlTitle || ''), dom.htmlTitle);
  ok(`[${code}] 静态接口标记存在`, dom.staticMarker === true);
  ok(`[${code}] data-view=ready`, dom.dataView === 'ready', dom.dataView);
  ok(`[${code}] 页面无横向溢出`, dom.overflow.scrollW <= dom.overflow.innerW + 1,
     JSON.stringify(dom.overflow));
  ok(`[${code}] 页面渲染股票名称`, dom.stockName.length > 0, dom.stockName);
  ok(`[${code}] 渲染三张读数卡`, dom.readouts.length === 3, String(dom.readouts.length));
  ok(`[${code}] DOM 行数 10`, dom.rows.length === 10, String(dom.rows.length));
  const apiReqs = apiNetworkRequests(cdp);
  ok(`[${code}] 页面加载无真实 /api 网络请求`, apiReqs.length === 0, JSON.stringify(apiReqs));
  ok(`[${code}] 静态接口 payload.ok`, !!(payload && payload.ok), JSON.stringify(payload).slice(0, 200));
  if (payload && payload.ok && payload.data) {
    const p = payload.data;
    const first = p.rows && p.rows[0];
    ok(`[${code}] 首行 T+0 且为快照交易日`, !!(first && first.key === 'T+0' && first.date === p.snapshot.trade_date),
       first ? first.key + ' ' + first.date + ' vs ' + p.snapshot.trade_date : 'no rows');
    ok(`[${code}] T+0 行显示「当日」chip`, true, dom.rows[0] && dom.rows[0].todayChip === '当日',
       JSON.stringify(dom.rows[0] && dom.rows[0].todayChip));
    ok(`[${code}] 页面顶部数据说明非空`, !!dom.noticeSub);
    ok(`[${code}] DOM 触发表可滚动或完整展示`,
       /T\+0/.test(JSON.stringify(dom.rows)), JSON.stringify(dom.rows.slice(0, 1)));
  }
  await assertClean(cdp, code);
  observed.cases[code] = { payload, dom };
  console.log(`  · 渲染：${dom.stockName} ${dom.stockCode} ${dom.price} ${dom.pct} | rows=${dom.rows.length} | 首行 ${dom.rows[0]?.key} ${dom.rows[0]?.date} ${dom.rows[0]?.pct}`);
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.rmSync('/tmp/qa/pages-chrome-profile', { recursive: true, force: true });
  fs.mkdirSync('/tmp/qa/pages-chrome-profile', { recursive: true });
  const chrome = findChrome();
  console.log('='.repeat(90));
  console.log(`异动雷达 · GitHub Pages 在线版验收  base=${BASE}`);
  console.log(`chrome=${chrome}`);
  console.log('='.repeat(90));
  const chromeProc = spawn(chrome, [
    '--headless', '--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu',
    '--disable-dev-shm-usage', '--hide-scrollbars', '--mute-audio',
    `--remote-debugging-port=${DEBUG_PORT}`,
    '--remote-allow-origins=*',
    '--user-data-dir=/tmp/qa/pages-chrome-profile',
    '--no-first-run', '--no-default-browser-check', 'about:blank',
  ], { stdio: 'ignore' });
  let cdp = null;
  try {
    const t0 = Date.now();
    let target = null;
    while (Date.now() - t0 < 25000) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
        target = list.find((t) => t.type === 'page');
        if (target) break;
      } catch { /* 启动中 */ }
      await sleep(250);
    }
    if (!target) throw new Error('chrome devtools 未就绪');
    cdp = await CDP.connect(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Log.enable');
    await cdp.send('Network.enable');

    // ---- 1. 桌面空态 ----
    console.log('\n[公网] 桌面空态首页');
    cdp.resetLogs();
    await cdp.setViewport(1280, 900, false);
    await cdp.navigate(BASE);
    await cdp.waitFor(`!document.getElementById('emptyView').hidden`, 20000);
    await cdp.eval(`fetch(location.pathname, {cache:'no-store'}).then(function(r){return r.text();}).then(function(t){ window.__ydrHtmlTitle = (t.match(/<title>([^<]*)<\\/title>/i)||[])[1] || ''; });`, true);
    const home = await extractDom(cdp);
    ok('[首页] 空态可见', home.emptyViewHidden === false);
    ok('[首页] 动态标题含「异动雷达」', /异动雷达/.test(home.title), home.title);
    ok('[首页] HTML 源 <title> 含「（在线版）」', /（在线版）/.test(home.htmlTitle || ''), home.htmlTitle);
    ok('[首页] 无横向溢出', home.overflow.scrollW <= home.overflow.innerW + 1);
    await cdp.screenshot(path.join(OUT_DIR, 'pages-desktop-home.png'));
    await assertClean(cdp, '首页');
    observed.cases.home = { dom: home };

    // ---- 2. 桌面数据态 000993 / 600664 / 688981 ----
    await cdp.setViewport(1280, 900, false);
    for (const [code, shot, label] of [
      ['000993', 'pages-desktop-000993.png', '（深主板）'],
      ['600664', 'pages-desktop-600664.png', '（沪主板）'],
      ['688981', 'pages-desktop-688981.png', '（科创板）'],
    ]) {
      try {
        await caseCode(cdp, code, { label });
        await cdp.screenshot(path.join(OUT_DIR, shot));
      } catch (e) { fail++; failures.push(`用例 ${code} 异常: ` + String(e).slice(0, 200)); console.log(`  ✗ 用例 ${code} 异常:`, e); }
    }

    // ---- 3. 错误态（含北交所/B 股：静态版应返回同契约错误码）----
    console.log('\n[公网] 错误态');
    const errorCases = [
      ['999999', 'NOT_FOUND', 'pages-error-999999.png'],
      ['123456', 'NOT_FOUND', 'pages-error-123456.png'],
      ['920185', 'NOT_SUPPORTED', 'pages-error-bse.png'],
      ['900901', 'NOT_SUPPORTED', 'pages-error-bshare.png'],
      ['abc', 'BAD_CODE', 'pages-error-badcode.png'],
    ];
    for (const [code, expectCode, shot] of errorCases) {
      try {
      cdp.resetLogs();
      await cdp.navigate(`${BASE}?code=${code}`);
      await cdp.waitFor(`!document.getElementById('errorView').hidden`, 20000);
      await sleep(500);
      const apiReqs = apiNetworkRequests(cdp);     // 仅页面自身行为，不含下面探针
      const raw = await pageRaw(cdp, code);        // 探针（可能额外触发一次网络请求）
      const dom = await extractDom(cdp);
      ok(`[错误态 ${code}] 错误视图可见`, dom.dataView === 'error', dom.dataView);
      ok(`[错误态 ${code}] 页面错误码=${expectCode}`, dom.errorCode === expectCode, dom.errorCode);
      ok(`[错误态 ${code}] 前端未向真实网络发起 /api 请求`, apiReqs.length === 0, JSON.stringify(apiReqs));
      ok(`[错误态 ${code}] 静态接口返回 JSON 错误对象`, !!(raw && raw.json && raw.json.error && raw.json.error.code === expectCode),
         JSON.stringify(raw && raw.json ? raw.json : raw));
      await cdp.screenshot(path.join(OUT_DIR, shot));
      await assertClean(cdp, `错误态${code}`, { allowApi4xx: true });
      observed.cases['error_' + code] = { dom, raw, apiReqs,
                                          probeApiReqs: apiNetworkRequests(cdp) };
      } catch (e) { fail++; failures.push(`错误态 ${code} 异常: ` + String(e).slice(0, 200)); console.log(`  ✗ 错误态 ${code} 异常:`, e); }
    }

    // ---- 4. 搜索（JSONP 联想）----
    console.log('\n[公网] 搜索联想（东方财富 JSONP）');
    async function searchFlow(q, shot, expectLoadedCode) {
      cdp.resetLogs();
      await cdp.setViewport(1280, 900, false);
      await cdp.navigate(BASE);
      await cdp.waitFor(`!document.getElementById('emptyView').hidden`, 20000);
      await cdp.eval(`document.getElementById('searchInput').focus()`);
      await cdp.send('Input.insertText', { text: q });
      const suggested = await cdp.waitFor(
        `(() => { const p = document.getElementById('searchPanel'); return p && !p.hidden && document.querySelectorAll('#searchList .search-option').length > 0; })()`,
        20000);
      const payload = await pageSearch(cdp, q);
      const options = await cdp.eval(`[...document.querySelectorAll('#searchList .search-option')].map(x=>x.textContent.replace(/\\s+/g,' ').trim())`);
      ok(`[搜索 ${q}] 出现联想面板`, suggested, JSON.stringify(options));
      ok(`[搜索 ${q}] 静态接口返回 items`, !!(payload && payload.ok && payload.data.items.length >= 1),
         JSON.stringify(payload).slice(0, 300));
      await cdp.screenshot(path.join(OUT_DIR, shot));
      await assertClean(cdp, `搜索${q}`);
      return { q, payload, options };
    }
    observed.search.star = await searchFlow('中芯国际', 'pages-search-star.png', '688981');
    observed.search.nh = await searchFlow('nh', 'pages-search-nh.png', null);
    observed.search.rh = await searchFlow('rh', 'pages-search-rh.png', null);
    observed.search.bse = await searchFlow('贝特瑞', 'pages-search-bse.png', null);
    // 从搜素选中科创板 -> 加载；北交所 -> NOT_SUPPORTED
    ok('[搜索 中芯国际] 联想含 688981/科创板',
       observed.search.star.payload.data.items.some((x) => x.code === '688981' && x.board === 'star'),
       JSON.stringify(observed.search.star.payload.data.items));
    ok('[搜索 nh] 联想含 A 股主板',
       observed.search.nh.payload.data.items.some((x) => x.board === 'main'),
       JSON.stringify(observed.search.nh.payload.data.items.map((x) => x.code + x.name)));
    ok('[搜索 rh] 联想含 A 股主板',
       observed.search.rh.payload.data.items.some((x) => x.board === 'main'),
       JSON.stringify(observed.search.rh.payload.data.items.map((x) => x.code + x.name)));
    ok('[搜索 贝特瑞] 联想含北交所条目',
       observed.search.bse.payload.data.items.some((x) => x.board === 'bse' && x.market === 'bj'),
       JSON.stringify(observed.search.bse.payload.data.items));

    // 选中科创板股票 -> 加载
    async function selectFirst(cdp, code) {
      cdp.resetLogs();
      await cdp.navigate(BASE);
      await cdp.waitFor(`!document.getElementById('emptyView').hidden`, 20000);
      await cdp.eval(`document.getElementById('searchInput').focus()`);
      await cdp.send('Input.insertText', { text: code });
      await cdp.waitFor(`document.querySelectorAll('#searchList .search-option').length > 0`, 20000);
      await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
      return await cdp.waitFor(`location.search.includes('code=688981') && ${ROWS_READY}`, 30000);
    }
    const starLoaded = await selectFirst(cdp, '中芯国际');
    ok('[搜索选择] 科创板股票加载成功', starLoaded);
    if (starLoaded) {
      const dom = await extractDom(cdp);
      observed.search.starLoaded = { dom };
      await cdp.screenshot(path.join(OUT_DIR, 'pages-search-star-loaded.png'));
      await assertClean(cdp, '搜索选中科创板');
    }
    // 选中北交所股票 -> NOT_SUPPORTED 错误态
    cdp.resetLogs();
    await cdp.navigate(BASE);
    await cdp.waitFor(`!document.getElementById('emptyView').hidden`, 20000);
    await cdp.eval(`document.getElementById('searchInput').focus()`);
    await cdp.send('Input.insertText', { text: '贝特瑞' });
    await cdp.waitFor(`document.querySelectorAll('#searchList .search-option').length > 0`, 20000);
    await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    const bseErr = await cdp.waitFor(`!document.getElementById('errorView').hidden`, 20000);
    await sleep(400);
    const bseDom = await extractDom(cdp);
    const bseRaw = await pageRaw(cdp, '920185');
    ok('[搜索选择] 北交所股票进入错误态', bseErr && bseDom.dataView === 'error');
    ok('[搜索选择] 北交所股票错误码=NOT_SUPPORTED（契约/后端行为）', bseDom.errorCode === 'NOT_SUPPORTED',
       `实际=${bseDom.errorCode}`);
    observed.search.bseSelected = { dom: bseDom, raw: bseRaw, apiReqs: apiNetworkRequests(cdp) };
    await cdp.screenshot(path.join(OUT_DIR, 'pages-search-bse-selected.png'));
    await assertClean(cdp, '搜索选中北交所', { allowApi4xx: true });

    // ---- 5. 移动端 ----
    console.log('\n[公网] 移动 390x844');
    await cdp.setViewport(390, 844, true);
    cdp.resetLogs();
    await cdp.navigate(BASE);
    await cdp.waitFor(`!document.getElementById('emptyView').hidden`, 20000);
    await cdp.screenshot(path.join(OUT_DIR, 'pages-mobile-home.png'));
    await cdp.navigate(`${BASE}?code=000993`);
    const mLoaded = await cdp.waitFor(ROWS_READY, 30000);
    ok('[移动] 触发表 10 行', mLoaded);
    const mPayload = await pagePayload(cdp, '000993');
    const mDom = await extractDom(cdp);
    ok('[移动] 无横向溢出', mDom.overflow.scrollW <= mDom.overflow.innerW + 1, JSON.stringify(mDom.overflow));
    ok('[移动] 渲染三张读数卡', mDom.readouts.length === 3);
    await cdp.screenshot(path.join(OUT_DIR, 'pages-mobile-000993.png'));
    await assertClean(cdp, '移动');
    observed.cases.mobile_000993 = { payload: mPayload, dom: mDom };

    // ---- 6. 日间主题 ----
    console.log('\n[公网] 日间主题');
    cdp.resetLogs();
    await cdp.setViewport(1280, 900, false);
    await cdp.navigate(`${BASE}?code=000993&theme=light`);
    await cdp.waitFor(ROWS_READY, 30000);
    const lightDom = await extractDom(cdp);
    ok('[日间] data-theme=light', lightDom.htmlTheme === 'light', lightDom.htmlTheme);
    await cdp.screenshot(path.join(OUT_DIR, 'pages-desktop-light.png'));
    await assertClean(cdp, '日间主题');
    observed.cases.light = { dom: lightDom };

    // ---- 7. 网络/控制台汇总（全局累计） ----
    const hosts = {};
    for (const r of cdp.allResponses) {
      try { const h = new URL(r.url).host; hosts[h] = (hosts[h] || 0) + 1; } catch { /* ignore */ }
    }
    const badResponses = cdp.allResponses.filter((r) => r.status >= 400);
    observed.network = { hosts, badResponses, total: cdp.allResponses.length };
    observed.console = cdp.errors();
    const expectHosts = ['cheris0419.github.io', 'd.10jqka.com.cn', 'searchapi.eastmoney.com'];
    const unexpected = Object.keys(hosts).filter((h) => !expectHosts.includes(h));
    ok('[网络] 无 CDN/第三方意外主机', unexpected.length === 0, JSON.stringify(unexpected) + ' hosts=' + JSON.stringify(hosts));
    ok('[网络] 未请求东财 push2 行情域（静态版应仅用同花顺行情）',
       !Object.keys(hosts).some((h) => /push2/.test(h)), JSON.stringify(Object.keys(hosts)));
    ok('[网络] 主站资源来自 GitHub Pages', (hosts['cheris0419.github.io'] || 0) >= 5, JSON.stringify(hosts));
    ok('[网络] 同花顺行情请求成功', (hosts['d.10jqka.com.cn'] || 0) >= 3, JSON.stringify(hosts));
    ok('[网络] 搜索 JSONP 请求成功', (hosts['searchapi.eastmoney.com'] || 0) >= 1, JSON.stringify(hosts));
    ok('[网络] 无对 /api/* 的真实网络请求（静态版应完全拦截）', 0,
       badResponses.filter((r) => String(r.url).includes('/api/')).length,
       JSON.stringify(badResponses.filter((r) => String(r.url).includes('/api/')).slice(0, 6)));
    ok('[网络] 无 4xx/5xx 资源（页面资源）', 0, badResponses.length, JSON.stringify(badResponses.slice(0, 6)));

    fs.writeFileSync(OUT_JSON, JSON.stringify(observed, null, 2));
    console.log(`\n观测数据已写入 ${OUT_JSON}`);
  } catch (e) {
    fail++;
    failures.push('脚本异常: ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : String(e)));
    console.log('\n脚本异常：', e);
  } finally {
    try { chromeProc.kill('SIGKILL'); } catch { /* ignore */ }
  }
  console.log('\n' + '='.repeat(90));
  console.log(`公网 UI 验收：PASS ${pass} / FAIL ${fail} / 共 ${pass + fail}`);
  if (failures.length) {
    console.log('FAIL 明细：');
    failures.forEach((f) => console.log('  -', f));
  }
  console.log(`截图目录：${OUT_DIR}`);
  console.log('='.repeat(90));
  process.exit(fail === 0 ? 0 : 1);
}

setTimeout(() => {
  console.log('\n[watchdog] 整体超时 9 分钟，强制退出');
  process.exit(3);
}, 9 * 60 * 1000);

main();

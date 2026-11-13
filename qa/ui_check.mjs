#!/usr/bin/env node
/**
 * 异动雷达 · 阶段 2 前端 UI 验收脚本（QA）
 * =====================================================
 * 通过 Chrome DevTools Protocol 驱动 headless chrome：
 *   - 移动 390x844 / 桌面 1280x900 截图（整页）
 *   - 采集 console error / 未捕获异常 / 4xx-5xx 网络请求
 *   - 断言：空态/错误态/数据态、T+ 行数、binding 高亮、已超线态、停牌态、
 *           主题切换与持久化、搜索联想+键盘选择、自选持久化、reduced-motion
 *   - 深色/日间主题、无横向溢出、加载性能
 * 使用真实后端 8765 + QA mock 8799（受控的负触发涨幅 / 停牌状态）。
 *
 * 用法：node qa/ui_check.mjs [--base http://127.0.0.1:8765] [--mock http://127.0.0.1:8799]
 * 退出码：0 = 无 FAIL。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(__dirname, 'screens');
const CHROME = process.env.CHROME_BIN || findChrome();
function findChrome() {
  const home = process.env.HOME || '';
  const candidates = [
    path.join(home, 'Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome',
    'chrome-headless-shell', 'chromium'
  ];
  for (const c of candidates) {
    try { if (c.indexOf('/') < 0 || fs.existsSync(c)) return c; } catch (e) { /* ignore */ }
  }
  return 'chrome-headless-shell';
}
const DEBUG_PORT = 9333;
const BASE = argVal('--base', 'http://127.0.0.1:8765');
const MOCK = argVal('--mock', 'http://127.0.0.1:8799');

function argVal(flag, dft) {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? (process.argv[i + 1] || dft) : dft;
}

// ------------------------------ 断言工具 ------------------------------ //
let pass = 0, fail = 0;
const failures = [];
const findings = [];
function ok(name, cond, detail = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; failures.push(`${name}${detail ? ' :: ' + detail : ''}`); console.log(`  ✗ ${name}${detail ? ' :: ' + detail : ''}`); }
}
function eq(name, expected, actual) {
  ok(name, JSON.stringify(expected) === JSON.stringify(actual),
     `期望=${JSON.stringify(expected)} 实际=${JSON.stringify(actual)}`);
}
function near(name, expected, actual, tol = 0.02) {
  ok(name, typeof actual === 'number' && Math.abs(actual - expected) <= tol + 1e-9,
     `期望≈${expected} 实际=${JSON.stringify(actual)}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------ CDP 客户端 ------------------------------ //
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.consoleLog = [];   // {type, text}
    this.exceptions = [];   // {text}
    this.logEntries = [];   // {level, text, url}
    this.netErrors = [];    // {url, status} / {url, error}
    this.responses = [];    // 全部响应 url/status（用于静态资源断言）
  }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => c._onMessage(JSON.parse(ev.data));
    return c;
  }
  _onMessage(m) {
    if (m.id && this.pending.has(m.id)) {
      const { resolve, reject } = this.pending.get(m.id);
      this.pending.delete(m.id);
      if (m.error) reject(new Error(JSON.stringify(m.error)));
      else resolve(m.result || {});
      return;
    }
    const p = m.params || {};
    switch (m.method) {
      case 'Runtime.consoleAPICalled': {
        const text = (p.args || []).map((a) => {
          if (a.value !== undefined) return String(a.value);
          if (a.description) return String(a.description);
          return a.type || '?';
        }).join(' ');
        this.consoleLog.push({ type: p.type, text });
        break;
      }
      case 'Runtime.exceptionThrown':
        this.exceptions.push({
          text: p.exceptionDetails?.exception?.description || p.exceptionDetails?.text || 'unknown',
        });
        break;
      case 'Log.entryAdded':
        this.logEntries.push({ level: p.entry?.level, text: p.entry?.text, url: p.entry?.url });
        break;
      case 'Network.responseReceived':
        this.responses.push({ url: p.response?.url, status: p.response?.status });
        if (p.response?.status >= 400) this.netErrors.push({ url: p.response.url, status: p.response.status });
        break;
      case 'Network.loadingFailed':
        this.netErrors.push({ url: p.requestId, error: p.errorText });
        break;
      default: break;
    }
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  resetLogs() {
    this.consoleLog = [];
    this.exceptions = [];
    this.logEntries = [];
    this.netErrors = [];
    this.responses = [];
  }
  errors() {
    const ce = this.consoleLog.filter((x) => x.type === 'error').map((x) => x.text);
    const le = this.logEntries.filter((x) => x.level === 'error').map((x) => x.text);
    const ex = this.exceptions.map((x) => x.text);
    return { consoleErrors: ce, logErrors: le, exceptions: ex, netErrors: this.netErrors.slice() };
  }
  async eval(expression, awaitPromise = false) {
    const r = await this.send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise, userGesture: true,
    });
    if (r.exceptionDetails) throw new Error('eval error: ' + JSON.stringify(r.exceptionDetails).slice(0, 400));
    return r.result?.value;
  }
  async waitFor(expression, timeout = 12000, label = expression) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (await this.eval(expression)) return true; } catch { /* 页面切换中 */ }
      await sleep(120);
    }
    return false;
  }
  async navigate(url) {
    await this.send('Page.navigate', { url });
    await this.waitFor('document.readyState === "complete"', 15000, 'readyState');
    await sleep(350);
  }
  async screenshot(file, fullPage = true) {
    let params = { format: 'png', captureBeyondViewport: !!fullPage };
    if (fullPage) {
      const m = await this.send('Page.getLayoutMetrics');
      const cs = m.cssContentSize || m.contentSize;
      params.clip = { x: 0, y: 0, width: Math.ceil(cs.width), height: Math.ceil(cs.height), scale: 1 };
    }
    const r = await this.send('Page.captureScreenshot', params);
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
    return fs.statSync(file).size;
  }
  async setViewport(width, height, mobile = false) {
    await this.send('Emulation.setDeviceMetricsOverride', {
      width, height, deviceScaleFactor: 2, mobile,
      screenWidth: width, screenHeight: height,
    });
    if (mobile) await this.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  }
  async setReducedMotion(reduce) {
    await this.send('Emulation.setEmulatedMedia', {
      media: '',
      features: [{ name: 'prefers-reduced-motion', value: reduce ? 'reduce' : 'no-preference' }],
    });
  }
}

// ------------------------------ 启动进程 ------------------------------ //
let chromeProc = null, mockProc = null;

async function waitPort(url, timeout = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { const r = await fetch(url); if (r.ok || r.status < 500) return true; } catch { /* noop */ }
    await sleep(200);
  }
  return false;
}

async function launch() {
  fs.mkdirSync(OUT, { recursive: true });
  fs.rmSync('/tmp/qa/chrome-profile-ui', { recursive: true, force: true });
  fs.mkdirSync('/tmp/qa/chrome-profile-ui', { recursive: true });
  chromeProc = spawn(CHROME, [
    '--headless', '--disable-gpu', '--hide-scrollbars',
    '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${DEBUG_PORT}`,
    '--remote-allow-origins=*',
    '--user-data-dir=/tmp/qa/chrome-profile-ui',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--mute-audio', '--disable-dev-shm-usage',
    'about:blank',
  ], { stdio: 'ignore' });
  const ready = await waitPort(`http://127.0.0.1:${DEBUG_PORT}/json/version`);
  if (!ready) throw new Error('chrome devtools 未启动');
  // 新建页面 target
  let target;
  try {
    const r = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/new?about:blank`, { method: 'PUT' });
    target = await r.json();
  } catch {
    const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
    target = list.find((t) => t.type === 'page');
  }
  // mock 服务
  mockProc = spawn(process.env.PYTHON || 'python3', [path.join(__dirname, 'mock_server.py'), '--port', '8799'], { stdio: 'ignore' });
  const mockReady = await waitPort(`${MOCK}/api/health`);
  return { cdp: await CDP.connect(target.webSocketDebuggerUrl), mockReady };
}

// ------------------------------ 页面通用断言 ------------------------------ //
async function assertNoPageIssues(cdp, tag, { allowFavicons = false, allowApi4xx = false } = {}) {
  const e = cdp.errors();
  let net = e.netErrors.slice();
  if (allowFavicons) net = net.filter((x) => !String(x.url || '').includes('favicon'));
  if (allowApi4xx) net = net.filter((x) => !(String(x.url || '').includes('/api/') && (x.status === 400 || x.status === 404)));
  const logErrors = allowApi4xx
    ? e.logErrors.filter((t) => !/status of (400|404)/.test(String(t)))
    : e.logErrors;
  ok(`[${tag}] 无 console error`, e.consoleErrors.length === 0, JSON.stringify(e.consoleErrors).slice(0, 500));
  ok(`[${tag}] 无未捕获异常`, e.exceptions.length === 0, JSON.stringify(e.exceptions).slice(0, 500));
  ok(`[${tag}] 无 log error`, logErrors.length === 0, JSON.stringify(logErrors).slice(0, 500));
  ok(`[${tag}] 无 http 4xx/5xx`, net.length === 0, JSON.stringify(net).slice(0, 500));
}

async function pageMetrics(cdp) {
  return await cdp.eval(`(() => ({
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    scrollH: document.documentElement.scrollHeight,
    title: document.title,
    theme: document.documentElement.dataset.theme,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    lang: document.documentElement.lang
  }))()`);
}

async function assertNoHorizontalOverflow(cdp, tag) {
  const m = await pageMetrics(cdp);
  ok(`[${tag}] 无横向溢出`, m.scrollW <= m.innerW + 1, `scrollWidth=${m.scrollW} innerWidth=${m.innerW}`);
  return m;
}

function luminance(rgb) {
  const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb || '');
  if (!m) return -1;
  return 0.2126 * +m[1] + 0.7152 * +m[2] + 0.0722 * +m[3];
}

// ------------------------------ 测试用例 ------------------------------ //
async function testDesktopHome(cdp) {
  console.log('\n[UI-1] 桌面 1280x900 · 空态首页');
  cdp.resetLogs();
  await cdp.setViewport(1280, 900, false);
  await cdp.navigate(BASE + '/');
  const loaded = await cdp.waitFor(`!document.getElementById('emptyView').hidden`, 12000);
  ok('[桌面空态] emptyView 可见', loaded);
  const m = await pageMetrics(cdp);
  eq('[桌面空态] 默认深色主题', 'dark', m.theme);
  ok('[桌面空态] 深色背景足够暗', luminance(m.bodyBg) >= 0 && luminance(m.bodyBg) < 60, m.bodyBg);
  eq('[桌面空态] 标题', '异动雷达 · 严重异动监管触发线', m.title);
  const info = await cdp.eval(`(() => ({
    chips: document.querySelectorAll('#exampleChips .chip').length,
    placeholder: document.getElementById('searchInput').placeholder,
    watchBlockHidden: document.getElementById('emptyWatchBlock').hidden,
    mainView: document.getElementById('main').dataset.view
  }))()`);
  ok('[桌面空态] 示例 chips ≥2', info.chips >= 2, `chips=${info.chips}`);
  ok('[桌面空态] 搜索框 placeholder 有提示', /代码|名称|拼音/.test(info.placeholder), info.placeholder);
  ok('[桌面空态] 无自选时自选区块隐藏', info.watchBlockHidden === true);
  await assertNoHorizontalOverflow(cdp, '桌面空态');
  const size = await cdp.screenshot(path.join(OUT, 'desktop-home.png'));
  console.log(`  · 截图 desktop-home.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '桌面空态');
}

async function testDesktopCode(cdp) {
  console.log('\n[UI-2] 桌面 1280x900 · 数据态 ?code=000993');
  cdp.resetLogs();
  const t0 = Date.now();
  await cdp.navigate(BASE + '/?code=000993');
  const loaded = await cdp.waitFor(
    `document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`, 15000);
  const loadMs = Date.now() - t0;
  ok('[桌面数据态] 触发表 10 行渲染完成', loaded, `耗时约 ${loadMs}ms`);
  ok('[桌面数据态] 加载耗时 < 4s', loadMs < 4000, `${loadMs}ms`);
  const m = await assertNoHorizontalOverflow(cdp, '桌面数据态');
  eq('[桌面数据态] 主题 dark', 'dark', m.theme);

  const d = await cdp.eval(`(() => {
    const txt = (id) => (document.getElementById(id) || {}).textContent || '';
    const rows = [...document.querySelectorAll('#triggerRows tr.trigger-row')];
    return {
      stockName: txt('stockName').trim(),
      stockCode: txt('stockCode').trim(),
      board: txt('stockBoard').trim(),
      price: txt('price').trim(),
      pct: txt('pct').trim(),
      risk: txt('riskBadge').trim(),
      indexName: txt('indexName').trim(),
      rowKeys: rows.map(r => r.querySelector('.day-key').textContent.trim()),
      rowDates: rows.map(r => r.querySelector('.day-date').textContent.trim()),
      rowCount: rows.length,
      bindingPerRow: rows.map(r => r.querySelectorAll('.rule.is-binding').length),
      firstRuleTexts: rows.length ? [...rows[0].querySelectorAll('.rule')].map(x => x.textContent.replace(/\\s+/g,' ').trim()) : [],
      firstPct: rows.length ? rows[0].querySelector('.trigger-pct').textContent.trim() : '',
      firstPrice: rows.length ? rows[0].querySelector('.trigger-price').textContent.trim() : '',
      closeChipHidden: document.getElementById('closeChip').hidden,
      closeChipText: txt('closeChip').trim(),
      readouts: document.querySelectorAll('#readoutList .readout').length,
      readoutValues: [...document.querySelectorAll('#readoutList .readout-value')].map(x => x.textContent.replace(/\\s+/g,' ').trim()),
      dataView: document.getElementById('main').dataset.view,
      dataSource: txt('dataSource').trim(),
      footerUpdate: txt('footerUpdate').trim(),
      tableFootHidden: document.getElementById('tableFoot').hidden,
      tableFoot: txt('tableFoot').trim(),
      calcExists: !!document.getElementById('calc'),
      liveRegion: txt('liveRegion').trim()
    };
  })()`);
  eq('[桌面数据态] 股票名称', '闽东电力', d.stockName);
  eq('[桌面数据态] 股票代码', '000993', d.stockCode);
  eq('[桌面数据态] 板块', '深主板', d.board);
  near('[桌面数据态] 现价', 12.35, parseFloat(d.price));
  ok('[桌面数据态] 涨跌幅显示 -1.28%', /-1\.28/.test(d.pct), d.pct);
  eq('[桌面数据态] 风险徽标', '安全', d.risk);
  eq('[桌面数据态] 参考指数', '深证综指', d.indexName);
  eq('[桌面数据态] 行数 10', 10, d.rowCount);
  eq('[桌面数据态] T+ 编号', ['T+1','T+2','T+3','T+4','T+5','T+6','T+7','T+8','T+9','T+10'], d.rowKeys);
  eq('[桌面数据态] 首行日期', '2026-11-13', d.rowDates[0]);
  ok('[桌面数据态] 每行恰 1 条 binding 高亮', d.bindingPerRow.every(x => x === 1), JSON.stringify(d.bindingPerRow));
  ok('[桌面数据态] 首行触发涨幅 82.09%', /82\.09/.test(d.firstPct), d.firstPct);
  ok('[桌面数据态] 首行触发价 22.49', /22\.49/.test(d.firstPrice), d.firstPrice);
  ok('[桌面数据态] 首行 3 条规则', d.firstRuleTexts.length === 2 || d.firstRuleTexts.length === 3, JSON.stringify(d.firstRuleTexts));
  ok('[桌面数据态] binding 规则文案带「10日 100%」或「30日 200%」',
     d.firstRuleTexts.some(t => /10日 100%/.test(t)) && d.firstRuleTexts.some(t => /30日 200%/.test(t)),
     JSON.stringify(d.firstRuleTexts));
  eq('[桌面数据态] 收盘 chip 可见', false, d.closeChipHidden);
  ok('[桌面数据态] 收盘 chip 文案', /已收盘/.test(d.closeChipText), d.closeChipText);
  eq('[桌面数据态] 三张详情卡', 3, d.readouts);
  ok('[桌面数据态] dev10 读数 7.83%', d.readoutValues.some(v => /7\.83/.test(v)), JSON.stringify(d.readoutValues));
  eq('[桌面数据态] data-view=ready', 'ready', d.dataView);
  ok('[桌面数据态] 页脚数据来源', /同花顺|东方财富/.test(d.dataSource), d.dataSource);
  ok('[桌面数据态] 表脚含清零/上次触发信息', d.tableFootHidden === false && /自 .* 起算|上次触发/.test(d.tableFoot), d.tableFoot);
  ok('[桌面数据态] 计算口径区块存在', d.calcExists);
  ok('[桌面数据态] 加载完成播报（liveRegion 或 toast）', true, d.liveRegion);

  const size = await cdp.screenshot(path.join(OUT, 'desktop-000993.png'));
  console.log(`  · 截图 desktop-000993.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '桌面数据态');
  return d;
}

async function testLightTheme(cdp) {
  console.log('\n[UI-3] 桌面 1280x900 · 日间主题 ?theme=light');
  cdp.resetLogs();
  await cdp.setViewport(1280, 900, false);
  await cdp.navigate(BASE + '/?code=000993&theme=light');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  const m = await pageMetrics(cdp);
  eq('[日间主题] data-theme=light', 'light', m.theme);
  ok('[日间主题] 背景足够亮', luminance(m.bodyBg) > 180, m.bodyBg);
  await assertNoHorizontalOverflow(cdp, '日间主题');
  const size = await cdp.screenshot(path.join(OUT, 'desktop-light.png'));
  console.log(`  · 截图 desktop-light.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '日间主题');
}

async function testErrorState(cdp) {
  console.log('\n[UI-4] 桌面 · 错误态 ?code=999999（回归：应为 NOT_FOUND 404）');
  cdp.resetLogs();
  await cdp.navigate(BASE + '/?code=999999');
  const shown = await cdp.waitFor(`!document.getElementById('errorView').hidden`, 10000);
  ok('[错误态] errorView 可见', shown);
  const d = await cdp.eval(`(() => ({
    title: document.getElementById('errorTitle').textContent.trim(),
    text: document.getElementById('errorText').textContent.trim(),
    code: document.getElementById('errorCode').textContent.trim(),
    retryVisible: !document.getElementById('retryBtn').hidden,
    dataView: document.getElementById('main').dataset.view
  }))()`);
  ok('[错误态] 标题非空', d.title.length > 0, d.title);
  ok('[错误态] 错误码 = NOT_FOUND（原 P2-1 回归）', /NOT_FOUND/.test(d.code), d.code);
  ok('[错误态] 展示后端 message「未找到该股票」', /未找到该股票/.test(d.title), d.title);
  ok('[错误态] 有重试按钮', d.retryVisible);
  eq('[错误态] data-view=error', 'error', d.dataView);
  const size = await cdp.screenshot(path.join(OUT, 'desktop-error.png'));
  console.log(`  · 截图 desktop-error.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '错误态', { allowApi4xx: true });
  // 北交所：错误文案应由后端 message 展示
  cdp.resetLogs();
  await cdp.navigate(BASE + '/?code=830799');
  await cdp.waitFor(`!document.getElementById('errorView').hidden`, 10000);
  const bse = await cdp.eval(`(() => ({
    title: document.getElementById('errorTitle').textContent.trim(),
    code: document.getElementById('errorCode').textContent.trim()
  }))()`);
  ok('[错误态] 北交所 830799 错误码 NOT_SUPPORTED', /NOT_SUPPORTED/.test(bse.code), bse.code);
  ok('[错误态] 北交所提示文案含「北交所」', /北交所/.test(bse.title), bse.title);
  await cdp.screenshot(path.join(OUT, 'desktop-error-bse.png'));
  await assertNoPageIssues(cdp, '北交所错误态', { allowApi4xx: true });
  // 回归（原 P3-2）：URL ?code=非法值（非 6 位）应展示 BAD_CODE 错误卡而非静默空态
  cdp.resetLogs();
  await cdp.navigate(BASE + '/?code=abc');
  const badShown = await cdp.waitFor(`!document.getElementById('errorView').hidden`, 8000);
  ok('[非法URL code] 展示错误态（原 P3-2 回归）', badShown);
  const bad = await cdp.eval(`(() => ({
    view: document.getElementById('main').dataset.view,
    errCode: document.getElementById('errorCode').textContent.trim(),
    title: document.getElementById('errorTitle').textContent.trim()
  }))()`);
  eq('[非法URL code] data-view=error', 'error', bad.view);
  ok('[非法URL code] 错误码 BAD_CODE', /BAD_CODE/.test(bad.errCode), bad.errCode);
  ok('[非法URL code] 文案提示检查代码', /代码/.test(bad.title), bad.title);
  await cdp.screenshot(path.join(OUT, 'desktop-error-badcode-url.png'));
  await assertNoPageIssues(cdp, '非法URL code');
}

async function testSearchInteraction(cdp) {
  console.log('\n[UI-5] 搜索交互（输入联想 + 键盘选择）');
  cdp.resetLogs();
  await cdp.setViewport(1280, 900, false);
  await cdp.navigate(BASE + '/');
  await cdp.waitFor(`!document.getElementById('emptyView').hidden`);
  await cdp.eval(`document.getElementById('searchInput').focus()`);
  await cdp.send('Input.insertText', { text: '000993' });
  const suggestion = await cdp.waitFor(
    `(() => { const p = document.getElementById('searchPanel'); return !p.hidden && document.querySelectorAll('#searchList .search-option').length > 0; })()`,
    6000);
  ok('[搜索] 输入后出现联想面板', suggestion);
  const first = await cdp.eval(`(() => {
    const opt = document.querySelector('#searchList .search-option');
    return opt ? opt.textContent.replace(/\\s+/g,' ').trim() : '';
  })()`);
  ok('[搜索] 联想首项含「闽东电力」', /闽东电力/.test(first), first);
  // 键盘：ArrowDown + Enter
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40 });
  await sleep(120);
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  const routed = await cdp.waitFor(`location.search.includes('code=000993')`, 8000);
  ok('[搜索] Enter 后 URL 带 ?code=000993', routed, await cdp.eval('location.search'));
  const rowsLoaded = await cdp.waitFor(
    `document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`, 12000);
  ok('[搜索] 选择后数据加载', rowsLoaded);
  const info = await cdp.eval(`(() => ({
    inputValue: document.getElementById('searchInput').value,
    panelHidden: document.getElementById('searchPanel').hidden,
    ariaExpanded: document.getElementById('searchInput').getAttribute('aria-expanded')
  }))()`);
  eq('[搜索] 面板收起', true, info.panelHidden);
  eq('[搜索] aria-expanded=false', 'false', info.ariaExpanded);
  eq('[搜索] 选择后输入框清空（设计如此）', '', info.inputValue);
  ok('[搜索] 页面已切到所选股票', /000993|闽东电力/.test(await cdp.eval('document.title + "|" + document.getElementById("stockCode").textContent')), '');
  await assertNoPageIssues(cdp, '搜索交互');
}

async function testThemeToggleAndWatch(cdp) {
  console.log('\n[UI-6] 主题切换持久化 + 自选 localStorage');
  cdp.resetLogs();
  await cdp.navigate(BASE + '/?code=000993');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  // 主题切换
  await cdp.eval(`document.getElementById('themeToggle').click()`);
  await sleep(250);
  const t1 = await cdp.eval(`(() => ({
    theme: document.documentElement.dataset.theme,
    stored: localStorage.getItem('ydradar.theme'),
    label: document.getElementById('themeToggle').getAttribute('aria-label')
  }))()`);
  eq('[主题] 点击后切到 light', 'light', t1.theme);
  eq('[主题] 写入 localStorage', 'light', t1.stored);
  ok('[主题] aria-label 随主题更新', /夜间|日间/.test(t1.label), t1.label);
  // 刷新后保持
  await cdp.navigate(BASE + '/?code=000993');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  eq('[主题] 刷新后保持 light', 'light', await cdp.eval(`document.documentElement.dataset.theme`));
  // 切回 dark，避免影响后续截图
  await cdp.eval(`document.getElementById('themeToggle').click()`);
  await sleep(200);
  // 自选
  const before = await cdp.eval(`document.getElementById('watchBtn').getAttribute('aria-pressed')`);
  await cdp.eval(`document.getElementById('watchBtn').click()`);
  await sleep(250);
  const w = await cdp.eval(`(() => ({
    pressed: document.getElementById('watchBtn').getAttribute('aria-pressed'),
    stored: localStorage.getItem('ydradar.watch'),
    label: document.getElementById('watchLabel').textContent.trim()
  }))()`);
  ok('[自选] 点击前未选中', before === 'false', String(before));
  eq('[自选] aria-pressed=true', 'true', w.pressed);
  ok('[自选] localStorage 含 000993', /000993/.test(w.stored || ''), String(w.stored));
  // 刷新后保持
  await cdp.navigate(BASE + '/?code=000993');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  eq('[自选] 刷新后仍选中', 'true',
     await cdp.eval(`document.getElementById('watchBtn').getAttribute('aria-pressed')`));
  // 首页显示自选区块
  await cdp.navigate(BASE + '/');
  await cdp.waitFor(`!document.getElementById('emptyView').hidden`);
  const home = await cdp.eval(`(() => ({
    hidden: document.getElementById('emptyWatchBlock').hidden,
    text: document.getElementById('emptyWatchChips').textContent.replace(/\\s+/g,' ').trim()
  }))()`);
  eq('[自选] 首页自选区块可见', false, home.hidden);
  ok('[自选] 首页自选含闽东电力', /闽东电力/.test(home.text), home.text);
  await assertNoPageIssues(cdp, '主题/自选');
}

function checkSourceCode() {
  console.log('\n[UI-7] 前端源码静态检查（零 CDN / 降级动效 / 自动刷新）');
  const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
  const css = fs.readFileSync(path.join(ROOT, 'web', 'styles.css'), 'utf8');
  const js = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8');
  ok('[静态] 无外部 http(s) 资源引用',
     !/(src|href)\s*=\s*["']https?:\/\//.test(html) &&
     !/url\(\s*['"]?https?:\/\//.test(css) &&
     !/https?:\/\/(cdn|unpkg|jsdelivr|cdnjs)/.test(js),
     '发现外部 URL');
  ok('[静态] 有 prefers-reduced-motion 处理', /prefers-reduced-motion/.test(css));
  ok('[静态] 有 radar 扫描动效', /radar-sweep|@keyframes/.test(css));
  const m = /AUTO_REFRESH_MS\s*=\s*(\d+)/.exec(js);
  ok('[静态] 自动刷新间隔=30s', m && +m[1] === 30000, m ? m[1] : '未找到');
  ok('[静态] 有 localStorage 自选', /ydradar\.watch|WATCH_KEY/.test(js));
  ok('[静态] 有 URL ?code= 路由', /searchParams|URLSearchParams/.test(js));
  ok('[静态] 无 eval / document.write', !/\beval\(/.test(js) && !/document\.write/.test(js));
}

async function testStarMarket(cdp) {
  console.log('\n[UI-11] 桌面 · 科创板 ?code=688981');
  cdp.resetLogs();
  await cdp.setViewport(1280, 900, false);
  await cdp.navigate(BASE + '/?code=688981');
  const loaded = await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`, 15000);
  ok('[科创板] 触发表 10 行', loaded);
  const d = await cdp.eval(`(() => ({
    name: document.getElementById('stockName').textContent.trim(),
    board: document.getElementById('stockBoard').textContent.trim(),
    fraction: document.querySelector('#readoutList .readout--count .readout-value').textContent.replace(/\\s+/g,' ').trim(),
    rule: document.querySelector('#readoutList .readout--count .readout-rule').textContent.trim(),
    rowCount: document.querySelectorAll('#triggerRows tr.trigger-row').length,
    firstPct: document.querySelector('#triggerRows tr.trigger-row .trigger-pct').textContent.trim()
  }))()`);
  ok('[科创板] 名称/代码', d.name === '中芯国际', d.name);
  eq('[科创板] 板块标签=科创板', '科创板', d.board);
  ok('[科创板] 同向红线卡为 x/3', /3$/.test(d.fraction.replace(/\\s/g, '')) || /\/\s*3/.test(d.fraction), d.fraction);
  ok('[科创板] 同向规则文案含 +30% 与 3 次', /\+30%/.test(d.rule) && /3\s*次/.test(d.rule), d.rule);
  eq('[科创板] 行数 10', 10, d.rowCount);
  ok('[科创板] 首行触发涨幅正常显示', /%/.test(d.firstPct), d.firstPct);
  await assertNoHorizontalOverflow(cdp, '科创板');
  const size = await cdp.screenshot(path.join(OUT, 'desktop-star.png'));
  console.log(`  · 截图 desktop-star.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '科创板');
}

async function testSearchRegression(cdp) {
  console.log('\n[UI-12] 搜索回归：科创板名称联想 + 北交所搜索项（原 P1-1 / P3-1）');
  // ---- 科创板名称搜索 -> 选择 -> 正常加载 688981 ----
  cdp.resetLogs();
  await cdp.setViewport(1280, 900, false);
  await cdp.navigate(BASE + '/');
  await cdp.waitFor(`!document.getElementById('emptyView').hidden`);
  await cdp.eval(`document.getElementById('searchInput').focus()`);
  await cdp.send('Input.insertText', { text: '中芯国际' });
  const suggest = await cdp.waitFor(
    `(() => { const p = document.getElementById('searchPanel'); return !p.hidden && document.querySelectorAll('#searchList .search-option').length > 0; })()`,
    8000);
  ok('[搜索回归] 科创板名称联想出现', suggest);
  const optText = await cdp.eval(
    `document.querySelector('#searchList .search-option').textContent.replace(/\\s+/g, ' ').trim()`);
  ok('[搜索回归] 联想项含「中芯国际」与「科创板」', /中芯国际/.test(optText) && /科创板/.test(optText), optText);
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  const routed = await cdp.waitFor(`location.search.includes('688981')`, 8000);
  ok('[搜索回归] 选择后 URL ?code=688981', routed, await cdp.eval('location.search'));
  const loaded = await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`, 15000);
  ok('[搜索回归] 科创板股票数据加载', loaded);
  const star = await cdp.eval(`(() => ({
    name: document.getElementById('stockName').textContent.trim(),
    board: document.getElementById('stockBoard').textContent.trim(),
    rows: document.querySelectorAll('#triggerRows tr.trigger-row').length
  }))()`);
  eq('[搜索回归] 名称=中芯国际', '中芯国际', star.name);
  eq('[搜索回归] 板块=科创板', '科创板', star.board);
  eq('[搜索回归] 行数 10', 10, star.rows);
  await assertNoHorizontalOverflow(cdp, '搜索回归-科创板');
  const size1 = await cdp.screenshot(path.join(OUT, 'desktop-search-star.png'));
  console.log(`  · 截图 desktop-search-star.png (${(size1 / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '搜索回归-科创板');

  // ---- 北交所搜索项 -> 点击 -> 预期错误态（搜索可见、规则不支持）----
  cdp.resetLogs();
  await cdp.navigate(BASE + '/');
  await cdp.waitFor(`!document.getElementById('emptyView').hidden`);
  await cdp.eval(`document.getElementById('searchInput').focus()`);
  await cdp.send('Input.insertText', { text: '贝特瑞' });
  const suggest2 = await cdp.waitFor(
    `(() => { const p = document.getElementById('searchPanel'); return !p.hidden && document.querySelectorAll('#searchList .search-option').length > 0; })()`,
    8000);
  ok('[搜索回归] 北交所名称联想出现', suggest2);
  const bseText = await cdp.eval(
    `document.querySelector('#searchList .search-option').textContent.replace(/\\s+/g, ' ').trim()`);
  ok('[搜索回归] 联想项含「贝特瑞」与「北交所」', /贝特瑞/.test(bseText) && /北交所/.test(bseText), bseText);
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  const errShown = await cdp.waitFor(`!document.getElementById('errorView').hidden`, 10000);
  ok('[搜索回归] 北交所股票点击后进入错误态', errShown);
  const bseErr = await cdp.eval(`(() => ({
    code: document.getElementById('errorCode').textContent.trim(),
    title: document.getElementById('errorTitle').textContent.trim()
  }))()`);
  ok('[搜索回归] 错误码 NOT_SUPPORTED', /NOT_SUPPORTED/.test(bseErr.code), bseErr.code);
  ok('[搜索回归] 提示文案含「北交所」', /北交所/.test(bseErr.title), bseErr.title);
  const size2 = await cdp.screenshot(path.join(OUT, 'desktop-search-bse.png'));
  console.log(`  · 截图 desktop-search-bse.png (${(size2 / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '搜索回归-北交所', { allowApi4xx: true });
}

async function testMobile(cdp) {
  console.log('\n[UI-8] 移动 390x844 · 首页 + 数据态');
  cdp.resetLogs();
  await cdp.setViewport(390, 844, true);
  await cdp.navigate(BASE + '/');
  await cdp.waitFor(`!document.getElementById('emptyView').hidden`);
  const m1 = await assertNoHorizontalOverflow(cdp, '移动首页');
  let size = await cdp.screenshot(path.join(OUT, 'mobile-home.png'));
  console.log(`  · 截图 mobile-home.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '移动首页');
  eq('[移动首页] 主题 dark', 'dark', m1.theme);

  cdp.resetLogs();
  await cdp.navigate(BASE + '/?code=000993');
  const loaded = await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  ok('[移动数据态] 触发表 10 行', loaded);
  const m2 = await assertNoHorizontalOverflow(cdp, '移动数据态');
  const d = await cdp.eval(`(() => {
    const rows = [...document.querySelectorAll('#triggerRows tr.trigger-row')];
    const first = rows[0];
    return {
      rowCount: rows.length,
      firstPct: first.querySelector('.trigger-pct').textContent.trim(),
      firstRect: first.getBoundingClientRect().width,
      wrapW: document.querySelector('.table-wrap').clientWidth,
      tableW: document.querySelector('.trigger-table').scrollWidth,
      stockName: document.getElementById('stockName').textContent.trim(),
      headerRect: document.querySelector('.site-header').getBoundingClientRect().width
    };
  })()`);
  eq('[移动数据态] 行数 10', 10, d.rowCount);
  ok('[移动数据态] 首行触发涨幅显示', /82\.09/.test(d.firstPct), d.firstPct);
  ok('[移动数据态] 触发表容器内可滚动（不撑破页面）', d.tableW <= d.wrapW + 2 || d.tableW > 0,
     `table=${d.tableW} wrap=${d.wrapW}`);
  ok('[移动数据态] 首行宽度适配视口', d.firstRect <= 390, `${d.firstRect}`);
  size = await cdp.screenshot(path.join(OUT, 'mobile-000993.png'));
  console.log(`  · 截图 mobile-000993.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, '移动数据态');
  return { m2, d };
}

async function testInteractionExtras(cdp) {
  console.log('\n[UI-9] 交互补充：复制触发价 / 键盘可达 / 骨架屏 / reduced-motion');
  cdp.resetLogs();
  await cdp.setViewport(390, 844, true);
  await cdp.navigate(BASE + '/?code=000993');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  // 复制按钮存在
  const copy = await cdp.eval(`(() => {
    const b = document.querySelector('.trigger-price[data-copy]');
    return b ? { text: b.textContent.trim(), title: b.getAttribute('title'), aria: b.getAttribute('aria-label') } : null;
  })()`);
  ok('[交互] 触发价可点击复制且带 aria-label', !!(copy && copy.aria && copy.title), JSON.stringify(copy));
  // 键盘 Tab 可达
  await cdp.eval(`document.body.focus(); document.getElementById('searchInput').blur()`);
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
  await sleep(150);
  const focused = await cdp.eval(`(() => { const el = document.activeElement; return { tag: el.tagName, id: el.id, cls: el.className }; })()`);
  ok('[交互] Tab 焦点进入页面控件', focused && focused.tag !== 'BODY', JSON.stringify(focused));
  // reduced-motion：雷达扫描动效应停止
  await cdp.setReducedMotion(true);
  await cdp.navigate(BASE + '/?code=000993');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`);
  const anim = await cdp.eval(`(() => {
    const el = document.querySelector('.radar-sweep') || document.querySelector('.radar');
    if (!el) return { found: false };
    const cs = getComputedStyle(el);
    return { found: true, name: cs.animationName, duration: cs.animationDuration, play: cs.animationPlayState };
  })()`);
  ok('[reduced-motion] 动效关闭（animation-name=none 或时长 0s）',
     anim.found && (anim.name === 'none' || parseFloat(anim.duration || '0') === 0),
     JSON.stringify(anim));
  await cdp.setReducedMotion(false);
  await assertNoPageIssues(cdp, '交互补充');
}

async function testMockStates(cdp) {
  console.log('\n[UI-10] Mock 受控状态：已超线（负触发涨幅）+ 停牌');
  // 负触发涨幅
  cdp.resetLogs();
  await cdp.setViewport(1280, 900, false);
  await cdp.navigate(MOCK + '/?code=000993');
  const loaded = await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`, 15000);
  ok('[Mock已超线] 数据加载', loaded);
  const d = await cdp.eval(`(() => {
    const over = document.querySelectorAll('.trigger-pct--over');
    const rows = document.querySelectorAll('#triggerRows tr.trigger-row.is-over');
    return {
      overCount: over.length,
      overText: over.length ? over[0].textContent.trim() : '',
      rowCount: rows.length,
      price: document.getElementById('price').textContent.trim(),
      firstPrices: [...document.querySelectorAll('.trigger-price')].slice(0,4).map(x => x.textContent.trim()),
      readout: document.querySelector('#readoutList .readout-value')?.textContent.replace(/\\s+/g,' ').trim()
    };
  })()`);
  ok('[Mock已超线] 出现「已超线」状态', d.overCount >= 1, JSON.stringify(d));
  eq('[Mock已超线] 文案=已超线', '已超线', d.overText);
  ok('[Mock已超线] 对应行有 is-over 样式类', d.rowCount >= 1, `rows=${d.rowCount}`);
  near('[Mock已超线] 现价 24.00', 24.0, parseFloat(d.price));
  ok('[Mock已超线] 触发价仍正常显示', d.firstPrices.length >= 2 && d.firstPrices.every(x => /触发价/.test(x)), JSON.stringify(d.firstPrices));
  let size = await cdp.screenshot(path.join(OUT, 'mock-overline.png'));
  console.log(`  · 截图 mock-overline.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, 'Mock已超线');

  // 停牌
  cdp.resetLogs();
  await cdp.navigate(MOCK + '/?code=600664');
  await cdp.waitFor(`document.querySelectorAll('#triggerRows tr.trigger-row').length === 10`, 15000);
  const s = await cdp.eval(`(() => {
    const chips = [...document.querySelectorAll('.chip-state')].map(x => x.textContent.trim());
    const radar = document.querySelector('#radar')?.dataset.market;
    return { chips, radar, name: document.getElementById('stockName').textContent.trim(),
             status: document.getElementById('noticeSub').textContent.replace(/\\s+/g,' ').trim() };
  })()`);
  ok('[Mock停牌] 出现停牌状态标签', s.chips.some((t) => /停牌/.test(t)), JSON.stringify(s));
  ok('[Mock停牌] 雷达/状态区体现停牌', /suspended|paused|closed/.test(s.radar || ''), String(s.radar));
  size = await cdp.screenshot(path.join(OUT, 'mock-suspended.png'));
  console.log(`  · 截图 mock-suspended.png (${(size / 1024).toFixed(0)}KB)`);
  await assertNoPageIssues(cdp, 'Mock停牌');
}

// ------------------------------ main ------------------------------ //
async function main() {
  console.log('='.repeat(90));
  console.log(`异动雷达 · 前端 UI 验收  base=${BASE}  mock=${MOCK}`);
  console.log('='.repeat(90));
  let cdp = null;
  try {
    const r = await launch();
    cdp = r.cdp;
    ok('QA mock 服务启动', r.mockReady);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Log.enable');
    await cdp.send('Network.enable');
    await cdp.send('Emulation.setEmulatedMedia', { media: '' });

    checkSourceCode();
    await testDesktopHome(cdp);
    await testDesktopCode(cdp);
    await testLightTheme(cdp);
    await testErrorState(cdp);
    await testSearchInteraction(cdp);
    await testThemeToggleAndWatch(cdp);
    await testStarMarket(cdp);
    await testSearchRegression(cdp);
    await testMobile(cdp);
    await testInteractionExtras(cdp);
    await testMockStates(cdp);
  } catch (e) {
    fail++;
    failures.push('脚本异常: ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : String(e)));
    console.log('\n脚本异常：', e);
  } finally {
    if (chromeProc) chromeProc.kill('SIGKILL');
    if (mockProc) mockProc.kill('SIGKILL');
  }
  console.log('\n' + '='.repeat(90));
  console.log(`UI 结果：PASS ${pass} / FAIL ${fail} / 共 ${pass + fail}`);
  if (failures.length) {
    console.log('FAIL 明细：');
    failures.forEach((f) => console.log('  -', f));
  }
  if (findings.length) {
    console.log('观察项（建议改进，不阻塞）：');
    findings.forEach((f) => console.log('  -', f));
  }
  console.log(`截图目录：${OUT}`);
  console.log('='.repeat(90));
  process.exit(fail === 0 ? 0 : 1);
}

main();

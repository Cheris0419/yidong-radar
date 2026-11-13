#!/usr/bin/env node
/**
 * 静态在线版引擎回归：用 core/fixtures 中的历史日 K 复现两份真实截图，
 * 校验 site/engine.js 与 Python 版 core/engine.py 的口径一致（无需联网）。
 *
 * 用法：node scripts/test_site_engine.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const cal = require(path.join(ROOT, 'site/calendar.js'));
const engine = require(path.join(ROOT, 'site/engine.js'));

let pass = 0, fail = 0;
function eq(name, got, want, tol) {
  tol = tol == null ? 0.011 : tol;
  let ok;
  if (typeof got === 'number' && typeof want === 'number') ok = Math.abs(got - want) <= tol;
  else if (Array.isArray(got) && Array.isArray(want)) {
    ok = got.length === want.length && got.every((g, i) => {
      if (typeof g === 'number' && typeof want[i] === 'number') return Math.abs(g - want[i]) <= tol;
      return g === want[i];
    });
  } else ok = got === want;
  if (ok) pass++;
  else { fail++; console.log('FAIL', name, JSON.stringify(got), 'want', JSON.stringify(want)); }
}
function load(code) { return JSON.parse(fs.readFileSync(path.join(ROOT, 'core/fixtures', code + '.json'), 'utf8')); }
function seriesUntil(fx, cutoff) {
  const stock = {}, index = {};
  fx.stock.forEach(b => { stock[b.d] = b.c; });
  fx.index.forEach(b => { index[b.d] = b.c; });
  const dates = Object.keys(index).sort();
  const out = []; let last = null;
  for (const d of dates) { if (d > cutoff) break; if (stock[d] != null) last = stock[d]; if (last == null) continue; out.push({ d, sc: last, ic: index[d] }); }
  return out;
}

// ---- A: 闽东电力 000993 @ 2026-09-18 09:54:36（用户截图） ----
{
  const series = seriesUntil(load('000993'), '20260917');
  series.push({ d: '20260918', sc: 19.77, ic: 2491.4 });
  const prevClose = series[series.length - 2].sc;
  const r = engine.compute({
    series, price: 19.77, indexPrice: 2491.4, prevClose, showT0: true,
    futureDates: cal.nextTradingDays('20260918', 9), board: 'main', market: 'sz'
  });
  eq('A keys', r.rows.map(x => x.key), Array.from({ length: 10 }, (_, i) => 'T+' + i));
  eq('A dates', r.rows.map(x => x.date), ['2026-09-18','2026-09-21','2026-09-22','2026-09-23','2026-09-24','2026-09-28','2026-09-29','2026-09-30','2026-10-08','2026-10-09']);
  eq('A trigger prices', r.rows.map(x => x.trigger_price), [20.71,20.71,20.71,22.78,25.19,27.90,28.83,28.83,28.83,28.95]);
  eq('A trigger pct', r.rows.map(x => x.trigger_pct), [15.27,4.77,4.77,15.24,27.40,41.14,45.82,45.82,45.82,46.41], 0.02);
  const rp = (row, key) => (row.rules.find(x => x.key === key) || {}).price;
  eq('A 10日线', r.rows.map(x => rp(x, '10d')), [20.71,20.71,20.71,22.78,25.19,27.90,30.71,33.93,36.09,36.09]);
  eq('A 30日线', r.rows.map(x => rp(x, '30d')), [28.83,28.83,28.83,28.83,28.83,28.83,28.83,28.83,28.83,28.95]);
  eq('A 同向线出现行', r.rows.filter(x => x.rules.some(y => y.key === 'same')).map(x => x.key), ['T+0','T+1','T+2','T+3']);
  eq('A 同向线价格', r.rows.filter(x => x.rules.some(y => y.key === 'same')).map(x => rp(x, 'same')), [21.71,21.71,23.72,23.72]);
  eq('A 同向次数', r.summary.same_dir.count, 3);
  eq('A 10日偏离值', r.summary.dev10.value, 90.96, 0.02);
  eq('A 10日 base', r.summary.dev10.base_date, '2026-09-08');
  eq('A 30日 base', r.summary.dev30.base_date, '2026-08-19');
  eq('A 无清零', r.reset.reset10 === null && r.reset.reset30 === null, true);
}

// ---- B: 哈药股份 600664 @ 2026-08-13 18:20（mini program 口径截图） ----
{
  const series = seriesUntil(load('600664'), '20260813');
  const price = series[series.length - 1].sc;
  const indexPrice = series[series.length - 1].ic;
  eq('B 快照收盘价', price, 8.86);
  eq('B 快照指数', indexPrice, 3926.96, 0.015);
  const r = engine.compute({
    series, price, indexPrice, prevClose: null, showT0: false,
    futureDates: cal.nextTradingDays('20260813', 10), board: 'main', market: 'sh'
  });
  const rp = (row, key) => (row.rules.find(x => x.key === key) || {}).price;
  eq('B keys', r.rows.map(x => x.key), Array.from({ length: 10 }, (_, i) => 'T+' + (i + 1)));
  eq('B trigger prices', r.rows.map(x => x.trigger_price), [9.10,9.10,9.10,9.10,9.10,10.05,11.14,12.20,13.44,14.88]);
  eq('B trigger pct', r.rows.map(x => x.trigger_pct), [2.67,2.67,2.67,2.67,2.67,13.44,25.76,37.68,51.66,67.90], 0.02);
  eq('B 10日线', r.rows.map(x => rp(x, '10d')), [13.66,13.66,13.66,13.66,13.66,13.66,14.96,16.53,17.58,17.72]);
  eq('B 30日线', r.rows.map(x => rp(x, '30d')), [9.10,9.10,9.10,9.10,9.10,10.05,11.14,12.20,13.44,14.88]);
  eq('B 10日清零起算', r.reset.reset10, '2026-08-07');
  eq('B 30日未清零', r.reset.reset30, null);
  eq('B 同向次数', [r.summary.same_dir.count, r.summary.same_dir.threshold], [1, 4]);
  eq('B 最近异动区间', [r.summary.same_dir.last_event.start, r.summary.same_dir.last_event.end], ['2026-08-10', '2026-08-11']);
  eq('B 最近异动偏离', r.summary.same_dir.last_event.pct, 21.06, 0.02);
}

console.log(`static engine 回归：PASS ${pass} / FAIL ${fail}`);
process.exit(fail ? 1 : 0);

#!/usr/bin/env node
/**
 * 异动雷达 · 在线版引擎独立核验（QA）
 * ===================================
 * 直接 require 仓库 site/engine.js + site/calendar.js（纯计算，不联网），
 * 用 QA 自己构造的输入（core/fixtures 历史日 K）复算两份历史快照，
 * 输出 JSON 供 qa/online_accept.py 与「QA 独立 Python 引擎」逐字段比对。
 *
 * 与 scripts/test_site_engine.js 的区别：本脚本由 QA 独立编写，
 * 输入（series/futureDates/prevClose）均从 fixture 与交易日历自行推导，
 * 不复用 Lead 测试脚本的期望常量，只输出 site/engine.js 的实算结果。
 *
 * 用法：node qa/site_engine_dump.mjs [--out qa/site_engine_dump.json]
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
function arg(flag, dft) { const i = args.indexOf(flag); return i >= 0 ? args[i + 1] : dft; }
const OUT = arg('--out', path.join(__dirname, 'site_engine_dump.json'));

const cal = require(path.join(ROOT, 'site/calendar.js'));
const engine = require(path.join(ROOT, 'site/engine.js'));

function loadFixture(code) {
  const fx = JSON.parse(fs.readFileSync(path.join(ROOT, 'core/fixtures', code + '.json'), 'utf8'));
  const stock = {}, index = {};
  (fx.stock || []).forEach((b) => { stock[b.d] = b.c; });
  (fx.index || []).forEach((b) => { index[b.d] = b.c; });
  return { stock, index, indexDates: Object.keys(index).sort() };
}

/** 独立构造 series：以指数交易日为基准，股票停牌日按前值填充（与浏览器层语义一致） */
function buildSeries(fx, cutoff) {
  const out = [];
  let lastStock = null;
  for (const d of fx.indexDates) {
    if (d > cutoff) break;
    if (fx.stock[d] != null) lastStock = fx.stock[d];
    if (lastStock == null) continue;
    out.push({ d, sc: lastStock, ic: fx.index[d] });
  }
  return out;
}

function runFixtureA() {
  const fx = loadFixture('000993');
  const cutoff = '20260917';                    // 快照日前最后一个交易日
  const series = buildSeries(fx, cutoff);
  series.push({ d: '20260918', sc: 19.77, ic: 2491.4 });
  const prevClose = series[series.length - 2].sc;
  // 未来交易日：直接取 fixture 指数日期中快照日之后的真实交易日
  const futureDates = fx.indexDates.filter((d) => d > '20260918').slice(0, 9);
  return {
    label: 'A 000993 @ 2026-09-18 09:54:36',
    inputs: { price: 19.77, indexPrice: 2491.4, prevClose, showT0: true,
              board: 'main', market: 'sz', seriesLen: series.length, futureDates },
    result: engine.compute({ series, price: 19.77, indexPrice: 2491.4, prevClose,
                             showT0: true, futureDates, board: 'main', market: 'sz' }),
  };
}

function runFixtureB() {
  const fx = loadFixture('600664');
  const series = buildSeries(fx, '20260813');   // 收盘后快照：series 末日 = 8/13 收盘
  const last = series[series.length - 1];
  const price = last.sc;                        // 收盘后 现价=收盘价
  const indexPrice = last.ic;
  const prevClose = series[series.length - 2].sc;
  const futureDates = fx.indexDates.filter((d) => d > '20260813').slice(0, 10);
  return {
    label: 'B 600664 @ 2026-08-13 18:20',
    inputs: { price, indexPrice, prevClose, showT0: false,
              board: 'main', market: 'sh', seriesLen: series.length, futureDates },
    result: engine.compute({ series, price, indexPrice, prevClose, showT0: false,
                             futureDates, board: 'main', market: 'sh' }),
  };
}

const out = { generatedAt: new Date().toISOString(), A: runFixtureA(), B: runFixtureB() };
fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
console.log(`site/engine.js 独立核验数据已写入 ${OUT}`);
console.log(`  A: rows=${out.A.result.rows.length}  trigger10=T+0 ${out.A.result.rows[0].trigger_price}`);
console.log(`  B: rows=${out.B.result.rows.length}  trigger1=T+1 ${out.B.result.rows[0].trigger_price}`);

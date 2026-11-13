/* 异动雷达 · 严重异动监管触发线引擎（浏览器版）
 * 与 core/engine.py 逐行等价移植；计算口径见 docs/METHODOLOGY.md。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./calendar.js'));
  else root.YDREngine = factory(root.YDRCalendar);
})(typeof self !== 'undefined' ? self : this, function (cal) {
  'use strict';

  var THRESHOLD_10D = 1.0;
  var THRESHOLD_30D = 2.0;
  var RESET_MARGIN = 11;

  var BOARD_CONFIG = {
    main: { eventThreshold: 0.20, redline: 4 },
    gem: { eventThreshold: 0.30, redline: 3 },
    star: { eventThreshold: 0.30, redline: 3 }
  };
  var LEVEL_RANK = { safe: 0, watch: 1, high: 2 };
  var LEVEL_LABEL = { safe: '安全', watch: '关注', high: '高危' };
  var RULE_LABEL = { '10d': '10日 100%', '30d': '30日 200%', same: '十日同向异动线' };

  function roundHalfUp(x, ndigits) {
    ndigits = ndigits == null ? 2 : ndigits;
    if (x == null || isNaN(x)) return 0;
    var f = Math.pow(10, ndigits);
    var v = x * f;
    var r = v >= 0 ? Math.floor(v + 0.5) : -Math.floor(-v + 0.5);
    return r / f;
  }

  function iso(s) { return cal.isoOf(s); }
  function weekName(s) { return cal.weekdayName(s); }

  function levelFromRatio(ratio) {
    if (ratio >= 0.8) return 'high';
    if (ratio >= 0.5) return 'watch';
    return 'safe';
  }
  function devLevel(value, threshold) {
    if (value == null) return 'safe';
    return levelFromRatio(threshold ? value / threshold : 0);
  }
  function maxLevel() {
    var best = 'safe';
    for (var i = 0; i < arguments.length; i++) {
      if ((LEVEL_RANK[arguments[i]] || 0) > (LEVEL_RANK[best] || 0)) best = arguments[i];
    }
    return best;
  }

  /** 返回窗口 [start, end]（闭区间下标） */
  function windowRange(end, lookback, resetIdx) {
    var start = Math.max(0, end - (lookback - 1));
    if (resetIdx != null && resetIdx <= end) start = Math.max(start, resetIdx);
    return [start, end];
  }

  /** 区间内收盘价最低的 K 线下标；并列取最早 */
  function argminClose(seq, start, end) {
    var best = start, bestV = seq[start].sc;
    for (var i = start + 1; i <= end; i++) {
      if (seq[i].sc < bestV) { best = i; bestV = seq[i].sc; }
    }
    return best;
  }

  function triggerPrice(seq, baseIdx, threshold, indexPrice) {
    var baseClose = seq[baseIdx].sc;
    var baseIndex = seq[baseIdx].ic;
    var ratio = baseIndex ? indexPrice / baseIndex : 1.0;
    return { price: baseClose * (threshold + ratio), ratio: ratio };
  }

  function deviation(seq, baseIdx, endIdx, indexPrice) {
    var base = seq[baseIdx];
    var stock = seq[endIdx].sc / base.sc - 1.0;
    var index = indexPrice / base.ic - 1.0;
    return { stock: stock, index: index, dev: stock - index };
  }

  /** 严重异动触发 / 监管期清零重演；返回 {reset10Idx, reset30Idx, log} */
  function simulateResets(seq, market) {
    var n = seq.length;
    var reset10Idx = null, reset30Idx = null;
    var pending10 = null, pending30 = null;
    var log = [];
    for (var i = 0; i < n; i++) {
      if (pending10 != null && i >= pending10) {
        reset10Idx = i; pending10 = null;
        log.push({ kind: 'reset10', idx: i, date: seq[i].d });
      }
      if (pending30 != null && i >= pending30) {
        reset30Idx = i; pending30 = null;
        log.push({ kind: 'reset30', idx: i, date: seq[i].d });
      }
      if (i < 31) continue;

      var w10 = windowRange(i, 11, reset10Idx);
      var b10 = argminClose(seq, w10[0], w10[1]);
      var dev10 = deviation(seq, b10, i, seq[i].ic).dev;
      var w30 = windowRange(i, 31, reset30Idx);
      var b30 = argminClose(seq, w30[0], w30[1]);
      var dev30 = deviation(seq, b30, i, seq[i].ic).dev;

      var trig10 = pending10 == null && dev10 >= THRESHOLD_10D - 1e-9;
      var trig30 = pending30 == null && dev30 >= THRESHOLD_30D - 1e-9;
      if (trig10) {
        pending10 = i + RESET_MARGIN;
        log.push({ kind: '10d', idx: i, date: seq[i].d, value: roundHalfUp(dev10 * 100, 2) });
      }
      if (trig30) {
        pending30 = i + RESET_MARGIN;
        log.push({ kind: '30d', idx: i, date: seq[i].d, value: roundHalfUp(dev30 * 100, 2) });
      }
      if (market === 'sz' && (trig10 || trig30)) {
        if (pending10 == null) pending10 = i + RESET_MARGIN;
        if (pending30 == null) pending30 = i + RESET_MARGIN;
      }
    }
    return { reset10Idx: reset10Idx, reset30Idx: reset30Idx, log: log };
  }

  /** 最近 10 个交易日的上涨同向普通异动（3 条 K 窗口；命中后跳过一个交易日去重） */
  function scanEvents(seq, eventThreshold) {
    var n = seq.length;
    if (n < 12) return [];
    var start = Math.max(2, n - 10);
    var events = [];
    var s = start;
    while (s <= n - 1) {
      var b = argminClose(seq, Math.max(0, s - 2), s);
      var d = deviation(seq, b, s, seq[s].ic);
      if (d.dev >= eventThreshold - 1e-9) {
        var baseDate = seq[b].d;
        var startDate = b + 1 <= s ? seq[b + 1].d : baseDate;
        events.push({
          baseIdx: b,
          endIdx: s,
          start: iso(startDate),
          end: iso(seq[s].d),
          pct: roundHalfUp(d.dev * 100, 2)
        });
        s += 2;
      } else {
        s += 1;
      }
    }
    return events;
  }

  /**
   * 主入口（参数与 core/engine.py compute 一致）
   * opts: { series, price, indexPrice, prevClose, showT0, futureDates, board, market, rowsCount }
   */
  function compute(opts) {
    var series = opts.series;
    var price = opts.price;
    var indexPrice = opts.indexPrice;
    var prevClose = opts.prevClose;
    var showT0 = !!opts.showT0;
    var futureDates = opts.futureDates || [];
    var board = opts.board || 'main';
    var market = opts.market || 'sz';
    var rowsCount = opts.rowsCount || 10;

    if (!series || series.length < 2) throw new Error('历史行情不足，无法计算触发线');
    var cfg = BOARD_CONFIG[board] || BOARD_CONFIG.main;
    var eventThreshold = cfg.eventThreshold;
    var redline = cfg.redline;

    var sim = simulateResets(series, market);
    var reset10Idx = sim.reset10Idx;
    var reset30Idx = sim.reset30Idx;
    var seq = series;
    var histEnd = seq[seq.length - 1].d;

    var dates = seq.map(function (b) { return b.d; }).concat(futureDates);
    var sc = seq.map(function (b) { return b.sc; }).concat(futureDates.map(function () { return price; }));
    var ic = seq.map(function (b) { return b.ic; }).concat(futureDates.map(function () { return indexPrice; }));
    var proj = dates.map(function (d, i) { return { d: d, sc: sc[i], ic: ic[i] }; });

    var events = scanEvents(seq, eventThreshold);
    var count = events.length;
    var sameLevel = count >= redline - 1 ? 'high' : (count >= 1 ? 'watch' : 'safe');
    var lastEvent = events.length ? events[events.length - 1] : null;

    function sameCountAt(j) {
      var edge = j - 9;
      var c = 0;
      for (var i = 0; i < events.length; i++) if (events[i].endIdx >= edge) c++;
      return c;
    }

    var rows = [];
    var basePos = seq.length - 1;
    for (var k = 0; k < rowsCount; k++) {
      var j = showT0 ? basePos + k : basePos + 1 + k;
      if (j >= proj.length) break;
      var isToday = showT0 && k === 0;
      var rulePrices = [];

      var w10 = windowRange(j, 11, reset10Idx);
      var b10 = argminClose(proj, w10[0], w10[1]);
      var x10 = triggerPrice(proj, b10, THRESHOLD_10D, indexPrice).price;
      rulePrices.push({ key: '10d', price: x10 });

      var w30 = windowRange(j, 31, reset30Idx);
      var b30 = argminClose(proj, w30[0], w30[1]);
      var x30 = triggerPrice(proj, b30, THRESHOLD_30D, indexPrice).price;
      rulePrices.push({ key: '30d', price: x30 });

      if (sameCountAt(j) >= redline - 1) {
        var b3 = argminClose(proj, Math.max(0, j - 2), j);
        var xs = triggerPrice(proj, b3, eventThreshold, indexPrice).price;
        rulePrices.push({ key: 'same', price: xs });
      }

      var refPrice = isToday ? prevClose : price;
      if (!refPrice || refPrice <= 0) refPrice = price;
      var binding = rulePrices[0];
      for (var ri = 1; ri < rulePrices.length; ri++) {
        if (rulePrices[ri].price < binding.price) binding = rulePrices[ri];
      }
      var triggerPct = (binding.price / refPrice) - 1.0;

      var rules = rulePrices.map(function (r) {
        return {
          key: r.key,
          label: RULE_LABEL[r.key],
          price: roundHalfUp(r.price, 2),
          pct: roundHalfUp((r.price / refPrice - 1.0) * 100, 2),
          binding: r.key === binding.key
        };
      });

      rows.push({
        key: 'T+' + (showT0 ? k : k + 1),
        date: iso(proj[j].d),
        weekday: weekName(proj[j].d),
        is_today: isToday,
        trigger_pct: roundHalfUp(triggerPct * 100, 2),
        trigger_price: roundHalfUp(binding.price, 2),
        trigger_rule: binding.key,
        rules: rules
      });
    }

    function summaryCard(lookback, threshold, resetIdx) {
      var w = windowRange(seq.length - 1, lookback, resetIdx);
      var b = argminClose(seq, w[0], w[1]);
      var d = deviation(seq, b, seq.length - 1, indexPrice);
      var tp = triggerPrice(seq, b, threshold, indexPrice).price;
      var level = devLevel(d.dev, threshold);
      return {
        value: roundHalfUp(d.dev * 100, 2),
        threshold: roundHalfUp(threshold * 100, 2),
        remaining: roundHalfUp((threshold - d.dev) * 100, 2),
        progress: threshold ? Math.round((d.dev / threshold) * 1e4) / 1e4 : 0,
        stock_change: roundHalfUp(d.stock * 100, 2),
        index_change: roundHalfUp(d.index * 100, 2),
        window_start: iso(seq[b].d),
        window_end: iso(histEnd),
        base_close: roundHalfUp(seq[b].sc, 2),
        base_date: iso(seq[b].d),
        trigger_price: roundHalfUp(tp, 2),
        level: level,
        label: LEVEL_LABEL[level]
      };
    }

    var dev10 = summaryCard(11, THRESHOLD_10D, reset10Idx);
    var dev30 = summaryCard(31, THRESHOLD_30D, reset30Idx);

    var sameDir = {
      count: count,
      threshold: redline,
      remaining: Math.max(0, redline - count),
      progress: redline ? Math.round((count / redline) * 1e4) / 1e4 : 0,
      level: sameLevel,
      label: LEVEL_LABEL[sameLevel],
      rule: '连续3日累计偏离+' + Math.round(eventThreshold * 100) + '% · 严重异动红线：10日内' + redline + '次上涨同向',
      last_event: lastEvent,
      events: events
    };

    var headerLevel = maxLevel(dev10.level, dev30.level, sameDir.level);
    var reasons = [];
    if (dev10.level !== 'safe') reasons.push('10日偏离值 ' + dev10.value.toFixed(2) + '%（阈值 ' + dev10.threshold.toFixed(0) + '%）');
    if (dev30.level !== 'safe') reasons.push('30日偏离值 ' + dev30.value.toFixed(2) + '%（阈值 ' + dev30.threshold.toFixed(0) + '%）');
    if (count > 0) reasons.push('10日同向异动 ' + count + '/' + redline);

    var triggerLogs = sim.log.filter(function (x) { return x.kind === '10d' || x.kind === '30d'; });
    var lastTrigger = null;
    if (triggerLogs.length) {
      var t = triggerLogs[triggerLogs.length - 1];
      lastTrigger = { kind: t.kind, date: iso(t.date), value: t.value };
    }
    var reset = {
      reset10: reset10Idx != null ? iso(seq[reset10Idx].d) : null,
      reset30: reset30Idx != null ? iso(seq[reset30Idx].d) : null,
      last_trigger: lastTrigger
    };

    return {
      summary: { dev10: dev10, dev30: dev30, same_dir: sameDir },
      risk: { level: headerLevel, label: LEVEL_LABEL[headerLevel], reasons: reasons },
      reset: reset,
      rows: rows
    };
  }

  return {
    compute: compute,
    roundHalfUp: roundHalfUp,
    simulateResets: simulateResets,
    scanEvents: scanEvents,
    LEVEL_LABEL: LEVEL_LABEL
  };
});

/* 异动雷达 · 交易日历（浏览器版）
 * 与 core/calendar_cn.py 保持同一份 2026-2027 沪深休市安排。
 * 所有日期计算用 UTC 毫秒数，避免用户本地时区/夏令时干扰。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.YDRCalendar = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var HOLIDAYS = [
    // 2026
    '2026-01-01', '2026-01-02', '2026-01-03',
    '2026-02-15', '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19',
    '2026-02-20', '2026-02-21', '2026-02-22', '2026-02-23',
    '2026-04-04', '2026-04-05', '2026-04-06',
    '2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05',
    '2026-06-19', '2026-06-20', '2026-06-21',
    '2026-09-25', '2026-09-26', '2026-09-27',
    '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05',
    '2026-10-06', '2026-10-07',
    // 2027
    '2027-01-01', '2027-01-02', '2027-01-03',
    '2027-02-05', '2027-02-06', '2027-02-07', '2027-02-08', '2027-02-09',
    '2027-02-10', '2027-02-11', '2027-02-12', '2027-02-13',
    '2027-04-03', '2027-04-04', '2027-04-05',
    '2027-05-01', '2027-05-02', '2027-05-03', '2027-05-04', '2027-05-05',
    '2027-06-09',
    '2027-09-15',
    '2027-10-01', '2027-10-02', '2027-10-03', '2027-10-04', '2027-10-05',
    '2027-10-06', '2027-10-07'
  ];
  var HOLIDAY_SET = {};
  HOLIDAYS.forEach(function (d) { HOLIDAY_SET[d] = true; });
  var COVERED_YEARS = { 2026: true, 2027: true };
  var WEEK_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function pad2(n) { return n < 10 ? '0' + n : '' + n; }

  /** 'YYYYMMDD' | 'YYYY-MM-DD' -> {y,m,d,utc} */
  function parseDate(s) {
    var t = String(s).replace(/-/g, '');
    var y = +t.slice(0, 4), m = +t.slice(4, 6), d = +t.slice(6, 8);
    return { y: y, m: m, d: d, utc: Date.UTC(y, m - 1, d) };
  }

  function isoOf(s) {
    var p = parseDate(s);
    return '' + p.y + '-' + pad2(p.m) + '-' + pad2(p.d);
  }

  function compactOf(utcMs) {
    var dt = new Date(utcMs);
    return '' + dt.getUTCFullYear() + pad2(dt.getUTCMonth() + 1) + pad2(dt.getUTCDate());
  }

  function isTradingDayUtc(utcMs) {
    var dt = new Date(utcMs);
    var wd = dt.getUTCDay();
    if (wd === 0 || wd === 6) return false;
    return !HOLIDAY_SET[isoOf(compactOf(utcMs))];
  }

  function isTradingDay(s) { return isTradingDayUtc(parseDate(s).utc); }

  /** 严格晚于 s 的 count 个交易日，返回 'YYYYMMDD' 数组 */
  function nextTradingDays(s, count) {
    var out = [];
    var utc = parseDate(s).utc;
    for (var i = 0; i < 3 * count + 60; i++) {
      utc += 86400000;
      if (isTradingDayUtc(utc)) {
        out.push(compactOf(utc));
        if (out.length >= count) break;
      }
    }
    return out;
  }

  function weekdayName(s) {
    var dt = new Date(parseDate(s).utc);
    return WEEK_NAMES[dt.getUTCDay()];
  }

  /** 上海时间“现在”：{dateStr:'YYYYMMDD', timeStr, iso, hours, minutes, seconds} */
  function shanghaiNow() {
    var parts;
    try {
      parts = new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      }).formatToParts(new Date());
    } catch (e) {
      // 极老浏览器兜底：按 UTC+8 硬算
      var d = new Date(Date.now() + 8 * 3600000);
      return {
        dateStr: '' + d.getUTCFullYear() + pad2(d.getUTCMonth() + 1) + pad2(d.getUTCDate()),
        timeStr: pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ':' + pad2(d.getUTCSeconds()),
        iso: d.toISOString(), hours: d.getUTCHours(), minutes: d.getUTCMinutes(), seconds: d.getUTCSeconds()
      };
    }
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    var hh = +map.hour, mm = +map.minute, ss = +map.second;
    var dateStr = map.year + map.month + map.day;
    return {
      dateStr: dateStr,
      timeStr: pad2(hh) + ':' + pad2(mm) + ':' + pad2(ss),
      iso: map.year + '-' + map.month + '-' + map.day + 'T' + pad2(hh) + ':' + pad2(mm) + ':' + pad2(ss) + '+08:00',
      hours: hh, minutes: mm, seconds: ss
    };
  }

  /** 交易阶段：pre_open / trading / lunch / closed / holiday */
  function marketPhase(now) {
    now = now || shanghaiNow();
    var d = parseDate(now.dateStr);
    var isTd = isTradingDayUtc(d.utc);
    if (!isTd) return { phase: 'holiday', isTradingDay: false, showT0: false };
    var mins = now.hours * 60 + now.minutes + now.seconds / 60;
    var phase;
    if (mins < 9 * 60 + 30) phase = 'pre_open';
    else if (mins < 11 * 60 + 30) phase = 'trading';
    else if (mins < 13 * 60) phase = 'lunch';
    else if (mins < 15 * 60) phase = 'trading';
    else phase = 'closed';
    return { phase: phase, isTradingDay: true, showT0: mins < 15 * 60 };
  }

  function calendarWarning(s) {
    var y = parseDate(s).y;
    return COVERED_YEARS[y] ? null : ('' + y + ' 年假期表未内置，交易日按周一至周五估算，请留意法定节假日休市。');
  }

  return {
    isTradingDay: isTradingDay,
    nextTradingDays: nextTradingDays,
    weekdayName: weekdayName,
    shanghaiNow: shanghaiNow,
    marketPhase: marketPhase,
    calendarWarning: calendarWarning,
    isoOf: isoOf,
    parseDate: parseDate
  };
});

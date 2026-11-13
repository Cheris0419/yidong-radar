/* 异动雷达 · 浏览器版行情层
 *
 * 纯静态托管（GitHub Pages 等）没有后端，行情由浏览器直接获取：
 *  - 个股/指数 实时快照与日 K：同花顺公开接口（CORS 允许，HTTPS）
 *  - 代码/名称/拼音搜索：东方财富 suggest JSONP
 * 计算由 site/engine.js 完成，返回结构与 docs/BACKEND-CONTRACT.md 一致。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./calendar.js'), require('./engine.js'));
  } else {
    root.YDRData = factory(root.YDRCalendar, root.YDREngine);
  }
})(typeof self !== 'undefined' ? self : this, function (cal, engine) {
  'use strict';

  var INDEXES = {
    sh: { code: '000001', name: '上证指数', ths: '1A0001' },
    sz: { code: '399106', name: '深证综指', ths: '399106' }
  };
  var THS_BASE = 'https://d.10jqka.com.cn/v6/';
  var EM_TOKEN = 'D43BF722C8E33BDC906FB84D85E326E8';
  var DISCLAIMER = '本工具按公开交易规则辅助估算，不构成投资建议；是否构成异常波动 / 严重异常波动，以交易所认定及上市公司公告为准。';
  var DATA_NOTE = '以下严重异动监管触发线，会根据指数进行变化';

  var quoteCache = {};   // code|idx -> {expire, value}
  var klineCache = {};   // code|idx -> {expire, value}
  var searchCache = {};  // keyword -> {expire, value}

  function DataError(code, message, status) {
    this.name = 'DataError';
    this.code = code;
    this.message = message;
    this.status = status || 502;
  }
  DataError.prototype = Object.create(Error.prototype);
  DataError.prototype.constructor = DataError;

  function cacheGet(store, key) {
    var it = store[key];
    if (it && Date.now() < it.expire) return it.value;
    if (it) delete store[key];
    return undefined;
  }
  function cacheSet(store, key, value, ms) { store[key] = { expire: Date.now() + ms, value: value }; }

  function classifyCode(code) {
    if (!/^\d{6}$/.test(code || '')) throw new DataError('BAD_CODE', '请输入 6 位 A 股代码', 400);
    if (/^(600|601|603|605)/.test(code)) return { market: 'sh', board: 'main', board_label: '沪主板' };
    if (/^(688|689)/.test(code)) return { market: 'sh', board: 'star', board_label: '科创板' };
    if (/^(000|001|002|003)/.test(code)) return { market: 'sz', board: 'main', board_label: '深主板' };
    if (/^(300|301)/.test(code)) return { market: 'sz', board: 'gem', board_label: '创业板' };
    if (/^(430|83|87|88|920|4|8)/.test(code)) throw new DataError('NOT_SUPPORTED', '北交所 / 老三板股票暂不支持（规则与沪深不同）', 400);
    if (/^(900|200|201)/.test(code)) throw new DataError('NOT_SUPPORTED', 'B 股暂不支持', 400);
    throw new DataError('NOT_FOUND', '未找到该股票，请检查代码是否正确', 404);
  }

  function fetchWithTimeout(url, timeoutMs) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, timeoutMs || 12000);
    return fetch(url, { signal: ctrl ? ctrl.signal : undefined, headers: { Accept: '*/*' } })
      .then(function (resp) {
        clearTimeout(timer);
        if (!resp.ok) throw new DataError('UPSTREAM_ERROR', '行情接口返回异常（HTTP ' + resp.status + '）', 502);
        return resp.text();
      })
      .catch(function (e) {
        clearTimeout(timer);
        if (e instanceof DataError) throw e;
        throw new DataError('UPSTREAM_ERROR', '行情接口请求失败，请稍后重试', 502);
      });
  }

  /** 解析同花顺 JSONP：quotebridge_xxx({...}) */
  function parseJsonp(text) {
    var l = text.indexOf('(');
    var r = text.lastIndexOf(')');
    if (l < 0 || r < 0) throw new DataError('UPSTREAM_ERROR', '行情接口返回格式异常', 502);
    try {
      return JSON.parse(text.slice(l + 1, r));
    } catch (e) {
      throw new DataError('UPSTREAM_ERROR', '行情接口返回格式异常', 502);
    }
  }

  function num(v) {
    var n = Number(v);
    return isNaN(n) ? null : n;
  }

  /* ---------------- 实时快照 ---------------- */
  function getStockQuote(code) {
    var key = 'stk|' + code;
    var hit = cacheGet(quoteCache, key);
    if (hit) return Promise.resolve(hit);
    return fetchWithTimeout(THS_BASE + 'realhead/hs_' + code + '/last.js').then(function (text) {
      var obj = parseJsonp(text);
      var items = obj.items || {};
      var price = num(items['10']);
      if (price === null) throw new DataError('NOT_FOUND', '未找到该股票，请检查代码是否正确', 404);
      var q = {
        code: code,
        name: '',
        price: price,
        prev_close: num(items['6']),
        pct: num(items['199112']),
        open: num(items['7']),
        high: num(items['8']),
        low: num(items['9']),
        status: String(items.stockStatus || ''),
        source: '10jqka'
      };
      cacheSet(quoteCache, key, q, 5000);
      return q;
    });
  }

  function getIndexQuote(market) {
    var info = INDEXES[market];
    var key = 'idx|' + market;
    var hit = cacheGet(quoteCache, key);
    if (hit) return Promise.resolve(hit);
    return fetchWithTimeout(THS_BASE + 'time/zs_' + info.ths + '/last.js').then(function (text) {
      var obj = parseJsonp(text);
      var body = obj['zs_' + info.ths] || {};
      var rows = String(body.data || '').split(';');
      var price = null;
      for (var i = rows.length - 1; i >= 0; i--) {
        var p = rows[i].split(',');
        if (p.length >= 2 && p[1]) {
          var v = parseFloat(p[1]);
          if (!isNaN(v)) { price = v; break; }
        }
      }
      if (price === null) throw new DataError('UPSTREAM_ERROR', '指数行情暂不可用，请稍后重试', 502);
      var q = {
        code: info.code,
        name: String(body.name || info.name),
        price: price,
        prev_close: num(body.pre),
        pct: null,
        source: '10jqka'
      };
      cacheSet(quoteCache, key, q, 5000);
      return q;
    });
  }

  /* ---------------- 日 K（同花顺 01 = 前复权） ---------------- */
  function parseKlineText(text) {
    var obj = parseJsonp(text);
    var name = String(obj.name || '');
    var bars = [];
    var rows = String(obj.data || '').split(';');
    for (var i = 0; i < rows.length; i++) {
      var p = rows[i].split(',');
      if (p.length < 5) continue;
      var close = parseFloat(p[4]);
      if (isNaN(close)) continue;
      bars.push({
        d: String(p[0]),
        o: parseFloat(p[1]),
        h: parseFloat(p[2]),
        l: parseFloat(p[3]),
        c: close,
        v: p[5] ? parseFloat(p[5]) : null
      });
    }
    bars.sort(function (a, b) { return a.d < b.d ? -1 : (a.d > b.d ? 1 : 0); });
    return { name: name, bars: bars };
  }

  function getStockKlines(code, isTradingDay) {
    var key = 'stk|' + code;
    var hit = cacheGet(klineCache, key);
    if (hit) return Promise.resolve(hit);
    return fetchWithTimeout(THS_BASE + 'line/hs_' + code + '/01/last.js').then(function (text) {
      var parsed = parseKlineText(text);
      if (!parsed.bars.length) throw new DataError('NOT_FOUND', '行情源未返回日 K 数据', 404);
      var value = { name: parsed.name, bars: parsed.bars };
      cacheSet(klineCache, key, value, isTradingDay ? 60000 : 3600000);
      return value;
    });
  }

  function getIndexKlines(market, isTradingDay) {
    var key = 'idx|' + market;
    var hit = cacheGet(klineCache, key);
    if (hit) return Promise.resolve(hit);
    return fetchWithTimeout(THS_BASE + 'line/zs_' + INDEXES[market].ths + '/01/last.js').then(function (text) {
      var parsed = parseKlineText(text);
      if (!parsed.bars.length) throw new DataError('UPSTREAM_ERROR', '行情源未返回指数日 K 数据', 502);
      var value = { name: parsed.name || INDEXES[market].name, bars: parsed.bars };
      cacheSet(klineCache, key, value, isTradingDay ? 60000 : 3600000);
      return value;
    });
  }

  /* ---------------- 搜索（东方财富 JSONP） ---------------- */
  var jsonpSeq = 0;
  function jsonp(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      if (typeof document === 'undefined') { reject(new DataError('UPSTREAM_ERROR', '当前环境不支持搜索', 502)); return; }
      var name = '__ydr_search_cb_' + (++jsonpSeq) + '_' + Date.now();
      var script = document.createElement('script');
      var timer = setTimeout(function () {
        cleanup();
        reject(new DataError('UPSTREAM_ERROR', '搜索接口超时，请稍后重试', 502));
      }, timeoutMs || 10000);
      function cleanup() {
        clearTimeout(timer);
        try { delete window[name]; } catch (e) { window[name] = undefined; }
        if (script.parentNode) script.parentNode.removeChild(script);
      }
      window[name] = function (data) { cleanup(); resolve(data); };
      script.onerror = function () { cleanup(); reject(new DataError('UPSTREAM_ERROR', '搜索接口请求失败，请稍后重试', 502)); };
      script.src = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'cb=' + name;
      document.head.appendChild(script);
    });
  }

  function mapSearchRows(rows) {
    var items = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i] || {};
      var classify = String(row.Classify || '');
      if (classify !== 'AStock' && classify !== '23' && classify !== 'NEEQ') continue;
      var code = String(row.Code || '');
      var marketNum = String(row.MktNum || '');
      if (marketNum !== '0' && marketNum !== '1') continue;
      var secName = String(row.SecurityTypeName || '');
      var item = null;
      if (secName === '京A' || secName === '北A') {
        item = { code: code, name: String(row.Name || ''), market: 'bj', board: 'bse', board_label: '北交所', secid: marketNum + '.' + code };
      } else {
        var cls;
        try { cls = classifyCode(code); } catch (e) { continue; }
        item = { code: code, name: String(row.Name || ''), market: cls.market, board: cls.board, board_label: cls.board_label, secid: marketNum + '.' + code };
      }
      items.push(item);
      if (items.length >= 8) break;
    }
    return items;
  }

  function search(keyword) {
    keyword = String(keyword || '').trim();
    if (!keyword) return Promise.resolve([]);
    var hit = cacheGet(searchCache, keyword);
    if (hit) return Promise.resolve(hit);
    var url = 'https://searchapi.eastmoney.com/api/suggest/get?input=' + encodeURIComponent(keyword) +
      '&type=14&token=' + EM_TOKEN + '&count=10';
    return jsonp(url, 10000).then(function (obj) {
      var rows = (((obj || {}).QuotationCodeTable || {}).Data) || [];
      var items = mapSearchRows(rows);
      cacheSet(searchCache, keyword, items, 5 * 60 * 1000);
      return items;
    }).catch(function () {
      if (/^\d{6}$/.test(keyword)) {
        var cls = classifyCode(keyword);
        return getStockQuote(keyword).then(function (q) {
          return [{ code: keyword, name: q.name || keyword, market: cls.market, board: cls.board, board_label: cls.board_label, secid: cls.market === 'sh' ? '1.' + keyword : '0.' + keyword }];
        }).catch(function () { return []; });
      }
      return [];
    });
  }

  /* ---------------- 上下文组装（与 core/data_source.build_context 等价） ---------------- */
  function buildContext(code) {
    var cls = classifyCode(code);
    var now = cal.shanghaiNow();
    var phaseInfo = cal.marketPhase(now);
    var phase = phaseInfo.phase;
    var isTd = phaseInfo.isTradingDay;
    var market = cls.market;

    return Promise.all([
      getStockQuote(code),
      getIndexQuote(market),
      getStockKlines(code, isTd),
      getIndexKlines(market, isTd)
    ]).then(function (arr) {
      var stockQ = arr[0], indexQ = arr[1], stockK = arr[2], indexK = arr[3];

      var indexMap = {}, stockMap = {};
      stockK.bars.forEach(function (b) { stockMap[b.d] = b.c; });
      indexK.bars.forEach(function (b) { indexMap[b.d] = b.c; });
      var allDates = Object.keys(indexMap).sort();
      if (!allDates.length) throw new DataError('UPSTREAM_ERROR', '行情源未返回指数日 K 数据', 502);
      var stockDates = Object.keys(stockMap).sort();
      var lastIndexDate = allDates[allDates.length - 1];
      var lastStockDate = stockDates.length ? stockDates[stockDates.length - 1] : lastIndexDate;
      var histEnd, price, indexPrice;

      if (isTd) {
        histEnd = now.dateStr;
        price = stockQ.price;
        if (price == null || price <= 0) price = stockMap[lastStockDate] || 0;
        indexPrice = indexQ.price;
        if (indexPrice == null || indexPrice <= 0) indexPrice = indexMap[lastIndexDate];
        stockMap[histEnd] = price;
        indexMap[histEnd] = indexPrice;
      } else {
        histEnd = lastIndexDate;
        price = stockMap[histEnd] != null ? stockMap[histEnd] : (stockQ.price || 0);
        indexPrice = indexMap[histEnd] != null ? indexMap[histEnd] : (indexQ.price || 0);
      }
      if (!price || price <= 0) throw new DataError('UPSTREAM_ERROR', '未能获取有效的最新价格，请稍后重试', 502);

      // 以指数交易日历为基准，股票停牌日按前值填充
      var series = [];
      var lastStock = null;
      for (var i = 0; i < allDates.length; i++) {
        var d = allDates[i];
        if (d > histEnd) continue;
        if (stockMap[d] != null) lastStock = stockMap[d];
        if (lastStock == null) continue;
        series.push({ d: d, sc: lastStock, ic: indexMap[d] });
      }
      if (!series.length || series[series.length - 1].d !== histEnd) {
        series.push({
          d: histEnd,
          sc: lastStock == null ? price : lastStock,
          ic: indexMap[histEnd] != null ? indexMap[histEnd] : indexPrice
        });
      }
      series[series.length - 1].sc = price;
      series[series.length - 1].ic = indexPrice;
      if (series.length < 35) throw new DataError('UPSTREAM_ERROR', '历史日 K 数据不足（至少需要 35 个交易日），无法计算', 502);

      var prevClose = series.length >= 2 ? series[series.length - 2].sc : null;
      var futureN = phaseInfo.showT0 ? 9 : 10;
      var futureDates = cal.nextTradingDays(histEnd, futureN);
      var name = stockK.name || stockQ.name || code;

      return {
        code: code,
        name: name,
        market: market,
        board: cls.board,
        board_label: cls.board_label,
        secid: (market === 'sh' ? '1.' : '0.') + code,
        stock_quote: stockQ,
        index_quote: indexQ,
        series: series,
        hist_end: histEnd,
        prev_close: prevClose,
        price: price,
        index_price: indexPrice,
        show_t0: !!phaseInfo.showT0,
        phase: phase,
        is_trading_day: !!isTd,
        future_dates: futureDates,
        now: now,
        suspended: price <= 0,
        index_name: indexQ.name || INDEXES[market].name
      };
    });
  }

  function buildTriggerPayload(code) {
    return buildContext(code).then(function (ctx) {
      var result = engine.compute({
        series: ctx.series,
        price: ctx.price,
        indexPrice: ctx.index_price,
        prevClose: ctx.prev_close,
        showT0: ctx.show_t0,
        futureDates: ctx.future_dates,
        board: ctx.board,
        market: ctx.market,
        rowsCount: 10
      });
      var series = ctx.series;
      var prevBar = series.length >= 2 ? series[series.length - 2] : series[series.length - 1];
      var stockPct = engine.roundHalfUp((ctx.price / prevBar.sc - 1) * 100, 2);
      var indexPct = engine.roundHalfUp((ctx.index_price / prevBar.ic - 1) * 100, 2);
      var status;
      if (ctx.suspended) status = 'suspended';
      else if (ctx.phase === 'pre_open') status = 'pre_open';
      else if (ctx.phase === 'trading' || ctx.phase === 'lunch') status = 'trading';
      else status = 'closed';

      var meta = {
        index_label: ctx.index_name,
        row_horizon: 10,
        data_source: '同花顺 · 东方财富（浏览器直连）',
        data_note: DATA_NOTE,
        disclaimer: DISCLAIMER
      };
      var warning = cal.calendarWarning(ctx.hist_end);
      if (warning) meta.calendar_warning = warning;

      return {
        stock: {
          code: ctx.code,
          name: ctx.name,
          market: ctx.market,
          board: ctx.board,
          board_label: ctx.board_label,
          secid: ctx.secid,
          price: engine.roundHalfUp(ctx.price, 2),
          prev_close: series.length >= 2 ? engine.roundHalfUp(series[series.length - 2].sc, 2) : null,
          pct: stockPct,
          open: ctx.stock_quote.open,
          high: ctx.stock_quote.high,
          low: ctx.stock_quote.low,
          status: status,
          suspended: !!ctx.suspended
        },
        index: {
          code: ctx.index_quote.code,
          name: ctx.index_name,
          price: engine.roundHalfUp(ctx.index_price, 2),
          prev_close: engine.roundHalfUp(prevBar.ic, 2),
          pct: indexPct
        },
        snapshot: {
          trade_date: cal.isoOf(ctx.hist_end),
          time: ctx.now.timeStr,
          phase: ctx.phase,
          is_trading_day: !!ctx.is_trading_day,
          show_t0: !!ctx.show_t0,
          updated_at: ctx.now.iso
        },
        risk: result.risk,
        summary: result.summary,
        reset: result.reset,
        rows: result.rows,
        meta: meta
      };
    });
  }

  return {
    DataError: DataError,
    classifyCode: classifyCode,
    getStockQuote: getStockQuote,
    getIndexQuote: getIndexQuote,
    getStockKlines: getStockKlines,
    getIndexKlines: getIndexKlines,
    search: search,
    buildContext: buildContext,
    buildTriggerPayload: buildTriggerPayload,
    INDEXES: INDEXES,
    clearCache: function () { quoteCache = {}; klineCache = {}; searchCache = {}; }
  };
});

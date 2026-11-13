/* 异动雷达 · 静态在线版 API 适配层
 *
 * GitHub Pages 等纯静态托管无法运行 Python 后端，这里在浏览器内把
 * /api/triggers、/api/search 两个接口“就地实现”：行情由浏览器直连
 * 东方财富公开接口（CORS 允许），计算由 site/engine.js 完成，
 * 返回结构与 docs/BACKEND-CONTRACT.md 完全一致，前端 app.js 无需改动。
 */
(function () {
  'use strict';
  if (!window.YDRData || typeof window.fetch !== 'function') return;

  var realFetch = window.fetch.bind(window);
  var TRIGGER_TTL = 10000; // 同一只股票 10s 内复用，配合前端 30s 自动刷新
  var triggerCache = {};

  function jsonResponse(payload, status) {
    var body = JSON.stringify(payload);
    return Promise.resolve(new Response(body, {
      status: status || 200,
      headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }
    }));
  }

  function errorResponse(err) {
    var e = err && err.code ? err : { code: 'INTERNAL', message: '服务内部错误，请稍后重试', status: 500 };
    return jsonResponse({ ok: false, error: { code: e.code, message: e.message || '' } }, e.status || 502);
  }

  function handleTriggers(code) {
    code = String(code || '').trim();
    if (!code) return jsonResponse({ ok: false, error: { code: 'BAD_CODE', message: '缺少 code 参数' } }, 400);
    var hit = triggerCache[code];
    if (hit && Date.now() < hit.expire) return jsonResponse(hit.payload);
    return window.YDRData.buildTriggerPayload(code).then(function (data) {
      var payload = { ok: true, data: data };
      triggerCache[code] = { expire: Date.now() + TRIGGER_TTL, payload: payload };
      return jsonResponse(payload);
    }).catch(function (err) {
      // 缩短错误缓存，避免行情源抖动时反复重试
      if (err && err.code === 'NOT_FOUND') triggerCache[code] = { expire: Date.now() + 30000, payload: { ok: false, error: { code: err.code, message: err.message } } };
      return errorResponse(err);
    });
  }

  function handleSearch(q) {
    q = String(q || '').trim();
    if (!q) return jsonResponse({ ok: false, error: { code: 'BAD_CODE', message: '请输入股票代码、名称或拼音首字母' } }, 400);
    return window.YDRData.search(q).then(function (items) {
      return jsonResponse({ ok: true, data: { items: items } });
    }).catch(errorResponse);
  }

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var path = '';
    try { path = new URL(url, window.location.href).pathname; } catch (e) { path = ''; }
    try {
      if (path.slice(-12) === '/api/health') {
        return jsonResponse({ ok: true, data: { status: 'ok', time: new Date().toISOString(), version: '1.0.0-static' } });
      }
      if (path.slice(-13) === '/api/triggers') {
        var code = '';
        try { code = new URL(url, window.location.href).searchParams.get('code') || ''; } catch (e2) { code = ''; }
        return handleTriggers(code);
      }
      if (path.slice(-11) === '/api/search') {
        var q = '';
        try { q = new URL(url, window.location.href).searchParams.get('q') || ''; } catch (e3) { q = ''; }
        return handleSearch(q);
      }
    } catch (e4) { /* 落到真实 fetch */ }
    return realFetch(input, init);
  };

  // 在线版标记：前端 meta.data_source 已由 data.js 标注为“东方财富（浏览器直连）”
  window.__YDR_STATIC__ = { version: '1.0.0', mode: 'browser-only' };
})();

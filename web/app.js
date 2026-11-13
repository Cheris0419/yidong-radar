/* =========================================================
   异动雷达 · app.js
   原生 JS，零依赖。只消费 /api/triggers 与 /api/search 契约字段。
   ========================================================= */
(function () {
  'use strict';

  /* ---------- 常量 ---------- */

  var THEME_KEY = 'ydradar.theme';
  var WATCH_KEY = 'ydradar.watch';
  var MAX_WATCH = 12;
  var AUTO_REFRESH_MS = 30000;
  var SEARCH_DEBOUNCE_MS = 300;

  var EXAMPLES = [
    { code: '000993', name: '闽东电力', board_label: '深主板' },
    { code: '600664', name: '哈药股份', board_label: '沪主板' },
    { code: '300750', name: '宁德时代', board_label: '创业板' }
  ];

  var RULE_LABELS = {
    '10d': '10日 100%',
    '30d': '30日 200%',
    'same': '十日同向异动线'
  };

  var PHASE_LABELS = {
    pre_open: '未开盘',
    auction: '集合竞价',
    trading: '扫描中',
    lunch: '午间休市',
    closed: '已收盘',
    holiday: '休市'
  };

  var ERROR_COPY = {
    BAD_CODE: {
      title: '请检查股票代码',
      text: '输入 6 位 A 股代码，例如 000993。'
    },
    NOT_FOUND: {
      title: '未找到该股票代码，请检查输入',
      text: '确认代码是否正确，也可以直接在上方搜索股票名称或拼音。'
    },
    NOT_SUPPORTED: {
      title: '暂不支持该股票',
      text: '可以返回搜索，换一只沪深 A 股查看。'
    },
    UPSTREAM_ERROR: {
      title: '行情源暂时不可用',
      text: '数据源没有返回有效数据，请稍后重试，或换一只股票看看。'
    },
    INTERNAL: {
      title: '服务出现异常',
      text: '请稍后重试；如果持续出现，请联系数据服务维护者。'
    },
    NETWORK: {
      title: '无法连接行情服务',
      text: '请确认本地服务正在运行、网络可用，然后重试。'
    }
  };

  var DEFAULT_DISCLAIMER = '本工具按公开交易规则辅助估算，不构成投资建议；是否构成异常波动 / 严重异常波动以交易所认定及公司公告为准。';

  /* ---------- 工具 ---------- */

  function $(id) { return document.getElementById(id); }
  function qs(sel, root) { return (root || document).querySelector(sel); }

  function toNum(value) {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string' && value.trim() !== '') {
      var n = Number(value);
      return Number.isFinite(n) ? n : null;
    }
    return null;
  }

  function fmtPrice(value) {
    var n = toNum(value);
    return n === null ? '—' : n.toFixed(2);
  }

  function fmtPct(value, digits) {
    var n = toNum(value);
    if (n === null) return '—';
    var d = typeof digits === 'number' ? digits : 2;
    return (n > 0 ? '+' : n < 0 ? '-' : '') + Math.abs(n).toFixed(d) + '%';
  }

  function fmtThreshold(value, fallback) {
    var n = toNum(value);
    if (n === null) return fallback === undefined ? '—' : String(fallback);
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }

  function trendOf(value) {
    var n = toNum(value);
    if (n === null) return 'flat';
    return n > 0 ? 'up' : n < 0 ? 'down' : 'flat';
  }

  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function fmtClock(iso) {
    var s = String(iso || '');
    var m = s.match(/^\d{4}-\d{2}-\d{2}[T ](\d{2}:\d{2}:\d{2})/);
    if (m) return m[1];
    if (/^\d{2}:\d{2}:\d{2}$/.test(s)) return s;
    return '—';
  }

  function fmtDateTime(iso) {
    var s = String(iso || '');
    var m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
    if (m) return m[1] + ' ' + m[2];
    if (/^\d{2}:\d{2}:\d{2}$/.test(s)) return s;
    return '—';
  }

  function badgeHtml(level, label) {
    if (!label) return '';
    return '<span class="badge badge-risk" data-level="' + esc(level || 'unknown') + '">' + esc(label) + '</span>';
  }

  function chipHtml(item) {
    var code = String((item && item.code) || '');
    var name = String((item && item.name) || code);
    return '<button type="button" class="chip" data-code="' + esc(code) + '">' +
      '<span class="chip-name">' + esc(name) + '</span>' +
      '<span class="chip-code num">' + esc(code) + '</span>' +
      '</button>';
  }

  /* ---------- 状态 ---------- */

  var els = {};
  var state = {
    code: null,
    failedCode: null,
    data: null,
    loading: false,
    lastRefresh: 0,
    refreshController: null,
    autoTimer: null,
    toastTimer: null,
    watch: [],
    search: {
      q: '',
      results: [],
      active: -1,
      open: false,
      timer: null,
      controller: null
    }
  };

  /* ---------- 主题 ---------- */

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function applyTheme(theme, persist) {
    var t = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    document.documentElement.style.colorScheme = t;
    var meta = qs('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', t === 'light' ? '#EDF2F3' : '#050D14');
    if (els.themeToggle) {
      els.themeToggle.setAttribute('aria-label', t === 'dark' ? '切换到日间主题' : '切换到夜间主题');
    }
    if (persist) {
      try { localStorage.setItem(THEME_KEY, t); } catch (err) { /* 忽略隐私模式写入失败 */ }
    }
  }

  function bindTheme() {
    if (!els.themeToggle) return;
    els.themeToggle.addEventListener('click', function () {
      applyTheme(currentTheme() === 'dark' ? 'light' : 'dark', true);
    });
  }

  /* ---------- 自选 ---------- */

  function loadWatch() {
    try {
      var raw = JSON.parse(localStorage.getItem(WATCH_KEY) || '[]');
      if (!Array.isArray(raw)) return [];
      return raw.filter(function (item) {
        return item && /^\d{6}$/.test(String(item.code || ''));
      }).slice(0, MAX_WATCH).map(function (item) {
        return { code: String(item.code), name: String(item.name || item.code) };
      });
    } catch (err) {
      return [];
    }
  }

  function saveWatch() {
    try { localStorage.setItem(WATCH_KEY, JSON.stringify(state.watch)); } catch (err) { /* 忽略 */ }
  }

  function isWatched(code) {
    return state.watch.some(function (item) { return item.code === code; });
  }

  function renderWatchChips() {
    if (els.emptyWatchChips) {
      els.emptyWatchChips.innerHTML = state.watch.map(chipHtml).join('');
    }
    if (els.emptyWatchBlock) {
      els.emptyWatchBlock.hidden = state.watch.length === 0;
    }
  }

  function syncWatchButton() {
    var stock = state.data && state.data.stock;
    var on = !!(stock && stock.code && isWatched(String(stock.code)));
    if (!els.watchBtn) return;
    els.watchBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    els.watchBtn.disabled = !stock;
    if (els.watchLabel) els.watchLabel.textContent = on ? '已自选' : '自选';
  }

  function toggleWatch() {
    var stock = state.data && state.data.stock;
    if (!stock || !stock.code) {
      showToast('请先选择一只股票');
      return;
    }
    var code = String(stock.code);
    var name = String(stock.name || code);
    var idx = state.watch.findIndex(function (item) { return item.code === code; });

    if (idx >= 0) {
      state.watch.splice(idx, 1);
      showToast('已从自选移除 ' + name);
    } else {
      if (state.watch.length >= MAX_WATCH) {
        showToast('自选已满 ' + MAX_WATCH + ' 只，请先移除一只');
        return;
      }
      state.watch.push({ code: code, name: name });
      showToast('已加入自选 ' + name);
    }

    saveWatch();
    renderWatchChips();
    syncWatchButton();
    if (state.search.open && !state.search.q) renderSearchIdle();
  }

  /* ---------- 搜索 ---------- */

  function setSearchExpanded(open) {
    state.search.open = open;
    if (els.searchPanel) els.searchPanel.hidden = !open;
    if (els.searchInput) els.searchInput.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open && els.searchInput) els.searchInput.removeAttribute('aria-activedescendant');
  }

  function openSearchPanel() { setSearchExpanded(true); }

  function closeSearchPanel() {
    setSearchExpanded(false);
    state.search.active = -1;
    clearActiveOptions();
  }

  function clearActiveOptions() {
    if (!els.searchList) return;
    Array.prototype.forEach.call(els.searchList.querySelectorAll('.search-option'), function (el) {
      el.classList.remove('is-active');
      el.setAttribute('aria-selected', 'false');
    });
  }

  function renderSearchIdle() {
    if (!els.searchList) return;
    var html = '';
    if (state.watch.length) {
      html += '<p class="panel-group-label">自选</p><div class="chip-row">' +
        state.watch.map(chipHtml).join('') + '</div>';
    } else {
      html += '<p class="panel-hint">还没有自选。在股票摘要卡点「自选」即可保存，最多 ' + MAX_WATCH + ' 只。</p>';
    }
    html += '<p class="panel-group-label">示例</p><div class="chip-row">' +
      EXAMPLES.map(chipHtml).join('') + '</div>';
    els.searchList.innerHTML = html;
    els.searchList.removeAttribute('aria-busy');
  }

  function renderSearchHint(text) {
    if (!els.searchList) return;
    els.searchList.innerHTML = '<p class="panel-hint">' + esc(text) + '</p>';
    els.searchList.removeAttribute('aria-busy');
  }

  function renderSearchResults(items) {
    if (!els.searchList) return;
    if (!items.length) {
      renderSearchHint('没有匹配的股票，试试 6 位代码或完整名称。');
      return;
    }
    els.searchList.innerHTML = items.map(function (item, i) {
      var code = String(item.code || '');
      var name = String(item.name || code);
      var board = String(item.board_label || '');
      return '<button type="button" role="option" id="search-opt-' + i + '" tabindex="-1" ' +
        'class="search-option" data-code="' + esc(code) + '" aria-selected="false">' +
        '<span class="so-code num">' + esc(code) + '</span>' +
        '<span class="so-name">' + esc(name) + '</span>' +
        '<span class="so-board">' + esc(board) + '</span>' +
        '</button>';
    }).join('');
    els.searchList.removeAttribute('aria-busy');
  }

  function setActiveOption(index) {
    var options = els.searchList ? els.searchList.querySelectorAll('.search-option') : [];
    if (!options.length) return;
    state.search.active = index;
    Array.prototype.forEach.call(options, function (el, i) {
      var on = i === index;
      el.classList.toggle('is-active', on);
      el.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var active = options[index];
    if (active) {
      els.searchInput.setAttribute('aria-activedescendant', active.id);
      if (active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
    }
  }

  function chooseStock(code) {
    code = String(code || '').trim();
    if (!/^\d{6}$/.test(code)) return;
    if (state.search.timer) {
      clearTimeout(state.search.timer);
      state.search.timer = null;
    }
    if (state.search.controller) {
      state.search.controller.abort();
      state.search.controller = null;
    }
    closeSearchPanel();
    els.searchInput.value = '';
    state.search.q = '';
    state.search.results = [];
    if (els.searchClear) els.searchClear.hidden = true;
    els.searchInput.blur();
    window.scrollTo(0, 0);
    navigate(code);
  }

  function fetchSearch(q) {
    if (state.search.controller) state.search.controller.abort();
    var controller = new AbortController();
    state.search.controller = controller;

    fetch('/api/search?q=' + encodeURIComponent(q), {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (payload) {
        if (state.search.q !== q) return;
        if (!res.ok || !payload || payload.ok !== true) {
          renderSearchHint('搜索失败，请稍后重试，或直接输入 6 位代码。');
          return;
        }
        var items = payload.data && Array.isArray(payload.data.items) ? payload.data.items : [];
        state.search.results = items;
        renderSearchResults(items);
      });
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      if (state.search.q === q) {
        renderSearchHint('搜索服务不可用，可直接输入 6 位代码查看。');
      }
    });
  }

  function onSearchInput() {
    var raw = els.searchInput.value;
    state.search.q = raw.trim();
    if (els.searchClear) els.searchClear.hidden = raw.length === 0;
    if (state.search.timer) clearTimeout(state.search.timer);
    if (state.search.controller) state.search.controller.abort();
    state.search.active = -1;
    state.search.results = [];

    if (!state.search.q) {
      openSearchPanel();
      renderSearchIdle();
      return;
    }

    openSearchPanel();
    if (els.searchList) els.searchList.setAttribute('aria-busy', 'true');
    renderSearchHint('搜索中…');
    var term = state.search.q;
    state.search.timer = setTimeout(function () {
      if (state.search.q === term) fetchSearch(term);
    }, SEARCH_DEBOUNCE_MS);
  }

  function onSearchKeydown(event) {
    var key = event.key;

    if (key === 'ArrowDown' || key === 'ArrowUp') {
      if (els.searchPanel.hidden) {
        openSearchPanel();
        if (!state.search.q) renderSearchIdle();
        else if (state.search.results.length) renderSearchResults(state.search.results);
      }
      var options = els.searchList.querySelectorAll('.search-option');
      if (!options.length) return;
      event.preventDefault();
      var dir = key === 'ArrowDown' ? 1 : -1;
      var next = state.search.active + dir;
      if (next < 0) next = options.length - 1;
      if (next >= options.length) next = 0;
      setActiveOption(next);
      return;
    }

    if (key === 'Enter') {
      event.preventDefault();
      var list = els.searchList.querySelectorAll('.search-option');
      if (state.search.active >= 0 && list[state.search.active]) {
        chooseStock(list[state.search.active].getAttribute('data-code'));
        return;
      }
      if (list.length) {
        chooseStock(list[0].getAttribute('data-code'));
        return;
      }
      var typed = els.searchInput.value.trim();
      if (/^\d{6}$/.test(typed)) chooseStock(typed);
      return;
    }

    if (key === 'Escape') {
      event.preventDefault();
      closeSearchPanel();
      els.searchInput.blur();
      return;
    }

    if (key === 'Tab') {
      closeSearchPanel();
    }
  }

  function bindSearch() {
    if (!els.searchInput) return;

    els.searchInput.addEventListener('input', onSearchInput);
    els.searchInput.addEventListener('keydown', onSearchKeydown);

    els.searchInput.addEventListener('focus', function () {
      if (!state.search.q) {
        openSearchPanel();
        renderSearchIdle();
        return;
      }
      openSearchPanel();
      if (state.search.results.length) renderSearchResults(state.search.results);
      else if (!state.search.timer) {
        state.search.timer = setTimeout(function () { fetchSearch(state.search.q); }, 0);
      }
    });

    if (els.searchClear) {
      els.searchClear.addEventListener('click', function () {
        els.searchInput.value = '';
        state.search.q = '';
        state.search.results = [];
        els.searchClear.hidden = true;
        renderSearchIdle();
        openSearchPanel();
        els.searchInput.focus();
      });
    }

    if (els.searchList) {
      els.searchList.addEventListener('click', function (event) {
        var btn = event.target.closest('.search-option, .chip[data-code]');
        if (!btn) return;
        chooseStock(btn.getAttribute('data-code'));
      });
    }

    document.addEventListener('click', function (event) {
      if (!els.searchRow || els.searchRow.contains(event.target)) return;
      closeSearchPanel();
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !els.searchPanel.hidden) closeSearchPanel();
    });
  }

  /* ---------- 视图切换 ---------- */

  function setMainView(view) {
    if (els.main) els.main.setAttribute('data-view', view);
    if (els.emptyView) els.emptyView.hidden = view !== 'empty';
    if (els.errorView) els.errorView.hidden = view !== 'error';
  }

  function setFeed(kind, label) {
    if (!els.feed) return;
    els.feed.setAttribute('data-state', kind);
    if (els.feedText) els.feedText.textContent = label;
    els.feed.title = '数据源状态：' + label;
  }

  function setRadarMarket(mode) {
    if (!els.radar) return;
    els.radar.setAttribute('data-market', mode);
    var caption = qs('.radar-caption');
    if (caption) caption.setAttribute('data-state', mode === 'open' ? 'live' : 'paused');
  }

  function setText(el, text) {
    if (el) el.textContent = text;
  }

  function setTrend(el, value) {
    if (!el) return;
    el.setAttribute('data-trend', trendOf(value));
  }

  function announce(text) {
    var live = $('liveRegion');
    if (!live) return;
    live.textContent = '';
    window.setTimeout(function () { live.textContent = text; }, 60);
  }

  /* ---------- 骨架屏 ---------- */

  function skeletonRows() {
    var html = '';
    for (var i = 0; i < 10; i++) {
      html += '<tr class="skeleton-row" aria-hidden="true">' +
        '<td><span class="sk sk-day"></span><span class="sk sk-date"></span></td>' +
        '<td><span class="sk sk-pct"></span><span class="sk sk-price"></span></td>' +
        '<td><span class="sk sk-rule"></span><span class="sk sk-rule"></span>' +
        '<span class="sk sk-rule sk-rule--short"></span></td>' +
        '</tr>';
    }
    return html;
  }

  function skeletonReadouts() {
    var card = '<div class="sk-readout" aria-hidden="true">' +
      '<span class="sk sk-line sk-line--title"></span>' +
      '<span class="sk sk-line sk-line--lg"></span>' +
      '<span class="sk sk-line sk-line--track"></span>' +
      '<span class="sk sk-line sk-line--grid"></span>' +
      '<span class="sk sk-line sk-line--foot"></span>' +
      '</div>';
    return card + card + card;
  }

  function resetSummaryFields(code) {
    if (els.summary) els.summary.classList.add('is-skeleton');
    if (els.riskBadge) {
      els.riskBadge.textContent = '—';
      els.riskBadge.setAttribute('data-level', 'unknown');
      els.riskBadge.hidden = false;
    }
    setText(els.boardBadge, '—');
    setText(els.dataTime, '—');
    setText(els.stockName, '加载中');
    setText(els.stockCode, code || '—');
    setText(els.stockBoard, '—');
    setText(els.price, '—');
    setText(els.pct, '—');
    setTrend(els.pct, null);
    setText(els.indexName, '—');
    setText(els.indexPrice, '—');
    setText(els.indexPct, '—');
    setTrend(els.indexPct, null);
    setText(els.riskReasons, '');
    setText(els.radarPrice, '—');
    setText(els.radarPct, '—');
    setTrend(els.radarPct, null);
    setText(els.radarStatus, '连接中');
    setRadarMarket('idle');
    setText(els.watchLabel, '自选');
    if (els.watchBtn) {
      els.watchBtn.setAttribute('aria-pressed', 'false');
      els.watchBtn.disabled = true;
    }
  }

  function showSkeleton(code) {
    setMainView('loading');
    resetSummaryFields(code);
    if (els.closeChip) els.closeChip.hidden = true;
    if (els.tableFoot) els.tableFoot.hidden = true;
    if (els.triggerRows) els.triggerRows.innerHTML = skeletonRows();
    if (els.readoutList) els.readoutList.innerHTML = skeletonReadouts();
    setText(els.noticeMain, '正在获取 ' + code + ' 的触发线…');
    if (els.noticeSub) els.noticeSub.innerHTML = '';
    setFeed('loading', '连接中');
    announce('正在获取 ' + code + ' 的触发线');
  }

  /* ---------- 空态 / 错误态 ---------- */

  function renderEmptyChips() {
    if (els.exampleChips) els.exampleChips.innerHTML = EXAMPLES.map(chipHtml).join('');
    renderWatchChips();
  }

  function showEmptyView() {
    if (state.refreshController) state.refreshController.abort();
    state.loading = false;
    state.data = null;
    state.code = null;
    state.failedCode = null;
    if (els.refreshBtn) {
      els.refreshBtn.classList.remove('is-spinning');
      els.refreshBtn.disabled = false;
    }
    setMainView('empty');
    renderEmptyChips();
    setFeed('idle', '待命');
    setText(els.watchLabel, '自选');
    if (els.watchBtn) {
      els.watchBtn.setAttribute('aria-pressed', 'false');
      els.watchBtn.disabled = true;
    }
    closeSearchPanel();
    document.title = '异动雷达 · 严重异动监管触发线';
  }

  function showErrorView(error, code) {
    var key = (error && error.code) || 'INTERNAL';
    var copy = ERROR_COPY[key] || ERROR_COPY.INTERNAL;
    // 契约约定 error.message 是给用户看的文案，优先展示；NETWORK 异常不展示浏览器原始错误。
    var serverMessage = key !== 'NETWORK' && error && typeof error.message === 'string'
      ? error.message.trim()
      : '';
    state.loading = false;
    state.failedCode = code || state.code || null;
    state.data = null;
    if (els.refreshBtn) {
      els.refreshBtn.classList.remove('is-spinning');
      els.refreshBtn.disabled = false;
    }
    setMainView('error');
    setText(els.errorTitle, serverMessage || copy.title);
    setText(els.errorText, serverMessage ? copy.text : (code ? '代码 ' + code + '。' : '') + copy.text);
    setText(els.errorCode, key);
    if (els.summary) els.summary.classList.remove('is-skeleton');
    var online = key !== 'NETWORK' && key !== 'UPSTREAM_ERROR' && key !== 'INTERNAL';
    setFeed(online ? 'online' : 'offline', online ? '在线' : '离线');
    document.title = '加载失败 · 异动雷达';
    announce(copy.title);
  }

  /* ---------- 数据加载 ---------- */

  function normalizeData(raw) {
    var d = raw && typeof raw === 'object' ? raw : {};
    return {
      stock: d.stock && typeof d.stock === 'object' ? d.stock : null,
      index: d.index && typeof d.index === 'object' ? d.index : {},
      snapshot: d.snapshot && typeof d.snapshot === 'object' ? d.snapshot : {},
      risk: d.risk && typeof d.risk === 'object' ? d.risk : {},
      summary: d.summary && typeof d.summary === 'object' ? d.summary : {},
      reset: d.reset && typeof d.reset === 'object' ? d.reset : {},
      rows: Array.isArray(d.rows) ? d.rows : [],
      meta: d.meta && typeof d.meta === 'object' ? d.meta : {}
    };
  }

  function loadTriggers(code, options) {
    var opts = options || {};
    if (!/^\d{6}$/.test(code)) {
      showErrorView({ code: 'BAD_CODE' }, code);
      return;
    }

    var soft = opts.soft === true;
    var manual = opts.manual === true;
    var sameStock = !!(state.data && state.data.stock && String(state.data.stock.code) === code);

    state.code = code;
    state.loading = true;

    if (!soft || !sameStock) {
      showSkeleton(code);
    } else {
      setFeed('loading', '更新中');
    }

    if (els.refreshBtn) {
      els.refreshBtn.disabled = true;
      els.refreshBtn.classList.toggle('is-spinning', manual);
    }

    if (state.refreshController) state.refreshController.abort();
    var controller = new AbortController();
    state.refreshController = controller;

    var failure = null;
    var rawData = null;

    fetch('/api/triggers?code=' + encodeURIComponent(code), {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (payload) {
        if (state.refreshController !== controller) return;
        if (!res.ok || !payload || payload.ok !== true) {
          failure = (payload && payload.error) || {
            code: res.status === 404 ? 'NOT_FOUND' : 'INTERNAL',
            message: ''
          };
          if (!failure.code) failure.code = 'INTERNAL';
          return;
        }
        rawData = payload.data;
      });
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      if (state.refreshController !== controller) return;
      failure = { code: 'NETWORK', message: '' };
    }).then(function () {
      if (state.refreshController !== controller) return;

      state.loading = false;
      if (els.refreshBtn) {
        els.refreshBtn.disabled = false;
        els.refreshBtn.classList.remove('is-spinning');
      }

      if (failure) {
        if (soft && sameStock && state.data) {
          setFeed('offline', '离线');
          showToast('刷新失败，仍显示上次数据');
          announce('刷新失败，仍显示上次数据');
          return;
        }
        showErrorView(failure, code);
        return;
      }

      var data = normalizeData(rawData);
      if (!data.stock || !data.stock.code) {
        showErrorView({ code: 'INTERNAL' }, code);
        return;
      }

      state.data = data;
      state.code = String(data.stock.code) || code;
      state.lastRefresh = Date.now();
      renderReady(data);
    });
  }

  /* ---------- 渲染：摘要 ---------- */

  function renderSummary(data) {
    var stock = data.stock || {};
    var index = data.index || {};
    var snap = data.snapshot || {};
    var risk = data.risk || {};
    var meta = data.meta || {};

    if (els.summary) els.summary.classList.remove('is-skeleton');

    if (els.riskBadge) {
      if (risk.label) {
        els.riskBadge.hidden = false;
        els.riskBadge.textContent = String(risk.label);
        els.riskBadge.setAttribute('data-level', risk.level || 'unknown');
      } else {
        els.riskBadge.hidden = true;
      }
    }

    if (els.boardBadge) {
      var boardText = stock.board_label || '';
      els.boardBadge.hidden = !boardText;
      if (boardText) els.boardBadge.textContent = String(boardText);
    }

    var updated = snap.time || fmtClock(snap.updated_at);
    setText(els.dataTime, updated);
    setText(els.stockName, stock.name || '—');
    setText(els.stockCode, stock.code || state.code || '—');
    setText(els.stockBoard, stock.board_label || '—');

    setText(els.price, fmtPrice(stock.price));
    setText(els.pct, fmtPct(stock.pct));
    setTrend(els.pct, stock.pct);

    setText(els.indexName, index.name || meta.index_label || '—');
    setText(els.indexPrice, fmtPrice(index.price));
    setText(els.indexPct, fmtPct(index.pct));
    setTrend(els.indexPct, index.pct);

    setText(els.radarPrice, fmtPrice(stock.price));
    setText(els.radarPct, fmtPct(stock.pct));
    setTrend(els.radarPct, stock.pct);

    var live = !stock.suspended && snap.phase === 'trading';
    setRadarMarket(live ? 'open' : 'paused');
    var radarLabel = stock.suspended ? '停牌' : (PHASE_LABELS[snap.phase] || (snap.is_trading_day === false ? '休市' : '待扫描'));
    setText(els.radarStatus, radarLabel);

    if (els.riskReasons) {
      var reasons = Array.isArray(risk.reasons) ? risk.reasons.filter(Boolean) : [];
      els.riskReasons.textContent = reasons.length ? reasons.join(' · ') : '';
    }
  }

  /* ---------- 渲染：提示行 ---------- */

  function renderNotice(data) {
    var snap = data.snapshot || {};
    var stock = data.stock || {};
    var index = data.index || {};
    var meta = data.meta || {};

    setText(els.noticeMain, meta.data_note || '以下严重异动监管触发线会根据指数收盘变化而变化，数据为估算值。');

    var chips = [];
    var suspended = stock.suspended === true || stock.status === 'suspended';
    if (suspended) {
      chips.push('<span class="chip-state" data-kind="danger">停牌 · 按最新价测算</span>');
    }
    if (snap.is_trading_day === false) {
      chips.push('<span class="chip-state" data-kind="muted">非交易日</span>');
    }
    var phase = snap.phase;
    if (!suspended) {
      if (phase === 'auction') chips.push('<span class="chip-state" data-kind="muted">集合竞价</span>');
      else if (phase === 'lunch') chips.push('<span class="chip-state" data-kind="muted">午间休市</span>');
      else if (phase === 'pre_open') chips.push('<span class="chip-state" data-kind="muted">未开盘</span>');
      else if (phase === 'holiday') chips.push('<span class="chip-state" data-kind="muted">休市</span>');
    }

    var bits = [];
    if (snap.time) bits.push('数据更新 <span class="num">' + esc(snap.time) + '</span>');
    else if (snap.updated_at) bits.push('数据更新 <span class="num">' + esc(fmtDateTime(snap.updated_at)) + '</span>');
    if (snap.trade_date) bits.push('交易日 <span class="num">' + esc(snap.trade_date) + '</span>');
    if (index.name) bits.push('参考指数 ' + esc(index.name));

    els.noticeSub.innerHTML = chips.join('') +
      bits.map(function (bit) { return '<span>' + bit + '</span>'; }).join('');

    var showClose = snap.show_t0 === false;
    if (els.closeChip) {
      els.closeChip.hidden = !showClose;
      if (showClose) {
        els.closeChip.textContent = (phase === 'holiday' ? '休市' : '已收盘') + ' · 展示下一交易日起触发线';
      }
    }
  }

  /* ---------- 渲染：触发表 ---------- */

  function normalizeRules(row) {
    var rules = row && Array.isArray(row.rules) ? row.rules : null;
    if (rules && rules.length) {
      return rules.map(function (rule) {
        var r = rule && typeof rule === 'object' ? rule : {};
        return {
          key: r.key,
          label: r.label || RULE_LABELS[r.key] || '触发规则',
          price: toNum(r.price),
          pct: toNum(r.pct),
          binding: r.binding === true
        };
      });
    }
    if (row && toNum(row.trigger_price) !== null) {
      return [{
        key: row.trigger_rule,
        label: RULE_LABELS[row.trigger_rule] || '触发规则',
        price: toNum(row.trigger_price),
        pct: toNum(row.trigger_pct),
        binding: true
      }];
    }
    return [];
  }

  function ruleHtml(rule) {
    var label = rule.label || RULE_LABELS[rule.key] || '触发规则';
    var price = rule.price === null || rule.price === undefined
      ? '触发价 —'
      : '<span class="rule-price-label">触发价 </span><b>' + rule.price.toFixed(2) + '</b>';
    return '<li class="rule' + (rule.binding ? ' is-binding' : '') + '">' +
      '<span class="rule-label">' + esc(label) + '</span>' +
      '<span class="rule-price">' + price + '</span>' +
      '</li>';
  }

  function triggerRowHtml(row) {
    var r = row && typeof row === 'object' ? row : {};
    var key = r.key ? String(r.key) : '';
    var date = r.date ? String(r.date) : '';
    var weekday = r.weekday ? String(r.weekday) : '';
    var isToday = r.is_today === true;
    var pct = toNum(r.trigger_pct);
    var price = toNum(r.trigger_price);
    var over = pct !== null && pct < 0;
    var rules = normalizeRules(r);

    var pctHtml;
    if (over) {
      pctHtml = '<strong class="trigger-pct trigger-pct--over">已超线</strong>';
    } else if (pct === null) {
      pctHtml = '<strong class="trigger-pct">—</strong>';
    } else {
      pctHtml = '<strong class="trigger-pct">' + pct.toFixed(2) + '<span class="unit">%</span></strong>';
    }

    var priceHtml = price === null || price === undefined
      ? '<span class="trigger-price num">触发价 —</span>'
      : '<button type="button" class="trigger-price num" data-copy="' + price.toFixed(2) +
        '" title="点击复制触发价 ' + price.toFixed(2) + '" aria-label="复制触发价 ' + price.toFixed(2) + '">触发价 ' +
        price.toFixed(2) + '</button>';

    var rulesHtml = rules.length
      ? rules.map(ruleHtml).join('')
      : '<li class="rule"><span class="rule-label">—</span></li>';

    return '<tr class="trigger-row' + (isToday ? ' is-today' : '') + (over ? ' is-over' : '') + '">' +
      '<td class="cell-day">' +
        '<span class="day-key">' + esc(key || '—') + '</span>' +
        (isToday ? '<span class="today-chip">当日</span>' : '') +
        '<span class="day-date num"' + (weekday ? ' title="' + esc(weekday) + '"' : '') + '>' + esc(date || '—') + '</span>' +
      '</td>' +
      '<td class="cell-trigger">' + pctHtml + priceHtml + '</td>' +
      '<td class="cell-rules"><ul class="rule-list">' + rulesHtml + '</ul></td>' +
      '</tr>';
  }

  function renderTableFoot(data) {
    if (!els.tableFoot) return;
    var reset = data.reset || {};
    var parts = [];
    if (reset.reset10) parts.push('10 日线自 <span class="num">' + esc(reset.reset10) + '</span> 起算');
    if (reset.reset30) parts.push('30 日线自 <span class="num">' + esc(reset.reset30) + '</span> 起算');
    var last = reset.last_trigger;
    if (last && typeof last === 'object') {
      var kindLabel = RULE_LABELS[last.kind] || last.kind || '触发线';
      var text = '上次触发 ' + esc(kindLabel);
      if (last.date) text += ' · <span class="num">' + esc(last.date) + '</span>';
      if (toNum(last.value) !== null) text += '（偏离 ' + fmtPct(last.value) + '）';
      parts.push(text);
    }
    if (!parts.length) {
      els.tableFoot.hidden = true;
      els.tableFoot.innerHTML = '';
      return;
    }
    els.tableFoot.hidden = false;
    els.tableFoot.innerHTML = parts.join('<span class="sep" aria-hidden="true">　·　</span>');
  }

  function renderTriggers(data) {
    var rows = Array.isArray(data.rows) ? data.rows : [];
    if (els.triggerRows) {
      els.triggerRows.innerHTML = rows.length
        ? rows.map(triggerRowHtml).join('')
        : '<tr><td colspan="3" class="table-empty">暂无触发线数据</td></tr>';
    }
    renderTableFoot(data);
  }

  /* ---------- 渲染：详情卡 ---------- */

  function readoutDevHtml(dev, kind) {
    var d = dev && typeof dev === 'object' ? dev : {};
    var threshold = toNum(d.threshold);
    if (threshold === null) threshold = kind === 'dev30' ? 200 : 100;
    var value = toNum(d.value);
    var progress = toNum(d.progress);
    if (progress === null && value !== null && threshold) progress = value / threshold;
    var remaining = toNum(d.remaining);
    var level = d.level || 'unknown';
    var label = d.label || '';
    var title = (kind === 'dev30' ? '30日 ' : '10日 ') + fmtThreshold(threshold) + '% 偏离值';
    var pctWidth = progress === null ? 0 : Math.max(0, Math.min(1, progress)) * 100;

    var remainHtml = '';
    if (remaining !== null) {
      remainHtml = remaining >= 0
        ? '距 +' + fmtThreshold(threshold) + '% 还差 <b>' + remaining.toFixed(2) + '%</b>'
        : '已越过阈值 <b>' + Math.abs(remaining).toFixed(2) + '%</b>';
    } else if (value !== null) {
      remainHtml = '当前 <b>' + value.toFixed(2) + '%</b>';
    }

    var cells = [
      { label: '股票区间涨幅', value: fmtPct(d.stock_change), trend: trendOf(d.stock_change) },
      { label: '指数区间涨幅', value: fmtPct(d.index_change), trend: trendOf(d.index_change) },
      { label: '触发异动价', value: fmtPrice(d.trigger_price), trend: 'flat' }
    ];

    var gridHtml = cells.map(function (cell) {
      var trendAttr = cell.trend === 'flat' ? '' : ' data-trend="' + cell.trend + '"';
      return '<div><dt>' + esc(cell.label) + '</dt><dd class="num"' + trendAttr + '>' + esc(cell.value) + '</dd></div>';
    }).join('');

    var windowHtml = '';
    if (d.window_start || d.window_end) {
      windowHtml = '窗口 <span class="num">' + esc(d.window_start || '—') + ' → ' + esc(d.window_end || '—') + '</span>' +
        (toNum(d.base_close) !== null ? ' · 起算收盘 <span class="num">' + fmtPrice(d.base_close) + '</span>' : '') +
        (d.base_date ? '（<span class="num">' + esc(d.base_date) + '</span>）' : '');
    }

    var ariaNow = value === null ? 0 : value.toFixed(2);
    var ariaText = (value === null ? '暂无' : value.toFixed(2) + '%') + '，阈值 ' + fmtThreshold(threshold) + '%';

    return '<article class="readout" data-level="' + esc(level) + '" aria-label="' + esc(title) + '">' +
      '<header class="readout-head">' +
        '<h3 class="readout-title">' + esc(title) + '</h3>' +
        badgeHtml(level, label) +
      '</header>' +
      '<div class="readout-value-row">' +
        '<p class="readout-value">' + (value === null ? '—' : value.toFixed(2)) + '<span class="unit">%</span></p>' +
        (remainHtml ? '<p class="readout-remaining">' + remainHtml + '</p>' : '') +
      '</div>' +
      '<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="' + esc(fmtThreshold(threshold)) +
        '" aria-valuenow="' + esc(ariaNow) + '" aria-valuetext="' + esc(ariaText) + '">' +
        '<span class="progress-fill" style="--p:' + pctWidth.toFixed(2) + '%"></span>' +
      '</div>' +
      '<dl class="readout-grid">' + gridHtml + '</dl>' +
      (windowHtml ? '<p class="readout-window">' + windowHtml + '</p>' : '') +
      '</article>';
  }

  function readoutSameDirHtml(sd) {
    var d = sd && typeof sd === 'object' ? sd : {};
    var count = toNum(d.count);
    var threshold = toNum(d.threshold);
    if (threshold === null) threshold = 4;
    var remaining = toNum(d.remaining);
    if (remaining === null && count !== null) remaining = Math.max(0, threshold - count);
    var level = d.level || 'unknown';
    var label = d.label || '';
    var tallyCount = Math.max(1, Math.min(threshold, 8));
    var tally = '';
    for (var i = 0; i < tallyCount; i++) {
      tally += '<span class="tally-block' + (count !== null && i < count ? ' is-on' : '') + '"></span>';
    }

    var remainHtml = '';
    if (count !== null && remaining !== null) {
      remainHtml = remaining > 0
        ? '上涨同向已有 <b>' + count + '</b> 次，距离红线还差 <b>' + remaining + '</b> 次'
        : '上涨同向已有 <b>' + count + '</b> 次，已达红线';
    }

    var rule = d.rule ? String(d.rule) : '连续 3 日累计偏离 +20% · 严重异动红线：10 日内 4 次上涨同向';
    var lastEvent = '';
    if (d.last_event && typeof d.last_event === 'object') {
      var ev = d.last_event;
      lastEvent = '最近一次 · <span class="num">' + esc(ev.start || '—') +
        (ev.end ? ' → ' + esc(ev.end) : '') + '</span>' +
        (toNum(ev.pct) !== null ? ' · 累计偏离 <b data-trend="up">' + fmtPct(ev.pct) + '</b>' : '');
    } else {
      lastEvent = '近 10 个交易日暂无计入的上涨同向异动。';
    }

    return '<article class="readout readout--count" data-level="' + esc(level) + '" aria-label="10日同向异动红线">' +
      '<header class="readout-head">' +
        '<h3 class="readout-title">10日同向异动红线</h3>' +
        badgeHtml(level, label) +
      '</header>' +
      '<div class="readout-value-row">' +
        '<p class="readout-value readout-value--fraction">' +
          '<strong class="num">' + (count === null ? '—' : count) + '</strong>' +
          '<span class="fraction-sep">/</span>' +
          '<span class="num fraction-total">' + threshold + '</span>' +
        '</p>' +
        (remainHtml ? '<p class="readout-remaining">' + remainHtml + '</p>' : '') +
      '</div>' +
      '<div class="tally" aria-hidden="true">' + tally + '</div>' +
      '<p class="readout-rule">' + esc(rule) + '</p>' +
      '<p class="readout-event">' + lastEvent + '</p>' +
      '</article>';
  }

  function renderReadouts(data) {
    if (!els.readoutList) return;
    var summary = data.summary || {};
    var parts = [];
    if (summary.dev10) parts.push(readoutDevHtml(summary.dev10, 'dev10'));
    if (summary.dev30) parts.push(readoutDevHtml(summary.dev30, 'dev30'));
    if (summary.same_dir) parts.push(readoutSameDirHtml(summary.same_dir));
    els.readoutList.innerHTML = parts.length
      ? parts.join('')
      : '<p class="readouts-empty">暂无偏离值读数。</p>';
  }

  /* ---------- 渲染：页脚 ---------- */

  function renderFooter(data) {
    var meta = data.meta || {};
    var snap = data.snapshot || {};
    setText(els.disclaimer, meta.disclaimer || DEFAULT_DISCLAIMER);
    setText(els.dataSource, '数据来源 ' + (meta.data_source || '东方财富 / 同花顺 公开行情'));
    setText(els.footerUpdate, fmtDateTime(snap.updated_at) || (snap.trade_date || '—'));
    if (els.calendarWarning) {
      var warning = meta.calendar_warning ? String(meta.calendar_warning) : '';
      els.calendarWarning.hidden = !warning;
      els.calendarWarning.textContent = warning;
    }
  }

  /* ---------- 渲染入口 ---------- */

  function renderReady(data) {
    setMainView('ready');
    renderSummary(data);
    renderNotice(data);
    renderTriggers(data);
    renderReadouts(data);
    renderFooter(data);
    syncWatchButton();
    setFeed('online', '在线');
    var stock = data.stock || {};
    document.title = (stock.name ? stock.name + ' ' : '') + (stock.code || '') + ' · 异动雷达';
    announce('已更新 ' + (stock.name || '') + ' ' + (stock.code || '') + ' 的触发线');
  }

  /* ---------- URL / 路由 ---------- */

  function navigate(code) {
    var url = new URL(location.href);
    url.searchParams.set('code', code);
    history.pushState({ code: code }, '', url);
    loadTriggers(code);
  }

  function goHome(options) {
    var opts = options || {};
    if (opts.push !== false) {
      var code = new URLSearchParams(location.search).get('code');
      if (code) {
        var url = new URL(location.href);
        url.searchParams.delete('code');
        history.pushState({}, '', url);
      }
    }
    showEmptyView();
  }

  function routeFromLocation() {
    var params = new URLSearchParams(location.search);
    var rawCode = params.get('code');
    var code = rawCode === null ? '' : String(rawCode).trim();
    // 分享链接里的 code 参数格式不对时，给内联错误卡而不是静默进入空态。
    if (code && !/^\d{6}$/.test(code)) {
      showErrorView({ code: 'BAD_CODE' }, code);
      return;
    }
    if (/^\d{6}$/.test(code)) {
      loadTriggers(code);
    } else {
      showEmptyView();
    }
  }

  /* ---------- 交互绑定 ---------- */

  function showToast(message) {
    if (!els.toast) return;
    if (state.toastTimer) clearTimeout(state.toastTimer);
    els.toast.textContent = message;
    els.toast.hidden = false;
    window.requestAnimationFrame(function () {
      els.toast.classList.add('is-visible');
    });
    state.toastTimer = setTimeout(function () {
      els.toast.classList.remove('is-visible');
      state.toastTimer = setTimeout(function () {
        if (!els.toast.classList.contains('is-visible')) els.toast.hidden = true;
      }, 240);
    }, 1900);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }).catch(function () {
        return legacyCopy(text);
      });
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    try {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.top = '-1000px';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(area);
      return ok;
    } catch (err) {
      return false;
    }
  }

  function bindCopies() {
    if (!els.main) return;
    els.main.addEventListener('click', function (event) {
      var btn = event.target.closest('[data-copy]');
      if (!btn) return;
      event.preventDefault();
      var value = btn.getAttribute('data-copy');
      copyText(value).then(function (ok) {
        showToast(ok ? '已复制 ' + value : '复制失败，请长按手动复制');
      });
    });
  }

  function bindRefresh() {
    if (!els.refreshBtn) return;
    els.refreshBtn.addEventListener('click', function () {
      if (!state.code || state.loading) return;
      loadTriggers(state.code, { soft: true, manual: true });
    });
  }

  function bindWatch() {
    if (els.watchBtn) els.watchBtn.addEventListener('click', toggleWatch);
  }

  function bindNav() {
    document.addEventListener('click', function (event) {
      var link = event.target.closest('[data-nav-home]');
      if (!link) return;
      event.preventDefault();
      goHome();
      if (els.searchInput) els.searchInput.focus();
    });

    window.addEventListener('popstate', routeFromLocation);
  }

  function bindRetry() {
    if (!els.retryBtn) return;
    els.retryBtn.addEventListener('click', function () {
      var code = state.failedCode || new URLSearchParams(location.search).get('code') || '';
      if (/^\d{6}$/.test(code)) {
        loadTriggers(code);
      } else {
        goHome();
        if (els.searchInput) els.searchInput.focus();
      }
    });
  }

  /* ---------- 自动刷新 ---------- */

  function canAutoRefresh() {
    if (document.hidden || state.loading || !state.data || !state.code) return false;
    var data = state.data;
    if (data.stock && data.stock.suspended) return false;
    return !!data.snapshot && data.snapshot.phase === 'trading';
  }

  function autoRefreshTick() {
    if (!canAutoRefresh()) return;
    if (Date.now() - state.lastRefresh < AUTO_REFRESH_MS - 1000) return;
    loadTriggers(state.code, { soft: true });
  }

  function startAutoRefresh() {
    state.autoTimer = setInterval(autoRefreshTick, AUTO_REFRESH_MS);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) return;
      if (!canAutoRefresh()) return;
      if (Date.now() - state.lastRefresh >= AUTO_REFRESH_MS) {
        loadTriggers(state.code, { soft: true });
      }
    });
  }

  /* ---------- 初始化 ---------- */

  function cacheEls() {
    [
      'main', 'summary', 'searchRow', 'searchInput', 'searchClear', 'searchPanel', 'searchList',
      'themeToggle', 'feed', 'feedText', 'emptyView', 'errorView', 'exampleChips',
      'emptyWatchBlock', 'emptyWatchChips', 'errorTitle', 'errorText', 'errorCode', 'retryBtn',
      'riskBadge', 'boardBadge', 'dataTime', 'stockName', 'stockCode', 'stockBoard',
      'price', 'pct', 'indexName', 'indexPrice', 'indexPct', 'radar', 'radarPrice', 'radarPct',
      'radarStatus', 'refreshBtn', 'watchBtn', 'watchLabel', 'riskReasons', 'noticeMain',
      'noticeSub', 'closeChip', 'triggerRows', 'tableFoot', 'readoutList', 'disclaimer',
      'dataSource', 'footerUpdate', 'toast', 'calendarWarning'
    ].forEach(function (id) { els[id] = $(id); });
  }

  function bindGlobalErrors() {
    function report() {
      document.documentElement.setAttribute('data-runtime-error', '1');
      showToast('页面运行出错，请刷新重试');
    }
    window.addEventListener('error', report);
    window.addEventListener('unhandledrejection', report);
  }

  function init() {
    cacheEls();
    state.watch = loadWatch();
    applyTheme(currentTheme(), false);
    renderEmptyChips();
    syncWatchButton();

    bindTheme();
    bindSearch();
    bindCopies();
    bindRefresh();
    bindWatch();
    bindNav();
    bindRetry();
    bindGlobalErrors();

    routeFromLocation();
    startAutoRefresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();

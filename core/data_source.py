"""行情数据源：东方财富（主）+ 同花顺（备），带重试、限速、内存/磁盘缓存。

只提供数据，不做业务计算（业务口径见 core/engine.py）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from . import calendar_cn as cal

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(PROJECT_ROOT, ".cache")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
EM_REFERER = "https://quote.eastmoney.com/"
THS_REFERER = "http://q.10jqka.com.cn/"
TZ = _dt.timezone(_dt.timedelta(hours=8))  # Asia/Shanghai（A 股无夏令时）

INDEXES = {
    "sh": {"code": "000001", "name": "上证指数", "secid": "1.000001"},
    "sz": {"code": "399106", "name": "深证综指", "secid": "0.399106"},
}
# 沪市指数在同花顺的代码为 1A0001
THS_INDEX_CODE = {"sh": "1A0001", "sz": "399106"}

BOARD_LABEL = {"main": "主板", "gem": "创业板", "star": "科创板", "bse": "北交所"}


class DataError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, min_interval: float = 0.12):
        self._lock = threading.Lock()
        self._last = 0.0
        self._min = min_interval

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._min:
                time.sleep(self._min - delta)
            self._last = time.monotonic()


_RATE = _RateLimiter()
_CACHE_LOCK = threading.Lock()
_MEM: dict[str, tuple[float, Any]] = {}
_EM_DOWN_UNTIL = 0.0          # 东方财富行情接口熔断：失败后一段时间内直接走备用源
_EM_SEARCH_DOWN_UNTIL = 0.0   # 东方财富搜索接口单独熔断


def _em_available() -> bool:
    return time.time() >= _EM_DOWN_UNTIL


def _mark_em_down(seconds: float = 120.0):
    global _EM_DOWN_UNTIL
    _EM_DOWN_UNTIL = max(_EM_DOWN_UNTIL, time.time() + seconds)


def _em_search_available() -> bool:
    return time.time() >= _EM_SEARCH_DOWN_UNTIL


def _mark_em_search_down(seconds: float = 90.0):
    global _EM_SEARCH_DOWN_UNTIL
    _EM_SEARCH_DOWN_UNTIL = max(_EM_SEARCH_DOWN_UNTIL, time.time() + seconds)


def _http_get(url: str, referer: str, timeout: float = 8.0, retries: int = 3) -> str:
    headers = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        _RATE.wait()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data.decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            if exc.code == 404:
                raise DataError("NOT_FOUND", "行情源未收录该代码", 404) from exc
            last_err = exc
            time.sleep(0.4 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
            last_err = exc
            time.sleep(0.4 * (attempt + 1))
    raise DataError("UPSTREAM_ERROR", f"行情接口请求失败：{type(last_err).__name__}") from last_err


def _jsonp_decode(text: str) -> Any:
    """解析同花顺 JSONP：quotebridge_xxx({...})"""
    l = text.find("(")
    r = text.rfind(")")
    if l < 0 or r < 0:
        raise DataError("UPSTREAM_ERROR", "行情接口返回格式异常")
    return json.loads(text[l + 1 : r])


def _mem_get(key: str) -> Any:
    with _CACHE_LOCK:
        item = _MEM.get(key)
        if not item:
            return None
        expire, value = item
        if time.time() > expire:
            _MEM.pop(key, None)
            return None
        return value


def _mem_set(key: str, value: Any, ttl: float):
    with _CACHE_LOCK:
        _MEM[key] = (time.time() + ttl, value)


def _disk_cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", key)
    return os.path.join(CACHE_DIR, safe + ".json")


def _disk_read(key: str) -> Optional[dict]:
    path = _disk_cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _disk_write(key: str, payload: dict):
    path = _disk_cache_path(key)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - 缓存写入失败不影响主流程
        pass


def now_shanghai() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def market_phase(now: _dt.datetime) -> dict:
    """返回交易阶段信息。"""
    today = now.date()
    is_td = cal.is_trading_day(today)
    if not is_td:
        return {"phase": "holiday", "is_trading_day": False, "show_t0": False}
    t = now.time()
    if t < _dt.time(9, 30):
        phase = "pre_open"
    elif t < _dt.time(11, 30):
        phase = "trading"
    elif t < _dt.time(13, 0):
        phase = "lunch"
    elif t < _dt.time(15, 0):
        phase = "trading"
    else:
        phase = "closed"
    return {
        "phase": phase,
        "is_trading_day": True,
        "show_t0": t < _dt.time(15, 0),
    }


def _market_open_for_cache(now: _dt.datetime) -> bool:
    return bool(market_phase(now)["is_trading_day"]) and now.time() < _dt.time(15, 5)


# --------------------------------------------------------------------------
# 代码 / 板块
# --------------------------------------------------------------------------
def classify_code(code: str) -> dict:
    """根据 6 位代码判断市场与板块。"""
    if not re.fullmatch(r"\d{6}", code or ""):
        raise DataError("BAD_CODE", "请输入 6 位 A 股代码", 400)
    if code.startswith(("600", "601", "603", "605")):
        return {"market": "sh", "board": "main", "board_label": "沪主板"}
    if code.startswith(("688", "689")):
        return {"market": "sh", "board": "star", "board_label": "科创板"}
    if code.startswith(("000", "001", "002", "003")):
        return {"market": "sz", "board": "main", "board_label": "深主板"}
    if code.startswith(("300", "301")):
        return {"market": "sz", "board": "gem", "board_label": "创业板"}
    if code.startswith(("430", "83", "87", "88", "920", "4", "8")):
        raise DataError("NOT_SUPPORTED", "北交所 / 老三板股票暂不支持（规则与沪深不同）", 400)
    if code.startswith(("900", "200", "201")):
        raise DataError("NOT_SUPPORTED", "B 股暂不支持", 400)
    raise DataError("NOT_FOUND", "未找到该股票，请检查代码是否正确", 404)


def secid_for(market: str, code: str) -> str:
    return f"{'1' if market == 'sh' else '0'}.{code}"


def index_for_market(market: str) -> dict:
    info = dict(INDEXES[market])
    return info


# --------------------------------------------------------------------------
# 东方财富
# --------------------------------------------------------------------------
EM_QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f86,f169,f170"


def _em_quote(secid: str) -> dict:
    if not _em_available():
        raise DataError("UPSTREAM_ERROR", "行情源暂不可用（东财熔断中）")
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={EM_QUOTE_FIELDS}"
    try:
        text = _http_get(url, EM_REFERER)
    except DataError:
        _mark_em_down()
        raise
    obj = json.loads(text)
    data = obj.get("data")
    if not data:
        raise DataError("NOT_FOUND", "行情源未返回该代码的数据", 404)
    digits = data.get("f59") or 2

    def num(key: str) -> Optional[float]:
        v = data.get(key)
        if v in (None, "-", "", 0):
            return None
        try:
            return float(v) / (10 ** digits)
        except (TypeError, ValueError):
            return None

    price = num("f43")
    prev_close = num("f60")
    pct = data.get("f170")
    pct = (pct / 100.0) if isinstance(pct, (int, float)) else None
    ts = data.get("f86")
    dt = _dt.datetime.fromtimestamp(ts, TZ) if isinstance(ts, (int, float)) and ts > 0 else None
    return {
        "code": str(data.get("f57") or ""),
        "name": str(data.get("f58") or ""),
        "secid": secid,
        "price": price,
        "prev_close": prev_close,
        "pct": pct,
        "open": num("f46"),
        "high": num("f44"),
        "low": num("f45"),
        "volume": data.get("f47"),
        "amount": data.get("f48"),
        "ts": dt,
        "source": "eastmoney",
    }


def _em_klines(secid: str, beg: str, end: str, fqt: str = "0") -> list[dict]:
    if not _em_available():
        raise DataError("UPSTREAM_ERROR", "行情源暂不可用（东财熔断中）")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101"
        f"&fqt={fqt}&beg={beg}&end={end}"
    )
    try:
        text = _http_get(url, EM_REFERER)
    except DataError:
        _mark_em_down()
        raise
    obj = json.loads(text)
    data = obj.get("data") or {}
    bars: list[dict] = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 3:
            continue
        d = p[0].replace("-", "")
        try:
            bars.append(
                {
                    "d": d,
                    "o": float(p[1]),
                    "c": float(p[2]),
                    "h": float(p[3]) if len(p) > 3 and p[3] else None,
                    "l": float(p[4]) if len(p) > 4 and p[4] else None,
                    "v": float(p[5]) if len(p) > 5 and p[5] else None,
                }
            )
        except ValueError:
            continue
    if not bars:
        raise DataError("UPSTREAM_ERROR", "行情源未返回日 K 数据")
    return bars


def _em_search(keyword: str) -> list[dict]:
    token = "D43BF722C8E33BDC906FB84D85E326E8"
    url = (
        "https://searchapi.eastmoney.com/api/suggest/get"
        f"?input={urllib.parse.quote(keyword)}&type=14&token={token}&count=10"
    )
    if not _em_search_available():
        return []
    try:
        text = _http_get(url, EM_REFERER)
    except DataError:
        _mark_em_search_down()
        return []
    obj = json.loads(text)
    rows = (((obj.get("QuotationCodeTable") or {}).get("Data")) or [])
    items = []
    for row in rows:
        # 东方财富对主板/创业板返回 Classify=AStock，对科创板返回 Classify=23
        if row.get("Classify") not in ("AStock", "23", "NEEQ"):
            continue
        code = str(row.get("Code") or "")
        market_num = str(row.get("MktNum") or "")
        if market_num not in ("0", "1"):
            continue
        sec_name = str(row.get("SecurityTypeName") or "")
        if sec_name in ("京A", "北A"):
            # 北交所：规则不同暂不支持计算，但搜索可见，点击后由接口返回 NOT_SUPPORTED
            items.append(
                {
                    "code": code,
                    "name": str(row.get("Name") or ""),
                    "market": "bj",
                    "board": "bse",
                    "board_label": "北交所",
                    "secid": f"{market_num}.{code}",
                }
            )
            if len(items) >= 8:
                break
            continue
        try:
            cls = classify_code(code)
        except DataError:
            continue
        items.append(
            {
                "code": code,
                "name": str(row.get("Name") or ""),
                "market": cls["market"],
                "board": cls["board"],
                "board_label": cls["board_label"],
                "secid": f"{market_num}.{code}",
            }
        )
        if len(items) >= 8:
            break
    return items


# --------------------------------------------------------------------------
# 同花顺（备用）
# --------------------------------------------------------------------------
def _ths_quote_stock(code: str) -> dict:
    url = f"http://d.10jqka.com.cn/v6/realhead/hs_{code}/last.js"
    text = _http_get(url, THS_REFERER)
    obj = _jsonp_decode(text)
    items = obj.get("items") or {}

    def num(key: str) -> Optional[float]:
        v = items.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    price = num("10")
    prev_close = num("6")
    pct = num("199112")
    return {
        "code": code,
        "name": "",
        "secid": secid_for("sh" if code.startswith(("6", "9")) else "sz", code),
        "price": price,
        "prev_close": prev_close,
        "pct": pct,
        "open": num("7"),
        "high": num("8"),
        "low": num("9"),
        "volume": items.get("13"),
        "amount": items.get("19"),
        "ts": None,
        "source": "10jqka",
    }


def _ths_quote_index(market: str) -> dict:
    code = THS_INDEX_CODE[market]
    name = INDEXES[market]["name"]
    url = f"http://d.10jqka.com.cn/v6/time/zs_{code}/last.js"
    text = _http_get(url, THS_REFERER)
    obj = _jsonp_decode(text)
    body = obj.get("zs_" + code) or {}
    pre = float(body.get("pre") or 0) or None
    price = None
    rows = (body.get("data") or "").split(";")
    for row in reversed(rows):
        p = row.split(",")
        if len(p) >= 2 and p[1]:
            try:
                price = float(p[1])
                break
            except ValueError:
                continue
    if price is None:
        raise DataError("UPSTREAM_ERROR", "指数行情暂不可用")
    pct = ((price / pre - 1) * 100) if pre else None
    return {
        "code": INDEXES[market]["code"],
        "name": name,
        "price": price,
        "prev_close": pre,
        "pct": pct,
        "source": "10jqka",
    }


def _ths_klines(ths_code: str, is_index: bool) -> tuple[str, list[dict]]:
    prefix = f"zs_{ths_code}" if is_index else f"hs_{ths_code}"
    url = f"http://d.10jqka.com.cn/v6/line/{prefix}/01/last.js"
    text = _http_get(url, THS_REFERER)
    obj = _jsonp_decode(text)
    name = str(obj.get("name") or "")
    bars: list[dict] = []
    for row in (obj.get("data") or "").split(";"):
        p = row.split(",")
        if len(p) < 5:
            continue
        try:
            bars.append(
                {
                    "d": p[0],
                    "o": float(p[1]) if p[1] else None,
                    "h": float(p[2]) if p[2] else None,
                    "l": float(p[3]) if p[3] else None,
                    "c": float(p[4]),
                    "v": float(p[5]) if len(p) > 5 and p[5] else None,
                }
            )
        except ValueError:
            continue
    if not bars:
        raise DataError("UPSTREAM_ERROR", "行情源未返回日 K 数据")
    return name, bars


# --------------------------------------------------------------------------
# 对外接口（带缓存 / 降级）
# --------------------------------------------------------------------------
class MarketData:
    def __init__(self, quote_ttl: float = 6.0):
        self.quote_ttl = quote_ttl

    # ---- 快照 ----
    def get_stock_quote(self, code: str, market: str) -> dict:
        key = f"quote_stock_{market}_{code}"
        cached = _mem_get(key)
        if cached:
            return cached
        quote: Optional[dict] = None
        errors = []
        try:
            quote = _em_quote(secid_for(market, code))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        if not quote or not quote.get("price"):
            try:
                quote = _ths_quote_stock(code)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if not quote or not quote.get("price"):
            if any(isinstance(e, DataError) and e.code == "NOT_FOUND" for e in errors):
                raise DataError("NOT_FOUND", "未找到该股票，请检查代码是否正确", 404)
            raise DataError("UPSTREAM_ERROR", "无法获取个股实时行情，请稍后重试")
        if not quote.get("name"):
            quote["name"] = self._stock_name(code, market)
        _mem_set(key, quote, self.quote_ttl)
        return quote

    def get_index_quote(self, market: str) -> dict:
        key = f"quote_index_{market}"
        cached = _mem_get(key)
        if cached:
            return cached
        info = INDEXES[market]
        quote: Optional[dict] = None
        try:
            em = _em_quote(info["secid"])
            quote = {
                "code": info["code"],
                "name": info["name"],
                "price": em["price"],
                "prev_close": em["prev_close"],
                "pct": em["pct"],
                "source": "eastmoney",
            }
        except Exception:  # noqa: BLE001
            try:
                quote = _ths_quote_index(market)
            except Exception as exc:  # noqa: BLE001
                raise DataError("UPSTREAM_ERROR", "无法获取基准指数行情，请稍后重试") from exc
        _mem_set(key, quote, self.quote_ttl)
        return quote

    def _stock_name(self, code: str, market: str) -> str:
        """实时接口未返回名称时，用同花顺日 K 接口补名称。"""
        try:
            name, _ = _ths_klines(code, False)
            return name or code
        except Exception:  # noqa: BLE001
            return code

    # ---- 日 K ----
    def get_daily(self, code: str, market: str, is_index: bool = False) -> list[dict]:
        now = now_shanghai()
        ttl = 60.0 if _market_open_for_cache(now) else 3600.0
        key = f"kline_{'idx' if is_index else 'stk_qfq'}_{market}_{code}"
        cached = _mem_get(key)
        if cached:
            return cached

        today = now.date()
        beg = (today - _dt.timedelta(days=500)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        disk = _disk_read(key)
        if disk and time.time() - float(disk.get("fetched_at", 0)) < ttl:
            bars = disk.get("bars") or []
            if bars:
                _mem_set(key, bars, ttl)
                return bars

        bars: Optional[list[dict]] = None
        source = ""
        try:
            if is_index:
                bars = _em_klines(INDEXES[market]["secid"], beg, end, fqt="0")
            else:
                # 个股使用前复权（与行情软件 / mini program 口径一致；除权日不产生虚假跳空）
                bars = _em_klines(secid_for(market, code), beg, end, fqt="1")
            source = "eastmoney"
        except Exception:  # noqa: BLE001
            try:
                ths_code = THS_INDEX_CODE[market] if is_index else code
                _, bars = _ths_klines(ths_code, is_index=is_index)
                source = "10jqka"
            except Exception as exc:  # noqa: BLE001
                if disk and disk.get("bars"):
                    bars = disk["bars"]
                    source = (disk.get("source") or "cache") + "+stale"
                else:
                    raise DataError("UPSTREAM_ERROR", "无法获取历史日 K，请稍后重试") from exc

        bars = [b for b in (bars or []) if b.get("c")]
        bars.sort(key=lambda b: b["d"])
        _disk_write(key, {"fetched_at": time.time(), "source": source, "bars": bars})
        _mem_set(key, bars, ttl)
        return bars

    # ---- 搜索 ----
    def search(self, keyword: str) -> list[dict]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        key = "search_" + keyword
        cached = _mem_get(key)
        if cached is not None:
            return cached
        items: list[dict] = []
        try:
            items = _em_search(keyword)
        except Exception:  # noqa: BLE001
            items = []
        if not items and re.fullmatch(r"\d{6}", keyword):
            try:
                cls = classify_code(keyword)
                q = self.get_stock_quote(keyword, cls["market"])
                items = [
                    {
                        "code": keyword,
                        "name": q.get("name") or keyword,
                        "market": cls["market"],
                        "board": cls["board"],
                        "board_label": cls["board_label"],
                        "secid": secid_for(cls["market"], keyword),
                    }
                ]
            except Exception:  # noqa: BLE001
                items = []
        _mem_set(key, items, 600)
        return items


# --------------------------------------------------------------------------
# 上下文构建：快照 + 日 K → 引擎输入
# --------------------------------------------------------------------------
def _to_date(d: str) -> _dt.date:
    return _dt.date(int(d[0:4]), int(d[4:6]), int(d[6:8]))


def build_context(
    code: str,
    md: Optional[MarketData] = None,
    at: Optional[_dt.datetime] = None,
    price_override: Optional[float] = None,
    index_override: Optional[float] = None,
) -> dict:
    """构建引擎所需的完整上下文（快照 + 日 K 序列 + 交易日信息）。

    at / price_override / index_override 为可选的历史快照回放参数（用于 QA 与演示）：
    传入 at 后，以该时刻为“现在”、以当时日 K 为准重算；price_override 指定当时的现价。

    返回:
      {
        code,name,market,board,board_label,secid,
        stock_quote,index_quote,
        series:[{d,sc,ic}], hist_end, prev_close, price, index_price,
        show_t0, phase, is_trading_day, future_dates, now, suspended, source,
      }
    """
    md = md or MarketData()
    cls = classify_code(code)
    market, board = cls["market"], cls["board"]
    replay = at is not None
    if replay:
        now = at if at.tzinfo else at.replace(tzinfo=TZ)
    else:
        now = now_shanghai()
    phase_info = market_phase(now)
    phase = phase_info["phase"]
    is_td = phase_info["is_trading_day"]
    today = now.date()
    today_s = today.strftime("%Y%m%d")

    # 并行拉取 4 份数据（个股快照、指数快照、个股日K、指数日K），缩短首屏等待
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "stock_q": pool.submit(md.get_stock_quote, code, market),
            "index_q": pool.submit(md.get_index_quote, market),
            "stock_bars": pool.submit(md.get_daily, code, market, False),
            "index_bars": pool.submit(md.get_daily, INDEXES[market]["code"], market, True),
        }
        stock_q = futures["stock_q"].result()
        index_q = futures["index_q"].result()
        stock_bars = futures["stock_bars"].result()
        index_bars = futures["index_bars"].result()

    sources = {stock_q.get("source"), index_q.get("source")}
    index_map = {b["d"]: b["c"] for b in index_bars}
    stock_map = {b["d"]: b["c"] for b in stock_bars}
    if not index_map:
        raise DataError("UPSTREAM_ERROR", "行情源未返回指数日 K 数据")
    last_index_date = max(index_map.keys())
    last_stock_date = max(stock_map.keys()) if stock_map else last_index_date

    # ---- 回放模式：截断到历史日期 ----
    if replay:
        index_map = {k: v for k, v in index_map.items() if k <= today_s}
        stock_map = {k: v for k, v in stock_map.items() if k <= today_s}
        if not index_map:
            raise DataError("NOT_FOUND", "该日期没有可用的历史行情", 404)
        if not is_td:
            # 非交易日：以最近一个交易日为 hist_end
            hist_end = max(index_map.keys())
        else:
            hist_end = today_s
        price = price_override if price_override is not None else stock_map.get(hist_end)
        index_price = index_override if index_override is not None else index_map.get(hist_end)
        if price is None and stock_map:
            price = stock_map[max(stock_map.keys())]
        if index_price is None and index_map:
            index_price = index_map[max(index_map.keys())]
        index_map[hist_end] = float(index_price)
        stock_map[hist_end] = float(price)

    # ---- hist_end / 当前价 ----
    elif is_td:
        hist_end = today_s
        price = stock_q.get("price")
        if price is None or price <= 0:
            price = stock_map.get(last_stock_date) or 0.0
            stock_q["suspended"] = True
        index_price = index_q.get("price")
        if index_price is None or index_price <= 0:
            index_price = index_map.get(last_index_date)
        # 今日 K 线以实时快照为准（盘中=现价，收盘后=收盘价）
        stock_map[hist_end] = float(price)
        index_map[hist_end] = float(index_price)
    else:
        hist_end = last_index_date
        price = stock_map.get(hist_end) or stock_q.get("price") or 0.0
        index_price = index_map.get(hist_end) or index_q.get("price") or 0.0
        stock_map.setdefault(hist_end, price)
        index_map.setdefault(hist_end, index_price)

    if not price or price <= 0:
        raise DataError("UPSTREAM_ERROR", "未能获取有效的最新价格，请稍后重试")

    # ---- 构造序列（股票停牌日按前值填充） ----
    series: list[dict] = []
    last_stock = None
    for d in sorted(index_map.keys()):
        if d > hist_end:
            continue
        if d in stock_map:
            last_stock = stock_map[d]
        if last_stock is None:
            continue
        series.append({"d": d, "sc": last_stock, "ic": index_map[d]})
    if not series or series[-1]["d"] != hist_end:
        base_stock = last_stock if last_stock is not None else price
        series.append({"d": hist_end, "sc": base_stock, "ic": index_map.get(hist_end, index_price)})
    # 最后一条必须是当前价 / 当前指数
    series[-1]["sc"] = float(price)
    series[-1]["ic"] = float(index_price)
    if len(series) < 35:
        raise DataError("UPSTREAM_ERROR", "历史日 K 数据不足（至少需要 35 个交易日），无法计算")

    prev_close = series[-2]["sc"]
    future_n = 9 if phase_info["show_t0"] else 10
    future_dates = [
        x.strftime("%Y%m%d")
        for x in cal.next_trading_days(_to_date(hist_end), future_n)
    ]
    suspended = bool(stock_q.get("suspended")) or price is None or price <= 0

    return {
        "code": code,
        "name": stock_q.get("name") or code,
        "market": market,
        "board": board,
        "board_label": cls["board_label"],
        "secid": secid_for(market, code),
        "stock_quote": stock_q,
        "index_quote": index_q,
        "series": series,
        "hist_end": hist_end,
        "prev_close": prev_close,
        "price": float(price),
        "index_price": float(index_price),
        "show_t0": bool(phase_info["show_t0"]),
        "phase": phase,
        "is_trading_day": bool(is_td),
        "future_dates": future_dates,
        "now": now,
        "suspended": suspended,
        "source": "+".join(sorted(s for s in sources if s)) or "unknown",
        "calendar_warning": cal.calendar_warning(today),
    }

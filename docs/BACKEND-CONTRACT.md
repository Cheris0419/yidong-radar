# 异动雷达 · 后端接口契约 v1

前端（`web/`）只通过以下 HTTP JSON 接口取数。服务端由 Lead 实现，地址与前端同源：
用浏览器打开 `http://127.0.0.1:8765/` 即为前端页面；接口为 `/api/*`。

所有响应均为 `application/json; charset=utf-8`。
成功：`{"ok": true, "data": {...}}`，失败：`{"ok": false, "error": {"code": "BAD_CODE", "message": "..."}}`（HTTP 400/404/500/502）。

---

## 1. GET /api/health

```json
{"ok": true, "data": {"status": "ok", "time": "2026-09-18T09:54:36+08:00", "version": "1.0.0"}}
```

## 2. GET /api/search?q=<关键词>

按代码 / 名称 / 拼音首字母搜索 A 股。`q` 至少 1 个字符。最多返回 8 条。

```json
{
  "ok": true,
  "data": {
    "items": [
      {"code": "000993", "name": "闽东电力", "market": "sz", "board": "main",
       "board_label": "深主板", "secid": "0.000993"}
    ]
  }
}
```
- `market`: `"sh"` | `"sz"`（北交所搜索项为 `"bj"`，仅供提示，接口计算会返回 `NOT_SUPPORTED`）
- `board`: `"main"`（主板）| `"gem"`（创业板）| `"star"`（科创板）| `"bse"`（北交所，前端提示暂不支持）
- `board_label`: `沪主板` / `深主板` / `创业板` / `科创板` / `北交所`

## 3. GET /api/triggers?code=<6位代码>

核心接口。返回某只股票当前快照 + 未来 10 个交易日的严重异动监管触发线。

### 顶层结构

```jsonc
{
  "ok": true,
  "data": {
    "stock":   { ... },   // 个股快照
    "index":   { ... },   // 对应基准指数快照
    "snapshot":{ ... },   // 时间/交易阶段
    "risk":    { ... },   // 头部风险徽标
    "summary": {          // 三张详情卡数据
      "dev10":   { ... },
      "dev30":   { ... },
      "same_dir":{ ... }
    },
    "reset":   { ... },   // 监管清零状态（可为 null 字段）
    "rows":    [ ... ],   // T+0..T+9 或 T+1..T+10，10 行
    "meta":    { ... }    // 说明、免责声明
  }
}
```

### 3.1 stock

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 6 位代码 |
| `name` | string | 股票名称 |
| `market` | string | `sh` / `sz`（北交所项为 `bj`） |
| `board` | string | `main` / `gem` / `star` / `bse` |
| `board_label` | string | 例：`深主板` |
| `secid` | string | 数据源 ID，例 `0.000993` |
| `price` | number | 当前价（停牌时为最新价） |
| `prev_close` | number | 昨收 |
| `pct` | number | 涨跌幅（百分数，例 `10.02`） |
| `open` / `high` / `low` | number | 今日开/高/低 |
| `status` | string | `trading` / `auction` / `pre_open` / `closed` / `suspended` |
| `suspended` | bool | 是否停牌 |

### 3.2 index

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | `399106`（深证综指）或 `000001`（上证指数） |
| `name` | string | 指数名 |
| `price` | number | 当前点位 |
| `prev_close` | number | 昨收点位 |
| `pct` | number | 涨跌幅（百分数） |

### 3.3 snapshot

| 字段 | 类型 | 说明 |
|---|---|---|
| `trade_date` | string | `YYYY-MM-DD`，当前交易日（非交易日时为最近/下一交易日，见 `phase`） |
| `time` | string | `HH:MM:SS` |
| `phase` | string | `pre_open` / `trading` / `lunch` / `closed` / `holiday` |
| `is_trading_day` | bool | 今天是否交易日 |
| `show_t0` | bool | 是否展示 T+0 行。交易时段内为 true（行 T+0..T+9）；收盘后为 false（行 T+1..T+10） |
| `updated_at` | string | ISO8601 带时区，例 `2026-09-18T09:54:36+08:00` |

> 前端请按数组 `rows` 顺序渲染，不要自行推断 T+ 编号与日期。

### 3.4 risk（头部徽标）

```json
{"level": "high", "label": "高危", "reasons": ["10日偏离值 90.96%", "10日同向异动 3/4"]}
```
- `level`: `"high"` | `"watch"` | `"safe"`
- `label`: `高危` | `关注` | `安全`
- `reasons`: 0–3 条中间文字，前端可折叠/悬停展示

### 3.5 summary.dev10 / summary.dev30

当前 10 日（100%）/ 30 日（200%）累计偏离值卡片。

| 字段 | 类型 | 说明 |
|---|---|---|
| `value` | number | 当前累计偏离值（百分数，例 `90.96`） |
| `threshold` | number | 阈值（`100` / `200`） |
| `remaining` | number | 距阈值还差（百分数，可能为负=已超阈值） |
| `progress` | number | 0–1，`value/threshold`（可能 >1） |
| `stock_change` | number | 股票区间涨跌幅（百分数） |
| `index_change` | number | 指数区间涨跌幅（百分数） |
| `window_start` | string | 窗口起算日 `YYYY-MM-DD`（区间最低收盘价所在日） |
| `window_end` | string | 窗口结束日 `YYYY-MM-DD` |
| `base_close` | number | 区间最低收盘价 |
| `base_date` | string | 最低收盘价日期 |
| `trigger_price` | number | 触发异动价格 |
| `level` | string | 该卡片徽标 `high` / `watch` / `safe` |
| `label` | string | 该卡片徽标文字 |

### 3.6 summary.same_dir

10 日同向异动次数卡片。

| 字段 | 类型 | 说明 |
|---|---|---|
| `count` | number | 最近 10 个交易日内上涨同向普通异动次数 |
| `threshold` | number | 红线次数：主板 4，创业板/科创板 3 |
| `remaining` | number | 还差几次 |
| `progress` | number | 0–1 |
| `level` / `label` | string | 徽标 |
| `rule` | string | 固定说明：`连续3日累计偏离+20% · 严重异动红线：10日内4次上涨同向` |
| `last_event` | object\|null | 最近一次异动 `{start,end,pct}`（百分数） |
| `events` | array | 全部已计入的异动（最近 10 日） |

### 3.7 rows（核心表格）

10 行，按时间升序。每行：

```json
{
  "key": "T+0",
  "date": "2026-09-18",
  "weekday": "周五",
  "is_today": true,
  "trigger_pct": 15.27,          // 触发涨幅（百分数，可为负）
  "trigger_price": 20.71,        // 触发价（取各规则最低价）
  "trigger_rule": "10d",         // 决定 trigger_price 的规则 key
  "rules": [
    {"key": "10d",  "label": "10日 100%",     "price": 20.71, "pct": 15.27, "binding": true},
    {"key": "30d",  "label": "30日 200%",     "price": 28.83, "pct": 61.10, "binding": false},
    {"key": "same", "label": "十日同向异动线", "price": 21.71, "pct": 20.78, "binding": false}
  ]
}
```
- `key`: `T+0` … `T+9`（或 `T+1` … `T+10`）
- `weekday`: `周一`…`周日`
- `trigger_pct`：`T+0` 相对昨收，其余相对现价（百分数）
- `rules` 数组长度 2 或 3；`same` 行仅在「必要时」出现（后端决定）
- `binding` 为 true 的规则决定 `trigger_price` / `trigger_pct`，前端加粗高亮
- 若 `trigger_pct < 0` 表示现价已高于该触发价（已超线），前端需有专门视觉

### 3.8 reset

```json
{"reset10": "2026-07-23", "reset30": null, "last_trigger": {"kind": "10d", "date": "2026-07-23", "value": 100.04}}
```
- `reset10` / `reset30`：10 日 / 30 日窗口清零起算日（无则 `null`）
- `last_trigger`：最近一次严重异动触发记录（无则 `null`）

### 3.9 meta

```json
{
  "index_label": "深证综指",
  "row_horizon": 10,
  "data_source": "东方财富",
  "data_note": "以下严重异动监管触发线，会根据指数进行变化",
  "disclaimer": "本工具按公开交易规则辅助估算，不构成投资建议；是否构成异常波动/严重异常波动以交易所认定及公司公告为准。"
}
```

---

## 4. 计算口径备注（实现约定）

- 个股日 K 使用**前复权**价格、指数不复权；区间内并列最低收盘价取**最早**一根 K 线。
- 指数映射：沪市 → 上证指数（000001），深市 → 深证综指（399106）。
- 监管清零：严重异动触发后监管期 10 个交易日，第 11 个交易日起重新起算窗口（沪市仅清 10 日线，深市两条线均清）。
- `rows` 的 `trigger_price` 取该行各规则中的最低价；`binding=true` 的即该行决定 `trigger_price` 的规则。
- `trigger_pct` 为负表示现价已在触发价之上（已超线）。

---

## 5. 错误码

| code | HTTP | 场景 |
|---|---|---|
| `BAD_CODE` | 400 | 代码格式不对 / 缺失 |
| `NOT_FOUND` | 404 | 查不到该股票 |
| `NOT_SUPPORTED` | 400 | 北交所等暂不支持 |
| `UPSTREAM_ERROR` | 502 | 行情源失败 |
| `INTERNAL` | 500 | 其他 |

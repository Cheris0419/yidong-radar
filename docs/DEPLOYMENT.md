# 部署说明

本项目有两种运行形态，按需要选择或同时部署：

| 形态 | 目录 | 运行环境 | 行情获取 | 适用场景 |
|---|---|---|---|---|
| 本地服务版 | `app.py` + `core/` + `web/` | Python 3.10+（标准库） | 服务端请求东方财富 / 同花顺 | 日常使用、内网、自有服务器 |
| 静态在线版 | `site/` | 纯静态托管（GitHub Pages 等） | 浏览器直连公开行情接口 | 免安装、公开分享 |

> GitHub Pages 只能托管静态文件，**不能运行 Python 后端**。在线版通过 `site/calendar.js`、
> `site/engine.js`、`site/data.js`、`site/static-api.js` 在浏览器内完成取数与计算，
> 对外接口形态与本地版 `/api/*` 保持一致。

## 一、GitHub Pages（静态在线版，当前默认）

在线版通过 `gh-pages` 分支发布（仓库 Settings → Pages：
`Build and deployment → Source = Deploy from a branch → 分支 gh-pages / 根目录`）。

```bash
# 发布 / 更新在线版（会自动重建 site/ 并做离线自检）
./scripts/deploy_pages.sh
```

站点地址：`https://<owner>.github.io/<repo>/`（本项目：<https://cheris0419.github.io/yidong-radar/>）。

> 若希望 push 后自动部署，可将 `docs/ci/pages.yml` 复制到 `.github/workflows/` 并使用 GitHub Actions
> 作为 Pages Source（需要仓库的 Actions `workflow` 权限）。

### 修改前端后的同步

静态站复用 `web/` 的 `index.html / styles.css / app.js / favicon.svg`：

```bash
# 改完 web/ 后
python3 scripts/build_site.py     # 同步到 site/（注入静态版脚本）
node scripts/test_site_engine.js  # 静态引擎回归
git add site && git commit -m "feat: update web ui" && git push
./scripts/deploy_pages.sh         # 发布到 gh-pages
```

`site/calendar.js / engine.js / data.js / static-api.js` 为静态版专属文件，由构建脚本保留、不覆盖。
如需自定义域名，在 `site/CNAME` 写入域名（会随 `site/` 一起发布），或在 Pages 设置中填写。

### 自定义域名

仓库 Settings → Pages → Custom domain 填写域名，并按 GitHub 提示配置 CNAME（可在 `site/CNAME` 放置域名）。

## 二、容器 / 云平台（服务版，带缓存与备用源）

`Dockerfile` 已就绪，容器内暴露 `$PORT`（默认 8765），绑定 `0.0.0.0`：

```bash
docker build -t yidong-radar .
docker run -p 8765:8765 yidong-radar
# 打开 http://127.0.0.1:8765/
```

常见平台（任选其一，需要自有账号）：

- **Render**：New → Web Service → 选择仓库 → Runtime 选 Docker；或直接用官方 Python 环境：
  Build `pip install -r requirements.txt`（本项目无第三方依赖，可省略），
  Start `python3 app.py --host 0.0.0.0 --port $PORT`。
- **Railway**：New Project → Deploy from GitHub repo，自动识别 Dockerfile。
- **Fly.io**：`fly launch`（自动识别 Dockerfile）→ `fly deploy`。
- **自有 VPS**：`python3 app.py --host 0.0.0.0 --port 8765`，用 Nginx/Caddy 反代并配置 HTTPS。

注意事项：

- 服务版从服务器所在网络请求东方财富 / 同花顺，海外机房可能访问受限（可部署在境内或配置出口代理）；
- 免费实例通常有休眠/限流，首次打开或行情源抖动时会自动走备用源；
- 磁盘缓存目录为 `.cache/`（可挂载卷持久化）；
- 工具无用户系统、无写接口，暴露公网时建议仅绑定需要的端口并加访问控制（如 Basic Auth）。

## 三、本地开发

```bash
python3 app.py --port 8765            # 后端 + web/ 前端
python3 app.py --selftest             # 离线历史快照自检
cd site && python3 -m http.server 8130 # 本地预览静态在线版
```

## 四、排障

| 现象 | 排查 |
|---|---|
| 在线版提示「行情接口请求失败」 | 浏览器/网络无法访问 `d.10jqka.com.cn`；切换网络重试 |
| 在线版搜索失败 | 浏览器拦截了 `searchapi.eastmoney.com` 的 JSONP（广告拦截插件/企业网关），或接口临时异常 |
| 服务版首次打开慢 | 服务端正在向行情源取数（自动重试），随后有缓存 |
| 数据与行情软件有 0.01 差异 | 不同数据源的收盘价舍入差异，不影响结论 |
| 触发价/涨幅与参考小程序不完全一致 | 见 [METHODOLOGY.md](METHODOLOGY.md) 的「已知差异」 |

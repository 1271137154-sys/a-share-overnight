# A股尾盘选股（第一阶段）

一个仅用于研究的收盘后日线筛选网站：展示三套策略的候选股、未通过条件、历史日期以及 CSV 导出。**不连接券商、不下单、不构成投资建议。**

## 当前范围与限制

- 数据源：AkShare（公开数据）。接口可能受限、延迟或字段变动；结果需与行情软件核对。
- 第一阶段以收盘后日线研究为主，未实现 14:50 分钟级实时筛选。
- 可通过 `POST /performance/{筛选日期}` 补记次日开盘、最高、最低、收盘收益；9:35 收益必须使用分钟数据，因此第一阶段会明确显示为空。
- 为避免对公开接口造成高频全市场请求，刷新功能暂只拉取前 200 个主板候选的历史数据。这是工程保护措施，不是完整市场扫描；正式使用前应替换为稳定/授权的数据源并增加批量缓存任务。
- 涨停日期使用历史日涨幅约 9.5% 的近似识别；复权、ST 历史状态、停牌和涨停价规则将在下一阶段完善。

## 策略配置

所有参数都在 [config/strategies.json](config/strategies.json)，代码不硬编码策略阈值。包含：

1. 强势收盘隔夜动量
2. 近期涨停后的温和趋势
3. 涨停后整理再转强

## 安装与启动（Windows）

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pytest
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

电脑本机访问 `http://127.0.0.1:8000`。手机与电脑在同一 Wi-Fi 时，使用电脑局域网 IP，例如 `http://192.168.1.25:8000`。Windows 防火墙如询问，请只允许专用网络。

## 功能

- 首页按策略分组展示候选；多策略命中会高亮。
- 每只股票展示价格、涨幅、量比、换手、成交额、均线、距 5 日线、收盘/最高、最近涨停。
- 展开“查看未通过条件”可审计筛选逻辑。
- 支持历史日期和 CSV 导出。
- `/health` 用于健康检查。

## 后续建议

1. 添加完整主板历史日线批量缓存与交易日历。
2. 接入分钟数据，严格实现 14:50 选股与次日表现追踪。
3. 对接稳定授权行情源，增加数据质量校验。
4. 增加账号登录、HTTPS、云端部署、定时任务和手机提醒。

## 云端部署（生产版）

生产环境默认只读：访客仅能浏览、切换历史日期并导出 CSV。只有管理员登录后才可以触发公开数据刷新或写入次日收益；没有任何券商接口或下单能力。

### 生产环境变量

从 `.env.example` 复制 `.env`，然后至少设置：

```text
SESSION_SECRET=一段随机且足够长的字符串
ADMIN_USERNAME=你的管理员账号
ADMIN_PASSWORD_HASH=管理员密码的SHA-256哈希
```

生成密码哈希：

```bash
python -c "import hashlib; print(hashlib.sha256(b'你的密码').hexdigest())"
```

不要将 `.env` 提交到 Git，也不要在代码、镜像或前端中保存 API Token、管理员密码或券商信息。

### Docker Compose

安装 Docker 后，在项目根目录执行：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

生产启动命令（单容器）：

```bash
docker build -t a-share-overnight .
docker run -d --name a-share-overnight --restart unless-stopped --env-file .env -p 8000:8000 -v "$(pwd)/data:/app/data" -v "$(pwd)/logs:/app/logs" a-share-overnight
```

SQLite 和轮转日志位于 `data/`、`logs/`，Compose 已将二者作为宿主机持久化目录。应用日志单文件最大 5 MB，保留 5 个备份。`/health` 是无认证健康检查接口。

### Render 部署

1. 将代码推送到私有 GitHub 仓库，确保 `.env` 不被提交。
2. Render 新建 **Web Service**，连接仓库，环境选择 **Docker**。
3. 在 Render 的 Environment 中逐项设置 `.env.example` 里的变量；`SESSION_SECRET` 和管理员哈希必须使用自己的值。
4. 使用 Render 的 Persistent Disk，挂载路径设为 `/app/data`；日志建议输出到平台日志，或挂载 `/app/logs`。
5. Health Check Path 填 `/health`，服务端口使用 `8000`。
6. 免费实例可能休眠，且公开行情源在海外机房可能受限；作为长期数据任务不推荐免费方案。

## 免费手机访问：GitHub Pages

项目另带一个免费静态发布方案：GitHub Actions 在工作日北京时间约 15:10 运行筛选脚本，生成 `site/data/` 的结果文件并发布到 GitHub Pages。手机可随时打开 Pages 的固定网址，电脑无需开机。

限制：GitHub 的计划任务可能延迟，不能保证精准到 15:10；该模式是只读静态页面，不提供在线管理员登录或实时刷新。它保留今日候选、三种策略分组、多策略命中、结构评分、风险提示与淘汰原因。

启用步骤：

1. 在 GitHub 新建一个仓库并将项目推送上去；公开仓库可直接使用免费 Pages。
2. GitHub 仓库进入 **Settings → Pages**，Source 选 **GitHub Actions**。
3. 进入 **Actions**，允许 `Publish free dashboard` 工作流写入仓库内容和部署 Pages。
4. 手动运行一次工作流（Run workflow）完成首次数据生成和发布。
5. 部署完成后，GitHub 会在 Actions 的部署结果中显示可收藏到手机的 `https://用户名.github.io/仓库名/` 地址。

工作流位于 `.github/workflows/publish-pages.yml`。其计划表达式为 UTC `07:10`，即上海时间 `15:10`。

### 腾讯云 Ubuntu 部署

1. 创建 Ubuntu 22.04/24.04 云服务器，安全组仅放行 `22`、`80`、`443`；不要直接向公网开放 `8000`。
2. 安装 Docker：`curl -fsSL https://get.docker.com | sudo sh`，然后将当前用户加入 docker 组并重新登录。
3. 将项目上传到服务器，例如 `/opt/a-share-overnight`，创建并填写 `.env`。
4. 执行 `docker compose up -d --build`，用 `docker compose logs -f` 查看服务日志。
5. 用 Nginx/Caddy 反向代理域名到 `127.0.0.1:8000`，申请 HTTPS 证书；HTTPS 之后将 `https_only=True` 用于会话 Cookie。
6. 定期备份 `/opt/a-share-overnight/data/stock_selector.db`；SQLite 不适合多副本横向扩容，扩容时请迁移 PostgreSQL。

### 定时任务

应用内置 APScheduler：默认在上海时区每个工作日 `15:10` 触发。执行前会读取 A 股交易日历；非交易日会写入“skipped”状态，不拉取行情。实际筛选失败会写入 `logs/app.log` 和任务状态表。

- `ENABLE_SCHEDULER=true|false`：控制是否启用调度器。
- `SCHEDULER_HOUR=15`、`SCHEDULER_MINUTE=10`：调整执行时间。
- 页面 `/status` 展示最近一次任务状态、更新时间、候选数量和失败信息。
- 管理员可在 `/status` 或首页点击“立即更新”。同一交易日任务已成功后，重复触发只会返回已有结果，不会重复请求或插入数据。

部署多副本时只能启用一个调度器实例，否则会造成重复执行竞争；当前 SQLite 版本建议保持单副本运行。

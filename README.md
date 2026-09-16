# A股公告看板 · 方案3最终修正版

本版为“方案3”：综合方案1旧版与方案2新版后形成的最终修正版。基于两版代码与脱敏运维脚本修复主要业务与部署链路。继续使用 **PandaStack + SQLite + 静态网页**，不改成另一套托管平台。

最重要的部署变化：**一个固定、设置为 persistent 的采集沙箱，独占数据卷；Function 只负责向这个沙箱发起任务。** 禁止继续运行旧的“一轮新建一个挂卷沙箱”脚本。

## 新架构（GitHub 数据仓库 + GitHub Pages + 临时 Sandbox）

本版按最终规划改为：**GitHub 长期保存数据和静态网站；PandaStack 只做一次性计算。** 不再要求常驻 Sandbox、PandaStack Volume 或 PostgreSQL。

每次定时更新：`Schedule → Function → 创建临时 Sandbox → GitHub 拉取 → 临时 SQLite → 公告/K线/市值更新 → 静态建站 → GitHub push → 删除 Sandbox`。

GitHub 数据仓库建议单独建立 `panda-board-data`，其中：

```text
data/
  stocks/<code>.json
  meta/fetch_days.json
  meta/day_fetch.json
  meta/board_meta.json
site/
  index.html
  dashboard.html
  ...
```

采集不完整、建站失败或 Git push 未确认时，不提交新版本；网站继续使用上一版本。

详见 `docs/GITHUB_PANDA_DEPLOY.md`。

## 先看这两份文档

- [审核报告与已知边界](docs/AUDIT.md)：原问题、改法、测试证据、尚未验证的项目。
- [部署及旧库迁移](docs/DEPLOYMENT.md)：必须先暂停旧任务并备份；不要直接覆盖线上脚本后放任旧定时任务运行。

## 本地离线验证

需要 Python 3.10+（本次实际使用 3.12）、SQLite 3.25+；前端测试需要 Node.js 18+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/test_http_pool.py
node tests/test_frontend.cjs
```

Windows 对应解释器路径是 `.venv\Scripts\python.exe`。核心采集/构建可在 Windows 本地运行；固定 worker 的部署脚本需要 PandaStack Linux 环境。

离线生成演示网站（不会推送 Git，也不会调用 PandaStack）：

```bash
export BOARD_FETCHER=mock
export BOARD_DATA_DIR="$PWD/data-demo"
export BOARD_SITE_DIR="$PWD/dist-demo"
.venv/bin/python -m app.cli run --mode full --lookback-days 3 --max-attempts 1
.venv/bin/python -m app.cli build
.venv/bin/python -m http.server 8080 --directory dist-demo --bind 127.0.0.1
```

打开 `http://127.0.0.1:8080/`。页面会标注“模拟数据演示”；**上线前移除 `BOARD_FETCHER=mock`，不要把演示库当生产库。** 这些示例目录专供本地演示，不会自动清理或覆盖已有数据库。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python -m app.cli check` | 检查配置、挂载要求、SQLite 完整性和磁盘；首次运行会执行有备份的迁移 |
| `python -m app.cli run` | 采集近期公告并补扫90天内历史日期，刷新当前事件股行情 |
| `python -m app.cli run --mode full --start YYYY-MM-DD --end YYYY-MM-DD` | 补抓明确日期区间；两端均包含 |
| `python -m app.cli gaps --days 90` | 区分未抓取、不完整和来源确认零公告；有缺口时退出码1 |
| `python -m app.cli build` | 从一致的数据库快照生成完整、带哈希清单的静态产物 |
| `python -m app.cli publish` | 推送独立 `data` 分支，并等待**该提交对应的部署**变成 live |
| `python -m app.cli cycle` | 采集、构建、推送、部署确认；即使新增0条也会尝试发布 |
| `python -m app.cli backup --to /absolute/new-backup.db` | SQLite 在线备份，包括尚未 checkpoint 的 WAL 内容 |
| `python -m app.cli seed --from /absolute/backup.db` | 导入已验证备份；已有目标库时必须显式 `--force`，并自动留旧库备份 |
| `python -m app.cli reclassify` | 按当前规则重新分类已保留的公告，不重新下载全文 |

所有数据库 CLI 都加进程锁。`--no-deploy` 是明确的“仅推 Git、不确认上线”选项，不会被标为部署成功。任务失败退出非零；`cycle` 即使发布了可用的旧数据，也不会把不完整采集改写成成功。

## 数据口径

- 公告：取标题、日期、关联股票、原文链接等元数据；**不下载/解析公告 PDF 全文**。分类是可配置的关键词规则，不是对公告法律含义、利好利空或投资价值的认定。
- 近90天按北京时间的自然日计算，含周末和节假日；完整性只表示选定来源的分页与源计数校验通过，不代表交易所公告全覆盖。
- 默认东财公告；巨潮为可选源，默认查询沪深两栏，不能宣称已覆盖北交所。切换源后按新来源单独记账，历史数据不自动删除。
- K线：默认请求最近160根已收盘日线，网页提供最多120根，默认可视最近60根。同一展示价格段保持同一来源、复权口径与快照版本，缺失不填0。取得前复权数据时按前复权显示；仅取得不复权数据时明确标注。
- `日` / `5日`：最近1 / 5个价格间隔的累计变化，并不表示北京时间今天一定有新行情；看具体行情日期。
- `窗口首公告`：展示窗口中该股票首条公告日前一根收盘价至最新收盘价的变化；不随界面筛选重算，**不是公告发布后实际可买入的收益**。无基准价格时显示 `—`。
- 参考市值沿用原配置：首次成功获取后缓存，不按天刷新，不冒充实时市值。
- 无响应、超时、格式错误均不是“零公告/无行情”；旧数据保留，并显示不完整状态。

## 项目结构

| 目录 | 用途 |
| --- | --- |
| `app/` | 采集、分类、SQLite、构建、发布、命令行 |
| `app/sources/` | 东财/巨潮公告、腾讯/东财日线、离线模拟数据 |
| `config/` | 分类规则与不含真实密钥的配置样例 |
| `function/` | 无密钥源码包、定时任务启动器与部署工具 |
| `scripts/` | 固定 worker 的初始化和任务入口 |
| `tools/` | 固定 worker 控制、诊断、备份和源探测 |
| `web/` | 网页模板、交互脚本、保留的 ECharts 定制包 |
| `tests/` | 自动化业务/运维/前端行为测试 |
| `docs/` | 审核报告、迁移操作说明 |

审核时没有有效的 PandaStack/GitHub 凭据，也没有线上 `board.db`；真实源连接超时。当前交付的是可运行、带测试的修复源码，**不是已经替换并验收上线的生产网站**。

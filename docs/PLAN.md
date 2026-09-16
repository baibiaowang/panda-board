# panda-board 方案3：GitHub + PandaStack 最终规划

## 目标

- GitHub 长期保存结构化数据和静态网站。
- GitHub Pages 对外提供静态网站。
- PandaStack 不常驻 Sandbox、不使用长期 Volume、不使用 PostgreSQL。
- 每次更新只创建一个临时 Sandbox，完成后立即删除。

## 数据范围

只保存：

- 股票基础信息
- 公告列表元数据：日期、标题、股票、分类、链接等
- 日 K 线
- 市值
- 数据完整性和更新时间元数据

不保存公告正文、PDF、附件和图片。

## 运行流程

`Schedule → Function → 临时 Sandbox → clone GitHub → 临时 SQLite → 采集 → 校验 → 建站 → 导出数据 → Git commit/push → 删除 Sandbox`

## GitHub 数据仓库

```text
data/
  stocks/<code>.json
  meta/fetch_days.json
  meta/day_fetch.json
  meta/board_meta.json
  meta/export.json
site/
  index.html
  dashboard.html
  ...
```

每只股票一个 JSON 文件，包含该股票的基础信息、公告列表、K 线和行情快照元数据；跨股票的完整性记录放在 `data/meta/`。

## 临时 SQLite

SQLite 只作为一次更新期间的工作数据库。任务开始从 GitHub 导入，任务成功后再导出到 GitHub；Sandbox 销毁后本地 SQLite 消失。

## 发布原则

采集不完整、建站失败、数据校验失败或 Git push 未确认时，不提交 GitHub 新版本。这样 GitHub Pages 始终保留上一份可用版本。

## 定时

推荐每天北京时间 18:30 执行一次。Schedule 只负责触发 Function；Function 创建有 TTL 的 Sandbox，并在 `finally` 中显式删除。TTL 是第二道清理保险。

## Secret

Token 只存在 PandaStack Function 环境变量；Function 临时写入 Sandbox 的 `/workspace/github-token`，读取后立即删除。GitHub URL 不包含 Token。

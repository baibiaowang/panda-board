# 第三方图表资源

`echarts.min.js` 原样保留自用户上传的源码包。它是只导出到 `window.echarts` 的定制打包文件，运行时报告版本 `6.1.0`，不是本次从上游重新下载的标准发行包。

- 大小：605,715 bytes。
- SHA-256：`3ed43f0d8c36a780e708dab944efb991c215802ee1a892f5c5838c39e5a25476`。
- 本次验证了 JavaScript 语法、加载及 `echarts.init` 导出；没有逐行重新审计压缩后的第三方实现，也没有验证该文件与官方发行签名相符。
- 上游项目是 Apache ECharts，按 Apache License 2.0 分发；附通用许可文本 `LICENSE-Apache-2.0.txt`。附许可证不等同于已经核验这个定制包的完整供应链或所有转引组件的 NOTICE。
- 公开分发前应从 [Apache ECharts 官方下载页](https://echarts.apache.org/en/download.html) 核对来源、签名、组件清单及 NOTICE。更换图表包时需要重新执行浏览器验收；不要用未经核验的第三方 CDN 自动替换。

构建器复制此目录，并给图表 JavaScript 生成带内容哈希的版本名。不要手工修改 `dist` 中的文件后直接发布。

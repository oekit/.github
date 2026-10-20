# oekit

> **O**pen **E**asy **Kit** — 开源 · 易用 · 工具集合

Go 后端模块矩阵：**24 个独立发版的 Go 模块仓库**（根模块）＋ **7 个嵌套模块**各自独立发版，合计 **32 个发版单元**，从零依赖微工具库到 Hertz/Kitex 生产级生态。依赖严格自底向上，全矩阵无环。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/oekit/oekit/main/docs/assets/architecture-dark.svg">
    <img src="https://raw.githubusercontent.com/oekit/oekit/main/docs/assets/architecture.svg" alt="oekit 模块矩阵 · 架构总览">
  </picture>
</p>

- 🌐 官方站点：**https://oekit.github.io/**（含 9 篇指南）
- 📦 模块矩阵：[pkg](https://github.com/oekit/pkg)（23 合 1 微工具集）· [kitx](https://github.com/oekit/kitx) · [logx](https://github.com/oekit/logx) · [tracing](https://github.com/oekit/tracing) · [registry](https://github.com/oekit/registry) · [middleware](https://github.com/oekit/middleware) · [hertz](https://github.com/oekit/hertz) · [kitex](https://github.com/oekit/kitex) · [bootstrap](https://github.com/oekit/bootstrap) · [etcdx](https://github.com/oekit/etcdx) · [k8s](https://github.com/oekit/k8s) · …
- 🧩 嵌套模块（可选装 · 依赖隔离）：`logx/sink` · `logx/hlog` · `logx/hertzlog` · `logx/kitexlog` ·
  `bootstrap/nacos` · `bootstrap/obs` · `timertask/redis` —— 各自独立发版；
  只 import 根模块的消费者**不会**被拉入这些重依赖，需要时才单独 `go get`
- 🏗️ 架构总览：[oekit/oekit](https://github.com/oekit/oekit#架构总览)（上图源文件 + 分层表 + 关键依赖链）
- 🤖 AI Agent：每仓 `AGENTS.md` + 全站 [llms.txt](https://oekit.github.io/llms.txt) + [《AI Agent 使用》](https://oekit.github.io/guide/ai-agent.html)
- 📜 沿革：源自 ekit 单体库（git.enjoye.top/enjoydream/ekit v0.34.0，冻结维护），2026-10 按框架耦合分层拆分

## 治理大盘

全 org 的进度/质量/版本不散落在 28 个仓里看——收在 4 块 [GitHub Projects](https://github.com/orgs/oekit/projects) 上（公开、实时）：

| 大盘 | 看什么 | 链接 |
|---|---|---|
| **Issue 大盘** | 跨仓缺陷/增强/重构/文档统一视图，Status × Kind × Repository | [projects/1](https://github.com/orgs/oekit/projects/1) |
| **版本大盘** | 31 个发版单元（24 根模块 + 7 嵌套模块）的最新 tag / 上一版 / 层级归属 | [projects/2](https://github.com/orgs/oekit/projects/2) |
| **矩阵健康** | 安装行↔tag 对齐、门面壳/AGENTS/CHANGELOG 卫生巡检 | [projects/3](https://github.com/orgs/oekit/projects/3) |
| **PR 审查队列** | 跨仓 PR 统一进出，避免散落各仓被漏看 | [projects/4](https://github.com/orgs/oekit/projects/4) |

**联动约定**（细则见 [CONTRIBUTING](https://github.com/oekit/.github/blob/main/CONTRIBUTING.md)）：

1. 开 issue → 标题带 `[模块]` 前缀 → 进 **Issue 大盘**（开闭态自动映射 Todo/Done）
2. 提 PR → 在 **PR 审查队列** 入队，标 Scope（feat/fix/docs/…）
3. 发版 → 刷 **版本大盘** 的 Latest/Prior；安装行漂移会出现在 **矩阵健康**
4. `matrix-tools` 周巡检发现漂移 → 自动开 `matrix-drift` issue → 同样汇入 Issue 大盘

## 快速开始

```bash
go get github.com/oekit/pkg@latest        # 微工具集
go get github.com/oekit/logx@latest       # 日志门面
go get github.com/oekit/middleware@latest github.com/oekit/hertz@latest  # HTTP 全家
```

MIT License

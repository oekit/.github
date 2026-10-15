# oekit

> **O**pen **E**asy **Kit** — 开源 · 易用 · 工具集合

Go 后端模块矩阵：**23 个独立发版的仓库**，从零依赖微工具库到 Hertz/Kitex 生产级生态。依赖严格自底向上，全矩阵无环。

![oekit 模块矩阵 · 架构总览](https://raw.githubusercontent.com/oekit/oekit/main/docs/assets/architecture.svg)

- 🌐 官方站点：**https://oekit.github.io/**（含 8 篇指南）
- 📦 模块矩阵：[pkg](https://github.com/oekit/pkg)（16 合 1 微工具集）· [logx](https://github.com/oekit/logx) · [tracing](https://github.com/oekit/tracing) · [registry](https://github.com/oekit/registry) · [middleware](https://github.com/oekit/middleware) · [hertz](https://github.com/oekit/hertz) · [kitex](https://github.com/oekit/kitex) · [bootstrap](https://github.com/oekit/bootstrap) · …
- 🏗️ 架构总览：[oekit/oekit](https://github.com/oekit/oekit#架构总览)（上图源文件 + 分层表 + 关键依赖链）
- 🤖 AI Agent：每仓 `AGENTS.md` + 全站 [llms.txt](https://oekit.github.io/llms.txt) + [《AI Agent 使用》](https://oekit.github.io/guide/ai-agent.html)
- 📜 沿革：源自 ekit 单体库（git.enjoye.top/enjoydream/ekit v0.34.0，冻结维护），2026-10 按框架耦合分层拆分

## 快速开始

```bash
go get github.com/oekit/pkg@latest        # 微工具集
go get github.com/oekit/logx@latest       # 日志门面
go get github.com/oekit/middleware@latest github.com/oekit/hertz@latest  # HTTP 全家
```

MIT License

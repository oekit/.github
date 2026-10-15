# oekit

> **O**pen **E**asy **Kit** — 开源 · 易用 · 工具集合

Go 后端模块矩阵：**22 个独立发版的仓库**，从零依赖微工具库到 Hertz/Kitex 生产级生态。

- 🌐 官方站点：**https://oekit.github.io/**（含 7 篇指南）
- 📦 模块矩阵：[pkg](https://github.com/oekit/pkg)（16 合 1 微工具集）· [logx](https://github.com/oekit/logx) · [tracing](https://github.com/oekit/tracing) · [registry](https://github.com/oekit/registry) · [middleware](https://github.com/oekit/middleware) · [hertz](https://github.com/oekit/hertz) · [kitex](https://github.com/oekit/kitex) · [bootstrap](https://github.com/oekit/bootstrap) · …
- 📖 指南：[快速开始](https://oekit.github.io/guide/getting-started.html) · [日志](https://oekit.github.io/guide/logging.html) · [错误处理](https://oekit.github.io/guide/errors.html) · [HTTP 服务](https://oekit.github.io/guide/http-service.html) · [RPC 服务](https://oekit.github.io/guide/rpc-service.html) · [迁移](https://oekit.github.io/guide/migration.html)
- 📜 沿革：源自 ekit 单体库（git.enjoye.top/enjoydream/ekit v0.34.0，冻结维护），2026-10 按框架耦合分层拆分

## 快速开始

```bash
go get github.com/oekit/pkg@latest        # 微工具集
go get github.com/oekit/logx@latest       # 日志门面
go get github.com/oekit/middleware@latest github.com/oekit/hertz@latest  # HTTP 全家
```

MIT License

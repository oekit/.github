# 安全策略 — oekit

## 支持范围

oekit 是 **开源 Go 模块矩阵**，各仓独立 SemVer 发版。

| 版本线 | 安全修复 |
|---|---|
| 各模块最新 minor | ✅ 接收 |
| 更早的 minor / patch | ⚠️ 仅最新 minor 回移；0.x 阶段建议直接升级 |
| 已 retract 的版本 | ❌ 请勿使用，直接升级到替代版本 |

## 报告漏洞

**不要在公开 issue 里贴漏洞细节。**

1. 使用 GitHub 私密漏洞报告：https://github.com/oekit/<repo>/security/advisories/new  
   （任意模块仓皆可；跨模块问题报到 [oekit/oekit](https://github.com/oekit/oekit/security/advisories/new)）
2. 或发邮件至维护者（见 org 页 / 各仓 `CODEOWNERS`）。

请尽量包含：

- 受影响的模块与版本（`go list -m` 的输出最方便）
- 复现步骤或 PoC（可最小化）
- 影响面估计（是否可远程触发、是否需要特权、数据泄露/DoS/RCE 等）
- 你期望的披露时间表

## 响应节奏

| 节点 | 目标 |
|---|---|
| 首次确认 | 48 小时内 |
| 初步定级（严重/高/中/低） | 5 个工作日内 |
| 修复或缓解方案 | 严重：7 天内出补丁版本；高：14 天；中/低：进常规发版列车 |

我们默认 **协同披露**：修复版本发布前不公开细节；若你有独立时间表，请在报告里注明。

## 依赖与扫描

- 共享 CI 已启用 `govulncheck`（非阻塞报告）；模块侧若发现 **可利用的传递依赖漏洞**，请按上述渠道报。
- `matrix-tools` 周巡检会抓安装行/版本漂移；安全相关回移请同时开一条普通 issue 跟踪（可不含细节），挂到 [Issue 大盘](https://github.com/orgs/oekit/projects/1) 留痕。

## 密钥与凭据

- 仓库内 **禁止** 提交任何 token / 私钥 / 内网地址 / 客户名。
- CI 只用 GitHub 原生 `GITHUB_TOKEN`；不引入长期第三方密钥。

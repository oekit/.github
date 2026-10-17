# oekit/.github

oekit org 的 GitHub 统一规则仓：org profile + **全 org 共享的 CI 门禁（reusable workflows）**。

各模块仓库的 `.github/workflows/ci.yml` 只保留一个薄调用层，规则本体统一在本仓维护——
改一处（golangci-lint 版本、门禁步骤、覆盖率策略），所有引用 `@main` 的仓库立即生效。

## 治理大盘联动

本仓同时是 **org 治理入口**：org profile、社区规范文件、issue/PR 模板都在这里，与 4 块 [GitHub Projects](https://github.com/orgs/oekit/projects) 打通：

| 大盘 | 本仓的挂钩 |
|---|---|
| [Issue 大盘](https://github.com/orgs/oekit/projects/1) | `profile/README.md` 入口；issue 模板强制 `[模块]` 前缀与 Bug/Enhancement/Docs 标签 → Kind 可归类；`matrix-drift` 巡检 issue 自动汇入 |
| [版本大盘](https://github.com/orgs/oekit/projects/2) | CONTRIBUTING 发版清单；`templates/` 与 `scripts/refresh_versions` 语义对齐 |
| [矩阵健康](https://github.com/orgs/oekit/projects/3) | CONTRIBUTING「矩阵卫生」清单 = audit 硬门禁；漂移 issue 进 Issue 大盘 |
| [PR 审查队列](https://github.com/orgs/oekit/projects/4) | PR 模板 checklist 要求入队 + 定 Scope |

```text
issue 模板 ──► 各仓 issue ──► Issue 大盘 (Todo/Done 自动映射)
                                    ▲
matrix-tools 周巡检 ──► matrix-drift issue ┘
                                    │
PR 模板 ──► 各仓 PR ──► PR 审查队列 (待审→可合入)
                                    │
发版 tag ──► 版本大盘 Latest/Prior ──┴──► 矩阵健康 (安装行对齐)
```

细则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 共享 workflow

### `go-ci.yml` — Go 统一 CI 门禁

步骤：**go mod tidy -diff** → gofmt 检查 → golangci-lint → `go vet` → 测试（race）→ **嵌套子模块自动测试** → govulncheck 漏洞扫描 → 覆盖率门禁 → coverage 产物上传。

内置运维特性：同 ref 重复触发自动取消旧 run；lint/test 各有 `timeout-minutes`；覆盖率与 govulncheck 摘要写入 Step Summary；coverage 产物保留 14 天。

| input | 默认 | 说明 |
|---|---|---|
| `go-version` | `'1.26'` | setup-go 语义，如 `1.26` 解析为最新 1.26.x |
| `golangci-lint-version` | `v2.14.0` | golangci-lint 版本 |
| `coverage-threshold` | `30` | 总覆盖率下限（%）；`0` = 关闭门禁 |
| `test-script` | `go test -race -count=1 -coverprofile=coverage.out ./...` | 根模块测试命令；产出 `coverage.out` 即自动接门禁 |
| `test-nested` | `true` | 自动发现嵌套 `go.mod` 子模块并 `build + test -race`（logx/sink 这类不再漏测） |
| `additional-script` | `''` | 测试后的附加检查（多行 shell）；嵌套模块已由 `test-nested` 接管，只放特例 |
| `govulncheck` | `true` | 漏洞扫描（pin v1.9.0）——**非阻塞报告**。扫描器自身要求 go≥1.26：pin≥1.26 的仓正常出报告，1.24/1.25 仓显示黄色注解但不阻塞；转阻塞门禁需先统一工具链（go 版本升级暂缓中）|
| `tidy-check` | `true` | `go mod tidy -diff` 门禁，检测 go.mod/go.sum 漂移（指向已删 tag 的版本地板、旧 import 路径等）|

### 模块仓库接入方式

```yaml
# .github/workflows/ci.yml —— 全部内容
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

permissions:
  contents: read

jobs:
  ci:
    name: Lint & Test
    uses: oekit/.github/.github/workflows/go-ci.yml@main
    with:
      go-version: '1.26'
```

只写与共享默认不同的参数；与默认一致的字段省略。

## 引用 ref 策略

- `@main`（当前选择）：规则变更即时全量生效，符合"统一提交"诉求。
- 需要锁定时改为 `@v1` 这类 tag——本仓打 tag 后再批量改各仓引用。
- 安全边界：能推本仓 `main` 的人 = 能影响全 org CI，权限对齐 org admin。

## 运维

- **改规则**：直接改本仓 `.github/workflows/go-ci.yml`，merge 即生效，无需动模块仓。
- **升级 action 版本**：本仓 dependabot（`github-actions` ecosystem，weekly）提 PR，merge 即全 org 生效。
- **改治理大盘联动**：issue/PR 模板与 CONTRIBUTING 里的 Projects 链接/字段名；项目号（1/4）是稳定 ID，换板才需要改。
- **新增仓库**：拷上面的接入模板，改 `go-version` 即可；或跑
  `scripts/sync-callers.sh <repo-dir>` 从旧版完整 ci.yml 自动生成薄调用层
  （`--pr` 走 PR 模式）。

## 目录

```
.github/workflows/go-ci.yml          # 共享 reusable workflow（规则本体）
.github/workflows/workflow-lint.yml  # 本仓 Actions 语法自检（actionlint）
.github/dependabot.yml               # 本仓 action 依赖自动升级
.github/ISSUE_TEMPLATE/              # org 默认 issue 模板（bug / feature / docs / config）
.github/PULL_REQUEST_TEMPLATE.md     # org 默认 PR 模板（含入队 checklist）
profile/README.md                    # org 首页 profile（含治理大盘入口）
CONTRIBUTING.md                      # 全 org 贡献流程 + 大盘联动约定
SECURITY.md                          # 安全策略 / 私密披露
SUPPORT.md                           # 支持渠道索引
scripts/sync-callers.sh              # 模块仓 ci.yml → 薄调用层 批量迁移脚本
scripts/sync-dependabot.sh           # 模块仓 dependabot 批量同步
templates/dependabot-gomod.yml       # 模块仓 dependabot 模板
```

## 按仓特例速查

| 仓库 | 特例 |
|---|---|
| `bootstrap` | 嵌套 nacos/obs 由 `test-nested` 自动测试；根模块走默认覆盖率门禁 30 |
| `logx` | 嵌套 hlog/hertzlog/kitexlog/sink 由 `test-nested` 自动测试；dependabot 需为每个嵌套模块各一条 `directory` |
| `kitex-examples` | `test-script: go build ./...` + 关闭覆盖率门禁（示例仓无测试） |

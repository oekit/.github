# oekit/.github

oekit org 的 GitHub 统一规则仓：org profile + **全 org 共享的 CI 门禁（reusable workflows）**。

各模块仓库的 `.github/workflows/ci.yml` 只保留一个薄调用层，规则本体统一在本仓维护——
改一处（golangci-lint 版本、门禁步骤、覆盖率策略），所有引用 `@main` 的仓库立即生效。

## 共享 workflow

### `go-ci.yml` — Go 统一 CI 门禁

步骤：gofmt 检查 → golangci-lint → `go vet` → 测试（race）→ 覆盖率门禁 → coverage 产物上传。

| input | 默认 | 说明 |
|---|---|---|
| `go-version` | `'1.26'` | setup-go 语义，如 `1.26` 解析为最新 1.26.x |
| `golangci-lint-version` | `v2.14.0` | golangci-lint 版本 |
| `coverage-threshold` | `30` | 总覆盖率下限（%）；`0` = 关闭门禁 |
| `test-script` | `go test -race -count=1 -coverprofile=coverage.out ./...` | 测试命令；产出 `coverage.out` 即自动接门禁 |
| `additional-script` | `''` | 测试后的附加检查（多行 shell），如嵌套子模块 |

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
- **新增仓库**：拷上面的接入模板，改 `go-version` 即可；或跑
  `scripts/sync-callers.sh <repo-dir>` 从旧版完整 ci.yml 自动生成薄调用层
  （`--pr` 走 PR 模式）。

## 目录

```
.github/workflows/go-ci.yml   # 共享 reusable workflow（规则本体）
.github/dependabot.yml        # 本仓 action 依赖自动升级
scripts/sync-callers.sh       # 模块仓 ci.yml → 薄调用层 批量迁移脚本
profile/README.md             # org 首页 profile
```

## 按仓特例速查

| 仓库 | 特例 |
|---|---|
| `bootstrap` | `additional-script` 补测嵌套子模块（nacos/obs）；根模块走默认覆盖率门禁 30 |
| `kitex-examples` | `test-script: go build ./...` + 关闭覆盖率门禁（示例仓无测试） |

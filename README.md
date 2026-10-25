# oekit/.github

oekit org 的 GitHub 统一规则仓：org profile + **全 org 共享的 CI 门禁（reusable workflows）**。

各模块仓库的 `.github/workflows/ci.yml` 只保留一个薄调用层，规则本体统一在本仓维护——
改一处（golangci-lint 版本、门禁步骤、覆盖率策略、Python 检查范围），所有引用 `@main` 的仓库立即生效。

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

## 大盘自动化同步

GitHub Projects **不会**自动纳入 issue/PR，也不会跟进 tag。本仓提供**事件驱动 + 定时兜底**：

| 通道 | 触发 | 做什么 |
|---|---|---|
| 各仓 `issue-to-project.yml` | issue/PR 开关、tag 推送、main 提交 | **秒级**入板/刷新（`--link` / `--refresh-module`） |
| `issue-project-sync.yml` | 每小时 + 手动 | 全量对齐四块大盘（`--apply`） |
| `scripts/sync-project-hooks.sh` | 手动 | 给各模块仓装 hook + secret（默认逐仓开分支提 PR；`--push` 直写 main 仅适用于无分支保护的仓） |

同步规则：

1. **Issue 大盘** — org issue 缺则入板；`state=open→Todo / closed→Done`（人工标的 `In Progress` 不被冲掉）；`Kind` 按标题打分推导（Bug/Enhancement/Refactor/Docs/Chore，**每次覆盖**，人工微调请用 Labels）；`Module`=所属仓
   - **入板范围 = 全 org**：整点全量同步用 `gh search issues --owner oekit` 拉全量 issue，
     所以「某仓的 issue 进不进大盘」与下发无关；各仓的 `issue-to-project.yml`
     只决定**延迟**（秒级 vs ≤1 小时），下发范围 = 矩阵全部根模块 + 4 个 infra 仓；
   - **读路径以 issue 侧为准**：断言「某 issue 在不在看板上」读 `issue.projectItems`
     （`scripts/board_query.py`）——`projectV2.items` 分页**滞后**于 issue 侧，
     用分页判会把「已入板」误判成「不在板上」（假阴性，oekit/.github#24）。
   - **行序 = createdAt 倒序（最新在最前）**：Projects 的行序是**行的创建序**，
     公开 API 写不了视图排序、新行只能追加到末尾 ⇒ 想让新记录排在最前，
     只能把行按规范序重建（全删 + 按序重加）。脚本会**检测漂移并自动重建**，
     重建中断也不会丢数据（缺行由下一轮补回、漂移由下一轮再重建）。
2. **版本大盘** — 主模块与嵌套模块独立成行（Title=`logx` / `logx/hlog`）；`Latest`/`Prior`=近两版 root tag；`Parent` 标嵌套归属；缺行自动补
3. **矩阵健康** — `Install`/`Tag` 并排对照 + `Note` 诊断；`Hygiene`（全绿 / 有漂移 / 待审计）+ `Drift` 0/1
4. **PR 审查队列** — org PR 缺则入队；`open→待审 / merged→已合入 / closed→已关闭`；`Scope` 按 conventional commit 类型（build/deps → deps）
   - **Review 分权**：PR 落到终态（merged/closed）由脚本说了算；在途时
     `审阅中 / 需修改 / 可合入` 是**人工流转态，脚本不冲掉**（只有还没人动过才落 `待审`）。

### 前置：`OEKIT_PROJECT_TOKEN`

Actions 的 `GITHUB_TOKEN` 没有 `project` scope，需在 org secrets 配 PAT / GitHub App token：

```bash
# 生成（或用已有带 project 权限的 PAT）
gh auth refresh -s project
# Settings → Secrets and variables → Actions → New repository secret
# Name: OEKIT_PROJECT_TOKEN
```

权限要求：`project` + `read:org`。

### 各板视图

| 大盘 | 视图 |
|---|---|
| Issue 大盘 | 总览 · 开启(`status:Todo`) · 关闭(`status:Done`) · 看板 · 按模块 · 按类型 |
| 版本大盘 | 总览 · 按层级(Board) · 嵌套模块(`Parent` 非空) · 近期变更 |
| 矩阵健康 | 总览 · 有漂移 · 全绿 · 待审计 · 看板 |
| PR 审查队列 | 总览 · 待审 · 审阅中 · 可合入 · 已合入 · 已关闭 · 按 Scope · 看板 |

规格明细（**分组 / 过滤器 / 可见列**，四块板统一口径）：

| 大盘 | 视图 | 布局 | 分组 | 过滤器 | 可见列 |
|---|---|---|---|---|---|
| Issue | 总览 | 表格 | — | — | Title · Status · Kind · Module · Created |
| Issue | 开启 | 表格 | — | `status:Todo` | Title · Kind · Module · Created |
| Issue | 关闭 | 表格 | — | `status:Done` | Title · Kind · Module · Closed |
| Issue | 看板 | 看板 | **Kind** | — | Title · Status · Kind · Module |
| Issue | 按模块 | 表格 | **Module** | — | Title · Kind · Status · Created |
| Issue | 按类型 | 表格 | **Kind** | — | Title · Module · Status · Created |
| PR | 总览 | 表格 | — | — | Title · Review · Scope · Updated |
| PR | 待审 | 表格 | — | `review:"待审"` | Title · Scope · Updated |
| PR | 审阅中 | 表格 | — | `review:"审阅中"` | Title · Scope · Updated |
| PR | 可合入 | 表格 | — | `review:"可合入"` | Title · Scope · Updated |
| PR | 已合入 | 表格 | — | `review:"已合入"` | Title · Scope · Closed |
| PR | 已关闭 | 表格 | — | `review:"已关闭"` | Title · Scope · Closed |
| PR | 按 Scope | 表格 | **Scope** | — | Title · Review · Updated |
| PR | 看板 | 看板 | **Review** | — | Title · Review · Scope |

三条口径（改动时务必一起看）：

- **过滤器与可见列 = 脚本钉死**：`scripts/sync-projects.py` 里的 `PINNED_VIEWS`
  是唯一事实源，UI 上被人手改乱了会被下一轮同步改回来；`--selftest` 会校验这张表。
- **分组 = 只能手点**：公开 GraphQL 的 `UpdateProjectV2ViewInput` 里**没有 groupBy 字段**
  ⇒ 表格视图的 `Group by` / 看板视图的 `Column by` 只能进 UI 落盘
  （面板改草稿 → 视图 kebab 菜单「Save changes to new view」→ 改名 → 删旧视图）。
  期望值记在脚本的 `PINNED_GROUPS` 里，脚本只能声明、无法校验。
- **排序 = 存不住**：视图的 `Sort by` 在新版 UI 里只落 URL 草稿，面板 Save view 也留不下
  ⇒ 行序只能靠**建行顺序**保证（见上面同步规则里的「行序」一条）。

## 共享 workflow

### `go-ci.yml` — Go 统一 CI 门禁

步骤：**go mod tidy -diff** → gofmt 检查 → golangci-lint → `go vet` → 测试（race）→ **嵌套子模块自动测试** → govulncheck 漏洞扫描 → 覆盖率门禁 → coverage 产物上传。

内置运维特性：同 ref 重复触发自动取消旧 run；lint/test 各有 `timeout-minutes`；覆盖率与 govulncheck 摘要写入 Step Summary；coverage 产物保留 14 天。

| input | 默认 | 说明 |
|---|---|---|
| `go-version` | `'1.24'` | setup-go 语义，如 `1.24` 解析为最新 1.24.x（全 org 统一地板 1.24）|
| `golangci-lint-version` | `v2.14.0` | golangci-lint 版本 |
| `coverage-threshold` | `30` | 总覆盖率下限（%）；`0` = 关闭门禁 |
| `test-script` | `go test -race -count=1 -coverprofile=coverage.out ./...` | 根模块测试命令；产出 `coverage.out` 即自动接门禁 |
| `test-nested` | `true` | 自动发现嵌套 `go.mod` 子模块并 `build + test -race`（logx/sink 这类不再漏测） |
| `additional-script` | `''` | 测试后的附加检查（多行 shell）；嵌套模块已由 `test-nested` 接管，只放特例 |
| `govulncheck` | `true` | 漏洞扫描（pin v1.9.0）——**非阻塞报告**。扫描器自身要求 go≥1.26：pin≥1.26 的仓正常出报告，1.24/1.25 仓显示黄色注解但不阻塞；转阻塞门禁需先统一工具链（go 版本升级暂缓中）|
| `tidy-check` | `true` | `go mod tidy -diff` 门禁（含嵌套子模块），检测 go.mod/go.sum 漂移（指向已删 tag 的版本地板、旧 import 路径等）|

### `python-ci.yml` — Python 统一 CI 门禁

与 `go-ci.yml` 对称的 Python 侧门禁，供规则仓/工具仓（`matrix-tools` 等）引用。

步骤：**Python syntax**（`py_compile` 全仓 `.py`）→ **Shell syntax**（`bash -n` 全仓 `.sh`）→ **YAML**（workflows + templates + dependabot 全量解析）。

| input | 默认 | 说明 |
|---|---|---|
| `python-version` | `'3.12'` | setup-python 语义，如 3.12 解析为最新 3.12.x |

两条设计约束，改动时务必保留：

- **扫全仓库，不只扫 `scripts/`**：基础模块比脚本更容易被漏——脚本在 `scripts/` 目录里顺理成章，库却常常躺在仓库根。实测 `matrix-tools` 的 `matrix.py`（所有脚本的事实源）就因此被漏检过，语法坏掉时门禁全绿。
- **不执行脚本本体**：矩阵仓有一批脚本在缺少 `--help` 时会直接执行真实动作（发版、开 PR、写文件），盲跑冒烟会真的触发副作用。只有纯静态检查是安全且足够的。

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
      go-version: '1.24'
```

只写与共享默认不同的参数；与默认一致的字段省略。

### `issue-auto-assign.yml` — 新 issue 自动分配

新开 issue 自动分配给维护者，避免掉进无人区。本仓 `issues: opened` 即触发；模块仓可薄调用复用。

| input | 默认 | 说明 |
|---|---|---|
| `assignees` | `'zy84338719'` | 受理人 GitHub login，逗号分隔可多人 |
| `skip-if-assigned` | `true` | issue 已有 assignee 时跳过 |

规则：bot 开的 issue（dependabot 等）跳过；已分配过的 login 不重复添加。

```yaml
# 模块仓薄调用（可选——不接就用 org 默认模板或手写）
jobs:
  assign:
    uses: oekit/.github/.github/workflows/issue-auto-assign.yml@main
    with:
      assignees: 'zy84338719'
```

### `repo-ci.yml` — 规则仓自身 CI

本仓自身的薄调用层：`lint` 引用同仓 `python-ci.yml@main`（与各模块仓共用同一份规则本体），`smoke` 保留本仓特有的一条——`sync-projects.py` 能跑起来。与 `workflow-lint.yml`（actionlint）互补。

### 组织 workflow 模板（`workflow-templates/`）

新仓 **Actions → New workflow** 时可直接选用：

| 模板 | 用途 |
|---|---|
| `issue-auto-assign.yml` | Auto-assign new issues — 自动把新 issue 分给维护者 |
| `go-ci.yml` | Run a continuous integration test — 薄调用共享 Go CI 门禁 |

`$default-branch` 占位符在创建时会替换为该仓默认分支。

## 引用 ref 策略

- `@main`（当前选择）：规则变更即时全量生效，符合"统一提交"诉求。
- 需要锁定时改为 `@v1` 这类 tag——本仓打 tag 后再批量改各仓引用。
- 安全边界：能推本仓 `main` 的人 = 能影响全 org CI，权限对齐 org admin。

## 运维

- **改规则**：直接改本仓 `.github/workflows/go-ci.yml`（Go 门禁）或 `.github/workflows/python-ci.yml`（Python 门禁），merge 即生效，无需动模块仓。
- **升级 action 版本**：本仓 dependabot（`github-actions` ecosystem，weekly）提 PR，merge 即全 org 生效。
- **改治理大盘联动**：issue/PR 模板与 CONTRIBUTING 里的 Projects 链接/字段名；项目号（1/4）是稳定 ID，换板才需要改。
- **手动补同步**：`python3 scripts/sync-projects.py --apply`（或 Actions → 大盘同步 → Run workflow）。
- **手动重排行序**：`python3 scripts/sync-projects.py --apply --reorder --only <1|4>`。
  行序漂移时小板（≤500 行）会自动重建；大板（#4 PR 队列 1100+ 行，一次重建 ≈4400 次写）
  只告警不自动烧配额，需要时用 `--reorder` 手工触发。
- **同步未生效**：检查 `OEKIT_PROJECT_TOKEN` 是否过期 / 是否有 `project` scope。
- **新增仓库**：拷上面的接入模板，改 `go-version` 即可；或跑
  `scripts/sync-callers.sh <repo-dir>` 从旧版完整 ci.yml 自动生成薄调用层
  （`--pr` 走 PR 模式）。

## 目录

```
.github/workflows/go-ci.yml              # 共享 reusable workflow（Go 门禁规则本体）
.github/workflows/python-ci.yml          # 共享 reusable workflow（Python 门禁规则本体）
.github/workflows/issue-auto-assign.yml  # 新 issue 自动分配（可 reusable）
.github/workflows/repo-ci.yml            # 规则仓自身 CI（薄调用层 → python-ci.yml）
.github/workflows/workflow-lint.yml      # 本仓 Actions 语法自检（actionlint）
.github/workflows/issue-project-sync.yml # 大盘定时同步 workflow
.github/dependabot.yml                   # 本仓 action 依赖自动升级
.github/ISSUE_TEMPLATE/                  # org 默认 issue 模板（bug / feature / docs / config）
.github/PULL_REQUEST_TEMPLATE.md         # org 默认 PR 模板（含入队 checklist）
workflow-templates/issue-auto-assign.yml # 组织模板：新 issue 自动分配
workflow-templates/go-ci.yml             # 组织模板：Go CI 薄调用层
profile/README.md                        # org 首页 profile（含治理大盘入口）
CONTRIBUTING.md                          # 全 org 贡献流程 + 大盘联动约定
SECURITY.md                              # 安全策略 / 私密披露
SUPPORT.md                               # 支持渠道索引
scripts/sync-projects.py                 # 四块治理大盘统一同步（issue/PR/tag → Projects）
scripts/sync-callers.sh                  # 模块仓 ci.yml → 薄调用层 批量迁移脚本
scripts/sync-dependabot.sh               # 模块仓 dependabot 批量同步
templates/dependabot-gomod.yml           # 模块仓 dependabot 模板
```

## 按仓特例速查

| 仓库 | 特例 |
|---|---|
| `bootstrap` | 嵌套 nacos/obs 由 `test-nested` 自动测试；根模块走默认覆盖率门禁 30 |
| `logx` | 嵌套 hlog/hertzlog/kitexlog/sink 由 `test-nested` 自动测试；dependabot 需为每个嵌套模块各一条 `directory` |
| `kitex-examples` | `test-script: go build ./...` + 关闭覆盖率门禁（示例仓无测试） |

## 发布纪律（.github#5）

- **tag 不可变**：同名 tag 严禁重推（proxy/sumdb 缓存会让全体已缓存消费方 `checksum mismatch` 集体中毒，错误信息完全指不到根因）；各矩阵仓已启用 `protect-release-tags` ruleset（v* tag 禁 update/删除/非快进，无人可 bypass）。
- **版本号只能前进**：结构重置（历史清空）必须换新版本号段（哪怕大跳版），不得复用旧版本号——hertz v0.5.1 重置事故的直接教训。
- **tag 推送即门禁**：各仓 ci.yml 已加 `tags: ['v*']` 触发，tag 推送时完整跑 tidy/build/test/CHANGELOG 一致性检查（发版后亮红 = 立即处置，而不是等消费方报 checksum mismatch）。
- **嵌套模块同 train 发版**：用 matrix-tools `scripts/release_train.py`（ls-remote 查重/tidy/build/CHANGELOG 四道前置，全部通过才打 tag）。


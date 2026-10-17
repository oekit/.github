# 贡献指南 — oekit

感谢参与 oekit（Open Easy Kit）模块矩阵。本文件是 **全 org 共享** 的贡献约定；单仓更细的工程红线见各仓 `AGENTS.md`。

## 先看这 4 块大盘

贡献不是单仓事务——开的 issue、提的 PR、发的版都会汇入 org 级 [GitHub Projects](https://github.com/orgs/oekit/projects)：

| 大盘 | 你什么时候会碰到它 |
|---|---|
| [Issue 大盘](https://github.com/orgs/oekit/projects/1) | 提 issue 后自动出现在这里（标题带 `[模块]` 前缀更好归类） |
| [版本大盘](https://github.com/orgs/oekit/projects/2) | 发版后对齐 Latest/Prior；装新依赖前查目标模块版本 |
| [矩阵健康](https://github.com/orgs/oekit/projects/3) | 安装行/文档/门面壳漂移会亮红；PR 合入前顺手看一眼自己模块 |
| [PR 审查队列](https://github.com/orgs/oekit/projects/4) | 提完 PR 请入队，避免跨仓 PR 被漏看 |

## 流程一览

```text
  发现问题 / 有想法
        │
        ▼
  开 Issue（标题: [模块] 简述）──────────► Issue 大盘 (Todo)
        │
        ▼
  认领 / 开分支 / 提 PR ──────────────────► PR 审查队列 (待审)
        │
        ▼
  CI 门禁 (go-ci.yml) + 人工 review
        │
        ├── 需修改 ──► 更新 PR ──► 回到审查
        │
        ▼
  合入 main
        │
        ├── issue 关联关闭 ──► Issue 大盘 (Done)
        ├── 发版打 tag ──────► 版本大盘刷新
        └── 安装行/文档 ─────► matrix-tools 对齐，矩阵健康回绿
```

## 开 Issue

1. **选对仓库**：问题落在哪个模块就开在哪个仓；跨模块/矩阵拓扑问题开在 [oekit/oekit](https://github.com/oekit/oekit)。
2. **标题带模块前缀**：`[logx] …`、`[retry] …`、`[matrix] …`——和矩阵描述对齐，大盘分组更准。
3. **说清现状与建议**：我们偏好「现状（版本号 + 复现/证据）→ 影响 → 建议 → 验收」四段式；参考 [pkg#3](https://github.com/oekit/pkg/issues/3) 这类已有 issue 的写法。
4. **用模板**：仓库提供了 Bug / 特性 / 文档三类 issue 模板，按需选。

## 提 PR

1. **先搜后提**：搜一下 Issue 大盘和目标仓的 open issue，已有就评论认领，别开重复的。
2. **进 PR 审查队列**：PR 开出后执行
   ```bash
   gh project item-add 4 --owner oekit --url https://github.com/oekit/<repo>/pull/<n>
   ```
   在板上把 **Review** 拨到「待审」，**Scope** 选 feat/fix/docs/refactor/chore/ci。
3. **一个 PR 一件事**：feat 与 refactor 不要搅在一起；0.x 阶段允许 breaking，但必须写进 CHANGELOG。
4. **过 CI**：共享门禁 `oekit/.github/.github/workflows/go-ci.yml`（gofmt / golangci-lint / vet / race / govulncheck / 覆盖率）。本地至少跑通 `go test -race ./...`。
5. **对齐既有风格**：Option 命名、错误哨兵、`AGENTS.md` 红线以目标仓为准；新增公共 API 请补 godoc 与测试。

## 发版与版本大盘

- 版本方案 SemVer；0.x 阶段 minor 可 breaking，patch 只修不破。
- 打完 tag 后更新：
  - 该仓 README 安装行（或跑 `matrix-tools` 的 `refresh_versions.py --push`）
  - [版本大盘](https://github.com/orgs/oekit/projects/2) 对应行的 **Latest / Prior**
  - CHANGELOG
- 嵌套模块（如 `logx/hlog`、`bootstrap/obs`）各自独立 tag，在版本大盘的 **Nested** 列注明。

## 矩阵卫生

以下会被 [矩阵健康](https://github.com/orgs/oekit/projects/3) 和 `matrix-tools` 周巡检盯住：

- README 矩阵总览表与 `matrix.py` 描述一致
- 安装行版本 = 最新 tag
- `AGENTS.md` / `CHANGELOG.md` 在场且不过期
- 门面壳（可选能力进子包/嵌套模块）与声明一致
- 不出现已废弃 import 路径（`yi-nology` / `openekit` / `ekit-`）

发现漂移 → 自动开 `matrix-drift` issue → 汇入 Issue 大盘。

## 行为准则

理性、就事论事、对代码严苛对人温和。issue/PR 评论只谈技术事实；有分歧时用可运行的对比（基准/语义测试/依赖图）说话。

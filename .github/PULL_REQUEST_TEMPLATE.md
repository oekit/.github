## 变更说明

<!-- 一句话说清这 PR 解决什么问题。带关联 issue：Fixes oekit/xxx#N 或 oekit/xxx#N -->

## 类型

- [ ] feat 新能力
- [ ] fix 修 bug
- [ ] docs 文档
- [ ] refactor 重构（无语义变化）
- [ ] chore 杂项 / 依赖
- [ ] ci / 工具链

## 检查清单

- [ ] 已在 [PR 审查队列](https://github.com/orgs/oekit/projects/4) 入队（`gh project item-add 4 --owner oekit --url <pr-url>`），Review=待审，Scope 已选
- [ ] 相关 issue 以 `Fixes #N` 关联（合入后自动进 Issue 大盘 Done）
- [ ] `go test -race ./...` 本地通过
- [ ] 新增公共 API 有 godoc；行为变化写进 CHANGELOG
- [ ] 不引入与模块定位不符的新依赖（尤其根包保持薄）
- [ ] 若改了版本/安装行，知悉 [矩阵健康](https://github.com/orgs/oekit/projects/3) 与 [版本大盘](https://github.com/orgs/oekit/projects/2) 需同步

## 兼容性

<!-- 0.x 允许 breaking，但必须写清影响面与迁移方式；非 breaking 填「向后兼容」 -->

- [ ] 向后兼容
- [ ] 有 breaking（下述）

## 补充

<!-- 截图、基准数据、依赖图变化等 -->

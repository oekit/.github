## 这个 PR 解决什么

<!-- 一两句。关联 issue：Closes #NN（单仓）——跨仓变更在正文列全。 -->

## 变更类型

- [ ] fix（行为变更，需在 CHANGELOG 写清"改了什么、为什么"）
- [ ] feat（新增 API，注意向后兼容）
- [ ] docs / chore / refactor / perf

## 怎么验的

<!-- 贴命令和结果，不要只写"已测试"。改动跨依赖版本时，
     必须用 GOWORK=off 验一遍——本机 go.work 会把依赖指向本地仓，
     掩盖 go.mod 的版本地板问题（data#5 就是这么暴露的）。 -->

```
GOWORK=off go build ./...
GOWORK=off go vet ./...
GOWORK=off go test ./...
golangci-lint run ./...
```

## 影响面

- 消费方可见的行为变更：
- 需要同步的位置（README 安装行 / AGENTS 当前版本行 / matrix.py DESC）：
- 是否需要打 tag：改动合入 main 之后，再打 annotated tag

## oekit 约定自查

- [ ] 业务逻辑只进 `internal/service/`（或 wechat 的 `internal/logic/`）
- [ ] 导出符号有 godoc；包文档在 `doc.go`；函数选项模式叫 `XxxOption`
- [ ] 改动跨依赖版本时同步了 `go.mod` 版本地板
- [ ] `matrix-tools/scripts/audit.py` 与 `shell_sync.py` 通过
- [ ] CHANGELOG 已加节；README 安装行与 AGENTS 当前版本行对齐最新 tag
- [ ] tag 打在**已合入 main** 的 commit 上（tag 不走 PR）

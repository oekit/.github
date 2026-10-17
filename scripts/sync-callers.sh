#!/usr/bin/env bash
# 把 oekit org 各模块仓库的 .github/workflows/ci.yml 重写为
# 共享 reusable workflow（oekit/.github/.github/workflows/go-ci.yml）的薄调用层。
#
# 用法:
#   ./sync-callers.sh <repo-dir>...        # 直接改写并推送 main
#   ./sync-callers.sh --pr <repo-dir>...   # 改写后开分支 ci/shared-workflows 提 PR
#
# 每仓的 go-version 从现存 ci.yml 读取（保持各仓节奏）；覆盖率/测试命令特例
# 写在下方 OVERRIDES。嵌套子模块由共享 workflow 的 test-nested 自动发现测试，
# 无需写 additional-script。规则本体变更请改 oekit/.github 仓，不需要重跑本脚本。
set -euo pipefail

REF=main # 引用共享 workflow 的 ref；要锁定稳定性可改为 tag（如 v1）
BRANCH=ci/shared-workflows
MODE=push
if [ "${1:-}" = "--pr" ]; then MODE=pr; shift; fi
[ $# -ge 1 ] || { echo "usage: $0 [--pr] <repo-dir>..." >&2; exit 1; }

# 按仓覆盖（macOS bash 3.2 兼容：不用 declare -A）。
# 未命中的仓用共享默认：覆盖率门禁 30、标准测试命令、test-nested 自动开。
get_override() { # $1=repo basename → OV_THR/OV_TSCRIPT/OV_ASCRIPT/OV_NESTED（空串=共享默认）
  OV_THR=''; OV_TSCRIPT=''; OV_ASCRIPT=''; OV_NESTED=''
  case "$1" in
    # 示例仓只构建不测试，且不要嵌套自动测试噪音
    ekit-kitex-examples)
      OV_THR=0
      OV_TSCRIPT='go build ./...'
      OV_NESTED=false
      ;;
  esac
}

render_caller() { # $1=go_version $2=threshold $3=test_script $4=additional_script $5=test_nested
  local gover="$1" thr="$2" tscript="$3" ascript="$4" nested="$5"
  cat <<EOF
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  ci:
    name: Lint & Test
    uses: oekit/.github/.github/workflows/go-ci.yml@${REF}
    with:
EOF
  echo "      go-version: '${gover}'"
  [ -n "$thr" ] && [ "$thr" != 30 ] && echo "      coverage-threshold: $thr"
  [ -n "$nested" ] && [ "$nested" = false ] && echo "      test-nested: false"
  if [ -n "$tscript" ]; then
    echo "      test-script: |-"
    printf '%s\n' "$tscript" | sed 's/^/        /'
  fi
  if [ -n "$ascript" ]; then
    echo "      additional-script: |-"
    printf '%s\n' "$ascript" | sed 's/^/        /'
  fi
}

sync_repo() {
  local dir="$1" base
  base=$(basename "$dir")
  local ci="$dir/.github/workflows/ci.yml"
  [ -f "$ci" ] || { echo "skip $base: 无 ci.yml"; return 0; }

  git -C "$dir" diff --quiet -- .github/workflows/ci.yml ||
    { echo "skip $base: ci.yml 有未提交改动"; return 0; }

  local gover thr tscript ascript nested
  # 行形如 `go-version: '1.24'`；按引号切分取值（BSD sed 不支持 \?，勿用 sed BRE）
  gover=$(grep -m1 "go-version:" "$ci" | awk -F"'" '{print $2}')
  get_override "$base"
  if [ -n "$OV_THR" ] || [ -n "$OV_TSCRIPT" ] || [ -n "$OV_ASCRIPT" ] || [ -n "$OV_NESTED" ]; then
    thr="${OV_THR:-30}"; tscript="$OV_TSCRIPT"; ascript="$OV_ASCRIPT"; nested="$OV_NESTED"
  elif grep -q 'coverprofile' "$ci"; then
    thr=$(grep -o 'THRESHOLD=[0-9]*' "$ci" | head -1 | cut -d= -f2)
    thr=${thr:-30}; tscript=''; ascript=''; nested=''
  elif grep -q 'go-ci.yml' "$ci"; then
    # 已是薄调用层：保留 go-version，清掉历史 additional-script（test-nested 接管嵌套）
    thr=''; tscript=''; ascript=''; nested=''
  else
    echo "skip $base: 无覆盖率步骤且无 override，需人工确认"; return 0
  fi

  # PR 模式先开分支再改写提交，保证 commit 不落在本地 main 上
  if [ "$MODE" = pr ]; then
    git -C "$dir" checkout -q -B "$BRANCH"
  fi

  render_caller "$gover" "$thr" "$tscript" "$ascript" "$nested" > "$ci"

  git -C "$dir" add .github/workflows/ci.yml
  git -C "$dir" commit -q -m "ci: 收敛到共享 reusable workflow oekit/.github/go-ci" \
    -m "规则统一在 oekit/.github 维护；本仓只保留按仓参数（go ${gover}）。嵌套模块交由 test-nested 自动测试。"
  if [ "$MODE" = pr ]; then
    git -C "$dir" push -q -u origin "$BRANCH"
    (cd "$dir" && gh pr create --fill >/dev/null)
    git -C "$dir" checkout -q main
  else
    git -C "$dir" push -q origin HEAD:main
  fi
  echo "done $base (go ${gover}, threshold ${thr:-默认30})"
}

for d in "$@"; do sync_repo "$d"; done

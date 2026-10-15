#!/usr/bin/env bash
# 把 Go 模块仓的 .github/dependabot.yml 统一为全量分组模板
# （templates/dependabot-gomod.yml：gomod weekly + 单组全量）。
#
# 用法:
#   ./sync-dependabot.sh <repo-dir>...        # 直接改写并推送 main
#   ./sync-dependabot.sh --pr <repo-dir>...   # 开分支提 PR
#
# 模板变更在 oekit/.github 改，重跑本脚本即可再下发。
set -euo pipefail

BRANCH=ci/dependabot-grouping
MODE=push
if [ "${1:-}" = "--pr" ]; then MODE=pr; shift; fi
[ $# -ge 1 ] || { echo "usage: $0 [--pr] <repo-dir>..." >&2; exit 1; }
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TEMPLATE="$SCRIPT_DIR/../templates/dependabot-gomod.yml"

sync_repo() {
  local dir="$1" base dep
  base=$(basename "$dir")
  dep="$dir/.github/dependabot.yml"
  if [ -f "$dep" ] && diff -q "$TEMPLATE" "$dep" >/dev/null 2>&1; then
    echo "skip $base: 已是模板内容"; return 0
  fi
  git -C "$dir" diff --quiet -- .github/dependabot.yml 2>/dev/null ||
    { echo "skip $base: dependabot.yml 有未提交改动"; return 0; }
  mkdir -p "$dir/.github"
  cp "$TEMPLATE" "$dep"
  git -C "$dir" add .github/dependabot.yml
  git -C "$dir" commit -q -m "ci: dependabot 全量分组更新（gomod weekly 单 PR，根治逐包 PR 洪峰）"
  if [ "$MODE" = pr ]; then
    git -C "$dir" checkout -q -b "$BRANCH"
    git -C "$dir" push -q -u origin "$BRANCH"
    (cd "$dir" && gh pr create --fill >/dev/null)
    git -C "$dir" checkout -q main
  else
    git -C "$dir" push -q origin HEAD:main
  fi
  echo "done $base"
}

for d in "$@"; do sync_repo "$d"; done

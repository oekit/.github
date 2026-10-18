#!/usr/bin/env bash
# 给各模块仓安装「即时入板」workflow + OEKIT_PROJECT_TOKEN secret。
# 用 GitHub API 写文件，不依赖本地是否 clone。
#
# 用法：
#   bash scripts/sync-project-hooks.sh          # 安装
#   bash scripts/sync-project-hooks.sh --dry    # 只列出目标仓
set -euo pipefail

ORG=${ORG:-oekit}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/workflow-templates/issue-to-project.yml"
DRY=${1:-}
TOKEN=$(gh auth token)

MODULES=(
  pkg concurrency logx core metrics tracing registry
  data gormx redisx httpx network timertask middleware
  nns nns-endpoint bootstrap hertz kitex workflow kitex-examples
  matrix-tools oekit oekit.github.io .github
)

if [[ ! -f "$SRC" ]]; then
  echo "missing $SRC" >&2
  exit 1
fi

CONTENT=$(base64 < "$SRC" | tr -d '\n')
MSG="ci: issue/PR 开闭即时入板 org 大盘（复用 oekit/.github sync-projects）"
PATH_IN_REPO=".github/workflows/issue-to-project.yml"

for mod in "${MODULES[@]}"; do
  echo "=== $mod ==="
  if [[ "$DRY" == "--dry" ]]; then
    echo "  dry: $ORG/$mod"
    continue
  fi

  # secret
  if ! gh secret list --repo "$ORG/$mod" 2>/dev/null | grep -q OEKIT_PROJECT_TOKEN; then
    echo "$TOKEN" | gh secret set OEKIT_PROJECT_TOKEN --repo "$ORG/$mod"
    echo "  + secret OEKIT_PROJECT_TOKEN"
  else
    echo "  = secret already set"
  fi

  # workflow 文件（contents API，幂等）
  SHA=$(gh api "repos/$ORG/$mod/contents/$PATH_IN_REPO" --jq .sha 2>/dev/null || true)
  PAYLOAD=$(jq -n \
    --arg message "$MSG" \
    --arg content "$CONTENT" \
    --arg sha "$SHA" \
    '{message:$message, content:$content, branch:"main"}
     + (if $sha != "" and $sha != "null" then {sha:$sha} else {} end)')
  if gh api -X PUT "repos/$ORG/$mod/contents/$PATH_IN_REPO" --input - <<<"$PAYLOAD" >/dev/null; then
    echo "  ✓ workflow written"
  else
    echo "  ! workflow write failed" >&2
  fi
done

echo "DONE"

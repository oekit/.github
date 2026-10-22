#!/usr/bin/env bash
# 给各模块仓安装「即时入板」workflow + OEKIT_PROJECT_TOKEN secret。
# 用 GitHub API 写文件，不依赖本地是否 clone。
#
# 用法：
#   bash scripts/sync-project-hooks.sh                  # 默认：逐仓开分支提 PR
#   bash scripts/sync-project-hooks.sh --push           # 直写 main（仅无分支保护的仓可用）
#   bash scripts/sync-project-hooks.sh --dry            # 只列出目标仓
#   bash scripts/sync-project-hooks.sh --pr pkg core    # 只处理指定仓（默认全部）
#
# 为什么默认走 PR：org 全仓 main 都开了分支保护且 enforce_admins=true，
# 直推/直写一律被拒（422 "Changes must be made through a pull request."），
# 管理员也不例外 —— 所以「直写 main」不是可选项，是必然失败的路径。
# 这也是兄弟脚本 sync-callers.sh / sync-dependabot.sh 都有 --pr 模式的原因。
#
# 幂等：目标文件与模板逐字节一致时直接跳过，不建分支、不开 PR、不留噪音提交。
set -euo pipefail

ORG=${ORG:-oekit}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/workflow-templates/issue-to-project.yml"
BRANCH="ci/sync-project-hooks"
MODE=pr
TOKEN=$(gh auth token)

MODULES=(
  pkg concurrency logx core metrics tracing registry
  data gormx redisx httpx network timertask middleware
  nns nns-endpoint bootstrap hertz kitex workflow kitex-examples
  matrix-tools oekit oekit.github.io .github
)

# 参数解析：--pr / --push / --dry 决定模式，其余参数是仓库白名单。
ONLY=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)   MODE=pr ;;
    --push) MODE=push ;;
    --dry)  MODE=dry ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    -*)
      echo "unknown option: $1" >&2
      exit 2
      ;;
    *)  ONLY+=("$1") ;;
  esac
  shift
done

if [[ ! -f "$SRC" ]]; then
  echo "missing $SRC" >&2
  exit 1
fi

CONTENT=$(base64 < "$SRC" | tr -d '\n')
MSG="ci: issue/PR 开闭即时入板 org 大盘（复用 oekit/.github sync-projects）"
PATH_IN_REPO=".github/workflows/issue-to-project.yml"

TARGETS=("${MODULES[@]}")
if [[ ${#ONLY[@]} -gt 0 ]]; then
  TARGETS=("${ONLY[@]}")
fi

failed=0
skipped=0
written=0

for mod in "${TARGETS[@]}"; do
  echo "=== $mod ==="
  if [[ "$MODE" == "dry" ]]; then
    echo "  dry: $ORG/$mod"
    continue
  fi

  # secret（走 secrets API，不受分支保护影响）
  if ! gh secret list --repo "$ORG/$mod" 2>/dev/null | grep -q OEKIT_PROJECT_TOKEN; then
    echo "$TOKEN" | gh secret set OEKIT_PROJECT_TOKEN --repo "$ORG/$mod"
    echo "  + secret OEKIT_PROJECT_TOKEN"
  else
    echo "  = secret already set"
  fi

  # 读 main 上的现状：blob sha + 内容（内容用作幂等判据）
  cur_sha=""
  cur_content=""
  if cur_sha=$(gh api "repos/$ORG/$mod/contents/$PATH_IN_REPO" --jq .sha 2>/dev/null); then
    cur_content=$(gh api "repos/$ORG/$mod/contents/$PATH_IN_REPO" --jq .content 2>/dev/null | tr -d '\n' || true)
  fi

  if [[ -n "$cur_sha" && "$cur_content" == "$CONTENT" ]]; then
    echo "  = workflow already up to date"
    skipped=$((skipped + 1))
    continue
  fi

  if [[ "$MODE" == "push" ]]; then
    PAYLOAD=$(jq -n \
      --arg message "$MSG" \
      --arg content "$CONTENT" \
      --arg sha "$cur_sha" \
      '{message:$message, content:$content, branch:"main"}
       + (if $sha != "" and $sha != "null" then {sha:$sha} else {} end)')
    if err=$(gh api -X PUT "repos/$ORG/$mod/contents/$PATH_IN_REPO" --input - <<<"$PAYLOAD" 2>&1 >/dev/null); then
      echo "  ✓ workflow written to main"
      written=$((written + 1))
    else
      echo "  ! workflow write failed: $err" >&2
      failed=$((failed + 1))
    fi
    continue
  fi

  # PR 模式
  #   ⚠️ 不要 force-reset 分支：分支被推回 main 会让 GitHub 认为 head 提交
  #   已被抹掉，从而**自动关闭**已开的 PR ⇒ 重跑一次 churn 一个 PR。
  #   分支只在不存在时从 main 建；已存在就原地更新文件，PR 的 diff 自动跟着更新。
  if ! gh api "repos/$ORG/$mod/git/ref/heads/$BRANCH" --jq .object.sha >/dev/null 2>&1; then
    main_sha=$(gh api "repos/$ORG/$mod/git/ref/heads/main" --jq .object.sha)
    gh api -X POST "repos/$ORG/$mod/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$main_sha" >/dev/null
  fi

  # 写分支要用**分支上**该文件的 blob sha（分支已存在时它与 main 上的可能不同）
  branch_sha=""
  branch_content=""
  if branch_sha=$(gh api "repos/$ORG/$mod/contents/$PATH_IN_REPO?ref=$BRANCH" --jq .sha 2>/dev/null); then
    branch_content=$(gh api "repos/$ORG/$mod/contents/$PATH_IN_REPO?ref=$BRANCH" --jq .content 2>/dev/null | tr -d '\n' || true)
  fi

  if [[ -n "$branch_sha" && "$branch_content" == "$CONTENT" ]]; then
    echo "  = branch already up to date"
    skipped=$((skipped + 1))
  else
    PAYLOAD=$(jq -n \
      --arg message "$MSG" \
      --arg content "$CONTENT" \
      --arg sha "$branch_sha" \
      --arg branch "$BRANCH" \
      '{message:$message, content:$content, branch:$branch}
       + (if $sha != "" and $sha != "null" then {sha:$sha} else {} end)')
    if err=$(gh api -X PUT "repos/$ORG/$mod/contents/$PATH_IN_REPO" --input - <<<"$PAYLOAD" 2>&1 >/dev/null); then
      echo "  ✓ workflow written to $BRANCH"
      written=$((written + 1))
    else
      echo "  ! workflow write failed: $err" >&2
      failed=$((failed + 1))
      continue
    fi
  fi

  PR_BODY="同步 \`oekit/.github\` 仓的 \`workflow-templates/issue-to-project.yml\` 模板到本仓。

- 变更面：仅 \`${PATH_IN_REPO}\` 一个文件
- 来源：模板本体为唯一事实源，各仓副本与其逐字节一致
- 分支：\`${BRANCH}\`（由 \`scripts/sync-project-hooks.sh\` 自动开）"

  # 复用判据只认 head=分支 且 state=open 的 PR；
  # 已关闭的 PR 不复用（重开一个），避免把历史 PR 当成"已处理"。
  if pr_num=$(gh api "repos/$ORG/$mod/pulls?state=open&head=$ORG:$BRANCH" --jq '.[0].number // empty' 2>/dev/null) && [[ -n "$pr_num" ]]; then
    echo "  = PR #$pr_num already open (branch updated)"
  else
    if err=$(gh api -X POST "repos/$ORG/$mod/pulls" \
      -f title="ci: 同步即时入板 workflow（.github 模板 → ${mod}）" \
      -f head="$BRANCH" \
      -f base=main \
      -f body="$PR_BODY" 2>&1 >/dev/null); then
      echo "  ✓ PR opened"
    else
      echo "  ! PR open failed: $err" >&2
      failed=$((failed + 1))
    fi
  fi
done

echo "DONE: written=$written skipped=$skipped failed=$failed"
if [[ "$failed" -gt 0 ]]; then
  exit 1
fi

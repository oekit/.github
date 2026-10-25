#!/usr/bin/env python3
"""oekit 四块 GitHub Projects 大盘统一同步。

Projects v2 不会自动跟进 org 的 issue/PR/tag，本脚本补齐：

  #1 Issue 大盘   — org issue → 板；Status=Todo/Done；Kind 按标题细分；Module=所属仓
  #2 版本大盘     — 主模块/嵌套模块独立成行；Latest/Prior=近两版 tag，Parent=嵌套归属
  #3 矩阵健康     — Install/Tag 并排 + Note 诊断；Hygiene（全绿/有漂移/待审计）+ Drift
  #4 PR 审查队列  — org PR → 板；Review=待审/已合入/已关闭；Scope 按 CC 类型

用法：
  python3 scripts/sync-projects.py --apply           # 全量
  python3 scripts/sync-projects.py --apply --only 1  # 只跑 Issue 大盘
  python3 scripts/sync-projects.py --link URL --state open   # 单条即时入板
  python3 scripts/sync-projects.py --refresh-module logx     # 单模块版本+健康刷新
  python3 scripts/sync-projects.py                   # dry-run

环境：
  GH_TOKEN   需 project + read:org
  ORG        默认 oekit
  MATRIX_BASE  可选，本地检出根（健康检查用；缺省只查远端 README）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ORG = os.environ.get("ORG", "oekit")
PROJECT_ISSUE = 1
PROJECT_VERSION = 2
PROJECT_HEALTH = 3
PROJECT_PR = 4

# 「近期变更」视图的滚动窗口：名字 + 天数（起点 = 今天 - N 天）
RECENT_VIEW_NAME = "近期变更"
RECENT_WINDOW_DAYS = 7

# 钉死的语义型视图过滤器（值固定，不需要滚动）
PINNED_VIEW_FILTERS = {
    # 「嵌套模块」= Parent 非空的那些行。
    # 旧值 `nested:*` 里的 nested 不是 Projects DSL 的字段名 ⇒ 该视图恒为 0 行。
    "嵌套模块": "parent:*",
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _gh_run(argv: list[str]) -> str:
    """跑 gh 并返回 stdout；失败时把 **gh 自己的报错** 抛出来。

    原实现是 `check_output(..., stderr=subprocess.PIPE)` ⇒ gh 的真实报错被吞进
    管道，调用方只拿到一句 `returned non-zero exit status 4`，日志里看不到
    「为什么」。实测 2026-10-22：全 org 7 个 dependabot PR 的「即时入板」job
    全红，真实原因是 `To get started with GitHub CLI, please run: gh auth login`
    ——而这句话当时根本不在日志里，只有一个退出码。
    """
    p = subprocess.run(["gh", *argv], capture_output=True, text=True)
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(
            f"gh {' '.join(argv)} 失败（exit {p.returncode}）："
            f"{detail[:800] or '(无输出)'}"
        )
    return p.stdout


def gql(query: str, **variables: str) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        # Int! project number needs typed JSON; everything else stays string
        if k == "number" and str(v).isdigit():
            args += ["-F", f"{k}={v}"]
        else:
            args += ["-f", f"{k}={v}"]
    out = _gh_run(args)
    data = json.loads(out)
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def gh_json(argv: list[str]) -> object:
    out = _gh_run(argv)
    return json.loads(out) if out.strip() else None


def gh_auth_ready() -> bool:
    """gh 此刻有没有可用凭证。

    **Dependabot 触发的工作流拿不到 repo secret**：GitHub 只把 secret 暴露给
    Dependabot 自己声明的 `dependabot` 命名空间；普通 `pull_request` 触发里
    `secrets.OEKIT_PROJECT_TOKEN` 求值成空串。而 CI 机器上没有 gh 的
    hosts.yml ⇒ gh 完全未认证 ⇒ `gh api` 以 **exit 4** 退出
    （"To get started with GitHub CLI, please run: gh auth login"）。

    实测 2026-10-22：全 org 7 个 dependabot PR 的「即时入板」job 全红，
    人工 PR 全绿。**红是假的**——代价是红色淹没真信号（这正是「门禁要少而致命」
    的反面）。依赖类 PR 本来也不该进人工审查队列，所以正确处置是**跳过**，
    而不是让整条 job 假红。

    注意：token **存在但无效**时这里返回 True —— 那是真的配置错误，应该响亮地红。
    """
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    p = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return p.returncode == 0


def is_dependabot_run() -> bool:
    """这次运行是不是 Dependabot 触发的。

    Dependabot 触发的工作流**拿不到仓库 secret** —— GitHub 只把 secret 暴露给
    Dependabot 自己声明的 `dependabot` 命名空间；普通 `pull_request` 触发里
    `secrets.OEKIT_PROJECT_TOKEN` 求值成空串。而 CI 机器上没有 gh 的
    hosts.yml ⇒ gh 完全未认证 ⇒ `gh api` 以 **exit 4** 退出
    （"To get started with GitHub CLI, please run: gh auth login"）。

    实测 2026-10-22：全 org 7 个 dependabot PR 的「即时入板」job 全红，
    人工 PR 全绿。**红是假的**——代价是红色淹没真信号（这正是「门禁要少而致命」
    的反面）。依赖类 PR 本来也不该进人工审查队列，所以正确处置是**跳过**，
    而不是让整条 job 假红。

    判据用 `GITHUB_ACTOR`：Dependabot 开 PR 时 actor 就是 `dependabot[bot]`；
    人工合并后的 push 事件 actor 是合并者（不是 dependabot）⇒ secret 照常可用。
    """
    return os.environ.get("GITHUB_ACTOR") == "dependabot[bot]"


def no_credential_reason() -> str | None:
    """当前进程有没有可用 GitHub 凭证；没有则返回原因（可用返回 None）。

    分两种「没有」——**处置完全不同**：

    - **Dependabot 触发的工作流**：仓库 secret 拿不到（见 is_dependabot_run
      的注释）⇒ 这是**预期内**的 ⇒ 跳过同步，不要让整条 job 假红。
    - **其他运行**却也没凭证：secret 没建 / 没授权 ⇒ 真配置错误 ⇒ 必须响亮地红。
    """
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return None
    if gh_auth_ready():
        return None
    if is_dependabot_run():
        return (
            "Dependabot 触发的工作流拿不到仓库 secret"
            "（GitHub 只把 secret 暴露给 Dependabot 自己声明的 dependabot 命名空间）"
        )
    return "GH_TOKEN / GITHUB_TOKEN 为空，且 gh 本地无凭证"


def parse_github_url(url: str) -> tuple[str, int, str]:
    """→ (repo full name, number, kind) kind in {issues,pull}"""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)", url)
    if not m:
        raise ValueError(url)
    kind = "issues" if m.group(2) == "issues" else "pulls"  # REST path
    return m.group(1), int(m.group(3)), kind


# ---------------------------------------------------------------------------
# project primitives
# ---------------------------------------------------------------------------


def load_project(number: int) -> dict:
    """拉全项目字段与条目（items 游标分页，避免 first:100 静默截断）。"""
    fields: dict[str, dict] = {}
    items: list[dict] = []
    pid = None
    cursor: str | None = None
    while True:
        vars_ = {"org": ORG, "number": str(number)}
        if cursor:
            vars_["cursor"] = cursor
        data = gql(
            """
            query($org: String!, $number: Int!, $cursor: String) {
              organization(login: $org) {
                projectV2(number: $number) {
                  id
                  fields(first: 50) {
                    nodes {
                      ... on ProjectV2Field { id name }
                      ... on ProjectV2SingleSelectField {
                        id name
                        options { id name }
                      }
                    }
                  }
                  items(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      id
                      type
                      content {
                        ... on Issue { number url state title
                                       repository { nameWithOwner } }
                        ... on PullRequest { number url state title
                                             repository { nameWithOwner } }
                        ... on DraftIssue { title body }
                      }
                      fieldValues(first: 50) {
                        nodes {
                          ... on ProjectV2ItemFieldSingleSelectValue {
                            name
                            field {
                              ... on ProjectV2Field { id name }
                              ... on ProjectV2SingleSelectField { id name }
                            }
                          }
                          ... on ProjectV2ItemFieldTextValue {
                            text
                            field {
                              ... on ProjectV2Field { id name }
                              ... on ProjectV2SingleSelectField { id name }
                            }
                          }
                          ... on ProjectV2ItemFieldNumberValue {
                            number
                            field {
                              ... on ProjectV2Field { id name }
                              ... on ProjectV2SingleSelectField { id name }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            **vars_,
        )
        proj = data["organization"]["projectV2"]
        pid = proj["id"]
        for f in proj["fields"]["nodes"]:
            fields[f["name"]] = f
        for n in proj["items"]["nodes"]:
            content = n.get("content") or {}
            fv = {}
            for x in n.get("fieldValues", {}).get("nodes", []):
                fname = (x.get("field") or {}).get("name")
                if not fname:
                    continue
                if "text" in x and x.get("text") is not None:
                    fv[fname] = x.get("text")
                elif "number" in x and x.get("number") is not None:
                    fv[fname] = x.get("number")
                else:
                    fv[fname] = x.get("name")
            items.append(
                {
                    "id": n["id"],
                    "url": content.get("url"),
                    "title": content.get("title"),
                    "state": content.get("state"),
                    "repo": (content.get("repository") or {}).get("nameWithOwner"),
                    "number": content.get("number"),
                    "fv": fv,
                }
            )
        page = proj["items"]["pageInfo"]
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return {"id": pid, "fields": fields, "items": items}


def set_field(pid: str, item_id: str, field_id: str, *, option_id: str | None = None, text: str | None = None, number: float | None = None) -> None:
    if option_id is not None:
        value = f'{{ singleSelectOptionId: "{option_id}" }}'
    elif text is not None:
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        value = f'{{ text: "{esc}" }}'
    elif number is not None:
        value = f"{{ number: {number} }}"
    else:
        raise ValueError("no value")
    gql(
        f"""
        mutation {{
          updateProjectV2ItemFieldValue(input: {{
            projectId: "{pid}", itemId: "{item_id}", fieldId: "{field_id}",
            value: {value}
          }}) {{ projectV2Item {{ id }} }}
        }}
        """
    )


def add_url(pid: str, url: str) -> str:
    repo, num, kind = parse_github_url(url)
    node = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/{kind}/{num}", "--jq", ".node_id"],
        text=True,
    ).strip()
    data = gql(
        f"""
        mutation {{
          addProjectV2ItemById(input: {{ projectId: "{pid}", contentId: "{node}" }}) {{
            item {{ id }}
          }}
        }}
        """
    )
    return data["addProjectV2ItemById"]["item"]["id"]


def opt_map(field: dict | None) -> dict[str, str]:
    if not field:
        return {}
    return {o["name"]: o["id"] for o in field.get("options", [])}


# ---------------------------------------------------------------------------
# Issue 大盘
# ---------------------------------------------------------------------------

# (pattern, kind, weight) — 打分制，避免单关键词误伤（如「错误变体」被裸「误」判成 Bug）
KIND_SIGNALS: list[tuple[re.Pattern[str], str, int]] = [
    # ---- Bug（只收“已经坏了”的表述，不收“提议防 X”）----
    (re.compile(r"fix\(|\[fix\]|\[bug\]", re.I), "Bug", 5),
    (re.compile(
        r"不存在|静默|崩溃|越权|泄露|泄漏|泄进|矛盾|陷阱|绕过|折叠"
        r"|撞车|被重打|双.*编码|签名错误|全体折叠|直连可绕过|仍写|仍是"
        r"|全部 panic|均 panic|全 panic",
        re.I,
    ), "Bug", 4),
    (re.compile(r"误标|误导|误判|误用|误报|错误输出|错误文本|渲染为 \{\}", re.I), "Bug", 3),
    (re.compile(r"残留|过期|不去重|重复执行|破坏不变量|只读约定未声明", re.I), "Bug", 2),
    # ---- Enhancement ----
    (re.compile(
        r"建议|支持|提供|增强|诉求|选项|变体|模式|门禁|防护|钩子|特性|扩展"
        r"|已就绪|选择器|可否|能否|无法|官方化|采纳|产品化|评估|复用|三态|探测"
        r"|缺[一-鿿]|缺[A-Z]|^缺|——缺|缺「|缺声明|缺返回|缺单边|缺联动|缺链接|缺能力",
        re.I,
    ), "Enhancement", 3),
    (re.compile(r"\[feature\]|\[feat\]|\[缺能力\]|增加.*校验|增加.*门禁|增加.*检查", re.I), "Enhancement", 3),
    # ---- Refactor ----
    (re.compile(r"归位|拆出|拆子包|拆嵌套|下线|收编|迁至|迁到|去 hertz 化|逐字节相同|两份|无人引用", re.I), "Refactor", 3),
    # ---- Docs ----
    (re.compile(
        r"\[(docs|migration|readme|godoc|llms|site|llms\.txt)([/\]])"
        r"|指南|文档化|未文档化|文档站|文档缺|README|CONTRIBUTING|AGENTS|llms\.txt",
        re.I,
    ), "Docs", 4),
    (re.compile(r"注释把|注释错误|注释写|godoc|文档示例|包文档", re.I), "Docs", 2),
    # ---- Chore ----
    (re.compile(r"\[(chore|deps|ci)\]|报告$|汇总$|矩阵漂移报告", re.I), "Chore", 3),
]


def infer_kind(title: str) -> str:
    """按信号打分归类；同分按 Bug > Enhancement > Refactor > Docs > Chore。"""
    scores = {"Bug": 0, "Enhancement": 0, "Refactor": 0, "Docs": 0, "Chore": 0}
    for pat, kind, weight in KIND_SIGNALS:
        if pat.search(title or ""):
            scores[kind] += weight
    if max(scores.values()) <= 0:
        return "Enhancement"
    for k in ("Bug", "Enhancement", "Refactor", "Docs", "Chore"):
        if scores[k] == max(scores.values()):
            return k
    return "Enhancement"


def infer_module(repo, title: str) -> str | None:
    """从 issue 所在仓推 Module；仓不在矩阵内时看标题前缀，再退回 meta。"""
    name = ""
    if isinstance(repo, dict):
        name = repo.get("name") or (repo.get("nameWithOwner") or "").split("/")[-1]
    elif isinstance(repo, str):
        # 既可能是 oekit/pkg，也可能是 https://github.com/oekit/pkg
        name = repo.rstrip("/").split("/")[-1]
    if name:
        return name
    m = re.match(r"\[([^\]/]+)", title or "")
    if m:
        return m.group(1).strip()
    return "meta"


def list_org_issues() -> list[dict]:
    issues: list[dict] = []
    seen: set[str] = set()
    for state in ("open", "closed"):
        raw = subprocess.check_output(
            [
                "gh", "search", "issues",
                "--owner", ORG,
                "--state", state,
                # 必须留足余量：org issue 超过后 search 静默截断，板上会缺行
                "--limit", "200",
                "--json", "repository,title,number,url,state",
            ],
            text=True,
        )
        batch = json.loads(raw)
        if len(batch) >= 200:
            print(f"  ! search issues state={state} hit limit 200，可能被截断", file=sys.stderr)
        for i in batch:
            if i["url"] in seen or "/pull/" in i["url"]:
                continue
            seen.add(i["url"])
            issues.append(i)
    return issues


def sync_issue_board(apply: bool) -> None:
    print("\n=== #1 Issue 大盘 ===")
    proj = load_project(PROJECT_ISSUE)
    status_opts = opt_map(proj["fields"].get("Status"))
    kind_opts = opt_map(proj["fields"].get("Kind"))
    mod_opts = opt_map(proj["fields"].get("Module"))
    status_id = proj["fields"]["Status"]["id"]
    kind_id = proj["fields"].get("Kind", {}).get("id")
    mod_id = proj["fields"].get("Module", {}).get("id")

    by_url = {i["url"]: i for i in proj["items"] if i["url"]}
    issues = list_org_issues()
    print(f"org issues={len(issues)} board={len(proj['items'])}")

    adds: list[dict] = []
    syncs: list[dict] = []
    for iss in issues:
        # gh search 返回小写 open/closed，GraphQL 是大写 OPEN/CLOSED——统一不敏感比较
        want_status = "Todo" if iss["state"].upper() == "OPEN" else "Done"
        want_kind = infer_kind(iss["title"])
        want_mod = infer_module(iss.get("repository", ""), iss["title"])
        if iss["url"] not in by_url:
            adds.append(
                {
                    **iss,
                    "want_status": want_status,
                    "want_kind": want_kind,
                    "want_mod": want_mod,
                }
            )
            continue
        item = by_url[iss["url"]]
        cur_status = item["fv"].get("Status")
        cur_kind = item["fv"].get("Kind")
        cur_mod = item["fv"].get("Module")
        # Kind/Module 是推导列，跟规则走；Status 跟 state 走
        if (
            cur_status != want_status
            or (want_kind and cur_kind != want_kind)
            or (want_mod and cur_mod != want_mod)
        ):
            syncs.append(
                {
                    "id": item["id"],
                    "title": iss["title"],
                    "status": want_status,
                    "kind": want_kind,
                    "mod": want_mod,
                    "cur_kind": cur_kind,
                    "cur_mod": cur_mod,
                }
            )

    for a in adds:
        print(f"  + {a['url']}  {a['want_status']}/{a['want_kind']}/{a['want_mod']}")
    for s in syncs:
        bits = []
        if s["status"]:
            bits.append(s["status"])
        if s["kind"] and s["kind"] != s["cur_kind"]:
            bits.append(f"kind:{s['cur_kind']}→{s['kind']}")
        if s["mod"] and s["mod"] != s["cur_mod"]:
            bits.append(f"mod:{s['cur_mod']}→{s['mod']}")
        print(f"  ~ {s['title'][:48]}  → {'/'.join(bits)}")
    if not apply:
        print("  (dry-run)")
        return

    def assign(iid: str, status: str, kind: str | None, mod: str | None) -> None:
        if status in status_opts:
            set_field(proj["id"], iid, status_id, option_id=status_opts[status])
        if kind and kind in kind_opts and kind_id:
            set_field(proj["id"], iid, kind_id, option_id=kind_opts[kind])
        if mod and mod in mod_opts and mod_id:
            set_field(proj["id"], iid, mod_id, option_id=mod_opts[mod])
        elif mod and mod_id:
            # 模板里没有的模块名，落 meta，避免整列空着
            if "meta" in mod_opts:
                set_field(proj["id"], iid, mod_id, option_id=mod_opts["meta"])

    for a in adds:
        iid = add_url(proj["id"], a["url"])
        assign(iid, a["want_status"], a["want_kind"], a["want_mod"])
    for s in syncs:
        assign(s["id"], s["status"], s["kind"], s["mod"])


# ---------------------------------------------------------------------------
# PR 审查队列
# ---------------------------------------------------------------------------

# conventional commit 类型 → Scope（优先于正文关键词）
CC_TYPE_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\(|:|!)",
    re.I,
)
CC_TO_SCOPE = {
    "feat": "feat",
    "fix": "fix",
    "docs": "docs",
    "style": "chore",
    "refactor": "refactor",
    "perf": "feat",
    "test": "chore",
    "build": "deps",
    "ci": "ci",
    "chore": "chore",
    "revert": "chore",
}

SCOPE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdocs?\b|\[docs?\]|文档|README|godoc|llms", re.I), "docs"),
    (re.compile(r"\bfix\b|\[fix\]|修复|bugfix", re.I), "fix"),
    (re.compile(r"\brefactor\b|重构|归位|拆", re.I), "refactor"),
    (re.compile(r"\bci\b|workflow|action|门禁", re.I), "ci"),
    (re.compile(r"\bdeps\b|依赖|dependabot|bump ", re.I), "deps"),
    (re.compile(r"\bchore\b|杂项", re.I), "chore"),
    (re.compile(r"\bfeat\b|特性|新增|支持|增强", re.I), "feat"),
]


def infer_scope(title: str) -> str:
    """Scope：先认 conventional commit 类型，再落关键词，最后 feat。"""
    m = CC_TYPE_RE.match(title or "")
    if m:
        t = m.group(1).lower()
        if t == "chore" and re.search(r"\bdeps\b|dependabot|bump |依赖", title, re.I):
            return "deps"
        return CC_TO_SCOPE.get(t, "chore")
    for pat, scope in SCOPE_RULES:
        if pat.search(title):
            return scope
    return "feat"


def infer_review(state: str) -> str:
    """gh search prs state: open | merged | closed。"""
    s = (state or "").upper()
    if s == "OPEN":
        return "待审"
    if s == "MERGED":
        return "已合入"
    return "已关闭"


def list_org_prs() -> list[dict]:
    prs: list[dict] = []
    seen: set[str] = set()
    for state in ("open", "closed"):
        raw = subprocess.check_output(
            [
                "gh", "search", "prs",
                "--owner", ORG,
                "--state", state,
                # 曾用 50 导致 closed PR 被截断（实际 65+），15 条进不了板
                "--limit", "200",
                "--json", "repository,title,number,url,state",
            ],
            text=True,
        )
        batch = json.loads(raw)
        if len(batch) >= 200:
            print(f"  ! search prs state={state} hit limit 200，可能被截断", file=sys.stderr)
        for i in batch:
            if i["url"] in seen:
                continue
            seen.add(i["url"])
            prs.append(i)
    return prs


def sync_pr_board(apply: bool) -> None:
    print("\n=== #4 PR 审查队列 ===")
    proj = load_project(PROJECT_PR)
    review_opts = opt_map(proj["fields"].get("Review"))
    scope_opts = opt_map(proj["fields"].get("Scope"))
    review_id = proj["fields"]["Review"]["id"]
    scope_id = proj["fields"].get("Scope", {}).get("id")

    by_url = {i["url"]: i for i in proj["items"] if i["url"]}
    prs = list_org_prs()
    print(f"org prs={len(prs)} board={len(proj['items'])}")

    adds: list[dict] = []
    syncs: list[dict] = []
    for pr in prs:
        want_review = infer_review(pr["state"])
        want_scope = infer_scope(pr["title"])
        if pr["url"] not in by_url:
            adds.append({**pr, "want_review": want_review, "want_scope": want_scope})
            continue
        item = by_url[pr["url"]]
        cur_review = item["fv"].get("Review")
        cur_scope = item["fv"].get("Scope")
        # Review 跟 state 走；Scope 跟标题 CC 类型走（都是推导列）
        if cur_review != want_review or (want_scope and cur_scope != want_scope):
            syncs.append(
                {
                    "id": item["id"],
                    "title": pr["title"],
                    "review": want_review,
                    "scope": want_scope,
                    "cur_scope": cur_scope,
                }
            )

    for a in adds:
        print(f"  + {a['url']}  {a['want_review']}/{a['want_scope']}")
    for s in syncs:
        bits = [s["review"]]
        if s.get("scope") and s["scope"] != s.get("cur_scope"):
            bits.append(f"scope:{s.get('cur_scope')}→{s['scope']}")
        print(f"  ~ {s['title'][:48]}  → {'/'.join(bits)}")
    if not apply:
        print("  (dry-run)")
        return

    def assign(iid: str, review: str, scope: str | None) -> None:
        if review in review_opts:
            set_field(proj["id"], iid, review_id, option_id=review_opts[review])
        if scope and scope in scope_opts and scope_id:
            set_field(proj["id"], iid, scope_id, option_id=scope_opts[scope])

    for a in adds:
        iid = add_url(proj["id"], a["url"])
        assign(iid, a["want_review"], a["want_scope"])
    for s in syncs:
        assign(s["id"], s["review"], s["scope"])


# ---------------------------------------------------------------------------
# 版本大盘 + 矩阵健康
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 模块清单：真相源 = matrix-tools/matrix.py（唯一事实源）
# ---------------------------------------------------------------------------
#
# 为什么不能在本文件里手抄一份（2026-10-25 实测踩坑）：
#   本文件原来硬编码 21 个模块，而 matrix.py 早已扩到 24 个（+kitx / +etcdx / +k8s）
#   ⇒ 版本大盘少 3 行主模块，且**每小时的全量同步不会补**（它读的就是这份硬编码
#     清单）。副本漂移的形态是「静默少数据」：看板自己不会报错，只是一直缺行。
#   同理，嵌套模块原来靠**扫 tag**发现 ⇒ 真源已删掉的模块（data/all，目录已删、
#     仓库里还留着 all/v0.1.1 旧 tag）会被当成「活模块」重新建行 ⇒ 陈旧行删不掉，
#     每次全量同步还会把它复活。
#
# 现在：真相源优先从 matrix.py 读（本地工作区 / CI 都可指过去），读不到时用内嵌
# 副本兜底（CI 里只 checkout 了本仓），并且**比对两份是否一致**——不一致就显式
# 告警，把「静默少数据」变成「显式漂移」。
_FALLBACK_MODULES = [
    "pkg", "concurrency", "logx", "core", "kitx",
    "metrics", "tracing", "registry",
    "data", "gormx", "redisx", "etcdx", "httpx", "network", "timertask",
    "middleware", "k8s",
    "nns", "nns-endpoint",
    "bootstrap", "hertz", "kitex", "workflow", "kitex-examples",
]

# 嵌套模块：(父仓, 仓内路径) —— 与 matrix.py 的 NESTED_MODULES 同构。
# 注意 kitex 有一条**多层**路径：observability/tracing/instrumentation/kitexotel。
_FALLBACK_NESTED = [
    ("bootstrap", "nacos"), ("bootstrap", "obs"),
    ("logx", "sink"), ("logx", "hlog"), ("logx", "hertzlog"), ("logx", "kitexlog"),
    ("timertask", "redis"),
    ("registry", "nacosimpl"), ("registry", "nacos"),
    ("kitex", "observability/tracing/instrumentation/kitexotel"), ("kitex", "istio"),
]

# 主模块默认 Layer（新行落板时用；嵌套行继承父级）。
# 取自站点 docs/index.md 的「能力层」分组：基础库→基础、数据访问→数据、
# 通信·服务治理→网络/治理、应用引导·框架生态→服务（workflow→工具、示例→示例）。
LAYER_BY_MODULE = {
    "pkg": "基础", "concurrency": "基础", "core": "基础", "kitx": "基础",
    "logx": "基础", "metrics": "基础", "tracing": "基础",
    "data": "数据", "gormx": "数据", "redisx": "数据", "etcdx": "数据",
    "timertask": "数据",
    "httpx": "网络", "network": "网络",
    "registry": "治理", "nns": "治理", "nns-endpoint": "治理",
    "middleware": "服务", "bootstrap": "服务", "hertz": "服务", "kitex": "服务",
    "k8s": "服务",
    "workflow": "工具", "kitex-examples": "示例",
}


def _matrix_truth_path() -> Path | None:
    """定位 matrix-tools/matrix.py（模块矩阵唯一事实源）。找不到返回 None。

    探测顺序：MATRIX_TOOLS_PATH 环境变量 → 本仓克隆的兄弟目录 → 当前工作目录。
    本仓（oekit/.github）克隆在 <workspace>/.github ⇒ 兄弟目录即
    <workspace>/matrix-tools/matrix.py。
    """
    cands: list[Path] = []
    env = os.environ.get("MATRIX_TOOLS_PATH")
    if env:
        p = Path(env)
        cands.append(p / "matrix.py" if p.is_dir() else p)
    here = Path(__file__).resolve()
    cands.append(here.parents[2] / "matrix-tools" / "matrix.py")
    cands.append(Path.cwd() / "matrix-tools" / "matrix.py")
    cands.append(Path.cwd().parent / "matrix-tools" / "matrix.py")
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def load_truth() -> tuple[list[str], list[tuple[str, str]], str]:
    """返回 (主模块清单, 嵌套清单[(父仓, 路径)], 来源说明)。

    真相源 = matrix.py；读不到时退回内嵌副本并说明来源（不是静默）。
    """
    path = _matrix_truth_path()
    if path is None:
        return (
            list(_FALLBACK_MODULES),
            list(_FALLBACK_NESTED),
            "内嵌副本（未找到 matrix.py）",
        )
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("oekit_matrix_truth", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        modules = list(mod.MODULES)
        nested = [(n["repo"], n["path"]) for n in mod.NESTED_MODULES]
    except Exception as exc:  # 真相源坏了 ⇒ 用副本兜底，但要说出来
        print(f"::warning::真相源 {path} 读取失败（{exc}）——退回内嵌副本")
        return (
            list(_FALLBACK_MODULES),
            list(_FALLBACK_NESTED),
            f"内嵌副本（{path} 读取失败）",
        )
    if modules != list(_FALLBACK_MODULES) or nested != list(_FALLBACK_NESTED):
        print(
            f"::warning::内嵌副本与真相源 {path} 不一致"
            f"（副本 {len(_FALLBACK_MODULES)} 主/{len(_FALLBACK_NESTED)} 嵌套，"
            f"真相源 {len(modules)} 主/{len(nested)} 嵌套）——以真相源为准；"
            "请把 scripts/sync-projects.py 的 _FALLBACK_* 更新到一致"
        )
    return modules, nested, str(path)


def latest_tags(repo: str) -> list[str]:
    """Return recent root tags, newest first (vX.Y.Z only)."""
    raw = subprocess.check_output(
        ["gh", "api", f"repos/{ORG}/{repo}/tags?per_page=100", "--jq", "[.[].name]"],
        text=True,
    )
    tags = json.loads(raw)
    root = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return root


def parse_nested_tag(tag: str) -> tuple[str, str] | None:
    """拆嵌套 tag：<仓内路径>/vX.Y.Z → (路径, 版本)。不是嵌套 tag 返回 None。

    ⚠️ 必须 **rsplit** 而不是 split：嵌套路径可以是多层的
    （kitex 的 observability/tracing/instrumentation/kitexotel）。
    split("/", 1) 会把 path 切成 "observability"、ver 切成
    "tracing/instrumentation/kitexotel/v0.1.0" ⇒ 版本号正则不匹配 ⇒
    这个嵌套模块永远发现不了（2026-10-25 实测：kitexotel 在看板上一直缺行）。
    """
    if "/" not in tag:
        return None
    path, ver = tag.rsplit("/", 1)
    if not re.fullmatch(r"v\d+\.\d+\.\d+", ver):
        return None
    return path, ver


def nested_tags(repo: str) -> dict[str, list[str]]:
    """嵌套模块 tag：<仓内路径>/vX.Y.Z → {路径: [ver, ...] 新→旧}。"""
    raw = subprocess.check_output(
        ["gh", "api", f"repos/{ORG}/{repo}/tags?per_page=100", "--jq", "[.[].name]"],
        text=True,
    )
    tags = json.loads(raw)
    out: dict[str, list[str]] = {}
    for t in tags:
        parsed = parse_nested_tag(t)
        if parsed is None:
            continue
        path, ver = parsed
        out.setdefault(path, []).append(ver)

    def key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.lstrip("v").split("."))

    for p in out:
        out[p].sort(key=key, reverse=True)
    return out


def module_exists(title: str) -> bool:
    """看板行对应的仓（或仓内目录）是否**仍然存在**。

    title 形如 `data`（主模块）或 `data/all`（嵌套模块）。
    用于陈旧行清理：矩阵真源已移除、且仓/路径在 GitHub 上确实没了 ⇒ 才是陈旧行。
    反之（矩阵外的单元，如伞仓 oekit / matrix-tools / .github）一律不动。
    """
    if "/" in title:
        repo, path = title.split("/", 1)
        argv = ["gh", "api", f"repos/{ORG}/{repo}/contents/{path}"]
    else:
        argv = ["gh", "api", f"repos/{ORG}/{title}"]
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode == 0


def delete_item(pid: str, item_id: str) -> None:
    """删掉看板行（陈旧行清理用）。"""
    gql(
        f"""
        mutation {{
          deleteProjectV2Item(input: {{
            projectId: "{pid}", itemId: "{item_id}"
          }}) {{ clientMutationId }}
        }}
        """
    )


def project_views(number: int) -> tuple[str, list[dict]]:
    """返回 (project id, 视图列表)。视图 id 会变，故按**名字**定位，不写死 id。"""
    data = gql(
        """
        query($org: String!, $number: Int!) {
          organization(login: $org) {
            projectV2(number: $number) {
              id
              views(first: 20) { nodes { id name filter } }
            }
          }
        }
        """,
        org=ORG,
        number=str(number),
    )
    proj = data["organization"]["projectV2"]
    return proj["id"], proj["views"]["nodes"]


def roll_recent_changes_view(apply: bool) -> None:
    """滚动「近期变更」视图的过滤窗口：updated:>=<今天 - N 天>。

    Projects 的过滤器 DSL **只认固定日期**——相对日期会被判非法
    （实测 `updated:>=now-7d` ⇒ "Invalid value >=now-7d for updated.
    Fix by using date format YYYY-MM-DD"）。
    ⇒ 窗口必须有人滚，否则视图单调膨胀成「全部行」，「近期变更」名存实亡。

    每轮同步滚一次（日期没变就不写 ⇒ 一天最多一次 API 写）。
    写失败只告警不抛错：视图配置不是看板数据，挂了不该拖垮整轮同步。
    """
    try:
        _pid, views = project_views(PROJECT_VERSION)
    except Exception as exc:
        print(f"  ! 读取视图列表失败（{exc}）——跳过窗口滚动")
        return
    target = next((v for v in views if v.get("name") == RECENT_VIEW_NAME), None)
    if not target:
        print(f"  ! 视图「{RECENT_VIEW_NAME}」不存在——跳过窗口滚动")
        return
    start = (
        datetime.date.today() - datetime.timedelta(days=RECENT_WINDOW_DAYS)
    ).isoformat()
    want = f"updated:>={start}"
    cur = target.get("filter") or ""
    if cur == want:
        return
    print(f"  ~ {RECENT_VIEW_NAME} 窗口：{cur or '(空)'} → {want}")
    if not apply:
        return
    _write_view_filter(target["id"], want)


def sync_pinned_view_filters(apply: bool) -> None:
    """钉死语义型视图的过滤器（值固定，不需要滚动）。

    「嵌套模块」= Parent 非空的那些行 ⇒ `parent:*`。
    旧值 `nested:*` 是个**不存在的字段名**（Projects DSL 里没有 nested 这个字段）
    ⇒ 该视图恒为空（实测 0 行），而且看板上不报错——只能靠对账发现。
    """
    try:
        _pid, views = project_views(PROJECT_VERSION)
    except Exception as exc:
        print(f"  ! 读取视图列表失败（{exc}）——跳过过滤器对齐")
        return
    by_name = {v.get("name"): v for v in views}
    for name, want in PINNED_VIEW_FILTERS.items():
        v = by_name.get(name)
        if not v:
            print(f"  ! 视图「{name}」不存在——跳过过滤器对齐")
            continue
        cur = v.get("filter") or ""
        if cur == want:
            continue
        print(f"  ~ {name} 过滤器：{cur or '(空)'} → {want}")
        if apply:
            _write_view_filter(v["id"], want)


def _write_view_filter(view_id: str, want: str) -> None:
    """写视图过滤器；失败只告警不抛错（视图配置挂了不该拖垮看板同步）。"""
    try:
        gql(
            f"""
            mutation {{
              updateProjectV2View(input: {{
                viewId: "{view_id}", filter: "{want}"
              }}) {{ projectV2View {{ id name filter }} }}
            }}
            """
        )
    except Exception as exc:
        print(f"  ! 写视图过滤器失败（{exc}）——看板数据不受影响，继续")


def add_draft(pid: str, title: str, body: str = "") -> str:
    esc_t = title.replace("\\", "\\\\").replace('"', '\\"')
    esc_b = body.replace("\\", "\\\\").replace('"', '\\"')
    data = gql(
        f"""
        mutation {{
          addProjectV2DraftIssue(input: {{
            projectId: "{pid}", title: "{esc_t}", body: "{esc_b}"
          }}) {{ projectItem {{ id }} }}
        }}
        """
    )
    return data["addProjectV2DraftIssue"]["projectItem"]["id"]


def remote_install_line(repo: str) -> str | None:
    """README go get line version, fetched from default branch."""
    try:
        raw = subprocess.check_output(
            [
                "gh", "api", f"repos/{ORG}/{repo}/readme",
                "--jq", ".content",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None
    import base64

    text = base64.b64decode(raw).decode("utf-8", "replace")
    m = re.search(rf"github\.com/{ORG}/{re.escape(repo)}@(v[0-9.]+)", text)
    return m.group(1) if m else None


def sync_version_board(apply: bool) -> None:
    """版本矩阵：Title=模块路径，主模块与嵌套模块独立成行。

    嵌套行 title 形如 `logx/hlog`，Parent=父模块名；主模块 Parent 留空。

    行集合 = 矩阵真相源（MODULES + NESTED_MODULES），**不是**扫 tag 发现的：
    扫 tag 会把「真源已删、但仓库还留着旧 tag」的模块（如 data/all）当成活模块，
    于是陈旧行删不掉、每轮全量同步还会把它复活（2026-10-25 实测）。
    """
    print("\n=== #2 版本大盘 ===")
    proj = load_project(PROJECT_VERSION)
    latest_id = proj["fields"]["Latest"]["id"]
    prior_id = proj["fields"]["Prior"]["id"]
    parent_id = proj["fields"].get("Parent", {}).get("id")
    layer_id = proj["fields"].get("Layer", {}).get("id")
    layer_opts = opt_map(proj["fields"].get("Layer"))

    modules, nested, src = load_truth()
    print(f"  真相源：{src}（{len(modules)} 主模块 / {len(nested)} 嵌套模块）")
    missing_layer = [m for m in modules if m not in LAYER_BY_MODULE]
    if missing_layer:
        print(f"  ::warning::以下模块缺 Layer 归属（不会建行/刷层位）：{missing_layer}")

    # Title 一律精确匹配模块路径（历史 `pkg @ v0.7.0` 形式仅兼容读取）
    by_title: dict[str, dict] = {}
    for it in proj["items"]:
        if not it["title"]:
            continue
        key = it["title"].split("@")[0].strip()
        by_title[key] = it

    updates = 0

    def upsert(title: str, latest: str, prior: str, parent: str, layer: str | None) -> None:
        nonlocal updates
        item = by_title.get(title)
        if not item:
            print(f"  + {title}  {latest}/{prior or '-'}  parent={parent or '-'}")
            updates += 1
            if not apply:
                return
            iid = add_draft(proj["id"], title, f"{parent} 嵌套模块" if parent else title)
            by_title[title] = {"id": iid, "fv": {}}
            item = by_title[title]
            if parent and parent_id:
                set_field(proj["id"], iid, parent_id, text=parent)
            if layer and layer_id and layer in layer_opts:
                set_field(proj["id"], iid, layer_id, option_id=layer_opts[layer])
            if latest:
                set_field(proj["id"], iid, latest_id, text=latest)
            if prior:
                set_field(proj["id"], iid, prior_id, text=prior)
            return

        cur_l = item["fv"].get("Latest")
        cur_p = item["fv"].get("Prior")
        cur_parent = item["fv"].get("Parent")
        cur_layer = item["fv"].get("Layer")
        need = False
        if cur_l != latest or (prior and cur_p != prior):
            need = True
        if parent_id and (cur_parent or "") != (parent or ""):
            need = True
        # 层位也要能刷新：原来只更 Latest/Prior，层位改了永远刷不上
        if layer_id and layer and layer in layer_opts and (cur_layer or "") != layer:
            need = True
        if not need:
            return
        print(f"  ~ {title}: {cur_l}/{cur_p} → {latest}/{prior or '-'}  parent={parent or '-'}")
        updates += 1
        if not apply:
            return
        if latest:
            set_field(proj["id"], item["id"], latest_id, text=latest)
        if prior:
            set_field(proj["id"], item["id"], prior_id, text=prior)
        if parent_id:
            set_field(proj["id"], item["id"], parent_id, text=parent or "")
        if layer_id and layer and layer in layer_opts and (cur_layer or "") != layer:
            set_field(proj["id"], item["id"], layer_id, option_id=layer_opts[layer])

    nested_by_repo: dict[str, list[str]] = {}
    for repo, path in nested:
        nested_by_repo.setdefault(repo, []).append(path)

    for mod in modules:
        tags = latest_tags(mod)
        if not tags:
            continue
        latest, prior = tags[0], tags[1] if len(tags) > 1 else ""
        parent_layer = LAYER_BY_MODULE.get(mod) or (by_title.get(mod) or {}).get("fv", {}).get("Layer")
        upsert(mod, latest, prior, parent="", layer=LAYER_BY_MODULE.get(mod))

        # 嵌套模块独立成行（Layer 继承主模块）
        paths = nested_by_repo.get(mod, [])
        if not paths:
            continue
        nt = nested_tags(mod)
        for path in paths:
            vers = nt.get(path, [])
            ntitle = f"{mod}/{path}"
            upsert(
                ntitle,
                vers[0] if vers else "",
                vers[1] if len(vers) > 1 else "",
                parent=mod,
                layer=parent_layer,
            )

    # ---- 陈旧行清理 ----
    # 判定：行标题不在真相源里 **且** 对应的仓/目录在 GitHub 上确实不存在。
    # 两个条件缺一不可——矩阵外的单元（伞仓 oekit / matrix-tools / .github 等）
    # 在 GitHub 上是存在的 ⇒ 一律不动，避免误删人工加的行。
    expected_titles = set(modules) | {f"{repo}/{path}" for repo, path in nested}
    for title in sorted(set(by_title) - expected_titles):
        if module_exists(title):
            continue
        item = by_title[title]
        print(f"  - {title}（矩阵真源已无此模块，仓/目录也已不存在）")
        updates += 1
        if apply:
            delete_item(proj["id"], item["id"])

    if not updates:
        print("  all up to date")
    if not apply and updates:
        print("  (dry-run)")


def sync_health_board(apply: bool) -> None:
    """矩阵健康：Install/Tag 并排对照 + Note 诊断 + Hygiene/Drift 汇总。"""
    print("\n=== #3 矩阵健康 ===")
    proj = load_project(PROJECT_HEALTH)
    hy_opts = opt_map(proj["fields"].get("Hygiene"))
    hy_id = proj["fields"]["Hygiene"]["id"]
    drift_id = proj["fields"].get("Drift", {}).get("id")
    install_id = proj["fields"].get("Install", {}).get("id")
    tag_id = proj["fields"].get("Tag", {}).get("id")
    note_id = proj["fields"].get("Note", {}).get("id")

    by_title = {it["title"]: it for it in proj["items"] if it["title"]}
    updates = 0
    modules, _nested, src = load_truth()
    print(f"  真相源：{src}（{len(modules)} 主模块）")
    for mod in modules:
        item = by_title.get(mod)
        if not item:
            print(f"  + create row {mod}")
            updates += 1
            if not apply:
                continue
            iid = add_draft(proj["id"], mod, f"{mod} 矩阵健康")
            item = {"id": iid, "fv": {}}
            by_title[mod] = item
        tags = latest_tags(mod)
        latest = tags[0] if tags else ""
        install = remote_install_line(mod) or ""
        if not latest and not install:
            want, drift = "待审计", 0
            note = "无 tag / 无安装行"
        elif latest and not install:
            want, drift = "待审计", 0
            note = f"tag={latest}，README 无 go get 安装行"
        elif latest and install and install != latest:
            want, drift = "有漂移", 1
            note = f"安装行 {install} ≠ 最新 tag {latest}"
        else:
            want, drift = "全绿", 0
            note = f"安装行 = tag = {latest}"

        cur = item["fv"].get("Hygiene")
        cur_drift = item["fv"].get("Drift")
        cur_install = item["fv"].get("Install") or ""
        cur_tag = item["fv"].get("Tag") or ""
        cur_note = item["fv"].get("Note") or ""
        try:
            cur_drift_n = float(cur_drift) if cur_drift is not None else None
        except (TypeError, ValueError):
            cur_drift_n = None
        if (
            cur != want
            or cur_drift_n != float(drift)
            or cur_install != install
            or cur_tag != latest
            or cur_note != note
        ):
            print(f"  ~ {mod}: {cur}/{cur_drift} → {want}/{drift}  install={install or '-'} tag={latest or '-'}")
            updates += 1
            if apply:
                if want in hy_opts:
                    set_field(proj["id"], item["id"], hy_id, option_id=hy_opts[want])
                if drift_id is not None:
                    set_field(proj["id"], item["id"], drift_id, number=float(drift))
                if install_id is not None:
                    set_field(proj["id"], item["id"], install_id, text=install)
                if tag_id is not None:
                    set_field(proj["id"], item["id"], tag_id, text=latest)
                if note_id is not None:
                    set_field(proj["id"], item["id"], note_id, text=note)
    if not updates:
        print("  all up to date")
    if not apply and updates:
        print("  (dry-run)")


# ---------------------------------------------------------------------------
# 即时入板（issue/PR 事件触发，单条快速路径）
# ---------------------------------------------------------------------------


def link_item(url: str, state: str) -> None:
    """把一条 issue/PR 立刻挂进对应大盘并写字段。state: open|closed|merged。"""
    repo, num, kind = parse_github_url(url)  # kind: issues|pulls
    if kind == "pulls":
        proj_num = PROJECT_PR
        want_review = infer_review(state)
    else:
        proj_num = PROJECT_ISSUE
        want_status = "Todo" if state.upper() == "OPEN" else "Done"

    # 取 title 便于 Kind/Scope
    meta = gh_json(["api", f"repos/{repo}/{kind}/{num}"])
    title = (meta or {}).get("title") or ""

    proj = load_project(proj_num)
    by_url = {i["url"]: i for i in proj["items"] if i["url"]}
    item = by_url.get(url)
    if not item:
        print(f"  + add {url}")
        iid = add_url(proj["id"], url)
    else:
        iid = item["id"]
        print(f"  = already on board {url}")

    if kind == "issues":
        status_opts = opt_map(proj["fields"].get("Status"))
        kind_opts = opt_map(proj["fields"].get("Kind"))
        mod_opts = opt_map(proj["fields"].get("Module"))
        want_kind = infer_kind(title)
        want_mod = infer_module(repo, title)
        if want_status in status_opts:
            set_field(proj["id"], iid, proj["fields"]["Status"]["id"], option_id=status_opts[want_status])
        if want_kind in kind_opts:
            set_field(proj["id"], iid, proj["fields"]["Kind"]["id"], option_id=kind_opts[want_kind])
        if want_mod in mod_opts:
            set_field(proj["id"], iid, proj["fields"]["Module"]["id"], option_id=mod_opts[want_mod])
        elif "meta" in mod_opts:
            set_field(proj["id"], iid, proj["fields"]["Module"]["id"], option_id=mod_opts["meta"])
        print(f"  ✓ issue  status={want_status} kind={want_kind} mod={want_mod}  {title[:50]}")
    else:
        review_opts = opt_map(proj["fields"].get("Review"))
        scope_opts = opt_map(proj["fields"].get("Scope"))
        want_review = infer_review(state)
        want_scope = infer_scope(title)
        if want_review in review_opts:
            set_field(proj["id"], iid, proj["fields"]["Review"]["id"], option_id=review_opts[want_review])
        if want_scope in scope_opts:
            set_field(proj["id"], iid, proj["fields"]["Scope"]["id"], option_id=scope_opts[want_scope])
        print(f"  ✓ pr     review={want_review} scope={want_scope}  {title[:50]}")


def refresh_module(mod: str) -> None:
    """事件驱动的单模块刷新：版本矩阵（含嵌套）+ 矩阵健康。

    tag 推送 / README 安装行变更后调用，不等全量轮询。
    不在 MODULES 里的仓（.github / matrix-tools 等治理仓）只吃 issue/PR 入板，不建版本行。
    """
    print(f"\n=== refresh {mod} ===")
    modules, nested_truth, src = load_truth()
    if mod not in modules:
        print(f"  skip (不在矩阵 MODULES: {src})")
        return
    root = latest_tags(mod)
    nested = nested_tags(mod)
    install = remote_install_line(mod) or ""

    # ---- 版本大盘 ----
    proj = load_project(PROJECT_VERSION)
    latest_id = proj["fields"]["Latest"]["id"]
    prior_id = proj["fields"]["Prior"]["id"]
    parent_id = proj["fields"].get("Parent", {}).get("id")
    layer_id = proj["fields"].get("Layer", {}).get("id")
    layer_opts = opt_map(proj["fields"].get("Layer"))
    by_title = {
        (it["title"] or "").split("@")[0].strip(): it
        for it in proj["items"]
        if it["title"]
    }

    def upsert_version(title: str, latest: str, prior: str, parent: str, layer: str | None) -> None:
        item = by_title.get(title)
        if not item:
            print(f"  + version {title}  {latest}/{prior or '-'}")
            iid = add_draft(proj["id"], title, f"{parent} 嵌套模块" if parent else title)
            by_title[title] = {"id": iid, "fv": {}}
            item = by_title[title]
            if parent and parent_id:
                set_field(proj["id"], iid, parent_id, text=parent)
            if layer and layer_id and layer in layer_opts:
                set_field(proj["id"], iid, layer_id, option_id=layer_opts[layer])
            if latest:
                set_field(proj["id"], iid, latest_id, text=latest)
            if prior:
                set_field(proj["id"], iid, prior_id, text=prior)
            return
        cur_l = item["fv"].get("Latest")
        cur_p = item["fv"].get("Prior")
        cur_layer = item["fv"].get("Layer")
        need = cur_l != latest or (bool(prior) and cur_p != prior)
        if layer_id and layer and layer in layer_opts and (cur_layer or "") != layer:
            need = True
        if not need:
            return
        print(f"  ~ version {title}: {cur_l}/{cur_p} → {latest}/{prior or '-'}")
        if latest:
            set_field(proj["id"], item["id"], latest_id, text=latest)
        if prior:
            set_field(proj["id"], item["id"], prior_id, text=prior)
        if layer_id and layer and layer in layer_opts and (cur_layer or "") != layer:
            set_field(proj["id"], item["id"], layer_id, option_id=layer_opts[layer])

    if root:
        latest, prior = root[0], root[1] if len(root) > 1 else ""
        upsert_version(mod, latest, prior, parent="", layer=LAYER_BY_MODULE.get(mod))
        parent_layer = LAYER_BY_MODULE.get(mod) or (by_title.get(mod) or {}).get("fv", {}).get("Layer")
        # 嵌套行按**真相源**列（不是扫 tag 发现的）：矩阵删掉的嵌套模块不该被复活
        for repo, path in nested_truth:
            if repo != mod:
                continue
            vers = nested.get(path, [])
            upsert_version(
                f"{mod}/{path}",
                vers[0] if vers else "",
                vers[1] if len(vers) > 1 else "",
                parent=mod,
                layer=parent_layer,
            )

    # tag 推送 = 一次「近期变更」⇒ 顺手把窗口滚到包含今天
    sync_pinned_view_filters(True)
    roll_recent_changes_view(True)

    # ---- 矩阵健康 ----
    hproj = load_project(PROJECT_HEALTH)
    hy_opts = opt_map(hproj["fields"].get("Hygiene"))
    hy_id = hproj["fields"]["Hygiene"]["id"]
    drift_id = hproj["fields"].get("Drift", {}).get("id")
    install_id = hproj["fields"].get("Install", {}).get("id")
    tag_id = hproj["fields"].get("Tag", {}).get("id")
    note_id = hproj["fields"].get("Note", {}).get("id")
    hitem = next((it for it in hproj["items"] if it["title"] == mod), None)
    if not hitem:
        print(f"  + health row {mod}")
        hitem = {"id": add_draft(hproj["id"], mod, f"{mod} 矩阵健康"), "fv": {}}

    latest = root[0] if root else ""
    if not latest and not install:
        want, drift, note = "待审计", 0, "无 tag / 无安装行"
    elif latest and not install:
        want, drift, note = "待审计", 0, f"tag={latest}，README 无 go get 安装行"
    elif latest and install and install != latest:
        want, drift, note = "有漂移", 1, f"安装行 {install} ≠ 最新 tag {latest}"
    else:
        want, drift, note = "全绿", 0, f"安装行 = tag = {latest}"

    if want in hy_opts:
        set_field(hproj["id"], hitem["id"], hy_id, option_id=hy_opts[want])
    if drift_id is not None:
        set_field(hproj["id"], hitem["id"], drift_id, number=float(drift))
    if install_id is not None:
        set_field(hproj["id"], hitem["id"], install_id, text=install)
    if tag_id is not None:
        set_field(hproj["id"], hitem["id"], tag_id, text=latest)
    if note_id is not None:
        set_field(hproj["id"], hitem["id"], note_id, text=note)
    print(f"  ✓ health  {want}  install={install or '-'} tag={latest or '-'}")


# ---------------------------------------------------------------------------


def selftest() -> int:
    """离线自测：嵌套 tag 解析 / 真相源装载 / Layer 覆盖 / 副本是否漂移。

    纯静态断言，**不触网**（CI 里没有 gh 凭证也能跑）。
    判据失效必须红——否则这道门禁只是摆设（与 board_query.py --selftest 同思路）。

    这里红起来最有价值的一条是「内嵌副本 vs 真相源」：2026-10-25 之前，
    手抄清单与 matrix.py 漂移（21 vs 24）却没有任何门禁会报，
    代价是版本大盘长期缺 3 行主模块 + 1 行嵌套模块。
    """
    fails: list[str] = []

    # 1) 嵌套 tag 解析：多层路径必须完整保留
    cases = [
        ("hlog/v0.1.4", ("hlog", "v0.1.4")),
        (
            "observability/tracing/instrumentation/kitexotel/v0.1.0",
            ("observability/tracing/instrumentation/kitexotel", "v0.1.0"),
        ),
        ("v0.26.0", None),            # 根 tag 不是嵌套 tag
        ("hlog/notaversion", None),   # 版本号不合规
    ]
    for tag, want in cases:
        got = parse_nested_tag(tag)
        if got != want:
            fails.append(f"parse_nested_tag({tag!r}) = {got!r}，期望 {want!r}")

    # 2) 真相源装载 + Layer 覆盖
    modules, nested, src = load_truth()
    if not modules:
        fails.append("真相源模块清单为空")
    for m in modules:
        if m not in LAYER_BY_MODULE:
            fails.append(f"模块 {m} 缺 Layer 归属（建行会没有层位）")
    for extra in LAYER_BY_MODULE:
        if extra not in modules:
            fails.append(f"Layer 表里的 {extra} 不在模块清单里")
    for repo, path in nested:
        if not path or path.startswith("/") or path.endswith("/") or "//" in path:
            fails.append(f"嵌套项 {repo}/{path} 的仓内路径不合法")
        if repo not in modules:
            fails.append(f"嵌套项 {repo}/{path} 的父仓不在模块清单里")

    # 3) 副本与真相源一致（漂移即红——本次修复的核心不变量）
    if modules != list(_FALLBACK_MODULES) or nested != list(_FALLBACK_NESTED):
        fails.append(
            f"内嵌副本与真相源 {src} 漂移"
            f"（副本 {len(_FALLBACK_MODULES)} 主/{len(_FALLBACK_NESTED)} 嵌套，"
            f"真相源 {len(modules)} 主/{len(nested)} 嵌套）——"
            "请把 _FALLBACK_MODULES / _FALLBACK_NESTED 更新到一致"
        )

    for f in fails:
        print(f"FAIL {f}")
    if fails:
        return 1
    print(f"selftest OK（真相源 {src}：{len(modules)} 主模块 / {len(nested)} 嵌套模块）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", type=int, choices=[1, 2, 3, 4], help="只跑某块大盘")
    ap.add_argument("--link", metavar="URL", help="即时把单条 issue/PR 挂进大盘")
    ap.add_argument("--state", default="open", help="--link 配套状态：open|closed|merged")
    ap.add_argument(
        "--refresh-module",
        metavar="NAME",
        help="事件驱动：刷新单模块的版本矩阵 + 矩阵健康（tag/README 变更后）",
    )
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="离线自测：嵌套 tag 解析 / 真相源装载 / Layer 覆盖（不触网）",
    )
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # 凭证前置检查——**所有**模式都要过这一关（连 dry-run 也会调 API）。
    # 放在最前面是有意的：没有凭证时任何一步都会以 exit 4 崩在 gh 的调用上，
    # 报错还落在最内层，看不出「是没凭证」这件真正的事。
    reason = no_credential_reason()
    if reason is not None:
        if is_dependabot_run():
            print(
                f"::warning::{reason}——跳过看板同步。"
                "依赖类 PR 不进人工审查队列是预期行为；看板对齐由每小时定时任务兜底。"
            )
            return 0
        print(
            f"::error::{reason}——看板同步需要 OEKIT_PROJECT_TOKEN"
            "（scope: project + read:org）"
        )
        return 1

    if args.link:
        link_item(args.link, args.state)
        print("\nOK")
        return 0

    if args.refresh_module:
        refresh_module(args.refresh_module)
        print("\nOK")
        return 0

    only = args.only
    if only in (None, 1):
        sync_issue_board(args.apply)
    if only in (None, 4):
        sync_pr_board(args.apply)
    if only in (None, 2):
        sync_version_board(args.apply)
        sync_pinned_view_filters(args.apply)
        roll_recent_changes_view(args.apply)
    if only in (None, 3):
        sync_health_board(args.apply)
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

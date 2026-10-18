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
  python3 scripts/sync-projects.py                   # dry-run

环境：
  GH_TOKEN   需 project + read:org
  ORG        默认 oekit
  MATRIX_BASE  可选，本地检出根（健康检查用；缺省只查远端 README）
"""
from __future__ import annotations

import argparse
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

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def gql(query: str, **variables: str) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        # Int! project number needs typed JSON; everything else stays string
        if k == "number" and str(v).isdigit():
            args += ["-F", f"{k}={v}"]
        else:
            args += ["-f", f"{k}={v}"]
    out = subprocess.check_output(args, text=True, stderr=subprocess.PIPE)
    data = json.loads(out)
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def gh_json(argv: list[str]) -> object:
    out = subprocess.check_output(["gh", *argv], text=True, stderr=subprocess.PIPE)
    return json.loads(out) if out.strip() else None


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

MODULES = [
    "pkg", "concurrency", "logx", "core", "metrics", "tracing", "registry",
    "data", "gormx", "redisx", "httpx", "network", "timertask", "middleware",
    "nns", "nns-endpoint", "bootstrap", "hertz", "kitex", "workflow",
    "kitex-examples",
]

# 主模块默认 Layer（新行落板时用；嵌套行继承父级）
LAYER_BY_MODULE = {
    "pkg": "基础", "concurrency": "基础", "logx": "基础", "core": "基础",
    "metrics": "基础", "tracing": "基础",
    "registry": "治理", "nns": "治理", "nns-endpoint": "治理",
    "data": "数据", "gormx": "数据", "redisx": "数据", "timertask": "数据",
    "httpx": "网络", "network": "网络",
    "middleware": "服务", "bootstrap": "服务", "hertz": "服务", "kitex": "服务",
    "workflow": "工具", "kitex-examples": "示例",
}


def latest_tags(repo: str) -> list[str]:
    """Return recent root tags, newest first (vX.Y.Z only)."""
    raw = subprocess.check_output(
        ["gh", "api", f"repos/{ORG}/{repo}/tags?per_page=100", "--jq", "[.[].name]"],
        text=True,
    )
    tags = json.loads(raw)
    root = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return root


def nested_tags(repo: str) -> dict[str, list[str]]:
    """嵌套模块 tag：path/vX.Y.Z → {path: [ver, ...] 新→旧}。"""
    raw = subprocess.check_output(
        ["gh", "api", f"repos/{ORG}/{repo}/tags?per_page=100", "--jq", "[.[].name]"],
        text=True,
    )
    tags = json.loads(raw)
    out: dict[str, list[str]] = {}
    for t in tags:
        if "/" not in t:
            continue
        path, ver = t.split("/", 1)
        if re.fullmatch(r"v\d+\.\d+\.\d+", ver):
            out.setdefault(path, []).append(ver)

    def key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.lstrip("v").split("."))

    for p in out:
        out[p].sort(key=key, reverse=True)
    return out


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
    """
    print("\n=== #2 版本大盘 ===")
    proj = load_project(PROJECT_VERSION)
    latest_id = proj["fields"]["Latest"]["id"]
    prior_id = proj["fields"]["Prior"]["id"]
    parent_id = proj["fields"].get("Parent", {}).get("id")
    layer_id = proj["fields"].get("Layer", {}).get("id")
    layer_opts = opt_map(proj["fields"].get("Layer"))

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
        need = False
        if cur_l != latest or (prior and cur_p != prior):
            need = True
        if parent_id and (cur_parent or "") != (parent or ""):
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

    for mod in MODULES:
        tags = latest_tags(mod)
        if not tags:
            continue
        latest, prior = tags[0], tags[1] if len(tags) > 1 else ""
        upsert(mod, latest, prior, parent="", layer=LAYER_BY_MODULE.get(mod))

        # 嵌套模块独立成行（Layer 继承主模块）
        parent_layer = (by_title.get(mod) or {}).get("fv", {}).get("Layer")
        for path, vers in sorted(nested_tags(mod).items()):
            ntitle = f"{mod}/{path}"
            n_latest = vers[0] if vers else ""
            n_prior = vers[1] if len(vers) > 1 else ""
            upsert(ntitle, n_latest, n_prior, parent=mod, layer=parent_layer)

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
    for mod in MODULES:
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


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", type=int, choices=[1, 2, 3, 4], help="只跑某块大盘")
    ap.add_argument("--link", metavar="URL", help="即时把单条 issue/PR 挂进大盘")
    ap.add_argument("--state", default="open", help="--link 配套状态：open|closed|merged")
    args = ap.parse_args()

    if args.link:
        link_item(args.link, args.state)
        print("\nOK")
        return 0

    only = args.only
    if only in (None, 1):
        sync_issue_board(args.apply)
    if only in (None, 4):
        sync_pr_board(args.apply)
    if only in (None, 2):
        sync_version_board(args.apply)
    if only in (None, 3):
        sync_health_board(args.apply)
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

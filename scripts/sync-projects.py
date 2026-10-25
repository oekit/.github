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

# 钉死的视图配置：项目号 -> {视图名: {"filter": ..., "columns": [字段名, ...]}}
#
# 为什么要有这层：视图的**过滤器**与**可见列**是公开 GraphQL 写得下去的
# （updateProjectV2View 的 filter / configuration{visibleFieldIds}）⇒ 可以脚本自愈，
# UI 上被人手改乱了会被下一轮同步改回来。
#
# 但**分组**（表格视图的 Group by / 看板视图的 Column by）写不了：
# UpdateProjectV2ViewInput 里没有 groupBy 字段 ⇒ 分组只能靠 UI 落盘，
# 脚本这边只能**声明期望值**（见 PINNED_GROUPS），漂移时告警。
#
# 「嵌套模块」= Parent 非空的那些行。
# 旧值 `nested:*` 里的 nested 不是 Projects DSL 的字段名 ⇒ 该视图恒为 0 行。
PINNED_VIEWS: dict[int, dict[str, dict]] = {
    PROJECT_VERSION: {
        "嵌套模块": {"filter": "parent:*"},
    },
    PROJECT_ISSUE: {
        "总览": {"filter": "", "columns": ["Title", "Status", "Kind", "Module", "Created"]},
        "开启": {"filter": "status:Todo", "columns": ["Title", "Kind", "Module", "Created"]},
        "关闭": {"filter": "status:Done", "columns": ["Title", "Kind", "Module", "Closed"]},
        "看板": {"filter": "", "columns": ["Title", "Status", "Kind", "Module"]},
        # 分组视图必须**看得见分组列**（按模块就要有 Module，按类型就要有 Kind）
        "按模块": {"filter": "", "columns": ["Title", "Module", "Status", "Created"]},
        "按类型": {"filter": "", "columns": ["Title", "Kind", "Module", "Created"]},
    },
    PROJECT_PR: {
        "总览": {"filter": "", "columns": ["Title", "Review", "Scope", "Updated"]},
        "待审": {"filter": 'review:"待审"', "columns": ["Title", "Scope", "Updated"]},
        "审阅中": {"filter": 'review:"审阅中"', "columns": ["Title", "Scope", "Updated"]},
        "可合入": {"filter": 'review:"可合入"', "columns": ["Title", "Scope", "Updated"]},
        "已合入": {"filter": 'review:"已合入"', "columns": ["Title", "Scope", "Closed"]},
        "已关闭": {"filter": 'review:"已关闭"', "columns": ["Title", "Scope", "Closed"]},
        "按 Scope": {"filter": "", "columns": ["Title", "Scope", "Review", "Updated"]},
        "看板": {"filter": "", "columns": ["Title", "Review", "Scope"]},
    },
}

# 分组的期望值（Group by / Column by）——**只声明、只告警**，写不了。
# 格式：项目号 -> {视图名: 分组字段名}
PINNED_GROUPS: dict[int, dict[str, str]] = {
    PROJECT_ISSUE: {
        "看板": "Kind",
        "按模块": "Module",
        "按类型": "Kind",
    },
    PROJECT_PR: {
        "看板": "Review",
        "按 Scope": "Scope",
    },
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
                        ... on Issue { number url state title createdAt
                                       repository { nameWithOwner } }
                        ... on PullRequest { number url state title createdAt
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
                    "created": content.get("createdAt"),
                    "body": content.get("body"),
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


def set_field_safe(pid: str, item_id: str, field_id: str, **kw) -> bool:
    """写字段，失败只告警不抛错。

    为什么需要「安全版」：**并发重建**时另一轮同步可能正在处理同一批行，
    其中一部分已经被删掉（行重建 = 全删 + 按序重加）⇒ 写字段会 404。
    这种竞态不该把整轮同步打成红；写漏的字段下一轮会被 diff 出来补上（自愈）。

    返回 True=写成功 / False=失败（调用方可统计）。
    """
    try:
        set_field(pid, item_id, field_id, **kw)
        return True
    except Exception as exc:
        print(f"  ! 写字段失败（item {item_id}）：{str(exc)[:200]}")
        return False


def add_url_safe(pid: str, url: str) -> str | None:
    """把一条 issue/PR 加进看板；失败返回 None（不抛错）。

    为什么需要「安全版」：**行重建被打断**后，板上一部分行已经是新的，
    下一轮同步再按快照把它们当「缺失」去加 ⇒ GitHub 回 "already added"
    ⇒ 不容错的话整轮同步会被这一个错误打死（2026-10-25 实测：
    #4 的行序重建被整点同步取消后，重建残留的行让下一轮同步连挂）。
    """
    try:
        return add_url(pid, url)
    except Exception as exc:
        print(f"  ! 入板失败 {url}：{str(exc)[:200]}")
        return None


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
# 行序维护（#1 Issue 大盘 / #4 PR 审查队列）
# ---------------------------------------------------------------------------
#
# 为什么要单独一节：Projects v2 的行序 = **行的创建序**（items 数组按创建序返回），
# 而公开 API 能写的东西里**没有 Sort by**（实测面板里也存不住），新行又只能追加到
# 末尾 ⇒ 想让「最新的在最上面」，唯一手段是**按目标顺序把行重建**（全删 + 按序重加）。
#
# 代价是硬约束：每行 ≈ 1 删 + 1 加 + N 个推导列写。行数一多（#4 有 1100+ 行），
# 一次重建就能把 GraphQL 的每小时 5000 点配额打穿 ⇒
#   * 行数 ≤ REORDER_MAX_AUTO_ROWS：漂移时**自动重建**（自愈）；
#   * 行数 >  阈值：只告警 + 提示 --reorder，不自动烧配额。
#
# 中断不会丢数据：重建是「先全删、再按序重加」，中途挂掉时缺行会被下一轮同步的
# adds 路径补回；顺序漂移也会被下一轮漂移检测再次发现并重建 ⇒ 幂等、可自愈。

REORDER_MAX_AUTO_ROWS = 500

# 推导列之外、**不写**的字段：Title 是内置标题列（跟着内容走）；
# Status 是 GitHub 自动同步的默认字段（open→Todo / closed→Done），
# 实测 #4 的脚本从写 Status，但板上每一行都有 Status ⇒ 它是自动的。
AUTO_FIELDS = ("Title", "Status")


def _field_writes(fields_meta: dict, pairs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    """[(字段名, 值名)] -> [(field_id, option_id)]；模板里没有的字段/值直接跳过。"""
    out: list[tuple[str, str]] = []
    for fname, value in pairs:
        meta = fields_meta.get(fname)
        if not meta or value is None:
            continue
        opts = {o["name"]: o["id"] for o in meta.get("options", [])}
        if value in opts:
            out.append((meta["id"], opts[value]))
    return out


def build_row_plan(
    proj: dict,
    org_items: list[dict],
    want_fields: dict[str, list[tuple[str, str | None]]],
) -> list[dict]:
    """合成「规范序」的行计划。

    org_items    —— org 侧权威清单（url / title / createdAt）。
    want_fields  —— url -> [(字段名, 目标值)]，只列**推导列**（Status 自动同步，不写）。

    板上存在、但 org 清单里没有的行（草稿 SOP 行、跨仓 PR）**原样保留**：
    草稿行钉在最前面，其余按 createdAt 倒序。
    """
    by_url = {it["url"]: it for it in proj["items"] if it.get("url")}
    fields_meta = proj["fields"]
    org_urls: set[str] = set()
    plan: list[dict] = []

    for o in org_items:
        url = o["url"]
        org_urls.add(url)
        board = by_url.get(url)
        plan.append(
            {
                "url": url,
                "title": o.get("title") or (board or {}).get("title") or "",
                "created": o.get("createdAt") or (board or {}).get("created") or "",
                "draft": False,
                "body": "",
                "fields": _field_writes(fields_meta, want_fields.get(url, [])),
            }
        )

    for it in proj["items"]:
        if it.get("url") in org_urls:
            continue
        fv = {k: v for k, v in (it.get("fv") or {}).items() if k not in AUTO_FIELDS}
        plan.append(
            {
                "url": it.get("url"),
                "title": it.get("title") or "",
                "created": it.get("created") or "",
                "draft": not it.get("url"),
                "body": it.get("body") or "",
                "fields": _field_writes(
                    fields_meta, [(k, v) for k, v in fv.items() if isinstance(v, str)]
                ),
            }
        )

    drafts = [p for p in plan if p["draft"]]
    rest = sorted(
        [p for p in plan if not p["draft"]], key=lambda p: p["created"], reverse=True
    )
    return drafts + rest


def row_order_drift(proj: dict, plan: list[dict]) -> bool:
    """板上的实际行序 vs 规范序——不一致即为漂移（需要重建）。"""
    cur = [it.get("url") for it in proj["items"] if it.get("url")]
    want = [p["url"] for p in plan if not p["draft"]]
    return cur != want


def rebuild_rows(pid: str, board_items: list[dict], plan: list[dict]) -> tuple[int, int]:
    """按 plan 重建看板行：先全删，再按序重加，最后回填推导列。

    返回 (成功行数, 跳过行数)。跳过 = 内容已被删掉（重加时 404），
    这类行本来就不该留在板上，跳过不影响其余行的顺序。
    """
    for it in board_items:
        delete_item(pid, it["id"])
    ok = 0
    skipped = 0
    for row in plan:
        try:
            if row["draft"]:
                iid = add_draft(pid, row["title"], row["body"])
            else:
                iid = add_url(pid, row["url"])
        except Exception as exc:
            skipped += 1
            print(f"  ! 重建跳过 {row.get('url') or row.get('title')}：{exc}")
            continue
        for fid, oid in row["fields"]:
            set_field_safe(pid, iid, fid, option_id=oid)
        ok += 1
    return ok, skipped


def maintain_row_order(
    proj: dict,
    plan: list[dict],
    apply: bool,
    force: bool = False,
) -> bool:
    """行序自愈入口：漂移 ⇒ 重建。返回是否真的重建了。"""
    if not row_order_drift(proj, plan):
        return False
    n = len(proj["items"])
    if not (force or n <= REORDER_MAX_AUTO_ROWS):
        print(
            f"  ! 行序漂移（{n} 行 > 自动重建阈值 {REORDER_MAX_AUTO_ROWS}）——"
            "不自动重建以免打穿配额；需要时手工跑 --reorder"
        )
        return False
    if not apply:
        print(f"  ! 行序漂移：将按 createdAt 倒序重建 {n} 行（dry-run，未写入）")
        return False
    print(f"  ! 行序漂移：按 createdAt 倒序重建 {n} 行")
    ok, skipped = rebuild_rows(proj["id"], proj["items"], plan)
    print(f"  ✓ 重建完成：{ok} 行重排（跳过 {skipped}）")
    return True


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
                "--limit", "1000",
                # 按创建时间倒序：板子的行序 = 入板顺序 ⇒ 新进的 issue 必须排在最前，
                # 否则板上永远是「最早入板的那批」在最上面（2026-10-25 实测：
                # 总览第一屏全是两年前的旧 issue，新的全堆在末尾）
                "--sort", "created",
                "--order", "desc",
                "--json", "repository,title,number,url,state,createdAt",
            ],
            text=True,
        )
        batch = json.loads(raw)
        if len(batch) >= 1000:
            print(f"  ! search issues state={state} hit limit 1000，可能被截断", file=sys.stderr)
        for i in batch:
            if i["url"] in seen or "/pull/" in i["url"]:
                continue
            seen.add(i["url"])
            issues.append(i)
    return issues


def sync_issue_board(apply: bool, force_reorder: bool = False) -> None:
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
        cur_status = (by_url.get(iss["url"]) or {}).get("fv", {}).get("Status")
        want_status = want_issue_status(iss["state"], cur_status)
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

    # —— 行序：规范序 = createdAt 倒序（新的在最前）——
    # 漂移时走重建路径（重建里已包含新增行 + 推导列回填，不必再走增量）。
    want_fields = {
        iss["url"]: [
            ("Kind", infer_kind(iss["title"])),
            ("Module", infer_module(iss.get("repository", ""), iss["title"])),
        ]
        for iss in issues
    }
    plan = build_row_plan(proj, issues, want_fields)
    if maintain_row_order(proj, plan, apply, force=force_reorder):
        return

    if not apply:
        print("  (dry-run)")
        return

    def assign(iid: str, status: str, kind: str | None, mod: str | None) -> None:
        if status in status_opts:
            set_field_safe(proj["id"], iid, status_id, option_id=status_opts[status])
        if kind and kind in kind_opts and kind_id:
            set_field_safe(proj["id"], iid, kind_id, option_id=kind_opts[kind])
        if mod and mod in mod_opts and mod_id:
            set_field_safe(proj["id"], iid, mod_id, option_id=mod_opts[mod])
        elif mod and mod_id:
            # 模板里没有的模块名，落 meta，避免整列空着
            if "meta" in mod_opts:
                set_field_safe(proj["id"], iid, mod_id, option_id=mod_opts["meta"])

    for a in adds:
        iid = add_url_safe(proj["id"], a["url"])
        if not iid:
            continue
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


# Review 字段的**人工流转态**：PR 还开着时，人会在板上把它挪进这几档。
MANUAL_REVIEW_STATES = ("审阅中", "需修改", "可合入")

# Status 字段的人工态：issue 还开着时，人可以在板上标 In Progress。
MANUAL_STATUS_STATES = ("In Progress",)


def want_issue_status(issue_state: str, cur: str | None) -> str:
    """Status 该取什么值——和 want_review 同一套「脚本 / 人工分权」。

    closed ⇒ Done（终态，脚本说了算）；open ⇒ 人工挪到 In Progress 的不许被冲掉，
    其余落 Todo。
    """
    if (issue_state or "").upper() == "CLOSED":
        return "Done"
    if cur in MANUAL_STATUS_STATES:
        return cur
    return "Todo"


def want_review(pr_state: str, cur: str | None) -> str:
    """Review 该取什么值——分清「脚本说了算」与「人工说了算」。

    - PR 落到终态（merged / closed）⇒ 脚本说了算：已合入 / 已关闭；
    - PR 还开着 ⇒ 人工在板上流转（审阅中 / 需修改 / 可合入），**脚本不能冲掉**；
      只有还没人动过（当前为空）时才落 待审。

    为什么必须这么分：旧实现对**每一行**都写 infer_review(state)
    ⇒ 人工把 PR 挪到「审阅中」，每小时的定时同步就把它打回「待审」，
    人工流转被脚本吃掉（Review 字段有 6 档，脚本只会写 3 档，
    另外 3 档正是人工流转用的）。
    """
    s = (pr_state or "").upper()
    if s in ("MERGED", "CLOSED"):
        return infer_review(s)
    if cur in MANUAL_REVIEW_STATES:
        return cur
    return "待审"


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
                "--limit", "1000",
                # 按创建时间倒序：板子的行序 = 入板顺序 ⇒ 新进的 PR 排在最前
                "--sort", "created",
                "--order", "desc",
                "--json", "repository,title,number,url,state,createdAt",
            ],
            text=True,
        )
        batch = json.loads(raw)
        if len(batch) >= 1000:
            print(f"  ! search prs state={state} hit limit 1000，可能被截断", file=sys.stderr)
        for i in batch:
            if i["url"] in seen:
                continue
            seen.add(i["url"])
            prs.append(i)
    return prs


def sync_pr_board(apply: bool, force_reorder: bool = False) -> None:
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
        cur_review = (by_url.get(pr["url"]) or {}).get("fv", {}).get("Review")
        want_rev = want_review(pr["state"], cur_review)
        want_scope = infer_scope(pr["title"])
        if pr["url"] not in by_url:
            adds.append({**pr, "want_review": want_rev, "want_scope": want_scope})
            continue
        item = by_url[pr["url"]]
        cur_scope = item["fv"].get("Scope")
        # Review：终态由脚本说了算，在途的人工流转态由人工说了算；Scope 是推导列，跟标题走
        if cur_review != want_rev or (want_scope and cur_scope != want_scope):
            syncs.append(
                {
                    "id": item["id"],
                    "title": pr["title"],
                    "review": want_rev,
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

    # —— 行序：规范序 = createdAt 倒序（新的在最前）——
    # #4 有 1100+ 行 ⇒ 一次重建 ≈ 4400 次写，**超过自动阈值**：
    # 漂移时只告警并提示 --reorder（避免每小时定时任务把配额打穿）。
    want_fields = {
        pr["url"]: [
            (
                "Review",
                want_review(
                    pr["state"], (by_url.get(pr["url"]) or {}).get("fv", {}).get("Review")
                ),
            ),
            ("Scope", infer_scope(pr["title"])),
        ]
        for pr in prs
    }
    plan = build_row_plan(proj, prs, want_fields)
    if maintain_row_order(proj, plan, apply, force=force_reorder):
        return

    if not apply:
        print("  (dry-run)")
        return

    def assign(iid: str, review: str, scope: str | None) -> None:
        if review in review_opts:
            set_field_safe(proj["id"], iid, review_id, option_id=review_opts[review])
        if scope and scope in scope_opts and scope_id:
            set_field_safe(proj["id"], iid, scope_id, option_id=scope_opts[scope])

    for a in adds:
        iid = add_url_safe(proj["id"], a["url"])
        if not iid:
            continue
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


def _view_config(number: int) -> tuple[str, list[dict], dict[str, dict]]:
    """返回 (project id, 视图列表（含可见列）, 字段表 name->field)。

    视图 id 会变，故按**名字**定位，不写死 id。
    注意：读 `ProjectV2View.configuration` 会触发 GitHub 服务端错误，
    所以列要从 `ProjectV2View.fields` 读。
    """
    data = gql(
        """
        query($org: String!, $number: Int!) {
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
              views(first: 20) {
                nodes {
                  id
                  name
                  filter
                  layout
                  fields(first: 20) {
                    nodes {
                      ... on ProjectV2Field { id name }
                      ... on ProjectV2SingleSelectField { id name }
                    }
                  }
                }
              }
            }
          }
        }
        """,
        org=ORG,
        number=str(number),
    )
    proj = data["organization"]["projectV2"]
    fields = {f["name"]: f for f in proj["fields"]["nodes"]}
    views = [
        {
            "id": v["id"],
            "name": v.get("name"),
            "filter": v.get("filter") or "",
            "layout": v.get("layout"),
            "columns": [n.get("name") for n in v.get("fields", {}).get("nodes", [])],
        }
        for v in proj["views"]["nodes"]
    ]
    return proj["id"], views, fields


def project_views(number: int) -> tuple[str, list[dict]]:
    """返回 (project id, 视图列表)。视图 id 会变，故按**名字**定位，不写死 id。"""
    pid, views, _fields = _view_config(number)
    return pid, views


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


def sync_pinned_view_configs(apply: bool) -> None:
    """把视图的过滤器与可见列钉死（#1 / #2 / #4）。

    过滤器：语义型视图的值固定（如「嵌套模块」= `parent:*`），写错会让视图恒为
    0 行，而且看板上不报错——只能靠这道钉子兜住。
    旧值 `nested:*` 里的 nested 是个**不存在的字段名**（Projects DSL 里没有 nested）
    ⇒ 「嵌套模块」视图实测恒为空。

    可见列：列序 = 视图里字段的顺序，钉死它可防 UI 上被人手拖乱。

    分组不在这里：Group by / Column by **写不了**（见 PINNED_GROUPS 的说明），
    只在这里声明期望值、漂移时告警。
    """
    for number, spec in PINNED_VIEWS.items():
        try:
            _pid, views, fields = _view_config(number)
        except Exception as exc:
            print(f"  ! 读取 #{number} 视图配置失败（{exc}）——跳过")
            continue
        by_name = {v["name"]: v for v in views}
        for name, want in spec.items():
            v = by_name.get(name)
            if not v:
                print(f"  ! #{number} 视图「{name}」不存在——跳过")
                continue
            want_filter = want.get("filter")
            if want_filter is not None and (v.get("filter") or "") != want_filter:
                print(f"  ~ #{number} {name} 过滤器：{v.get('filter') or '(空)'} → {want_filter}")
                if apply:
                    _write_view_filter(v["id"], want_filter)
            want_cols = want.get("columns")
            if want_cols:
                want_ids = [fields[c]["id"] for c in want_cols if c in fields]
                cur_ids = [fields[c]["id"] for c in v["columns"] if c in fields]
                # 比**集合**而不是序列：实测 visibleFieldIds 写进去的顺序会被 GitHub
                # 按「项目字段顺序」归一（写 [Kind,Title,Module,Status,Created] 读回来
                # 仍是 Title|Status|Created|Kind|Module）⇒ 按序列比对会导致每轮同步
                # 都判定「列不一样」而重写（噪音，且会冲掉 UI 里拖出来的列序）。
                if sorted(cur_ids) != sorted(want_ids):
                    print(f"  ~ #{number} {name} 列：{v['columns']} → {want_cols}")
                    if apply:
                        _write_view_columns(v["id"], want_ids)


def _write_view_columns(view_id: str, field_ids: list[str]) -> None:
    """写视图可见列（顺序即列序）；失败只告警不抛错（视图配置挂了不该拖垮看板同步）。"""
    ids = ", ".join(f'"{i}"' for i in field_ids)
    try:
        gql(
            f"""
            mutation {{
              updateProjectV2View(input: {{
                viewId: "{view_id}",
                configuration: {{ visibleFieldIds: [{ids}] }}
              }}) {{ projectV2View {{ id name }} }}
            }}
            """
        )
    except Exception as exc:
        print(f"  ! 写视图可见列失败（{exc}）——看板数据不受影响，继续")


def check_pinned_groups(apply: bool = False) -> None:
    """分组（Group by / Column by）只能手点，脚本只能**声明期望值**。

    读分组要靠 GraphQL 的 groupByFields —— 实测该字段在所有板上都返回空
    （Projects v2 的公开 API 不暴露视图的分组）⇒ 这里**做不了自动校验**，
    只把期望值打印出来供人工对照（README 里也写了同样的表）。
    apply=True 时不打印，避免每次定时同步都刷一堆没有信息量的日志。
    """
    if apply:
        return
    for number, groups in PINNED_GROUPS.items():
        for name, field in groups.items():
            print(f"  · #{number} 视图「{name}」期望分组：{field}（只能 UI 落盘，脚本无法校验）")


def _write_view_filter(view_id: str, want: str) -> None:
    """写视图过滤器；失败只告警不抛错（视图配置挂了不该拖垮看板同步）。

    过滤器值里**可能带双引号**（单选字段的 DSL 就是 `review:"待审"` 这种写法）
    ⇒ 必须走 **GraphQL 变量**，把值拼进 query 字符串会打成非法语法：
    实测 2026-10-25，#4 的「可合入 / 已关闭」两个视图的过滤器就是这么写失败的
    （报错被截断成 "写视图过滤器失败"，差点被当成配额问题）。
    """
    try:
        gql(
            """
            mutation($filter: String!) {
              updateProjectV2View(input: {
                viewId: "%s", filter: $filter
              }) { projectV2View { id name filter } }
            }
            """
            % view_id,
            filter=want,
        )
    except Exception as exc:
        print(f"  ! 写视图过滤器失败（{exc}）——看板数据不受影响，继续")


def add_draft(pid: str, title: str, body: str = "") -> str:
    """建一行草稿（看板里的手工行）。

    正文与标题走 **GraphQL 变量**：GraphQL 的字符串字面量里不能有裸换行，
    拼进 query 字符串会让整个 mutation 变成 "Expected string or block string,
    but it was malformed" ⇒ 草稿行**建不出来**（2026-10-25 实测：
    #4 的行序重建跑完后，手工行没回来；根因就是这里没转义换行）。
    """
    data = gql(
        """
        mutation($title: String!, $body: String) {
          addProjectV2DraftIssue(input: {
            projectId: "%s", title: $title, body: $body
          }) { projectItem { id } }
        }
        """
        % pid,
        title=title,
        body=body or "",
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

    def upsert(
        title: str,
        latest: str,
        prior: str,
        parent: str,
        layer: str | None,
        order: float | None = None,
    ) -> None:
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
            if order is not None and order_id:
                set_field(proj["id"], iid, order_id, number=order)
            return

        cur_l = item["fv"].get("Latest")
        cur_p = item["fv"].get("Prior")
        cur_parent = item["fv"].get("Parent")
        cur_layer = item["fv"].get("Layer")
        cur_order = item["fv"].get("序号")
        need = False
        if cur_l != latest or (prior and cur_p != prior):
            need = True
        if parent_id and (cur_parent or "") != (parent or ""):
            need = True
        # 层位也要能刷新：原来只更 Latest/Prior，层位改了永远刷不上
        if layer_id and layer and layer in layer_opts and (cur_layer or "") != layer:
            need = True
        # 序号（依赖位次）也要能刷新：新增/重排模块后位次会变
        if order is not None and order_id:
            try:
                cur_order_n = float(cur_order) if cur_order is not None else None
            except (TypeError, ValueError):
                cur_order_n = None
            if cur_order_n != order:
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
        if order is not None and order_id:
            try:
                cur_order_n = float(cur_order) if cur_order is not None else None
            except (TypeError, ValueError):
                cur_order_n = None
            if cur_order_n != order:
                set_field(proj["id"], item["id"], order_id, number=order)

    nested_by_repo: dict[str, list[str]] = {}
    for repo, path in nested:
        nested_by_repo.setdefault(repo, []).append(path)

    # 「序号」= 依赖自底向上的位次（主模块 ×10，嵌套模块跟在父级后 1..9）。
    # 为什么要有这个字段：Projects 的**行序不是创建序**——删行+重建后位置会乱，
    # 新模块也只能追加到末尾（这正是 kitx/etcdx/k8s 当初排在板子末尾的原因）。
    # ⇒ 用一个数值字段承载位次，再在视图里按它排序，顺序才既正确又能长期自愈。
    order_id = proj["fields"].get("序号", {}).get("id")

    for mod_idx, mod in enumerate(modules):
        tags = latest_tags(mod)
        if not tags:
            continue
        latest, prior = tags[0], tags[1] if len(tags) > 1 else ""
        parent_layer = LAYER_BY_MODULE.get(mod) or (by_title.get(mod) or {}).get("fv", {}).get("Layer")
        upsert(
            mod,
            latest,
            prior,
            parent="",
            layer=LAYER_BY_MODULE.get(mod),
            order=float(mod_idx * 10),
        )

        # 嵌套模块独立成行（Layer 继承主模块）
        paths = nested_by_repo.get(mod, [])
        if not paths:
            continue
        nt = nested_tags(mod)
        for j, path in enumerate(paths):
            vers = nt.get(path, [])
            ntitle = f"{mod}/{path}"
            upsert(
                ntitle,
                vers[0] if vers else "",
                vers[1] if len(vers) > 1 else "",
                parent=mod,
                layer=parent_layer,
                order=float(mod_idx * 10 + j + 1),
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
    """矩阵健康：Install/Tag 并排对照 + Note 诊断 + Hygiene/Drift 汇总 + 依赖位次。"""
    print("\n=== #3 矩阵健康 ===")
    proj = load_project(PROJECT_HEALTH)
    hy_opts = opt_map(proj["fields"].get("Hygiene"))
    hy_id = proj["fields"]["Hygiene"]["id"]
    drift_id = proj["fields"].get("Drift", {}).get("id")
    install_id = proj["fields"].get("Install", {}).get("id")
    tag_id = proj["fields"].get("Tag", {}).get("id")
    note_id = proj["fields"].get("Note", {}).get("id")
    # 「序号」= 依赖位次，与 #2 版本大盘同一口径（主模块 ×10）
    order_id = proj["fields"].get("序号", {}).get("id")

    by_title = {it["title"]: it for it in proj["items"] if it["title"]}
    updates = 0
    modules, _nested, src = load_truth()
    print(f"  真相源：{src}（{len(modules)} 主模块）")
    for mod_idx, mod in enumerate(modules):
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
        cur_order = item["fv"].get("序号")
        try:
            cur_drift_n = float(cur_drift) if cur_drift is not None else None
        except (TypeError, ValueError):
            cur_drift_n = None
        try:
            cur_order_n = float(cur_order) if cur_order is not None else None
        except (TypeError, ValueError):
            cur_order_n = None
        if (
            cur != want
            or cur_drift_n != float(drift)
            or cur_install != install
            or cur_tag != latest
            or cur_note != note
            or (order_id is not None and cur_order_n != float(mod_idx * 10))
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
                if order_id is not None and cur_order_n != float(mod_idx * 10):
                    set_field(proj["id"], item["id"], order_id, number=float(mod_idx * 10))

    # ---- 陈旧行清理（健康大盘是**模块口径**）----
    # 为什么按模块口径清：这块板子的四个字段（Tag/Install/Drift/Hygiene）判的是
    # 「README 安装行 == 最新 tag」，与 matrix-tools/scripts/audit.py 的安装行门禁
    # **同一口径**（那边也是 `for repo in MODULES:`）。
    # 伞仓 / 站点 / 治理仓（oekit / oekit.github.io / .github）不是矩阵模块，
    # 对它们做不出这个判定，且脚本也不刷新它们 ⇒ 永远卡在「待审计 / 无 tag / 无安装行」
    # ⇒ 只是噪音，还会把「待审计」视图刷成 3 条假待办 ⇒ 清掉。
    for title in sorted(set(by_title) - set(modules)):
        item = by_title[title]
        print(f"  - {title}（非矩阵模块，健康大盘按模块口径）")
        updates += 1
        if apply:
            delete_item(proj["id"], item["id"])

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
    else:
        proj_num = PROJECT_ISSUE

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
        cur_status = (item or {}).get("fv", {}).get("Status")
        want_status = want_issue_status(state, cur_status)
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
        cur_review = (item or {}).get("fv", {}).get("Review")
        want_rev = want_review(state, cur_review)
        want_scope = infer_scope(title)
        if want_rev in review_opts:
            set_field(proj["id"], iid, proj["fields"]["Review"]["id"], option_id=review_opts[want_rev])
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
    order_id = proj["fields"].get("序号", {}).get("id")
    by_title = {
        (it["title"] or "").split("@")[0].strip(): it
        for it in proj["items"]
        if it["title"]
    }

    def upsert_version(
        title: str,
        latest: str,
        prior: str,
        parent: str,
        layer: str | None,
        order: float | None = None,
    ) -> None:
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
            if order is not None and order_id:
                set_field(proj["id"], iid, order_id, number=order)
            return
        cur_l = item["fv"].get("Latest")
        cur_p = item["fv"].get("Prior")
        cur_layer = item["fv"].get("Layer")
        cur_order = item["fv"].get("序号")
        need = cur_l != latest or (bool(prior) and cur_p != prior)
        if layer_id and layer and layer in layer_opts and (cur_layer or "") != layer:
            need = True
        order_n = None
        if order is not None and order_id:
            try:
                order_n = float(cur_order) if cur_order is not None else None
            except (TypeError, ValueError):
                order_n = None
            if order_n != order:
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
        if order is not None and order_id and order_n != order:
            set_field(proj["id"], item["id"], order_id, number=order)

    if root:
        mod_idx = modules.index(mod) if mod in modules else 0
        latest, prior = root[0], root[1] if len(root) > 1 else ""
        upsert_version(
            mod,
            latest,
            prior,
            parent="",
            layer=LAYER_BY_MODULE.get(mod),
            order=float(mod_idx * 10),
        )
        parent_layer = LAYER_BY_MODULE.get(mod) or (by_title.get(mod) or {}).get("fv", {}).get("Layer")
        # 嵌套行按**真相源**列（不是扫 tag 发现的）：矩阵删掉的嵌套模块不该被复活
        nested_paths = [p for r, p in nested_truth if r == mod]
        for j, path in enumerate(nested_paths):
            vers = nested.get(path, [])
            upsert_version(
                f"{mod}/{path}",
                vers[0] if vers else "",
                vers[1] if len(vers) > 1 else "",
                parent=mod,
                layer=parent_layer,
                order=float(mod_idx * 10 + j + 1),
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

    # 4) 行序：规范序 = 草稿钉最前 + createdAt 倒序；漂移检测要能认出「反了」
    fake_fields = {
        "Kind": {"id": "F-KIND", "options": [{"id": "O-BUG", "name": "Bug"}]},
    }
    org_items = [
        {"url": "u1", "title": "old", "createdAt": "2026-10-01T00:00:00Z"},
        {"url": "u2", "title": "new", "createdAt": "2026-10-20T00:00:00Z"},
        {"url": "u3", "title": "mid", "createdAt": "2026-10-10T00:00:00Z"},
    ]
    fake_board = {
        "fields": fake_fields,
        "items": [
            {"id": "i-draft", "url": None, "title": "SOP", "created": None, "body": "b", "fv": {}},
            {"id": "i-new", "url": "u2", "title": "new", "created": "2026-10-20T00:00:00Z", "fv": {}},
            {"id": "i-mid", "url": "u3", "title": "mid", "created": "2026-10-10T00:00:00Z", "fv": {}},
            {"id": "i-old", "url": "u1", "title": "old", "created": "2026-10-01T00:00:00Z", "fv": {}},
        ],
    }
    plan = build_row_plan(fake_board, org_items, {"u1": [("Kind", "Bug")]})
    plan_urls = [p["url"] for p in plan]
    if plan_urls != [None, "u2", "u3", "u1"]:
        fails.append(f"build_row_plan 顺序 = {plan_urls}，期望 [None, 'u2', 'u3', 'u1']")
    row_u1 = next((p for p in plan if p["url"] == "u1"), None)
    if not row_u1 or row_u1["fields"] != [("F-KIND", "O-BUG")]:
        fails.append(f"build_row_plan 没把推导列写成 (field_id, option_id)：{row_u1}")
    if row_order_drift(fake_board, plan):
        fails.append("row_order_drift 对「已经是规范序」误报漂移")
    shuffled = dict(fake_board, items=list(reversed(fake_board["items"])))
    if not row_order_drift(shuffled, plan):
        fails.append("row_order_drift 对「顺序反了」漏报漂移")
    # 草稿行必须钉在最前，且重建时不丢正文
    if not plan[0]["draft"] or plan[0]["body"] != "b":
        fails.append("build_row_plan 没把草稿行钉在最前 / 丢了正文")

    # 5) 视图钉死表：过滤器与列必须字段名齐全（写错的名字会被静默跳过）
    for number, spec in PINNED_VIEWS.items():
        for name, want in spec.items():
            for col in want.get("columns", []):
                if col not in ("Title", "Status", "Kind", "Module", "Created", "Closed",
                               "Review", "Scope", "Updated"):
                    fails.append(f"#{number} 视图「{name}」钉了未知列 {col}")

    # 6) 人工流转态不许被脚本冲掉（Review / Status 都一样）
    review_cases = [
        ("merged", "审阅中", "已合入"),   # 落到终态 ⇒ 脚本说了算
        ("closed", "审阅中", "已关闭"),
        ("open", "审阅中", "审阅中"),     # 在途的人工流转态 ⇒ 原样保留
        ("open", "需修改", "需修改"),
        ("open", "可合入", "可合入"),
        ("open", None, "待审"),          # 没人动过 ⇒ 落待审
        ("open", "待审", "待审"),
    ]
    for state, cur, want in review_cases:
        got = want_review(state, cur)
        if got != want:
            fails.append(f"want_review({state!r}, {cur!r}) = {got!r}，期望 {want!r}")
    status_cases = [
        ("closed", "In Progress", "Done"),
        ("open", "In Progress", "In Progress"),
        ("open", None, "Todo"),
        ("open", "Todo", "Todo"),
    ]
    for state, cur, want in status_cases:
        got = want_issue_status(state, cur)
        if got != want:
            fails.append(f"want_issue_status({state!r}, {cur!r}) = {got!r}，期望 {want!r}")

    for f in fails:
        print(f"FAIL {f}")
    if fails:
        return 1
    print(f"selftest OK（真相源 {src}：{len(modules)} 主模块 / {len(nested)} 嵌套模块）")
    return 0


def _enable_line_buffering() -> None:
    """让 stdout 行缓冲。

    2026-10-25 实测：#4 的行序重建跑到一半被整点同步 cancel 时，
    **CI 日志里只剩第一行**（进程被杀 ⇒ 块缓冲的 stdout 没落盘），
    现场完全看不到「删到第几行、重建到第几行」。
    行缓冲后每个 print 立刻落盘 ⇒ 被 kill 也留得下已经发生的部分。
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass  # stdout 不是 TextIOWrapper（例如被重定向成别的流）时忽略


def main() -> int:
    _enable_line_buffering()
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
        help="离线自测：嵌套 tag 解析 / 真相源装载 / Layer 覆盖 / 行序规则（不触网）",
    )
    ap.add_argument(
        "--reorder",
        action="store_true",
        help="强制重建看板行序（#1/#4），无视行数阈值——大板子必须手工指定",
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
        sync_issue_board(args.apply, force_reorder=args.reorder)
    if only in (None, 4):
        sync_pr_board(args.apply, force_reorder=args.reorder)
    if only in (None, 2):
        sync_version_board(args.apply)
        roll_recent_changes_view(args.apply)
    if only in (None, 3):
        sync_health_board(args.apply)
    # 视图的过滤器/可见列钉死（三块板统一在这里收口）
    sync_pinned_view_configs(args.apply)
    check_pinned_groups(args.apply)
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

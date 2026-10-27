#!/usr/bin/env python3
"""四块治理大盘的对账器（**只读**）：行 / 推导列 / 视图配置 / 行序。

为什么要有它：看板的呈现层配置此前**没有任何断言**，
漂移了只能靠人点开看板才发现（2026-10-27 深查一次就揪出三个问题：
手工行无主而丢失、#2/#3 的视图压根没被钉死、跨仓 PR 的 Review 标错）。
⇒ 把它接进 CI，让漂移**当场报出来**。

判定口径与 `sync-projects.py` 完全一致：推导规则直接 import 那个脚本，不复刻。

用法：
    python3 scripts/board_audit.py            # 只读检查，退出码 0=全绿 1=有问题
    python3 scripts/board_audit.py --json     # 机器可读输出

注意：行序的判定是**警告**而不是失败——Projects 的新行只能追加到末尾
（「重建之后新开的 PR 会落到板尾」是预期行为，不是坏了）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_projects", HERE / "sync-projects.py")
    if spec is None or spec.loader is None:
        raise SystemExit("加载 sync-projects.py 失败")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SP = _load_sync_module()
ORG = os.environ.get("ORG", "oekit")

BOARDS = {
    1: ("issues", "#1 Issue 大盘"),
    2: (None, "#2 版本大盘"),
    3: (None, "#3 矩阵健康"),
    4: ("prs", "#4 PR 审查队列"),
}

VIEW_Q = """
query($num: Int!, $cursor: String) {
  organization(login: "oekit") {
    projectV2(number: $num) {
      id title shortDescription
      fields(first: 50) {
        nodes {
          ... on ProjectV2Field { id name }
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
      views(first: 30) {
        nodes {
          id number name layout filter
          fields(first: 30) {
            nodes { ... on ProjectV2Field { id name } ... on ProjectV2SingleSelectField { id name } }
          }
        }
      }
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id type
          content {
            ... on Issue { number title url createdAt state repository { nameWithOwner } }
            ... on PullRequest { number title url createdAt state repository { nameWithOwner } }
            ... on DraftIssue { title body }
          }
          fieldValues(first: 30) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2Field { name } ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldTextValue {
                text field { ... on ProjectV2Field { name } ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number field { ... on ProjectV2Field { name } ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def gql(query: str, num: int, cursor: str | None = None) -> dict:
    """跑一条 GraphQL 读查询，**重试瞬时故障**，失败时给出可读报错。

    为什么必须重试：2026-10-27 实测本地跑对账器时，GitHub 那边直接回了个
    EOF（stdout 空、gh 报 stderr）⇒ 旧实现 json.loads('') 抛裸栈 ⇒
    CI 的「对账」步骤会因一次网络抖动整体崩掉，日志里还看不出是哪块板子。
    同步脚本那边早有 _graphql_raw_retry（2026-10-26 加的），对账器这边漏了。
    """
    cmd = ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"num={num}"]
    if cursor:
        cmd += ["-F", f"cursor={cursor}"]
    last = ""
    for i in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True)
        out = (p.stdout or "").strip()
        try:
            doc = json.loads(out)
        except Exception:
            last = (
                f"响应不是 JSON（exit {p.returncode}）："
                f"stdout={out[:120]!r} stderr={(p.stderr or '').strip()[:200]!r}"
            )
        else:
            if "errors" in doc:
                raise SystemExit(
                    f"GraphQL 报错：{json.dumps(doc['errors'], ensure_ascii=False)[:400]}"
                )
            return doc["data"]["organization"]["projectV2"]
        if i + 1 < 3:
            time.sleep(2 * (i + 1))
    raise SystemExit(f"GraphQL 重试 3 次仍失败（板 #{num}）：{last}")


def load_board(num: int):
    proj = gql(VIEW_Q, num)
    rows = []
    cursor = None
    while True:
        data = gql(VIEW_Q, num, cursor) if cursor else proj
        for n in data["items"]["nodes"]:
            c = n.get("content") or {}
            fv = {}
            for f in (n.get("fieldValues") or {}).get("nodes", []):
                k = (f.get("field") or {}).get("name")
                if not k:
                    continue
                v = f.get("name")
                if v is None:
                    v = f.get("text")
                if v is None:
                    v = f.get("number")
                fv[k] = v
            rows.append({
                "id": n.get("id"), "type": n.get("type"), "num": c.get("number"),
                "title": c.get("title"), "url": c.get("url"), "created": c.get("createdAt"),
                "state": c.get("state"), "repo": (c.get("repository") or {}).get("nameWithOwner"),
                "fv": fv,
            })
        if not data["items"]["pageInfo"]["hasNextPage"]:
            break
        cursor = data["items"]["pageInfo"]["endCursor"]
    fields = {f["name"]: f for f in proj["fields"]["nodes"]}
    views = {v["name"]: v for v in proj["views"]["nodes"]}
    return proj, fields, views, rows


def org_repos() -> list[str]:
    return subprocess.run(
        ["gh", "api", "--paginate", f"/orgs/{ORG}/repos?per_page=100", "--jq", ".[].full_name"],
        capture_output=True, text=True).stdout.split()


def org_items(kind: str) -> list[dict]:
    """org 侧真实条目（按仓枚举 ⇒ 不受 search 的 1000 条上限影响）。"""
    repos = org_repos()
    out, seen = [], set()
    for repo in repos:
        states = ("open", "merged", "closed") if kind == "prs" else ("open", "closed")
        for st in states:
            cmd = ["gh", ("pr" if kind == "prs" else "issue"), "list", "--repo", repo,
                   "--state", st, "--limit", "1000",
                   "--json", "number,title,url,createdAt,state"]
            try:
                batch = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout or "[]")
            except json.JSONDecodeError:
                continue
            for i in batch:
                if i["url"] in seen:
                    continue
                seen.add(i["url"])
                out.append(i)
    return out


def check_board(num: int, kind: str | None, label: str) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    proj, fields, views, rows = load_board(num)
    content = [r for r in rows if r["type"] != "DRAFT_ISSUE"]

    # 1) 行数：板上 vs org 真实条目
    if kind:
        org = org_items(kind)
        board_urls = {r["url"] for r in content if r.get("url")}
        org_urls = {i["url"] for i in org}
        missing = org_urls - board_urls
        if missing:
            problems.append(f"{label}: 缺 {len(missing)} 行（org 有、板上没有），例：{sorted(missing)[:2]}")
        # 「板上多出的行」只有**仓还在 org 里**的那种才算陈旧（内容已删）；
        # 仓不在 org 里的是跨仓 PR（来自 fork）⇒ 事件驱动入板，属正常 ✓。
        org_repo_set = set(org_repos())
        stale = []
        for r in content:
            u = r.get("url") or ""
            if u in org_urls:
                continue
            if (r.get("repo") or "") in org_repo_set:
                stale.append(u)
        if stale:
            problems.append(f"{label}: {len(stale)} 行陈旧（内容已删但行还在），例：{sorted(stale)[:2]}")

    # 2) 推导列的值（口径直接取自 sync-projects.py）
    for r in content:
        t = r["title"] or ""
        if kind == "issues":
            want = {"Kind": SP.infer_kind(t), "Module": (r["repo"] or "").split("/")[-1]}
        elif kind == "prs":
            want = {"Scope": SP.infer_scope(t), "Review": SP.infer_review(r["state"])}
        else:
            want = {}
        fv = {k.lower(): v for k, v in r["fv"].items()}
        for k, v in want.items():
            if fv.get(k.lower()) != v:
                problems.append(f"{label}: {r['title'][:36]!r} 的 {k} 板上 {fv.get(k.lower())!r}，应为 {v!r}")

    # 3) 视图配置 vs PINNED_VIEWS
    pinned = SP.PINNED_VIEWS.get(num, {})
    for vname, v in views.items():
        want = pinned.get(vname)
        if not want:
            problems.append(f"{label}: 视图「{vname}」没有被 PINNED_VIEWS 钉死（会静默漂移）")
            continue
        if want.get("filter") is not None and (v.get("filter") or "") != want["filter"]:
            problems.append(f"{label}: 视图「{vname}」过滤器 {v.get('filter')!r} ≠ {want['filter']!r}")
        if want.get("columns"):
            cur = {n["name"] for n in v["fields"]["nodes"]}
            if cur != set(want["columns"]):
                problems.append(f"{label}: 视图「{vname}」列 {sorted(cur)} ≠ {sorted(want['columns'])}")

    # 4) 手工行（#4 的 SOP 备注行）必须在
    if num == 4:
        have = any((r["title"] or "") == SP.PR_BOARD_NOTE_TITLE for r in rows)
        if not have:
            problems.append(f"{label}: SOP 备注行缺失（{SP.PR_BOARD_NOTE_TITLE}）")

    # 5) 行序：只警告（重建后新开的 PR 会落到板尾，是预期行为）
    cr = [r["created"] for r in content if r.get("created")]
    if cr and cr != sorted(cr, reverse=True):
        breaks = [i for i in range(1, len(cr)) if cr[i - 1] < cr[i]]
        tail = [r for r in content[-3:]]
        warnings.append(
            f"{label}: 行序有 {len(breaks)} 处断点（板尾 {len(tail)} 行是重建之后新加入的 ⇒ 预期行为）；"
            f"末行：{[r['title'][:28] for r in tail]}"
        )
    return problems, warnings


def main() -> int:
    all_p: list[str] = []
    all_w: list[str] = []
    for num, (kind, label) in BOARDS.items():
        p, w = check_board(num, kind, label)
        all_p += p
        all_w += w
    if "--json" in sys.argv:
        print(json.dumps({"problems": all_p, "warnings": all_w}, ensure_ascii=False, indent=2))
    else:
        for x in all_w:
            print(f"WARN {x}")
        for x in all_p:
            print(f"FAIL {x}")
        print("=== 结论 ===")
        print("  全部对得上 ✅" if not all_p else f"  {len(all_p)} 处不一致 ❌")
    return 1 if all_p else 0


if __name__ == "__main__":
    raise SystemExit(main())

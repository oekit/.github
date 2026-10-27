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

import datetime
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


def _load_bq_module():
    spec = importlib.util.spec_from_file_location("board_query", HERE / "board_query.py")
    if spec is None or spec.loader is None:
        raise SystemExit("加载 board_query.py 失败")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SP = _load_sync_module()
BQ = _load_bq_module()

# 入板传播宽限：刚开的 issue/PR 从「创建」到「即时入板 workflow 把它加到板上」
# 有延迟（2026-10-27 实测：新 PR 提交后 audit 立刻跑，板上还没有 ⇒ 假红）。
# ⇒ 宽限期内的缺失只报 WARN，不判红。
GRACE_SECONDS = 2 * 3600
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


_org_repos_cache: list[str] | None = None


def org_repos() -> list[str]:
    """org 的仓列表（缓存：每次调用都是 24 次 REST，而一个 audit 里要用好几次）。"""
    global _org_repos_cache
    if _org_repos_cache is not None:
        return _org_repos_cache
    _org_repos_cache = subprocess.run(
        ["gh", "api", "--paginate", f"/orgs/{ORG}/repos?per_page=100", "--jq", ".[].full_name"],
        capture_output=True, text=True).stdout.split()
    return _org_repos_cache


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


def authoritative_on_board(url: str, project_number: int) -> bool | None:
    """权威复核：这条 issue/PR 到底在不在某块板上。

    返回 True=在板上 / False=确实不在 / None=**读不到**（判断不了）。

    为什么必须有这一步：2026-10-27 实测 —— `projectV2.items` 分页**会漏行**。
    #4 板实际 1124 行，`projectV2.items` 只返回 1122 行，漏掉的
    oekit/pkg#102、#82 用 issue 侧 projectItems 查是**在板上的**。
    ⇒ 拿 `projectV2.items` 当「板上全集」会得出「缺行」的**假阴性**
    ⇒ CI 的对账门禁会长期假红（红色淹没真信号）。
    权威视角 = issue/PR 自己的 projectItems（实时），与 board_query.py 同源。
    """
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url)
    if not m:
        return None
    owner, repo, num = m.group(1), m.group(2), int(m.group(3))
    try:
        return BQ.on_board(repo, num, project_number, owner=owner)
    except Exception:
        return None  # 读不到 ≠ 不在板上


def classify_missing(
    missing: list[str],
    org_by_url: dict,
    num: int,
    now: datetime.datetime | None = None,
    probe=None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """把「疑似缺失」的条目分成四类。

    返回 (真缺, 传播中, 读不到, 读路径漏读)。

    - 真缺        ：权威视角确认不在板上，且已过入板宽限期 ⇒ **判红**
    - 传播中      ：权威视角确认不在板上，但刚创建 ⇒ 只 WARN
                    （入板 workflow 还没跑到，不是板子坏了）
    - 读不到      ：权威视角本身也失败 ⇒ 判断不了 ⇒ 只 WARN（不能当成缺）
    - 读路径漏读  ：权威视角确认**在板上**，只是 `projectV2.items` 没返回
                    ⇒ 不是缺行（这条 2026-10-27 实测踩过：#4 板漏读
                      oekit/pkg#102、#82 ⇒ 旧的差集判定长期假红）

    probe 可注入 ⇒ 判定逻辑能离线自测（判据失效必须红）。
    """
    probe = probe or authoritative_on_board
    now = now or datetime.datetime.now(datetime.timezone.utc)
    real: list[str] = []
    grace: list[str] = []
    unreadable: list[str] = []
    skipped: list[str] = []
    for u in sorted(missing):
        on = probe(u, num)
        if on is True:
            skipped.append(u)
            continue
        if on is None:
            unreadable.append(u)
            continue
        created = (org_by_url.get(u) or {}).get("createdAt") or ""
        try:
            ts = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            ts = None
        if ts is not None and (now - ts).total_seconds() < GRACE_SECONDS:
            grace.append(u)
        else:
            real.append(u)
    return real, grace, unreadable, skipped


def check_board(num: int, kind: str | None, label: str) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    proj, fields, views, rows = load_board(num)
    content = [r for r in rows if r["type"] != "DRAFT_ISSUE"]

    # 1) 行数：板上 vs org 真实条目
    if kind:
        org = org_items(kind)
        board_urls = {r["url"] for r in content if r.get("url")}
        org_by_url = {i["url"]: i for i in org}
        org_urls = set(org_by_url)
        # 差集先按「projectV2.items 漏读」复核，再谈缺行——
        # 漏读的那几条**在板上**，判缺行就是假红。
        real_missing, grace, unreadable, skipped = classify_missing(
            sorted(org_urls - board_urls), org_by_url, num
        )
        if real_missing:
            problems.append(
                f"{label}: 缺 {len(real_missing)} 行（org 有、板上没有），例：{real_missing[:2]}"
            )
        if grace:
            warnings.append(
                f"{label}: {len(grace)} 行刚创建、入板还在传播中（宽限 {GRACE_SECONDS // 60} 分钟）"
                f"⇒ 只警告不判红，例：{grace[:2]}"
            )
        if unreadable:
            warnings.append(
                f"{label}: {len(unreadable)} 行权威视角也读不到（判断不了），例：{unreadable[:2]}"
            )
        if skipped:
            warnings.append(
                f"{label}: {len(skipped)} 行 projectV2.items 分页读不到、但权威视角确认在板上"
                f"（读路径漏读，不算缺行），例：{skipped[:2]}"
            )
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


def _selftest() -> int:
    """离线自测：缺行判定必须能分辨四种情况。

    判据失效必须红——否则这道门禁只是摆设（与 board_query.py --selftest 同思路）。
    纯离线：不触网（probe 全部注入假的）。
    """
    now = datetime.datetime(2026, 10, 27, 4, 0, 0, tzinfo=datetime.timezone.utc)
    old = (now - datetime.timedelta(days=3)).isoformat().replace("+00:00", "Z")
    fresh = (now - datetime.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    org_by_url = {
        "https://github.com/oekit/pkg/pull/1": {"createdAt": old},   # 漏读（在板上）
        "https://github.com/oekit/pkg/pull/2": {"createdAt": old},   # 真缺
        "https://github.com/oekit/pkg/pull/3": {"createdAt": fresh},  # 传播中
        "https://github.com/oekit/pkg/pull/4": {"createdAt": old},   # 读不到
    }
    u1, u2, u3, u4 = list(org_by_url)
    fails: list[str] = []

    # 1) 权威视角说「在板上」⇒ 是读路径漏读 ⇒ 不算缺行
    real, grace, unread, skipped = classify_missing([u1], org_by_url, 4, now=now, probe=lambda u, n: True)
    if real or grace or unread or skipped != [u1]:
        fails.append(f"漏读条目被误判：real={real} grace={grace} unread={unread} skipped={skipped}")

    # 2) 权威视角说「不在」+ 老条目 ⇒ 真缺 ⇒ 判红
    real, grace, unread, skipped = classify_missing([u2], org_by_url, 4, now=now, probe=lambda u, n: False)
    if real != [u2] or grace or unread or skipped:
        fails.append(f"真缺没被判红：real={real} grace={grace} unread={unread} skipped={skipped}")

    # 3) 权威视角说「不在」+ 刚创建 ⇒ 入板传播中 ⇒ 只警告
    real, grace, unread, skipped = classify_missing([u3], org_by_url, 4, now=now, probe=lambda u, n: False)
    if real or grace != [u3]:
        fails.append(f"刚创建的缺失被误判成真缺：real={real} grace={grace}")

    # 4) 权威视角也读不到 ⇒ 判断不了 ⇒ 不能当成缺行
    real, grace, unread, skipped = classify_missing([u4], org_by_url, 4, now=now, probe=lambda u, n: None)
    if real or unread != [u4]:
        fails.append(f"读不到的条目被误判成缺行：real={real} unread={unread}")

    # 5) 反证：复核必须真的缩小判红面。
    #    旧逻辑（拿差集直接当缺行）会把 4 条全判红；复核后只应剩 1 条。
    probe = lambda u, n: True if u == u1 else (None if u == u4 else False)  # noqa: E731
    real, grace, unread, skipped = classify_missing(list(org_by_url), org_by_url, 4, now=now, probe=probe)
    if len(real) != 1 or real != [u2]:
        fails.append(f"复核没把判红面缩小到唯一真缺：real={real}")
    if len(skipped) != 1 or len(unread) != 1 or len(grace) != 1:
        fails.append(f"四类分流不对：real={real} grace={grace} unread={unread} skipped={skipped}")

    # 6) 非法 URL ⇒ 判「读不到」而不是「不在板上」
    if authoritative_on_board("not-a-url", 4) is not None:
        fails.append("非法 URL 应当判为「读不到」(None)")

    for f in fails:
        print(f"FAIL {f}")
    if fails:
        return 1
    print("selftest OK（缺行判定：真缺 / 传播中 / 读不到 / 读路径漏读 四类分流）")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
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

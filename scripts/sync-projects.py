#!/usr/bin/env python3
"""oekit 四块 GitHub Projects 大盘统一同步。

Projects v2 不会自动跟进 org 的 issue/PR/tag，本脚本补齐：

  #1 Issue 大盘   — org issue → 板；Status=Todo/Done；Kind 按标题粗分
  #2 版本大盘     — 各模块最新 tag → Latest/Prior（draft 行按模块名对齐）
  #3 矩阵健康     — README 安装行 vs 最新 tag → Hygiene（全绿/有漂移/待审计）
  #4 PR 审查队列  — org PR → 板；Review=待审/已合入；Scope 按标题

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
    data = gql(
        """
        query($org: String!, $number: Int!) {
          organization(login: $org) {
            projectV2(number: $number) {
              id
              fields(first: 30) {
                nodes {
                  ... on ProjectV2Field { id name }
                  ... on ProjectV2SingleSelectField {
                    id name
                    options { id name }
                  }
                }
              }
              items(first: 100) {
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
                  fieldValues(first: 20) {
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
        org=ORG,
        number=str(number),
    )
    proj = data["organization"]["projectV2"]
    fields = {f["name"]: f for f in proj["fields"]["nodes"]}
    items = []
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
    return {"id": proj["id"], "fields": fields, "items": items}


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

TITLE_KIND_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[(docs|migration|readme|godoc|llms|site|docs/)", re.I), "Docs"),
    (re.compile(r"\[(refactor|matrix|chore|deps|ci)\]", re.I), "Chore"),
    (re.compile(r"fix\(|\[fix\]|\[bug\]|不存在|静默|崩溃|越权|丢失|不一致|炸", re.I), "Bug"),
    (re.compile(r"建议|支持|提供|增强|缺|诉求|选项|变体|模式|门禁|防护|钩子|特性", re.I), "Enhancement"),
    (re.compile(r"归位|拆|下线|收编|迁", re.I), "Refactor"),
]


def infer_kind(title: str) -> str | None:
    for pat, kind in TITLE_KIND_RULES:
        if pat.search(title):
            return kind
    return None


def list_org_issues() -> list[dict]:
    issues: list[dict] = []
    seen: set[str] = set()
    for state in ("open", "closed"):
        raw = subprocess.check_output(
            [
                "gh", "search", "issues",
                "--owner", ORG,
                "--state", state,
                "--limit", "100",
                "--json", "repository,title,number,url,state",
            ],
            text=True,
        )
        for i in json.loads(raw):
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
    status_id = proj["fields"]["Status"]["id"]
    kind_id = proj["fields"].get("Kind", {}).get("id")

    by_url = {i["url"]: i for i in proj["items"] if i["url"]}
    issues = list_org_issues()
    print(f"org issues={len(issues)} board={len(proj['items'])}")

    adds: list[dict] = []
    syncs: list[tuple[str, str, str, str | None]] = []
    for iss in issues:
        want_status = "Todo" if iss["state"] == "OPEN" else "Done"
        want_kind = infer_kind(iss["title"])
        if iss["url"] not in by_url:
            adds.append({**iss, "want_status": want_status, "want_kind": want_kind})
            continue
        item = by_url[iss["url"]]
        cur_status = item["fv"].get("Status")
        cur_kind = item["fv"].get("Kind")
        if cur_status != want_status or (want_kind and not cur_kind):
            syncs.append((item["id"], iss["title"], want_status, want_kind))

    for a in adds:
        print(f"  + {a['url']}  {a['want_status']}/{a['want_kind']}")
    for _, title, st, kd in syncs:
        print(f"  ~ {title[:48]}  → {st} {kd or ''}")
    if not apply:
        print("  (dry-run)")
        return

    for a in adds:
        iid = add_url(proj["id"], a["url"])
        if a["want_status"] in status_opts:
            set_field(proj["id"], iid, status_id, option_id=status_opts[a["want_status"]])
        if a["want_kind"] and a["want_kind"] in kind_opts and kind_id:
            set_field(proj["id"], iid, kind_id, option_id=kind_opts[a["want_kind"]])
    for iid, title, st, kd in syncs:
        if st in status_opts:
            set_field(proj["id"], iid, status_id, option_id=status_opts[st])
        if kd and kd in kind_opts and kind_id:
            item = next((x for x in proj["items"] if x["id"] == iid), None)
            if item and not item["fv"].get("Kind"):
                set_field(proj["id"], iid, kind_id, option_id=kind_opts[kd])


# ---------------------------------------------------------------------------
# PR 审查队列
# ---------------------------------------------------------------------------

SCOPE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdocs?\b|\[docs?\]|文档|README|godoc|llms", re.I), "docs"),
    (re.compile(r"\bfix\b|\[fix\]|修复|bugfix", re.I), "fix"),
    (re.compile(r"\brefactor\b|重构|归位|拆", re.I), "refactor"),
    (re.compile(r"\bchore\b|杂项|依赖|deps", re.I), "chore"),
    (re.compile(r"\bci\b|workflow|action|门禁", re.I), "ci"),
    (re.compile(r"\bfeat\b|特性|新增|支持|增强", re.I), "feat"),
]


def infer_scope(title: str) -> str | None:
    for pat, scope in SCOPE_RULES:
        if pat.search(title):
            return scope
    return "feat"


def list_org_prs() -> list[dict]:
    prs: list[dict] = []
    seen: set[str] = set()
    for state in ("open", "closed"):
        raw = subprocess.check_output(
            [
                "gh", "search", "prs",
                "--owner", ORG,
                "--state", state,
                "--limit", "50",
                "--json", "repository,title,number,url,state",
            ],
            text=True,
        )
        for i in json.loads(raw):
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
    syncs: list[tuple[str, str, str, str | None]] = []
    for pr in prs:
        want_review = "待审" if pr["state"] == "OPEN" else "已合入"
        want_scope = infer_scope(pr["title"])
        if pr["url"] not in by_url:
            adds.append({**pr, "want_review": want_review, "want_scope": want_scope})
            continue
        item = by_url[pr["url"]]
        if item["fv"].get("Review") != want_review:
            syncs.append((item["id"], pr["title"], want_review, want_scope))

    for a in adds:
        print(f"  + {a['url']}  {a['want_review']}/{a['want_scope']}")
    if not apply:
        print("  (dry-run)")
        return

    for a in adds:
        iid = add_url(proj["id"], a["url"])
        if a["want_review"] in review_opts:
            set_field(proj["id"], iid, review_id, option_id=review_opts[a["want_review"]])
        if a["want_scope"] in scope_opts and scope_id:
            set_field(proj["id"], iid, scope_id, option_id=scope_opts[a["want_scope"]])
    for iid, _, review, _ in syncs:
        if review in review_opts:
            set_field(proj["id"], iid, review_id, option_id=review_opts[review])


# ---------------------------------------------------------------------------
# 版本大盘 + 矩阵健康
# ---------------------------------------------------------------------------

MODULES = [
    "pkg", "concurrency", "logx", "core", "metrics", "tracing", "registry",
    "data", "gormx", "redisx", "httpx", "network", "timertask", "middleware",
    "nns", "nns-endpoint", "bootstrap", "hertz", "kitex", "workflow",
    "kitex-examples",
]


def latest_tags(repo: str) -> list[str]:
    """Return recent tags, newest first (root tags only, no nested prefixes)."""
    raw = subprocess.check_output(
        ["gh", "api", f"repos/{ORG}/{repo}/tags?per_page=10", "--jq", "[.[].name]"],
        text=True,
    )
    tags = json.loads(raw)
    # keep only vX.Y.Z style root tags
    root = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return root


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
    print("\n=== #2 版本大盘 ===")
    proj = load_project(PROJECT_VERSION)
    latest_id = proj["fields"]["Latest"]["id"]
    prior_id = proj["fields"]["Prior"]["id"]

    by_title: dict[str, dict] = {}
    for it in proj["items"]:
        if it["title"]:
            by_title[it["title"].split("@")[0].strip()] = it

    updates = 0
    for mod in MODULES:
        tags = latest_tags(mod)
        if not tags:
            continue
        latest, prior = tags[0], tags[1] if len(tags) > 1 else ""
        item = by_title.get(mod)
        if not item:
            print(f"  ? no board row for {mod}")
            continue
        cur_l, cur_p = item["fv"].get("Latest"), item["fv"].get("Prior")
        if cur_l != latest or (prior and cur_p != prior):
            print(f"  ~ {mod}: {cur_l}/{cur_p} → {latest}/{prior}")
            updates += 1
            if apply:
                set_field(proj["id"], item["id"], latest_id, text=latest)
                if prior:
                    set_field(proj["id"], item["id"], prior_id, text=prior)
    if not updates:
        print("  all up to date")
    if not apply and updates:
        print("  (dry-run)")


def sync_health_board(apply: bool) -> None:
    print("\n=== #3 矩阵健康 ===")
    proj = load_project(PROJECT_HEALTH)
    hy_opts = opt_map(proj["fields"].get("Hygiene"))
    hy_id = proj["fields"]["Hygiene"]["id"]
    drift_id = proj["fields"].get("Drift", {}).get("id")

    by_title = {it["title"]: it for it in proj["items"] if it["title"]}
    updates = 0
    for mod in MODULES:
        item = by_title.get(mod)
        if not item:
            continue
        tags = latest_tags(mod)
        latest = tags[0] if tags else None
        install = remote_install_line(mod)
        if latest is None and install is None:
            want, drift = "待审计", 0
            note = "no tag/readme"
        elif latest and install is None:
            want, drift = "待审计", 0
            note = f"tag={latest} 无安装行"
        elif latest and install and install != latest:
            want, drift = "有漂移", 1
            note = f"安装行 {install} ≠ tag {latest}"
        else:
            want, drift = "全绿", 0
            note = f"安装行 {install} = tag {latest}"

        cur = item["fv"].get("Hygiene")
        cur_drift = item["fv"].get("Drift")
        try:
            cur_drift_n = float(cur_drift) if cur_drift is not None else None
        except (TypeError, ValueError):
            cur_drift_n = None
        if cur != want or cur_drift_n != float(drift):
            print(f"  ~ {mod}: {cur}/{cur_drift} → {want}/{drift}  ({note})")
            updates += 1
            if apply:
                if want in hy_opts:
                    set_field(proj["id"], item["id"], hy_id, option_id=hy_opts[want])
                if drift_id is not None:
                    set_field(proj["id"], item["id"], drift_id, number=float(drift))
    if not updates:
        print("  all up to date")
    if not apply and updates:
        print("  (dry-run)")


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", type=int, choices=[1, 2, 3, 4], help="只跑某块大盘")
    args = ap.parse_args()

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

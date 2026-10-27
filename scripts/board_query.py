#!/usr/bin/env python3
"""看板（Project 1）**权威读路径**：以 issue 侧的 `projectItems` 为准。

**背景（2026-10-23 实测，oekit/.github#24）**

看板有两条读路径，结论**可能相反**：

| 读路径 | 入口 | 实测行为 |
|---|---|---|
| 分页视角 | `projectV2.items(first:100, after:…)` | **滞后**：issue 已入板、字段已写好，分页里仍查不到 |
| 权威视角 | `issue.projectItems` | **实时**：issue 一入板就能读到条目与字段值 |

⇒ 断言「某 issue 在不在看板上」若以分页结果为准 ⇒ **假阴性**：
issue 明明在板上（`issue.projectItems` 已能读到 `Done / Chore / metrics`），
分页里查不到 ⇒ 巡检判它「不在看板上」⇒ 候选漏判 / 误报缺项。

⇒ 本模块只提供**权威视角**：`projectItems` 直读，不解析 `projectV2.items`。
分页视角仍用于「全量概览」（看板有多少条），但**不再用于单条断言**。

**刻意不复用被检对象**：本模块不 import `sync-projects.py`、不解析
`projectV2.items` 的分页结果——门禁/判据若与被检对象共享逻辑，会一起错。

**三态语义**：单条断言的结果是「在板上 / 不在板上 / **判不了**」三态，
读不到（命令失败 / 返回体不可解析）⇒ 抛错中止，**不得**降级成「不在板上」
（那正是把「读不到」当成「事实为空」的旧坑）。

用法：
  python3 scripts/board_query.py metrics#28              # 单条权威读
  python3 scripts/board_query.py metrics#28 k8s#19       # 多条
  python3 scripts/board_query.py --project 1 pkg#47      # 指定看板号（默认 1）
  python3 scripts/board_query.py --selftest              # 变异测试（假 gh，不联网）

退出码：0 = 全部读出；1 = 有条目**读不到**（判不了）；2 = 用法错误。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ORG = "oekit"
DEFAULT_PROJECT = 1

# issue 侧的权威读路径：一次拿到该 issue 在**所有**看板上的条目与字段值。
# 字段值用 inline fragment 取单选值——非单选值（日期/迭代等）会返回 {}，
# 解析侧必须跳过它们（见 parse_fields）。
Q_ISSUE_ITEMS = """query($owner:String!,$repo:String!,$number:Int!){
  repository(owner:$owner,name:$repo){
    issue(number:$number){
      number
      title
      projectItems(first:20){
        nodes{
          id
          project{ id number title }
          fieldValues(first:20){
            nodes{
              ... on ProjectV2ItemFieldSingleSelectValue{
                name
                field{ ... on ProjectV2FieldCommon{ name } }
              }
            }
          }
        }
      }
    }
  }
}"""

# 同一条目的 issue / PR **两种类型**都要查：GitHub 里 issue 与 pullRequest
# 是不同类型，`issue(number:N)` 对 PR 恒为 null ⇒ 只查 issue 会把 PR 判成「读不到」。
#
# 用 alias 同时查两种类型，且**忽略类型不匹配带来的 errors**：
# `issue(number:N)` 对一个 PR 编号是**报错**（"Could not resolve to an Issue"）
# 而不是返回 null ⇒ 严格版 gql 会把整个查询判失败 ⇒ PR 一条都读不到。
# 实测 2026-10-27：pkg#102 是 PR，用非 alias 的写法整条查询带 errors ⇒
# 权威复核全部落进「读不到」分支 ⇒ 对账门禁判红能力被悄悄削弱。
Q_ITEM_ITEMS = """query($owner:String!,$repo:String!,$number:Int!){
  repository(owner:$owner,name:$repo){
    asIssue: issue(number:$number){
      number
      url
      title
      createdAt
      state
      projectItems(first:20){
        nodes{
          id
          project{ id number title }
          fieldValues(first:20){
            nodes{
              ... on ProjectV2ItemFieldSingleSelectValue{
                name
                field{ ... on ProjectV2FieldCommon{ name } }
              }
            }
          }
        }
      }
    }
    asPR: pullRequest(number:$number){
      number
      url
      title
      createdAt
      state
      projectItems(first:20){
        nodes{
          id
          project{ id number title }
          fieldValues(first:20){
            nodes{
              ... on ProjectV2ItemFieldSingleSelectValue{
                name
                field{ ... on ProjectV2FieldCommon{ name } }
              }
            }
          }
        }
      }
    }
  }
}"""


class BoardReadError(RuntimeError):
    """看板读不到（命令失败 / 返回体不可解析）。

    单独一类，是为了让调用方能把它与「不在板上」区分开——
    **读不到不是「没有」**。
    """


def gql(query: str, variables: dict) -> dict:
    """跑一次 graphql，取回 `data`。

    变量类型纪律：`String!` 一律用 `-f`（raw），`Int!` 用 `-F`——
    单选 option id / 仓名都是短十六进制串或字符串，用 `-F` 会被当数字发出去，
    GraphQL 报「provided invalid value」。
    """
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        cmd += ["-F" if isinstance(v, int) and not isinstance(v, bool) else "-f", f"{k}={v}"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise BoardReadError(f"gh 退出码 {p.returncode}：{(p.stderr or p.stdout).strip()[:300]}")
    raw = p.stdout.strip()
    if not raw:
        raise BoardReadError("gh 返回空正文（不是「没有条目」，是读不到）")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BoardReadError(f"gh 返回体不可解析：{raw[:200]!r}（{e}）") from e
    if data.get("errors"):
        raise BoardReadError("GraphQL 报错：" + json.dumps(data["errors"], ensure_ascii=False)[:300])
    if "data" not in data:
        raise BoardReadError(f"gh 返回体没有 data 字段：{raw[:200]!r}")
    return data["data"]


def gql_tolerant(query: str, variables: dict) -> dict:
    """跑一次 graphql，**容忍 errors**（只要 data 里有需要的字段就够）。

    为什么需要：`issue(number:N)` 对一个 PR 编号会**报错**而不是返回 null，
    所以「同时查 issue 和 pullRequest」的查询必然带 errors ⇒ 严格版 gql 会抛。
    ⇒ 这里只认「data 字段在不在」，errors 忽略；data 整个缺失才算读不到。

    注意：`gh api graphql` 带 errors 时**退出码非 0**，但 stdout 里仍有完整 JSON
    ⇒ 不能拿 returncode 判失败，只看 stdout 有没有可解析的 data。
    """
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        cmd += ["-F" if isinstance(v, int) and not isinstance(v, bool) else "-f", f"{k}={v}"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    raw = (p.stdout or "").strip()
    if not raw:
        raise BoardReadError(
            f"gh 返回空正文（不是「没有条目」，是读不到）：{(p.stderr or '').strip()[:300]}"
        )
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BoardReadError(f"gh 返回体不可解析：{raw[:200]!r}（{e}）") from e
    if not isinstance(doc.get("data"), dict):
        raise BoardReadError(
            "gh 返回体没有 data 字段："
            + json.dumps(doc.get("errors"), ensure_ascii=False)[:300]
        )
    return doc["data"]


def parse_fields(field_values: dict) -> dict:
    """条目字段值：{字段名: 选项名}。

    inline fragment 只命中单选值，其余类型（日期/迭代/文本）会返回 `{}` ⇒ 必须跳过，
    否则 `f["field"]["name"]` 抛 KeyError，把「字段类型不同」变成「读失败」。
    """
    out = {}
    for f in (field_values or {}).get("nodes") or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        field = f.get("field")
        if name and isinstance(field, dict) and field.get("name"):
            out[field["name"]] = name
    return out


def project_items(repo: str, num: int, owner: str = ORG) -> list:
    """读该 issue 在所有看板上的条目（权威视角）。

    返回 [{item_id, project_number, project_title, fields{Status:…, …}}]，
    没有条目 ⇒ 空列表（这是「事实为空」，不是读不到）。
    """
    data = gql(Q_ISSUE_ITEMS, {"owner": owner, "repo": repo, "number": num})
    issue = ((data.get("repository") or {}).get("issue"))
    if issue is None:
        raise BoardReadError(f"{owner}/{repo}#{num}：返回体里没有 issue（编号可能不存在）")
    nodes = ((issue.get("projectItems") or {}).get("nodes")) or []
    out = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        proj = node.get("project") or {}
        out.append({
            "item_id": node.get("id"),
            "project_number": proj.get("number"),
            "project_title": proj.get("title"),
            "fields": parse_fields(node.get("fieldValues")),
        })
    return out


def find_on(repo: str, num: int, project_number: int = DEFAULT_PROJECT, owner: str = ORG) -> Optional[dict]:
    """该 issue 是否在指定看板上；在 ⇒ 返回条目（含字段值），不在 ⇒ None。"""
    for item in project_items(repo, num, owner=owner):
        if item["project_number"] == project_number:
            return item
    return None


def item_project_items(repo: str, num: int, owner: str = ORG) -> list:
    """读 issue **或 PR** 在所有看板上的条目（权威视角）。

    为什么单列一个：Q_ISSUE_ITEMS 只查 `issue(number:)`，而 GitHub 的
    issue 与 pullRequest 是**两种不同类型** ⇒ PR 用 issue 查恒为 null。
    而 PR 队列盘（#4）上的东西全是 PR ⇒ 用 issue 视角查会一片「读不到」。
    """
    data = gql_tolerant(Q_ITEM_ITEMS, {"owner": owner, "repo": repo, "number": num})
    repo_node = data.get("repository") or {}
    node = repo_node.get("asIssue") or repo_node.get("asPR")
    if node is None:
        raise BoardReadError(
            f"{owner}/{repo}#{num}：既不是 issue 也不是 PR（编号可能不存在）"
        )
    out = []
    for it in ((node.get("projectItems") or {}).get("nodes")) or []:
        if not isinstance(it, dict):
            continue
        proj = it.get("project") or {}
        out.append({
            "item_id": it.get("id"),
            "project_number": proj.get("number"),
            "project_title": proj.get("title"),
            "fields": parse_fields(it.get("fieldValues")),
        })
    return out


def on_board(repo: str, num: int, project_number: int = DEFAULT_PROJECT, owner: str = ORG) -> bool:
    """该 issue/PR 是否在指定看板上（权威视角）。

    **读不到会抛 BoardReadError 而不是返回 False**——
    「读不到」与「不在板上」必须分开：前者是判断不了，后者才是事实。
    """
    for item in item_project_items(repo, num, owner=owner):
        if item["project_number"] == project_number:
            return True
    return False


def describe(repo: str, num: int, project_number: int = DEFAULT_PROJECT) -> str:
    """人读的一行摘要。"""
    item = find_on(repo, num, project_number)
    if item is None:
        return f"{repo}#{num}: 不在板 {project_number} 上"
    fv = item["fields"]
    return (f"{repo}#{num}: 在板 {project_number} 上（item {item['item_id']}）"
            f"  Status={fv.get('Status')} Kind={fv.get('Kind')} Module={fv.get('Module')}")


# ---------------------------------------------------------------- selftest

FAKE_GH = r'''#!/usr/bin/env python3
"""假 gh：按 fixture 返回 canned JSON，并记录收到的参数。"""
import json, os, sys

args = sys.argv[1:]
vars = {}
flags = {}
i = 0
while i < len(args):
    if args[i] in ("-f", "-F"):
        k, _, v = args[i + 1].partition("=")
        vars[k] = v
        flags[k] = args[i]
        i += 2
    else:
        i += 1

log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"vars": vars, "flags": flags}, ensure_ascii=False) + "\n")

repo = vars.get("repo", "")
num = vars.get("number", "")
key = "%s#%s" % (repo, num)
fixtures = json.loads(os.environ.get("FAKE_GH_FIXTURES", "{}"))
if key in fixtures:
    payload = fixtures[key]
    if payload == "__FAIL__":
        sys.stderr.write("gh: simulated command failure\n")
        sys.exit(1)
    if payload == "__EMPTY__":
        sys.stdout.write("")
        sys.exit(0)
    if payload == "__GARBAGE__":
        sys.stdout.write("<not json>")
        sys.exit(0)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)

print(json.dumps({"data": {"repository": {"issue": {
    "number": int(num) if num.isdigit() else 0,
    "title": "fake",
    "projectItems": {"nodes": []},
}}}}))
'''


def _fixture_item(item_id: str, project_number: int, fields: dict) -> dict:
    """构造一条 projectItems 条目的 canned 载荷。"""
    nodes = []
    for fname, fval in fields.items():
        nodes.append({"name": fval, "field": {"name": fname}})
    nodes.append({})          # 非单选值在真实响应里长这样（inline fragment 不命中）
    return {
        "data": {"repository": {"issue": {
            "number": 28,
            "title": "fake issue",
            "projectItems": {"nodes": [{
                "id": item_id,
                "project": {"id": "PVT_fake", "number": project_number, "title": "fake board"},
                "fieldValues": {"nodes": nodes},
            }]},
        }}},
    }


def _fixture_item_any(item_id: str, project_number: int, as_pr: bool) -> dict:
    """构造 alias 版（asIssue / asPR）的 canned 载荷，**并带上真实存在的 errors**。

    为什么带 errors：真实响应里，查 PR 编号时 `asIssue` 字段会报错
    （"Could not resolve to an Issue with the number of N"），
    而 `asPR` 仍有完整数据 ⇒ 这才是 gql_tolerant 要处理的形态。
    fixture 里若没有 errors，用例就测不出「容忍 errors」这件事。
    """
    node = {
        "id": item_id,
        "project": {"id": "PVT_fake", "number": project_number, "title": "fake board"},
    }
    if as_pr:
        repo = {
            "asIssue": None,
            "asPR": {"number": 102, "url": "https://github.com/fake/pkg/pull/102",
                     "projectItems": {"nodes": [node]}},
        }
        errors = [{"message": "Could not resolve to an Issue with the number of 102."}]
    else:
        repo = {
            "asIssue": {"number": 32, "url": "https://github.com/fake/core/issues/32",
                        "projectItems": {"nodes": [node]}},
            "asPR": None,
        }
        errors = [{"message": "Could not resolve to a PullRequest with the number of 32."}]
    return {"data": {"repository": repo}, "errors": errors}


def _raises_read_error(fn) -> bool:
    """fn 必须抛 BoardReadError（读不到 ⇒ 判不了），返回 True/False。"""
    try:
        fn()
    except BoardReadError:
        return True
    except Exception:
        return False
    return False


def _flags_case(log_path: str) -> bool:
    """读一次 issue，把实际使用的变量 flag 记进 log，再断言 flag 类型。"""
    find_on("metrics", 28)
    flags = {}
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                flags = json.loads(line).get("flags", {})
    return (flags.get("number") == "-F"
            and flags.get("repo") == "-f"
            and flags.get("owner") == "-f")


def _selftest() -> int:
    """变异测试：每个用例都必须**红在断言层**（不是 import/编译层）。

    异常也算判红：被测代码抛异常 ⇒ 该用例判失败（而不是让整轮 selftest 崩掉，
    崩掉会掩盖其余用例的结论）。
    """
    import os
    import shutil
    import tempfile

    cases = []

    def case(name, fn):
        """跑一个用例：断言不成立或抛异常 ⇒ 判红。"""
        try:
            ok = bool(fn())
        except Exception as e:                      # 被测代码炸了也算判红
            ok = False
            print("      （用例抛异常：%s: %s）" % (type(e).__name__, e))
        cases.append((name, ok))

    with tempfile.TemporaryDirectory() as td:
        fake_dir = os.path.join(td, "bin")
        os.makedirs(fake_dir)
        fake = os.path.join(fake_dir, "gh")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_GH)
        os.chmod(fake, 0o755)
        old_path = os.environ["PATH"]
        old_fixtures = os.environ.get("FAKE_GH_FIXTURES")
        old_log = os.environ.get("FAKE_GH_LOG")
        os.environ["PATH"] = fake_dir + os.pathsep + old_path
        log_path = os.path.join(td, "calls.log")
        os.environ["FAKE_GH_LOG"] = log_path

        try:
            # 用例 1（已知答案）：条目在板上 ⇒ 字段必须解析对
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({
                "metrics#28": _fixture_item("PVTI_1", 1, {"Status": "Done", "Kind": "Chore",
                                                          "Module": "metrics"}),
            })
            item = {"fields": {}}

            def _case1():
                it = find_on("metrics", 28)
                return (it is not None
                        and it["fields"].get("Status") == "Done"
                        and it["fields"].get("Module") == "metrics")

            case("已知答案：字段解析", _case1)
            case("非单选值不影响字段",
                 lambda: (lambda it: it is not None
                          and it["fields"].get("Kind") == "Chore")(find_on("metrics", 28)))

            # 用例 2（空 projectItems）⇒ 不在板上（这是事实为空，不是读不到）
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({})
            case("空条目⇒不在板上", lambda: find_on("metrics", 28) is None)

            # 用例 3（**假阴性回归**）：分页视角缺失、权威视角命中 ⇒ 必须报「在板上」
            #      这条是本模块存在的理由：判据一旦退回分页视角就会红。
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({
                "k8s#19": _fixture_item("PVTI_2", 1, {"Status": "Todo", "Kind": "Chore",
                                                       "Module": "k8s"}),
            })

            def _case3():
                it = find_on("k8s", 19)
                return it is not None and it["fields"].get("Module") == "k8s"

            case("假阴性回归：权威视角命中", _case3)

            # 用例 4（看板号不符）⇒ 不在板 1 上
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({
                "pkg#47": _fixture_item("PVTI_3", 2, {"Status": "Todo"}),
            })
            case("看板号不匹配⇒不在板1", lambda: find_on("pkg", 47, 1) is None)
            case("看板号匹配⇒在板2", lambda: find_on("pkg", 47, 2) is not None)

            # 用例 5（读失败）⇒ 必须抛错，**不得**返回「不在板上」
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({"core#1": "__FAIL__"})
            case("命令失败⇒必须报错", lambda: _raises_read_error(lambda: find_on("core", 1)))

            # 用例 6（空正文）⇒ 必须抛错（空 ≠ 没有）
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({"core#2": "__EMPTY__"})
            case("空正文⇒必须报错", lambda: _raises_read_error(lambda: find_on("core", 2)))

            # 用例 7（返回体不可解析）⇒ 必须抛错
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({"core#3": "__GARBAGE__"})
            case("不可解析⇒必须报错", lambda: _raises_read_error(lambda: find_on("core", 3)))

            # 用例 8（变量类型）：number 走 -F（Int!），repo/owner 走 -f（String!）
            #      判据是**实际用的 flag**——用 -f 发 Int 变量 GraphQL 会报
            #      "Variable $number of type Int! was provided invalid value"。
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({
                "metrics#28": _fixture_item("PVTI_4", 1, {"Status": "Done"}),
            })
            if os.path.exists(log_path):
                os.remove(log_path)
            case("变量类型：number 用 -F / repo·owner 用 -f",
                 lambda: _flags_case(log_path))

            # 用例 9（PR 读法）：**PR 必须能查**。
            #      2026-10-27 实测：`issue(number:N)` 对一个 PR 编号是**报错**
            #      （"Could not resolve to an Issue"）而不是返回 null ⇒
            #      非 alias 的写法会让整条查询带 errors ⇒ PR 一条都读不到
            #      ⇒ 权威复核全落进「读不到」分支 ⇒ 对账门禁判红能力被悄悄削弱。
            #      这条用例守住 alias + 容忍 errors 的写法。
            os.environ["FAKE_GH_FIXTURES"] = json.dumps({
                "pkg#102": _fixture_item_any("PVTI_5", 4, as_pr=True),
                "pkg#103": _fixture_item_any("PVTI_6", 4, as_pr=True),
                "core#32": _fixture_item_any("PVTI_7", 1, as_pr=False),
            })
            case("PR 在板上⇒on_board 为真",
                 lambda: on_board("pkg", 102, 4) is True)
            case("PR 不在板上⇒on_board 为假",
                 lambda: on_board("pkg", 103, 5) is False)
            case("issue 在板上⇒on_board 为真",
                 lambda: on_board("core", 32, 1) is True)
            case("PR 读法：item_project_items 能拿到条目",
                 lambda: bool(item_project_items("pkg", 102)))
            case("PR 读法：issue 与 PR 两种类型都能命中",
                 lambda: (item_project_items("pkg", 102) and item_project_items("core", 32)))
        finally:
            os.environ["PATH"] = old_path
            if old_fixtures is None:
                os.environ.pop("FAKE_GH_FIXTURES", None)
            else:
                os.environ["FAKE_GH_FIXTURES"] = old_fixtures
            if old_log is None:
                os.environ.pop("FAKE_GH_LOG", None)
            else:
                os.environ["FAKE_GH_LOG"] = old_log
            shutil.rmtree(td, ignore_errors=True)

    failed = 0
    for name, ok in cases:
        print("  %s  %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            failed += 1
    print("[board-query] selftest: %d/%d 通过" % (len(cases) - failed, len(cases)))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="看板权威读路径（issue.projectItems）")
    ap.add_argument("targets", nargs="*", help="<repo>#<N>，可多个")
    ap.add_argument("--project", type=int, default=DEFAULT_PROJECT, help="看板号（默认 1）")
    ap.add_argument("--selftest", action="store_true", help="跑变异测试（假 gh，不联网）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.targets:
        ap.print_help()
        return 2

    unreadable = 0
    for ref in args.targets:
        if "#" not in ref:
            print("  用法错误：%r 不是 <repo>#<N>" % ref, file=sys.stderr)
            return 2
        repo, _, num = ref.rpartition("#")
        try:
            print("  " + describe(repo, int(num), args.project))
        except BoardReadError as e:
            unreadable += 1
            print("  %s#%s: **判不了**（读不到，不是「不在板上」）：%s" % (repo, num, e),
                  file=sys.stderr)
    return 1 if unreadable else 0


if __name__ == "__main__":
    sys.exit(main())

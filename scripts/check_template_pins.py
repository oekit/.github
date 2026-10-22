#!/usr/bin/env python3
"""本体/副本 action pin 一致性门禁（oekit/.github）。

**背景（2026-10-23 实测，dependabot PR #22 触发）**：

dependabot 的 `github-actions` ecosystem **只扫 `.github/workflows/`**（`directory: /`），
**扫不到 `workflow-templates/`**。而后者才是本体——`scripts/sync-project-hooks.sh`
的 `SRC` 就是 `workflow-templates/issue-to-project.yml`，它把本体下发到 28 份副本
（27 个仓 + `.github` 自己），幂等判据是**逐字节一致**。

⇒ 真实发生的形态（2026-10-22 实测）：
  1. dependabot 升了 `.github/workflows/issue-to-project.yml` 的 pin（v5 → v7）；
  2. 同仓 `workflow-templates/issue-to-project.yml` 的**同款 pin 原封不动**（仍 v5）；
  3. 副本升级、本体陈旧 ⇒ **下次跑 sync-project-hooks.sh 就把升级 revert 掉**，
     并把 24 个模块仓副本一起钉死在旧版。
  4. 这个回灌**没有任何门禁会报**——它发生在一个手工脚本里，不是 CI。

**本门禁判什么**：本体的 pin **不得落后于** `.github/workflows/` 下同名 workflow 的 pin。
落后 = 下一次同步必然回灌。

**刻意不复用被检对象**：这里不 import `sync-project-hooks.sh`，也不解析 dependabot 的
配置，而是**直接扫原始文本里的 pin 事实**（仓库既有纪律：门禁不得与被检对象共享判断逻辑）。

用法：
  python3 scripts/check_template_pins.py            # 缺省扫本仓
  python3 scripts/check_template_pins.py --selftest

退出码：0 = 一致；1 = 有落后（打印清单）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# `uses: <action>@<ref>`（action 名含 owner/repo[/path]）
_PIN_RE = re.compile(r"uses:\s*([A-Za-z0-9._-]+/[A-Za-z0-9._/-]+)@([^\s#]+)")

# 需要比对的一对：本体（模板）↔ 本仓的实际 workflow
PAIRS = [
    ("workflow-templates/issue-to-project.yml", ".github/workflows/issue-to-project.yml"),
    ("workflow-templates/issue-auto-assign.yml", ".github/workflows/issue-auto-assign.yml"),
]

# 本仓自有、不由模板下发的 workflow（它们的 pin 不参与比对）
SKIP_ACTIONS = ("oekit/.github",)


def pins(text: str) -> dict[str, str]:
    """抽出文本里所有 action pin：{action_name: ref}。

    同一 action 出现多次时取**最后一个**（YAML 里同名 action 一般同版本；
    不一致本身不作为本门禁的判定对象）。
    """
    out: dict[str, str] = {}
    for action, ref in _PIN_RE.findall(text):
        if any(action.startswith(p) for p in SKIP_ACTIONS):
            continue
        out[action] = ref
    return out


def major(ref: str) -> int:
    """取 ref 的 major 号；无法解析时返回 -1。"""
    m = re.match(r"v?(\d+)", ref)
    return int(m.group(1)) if m else -1


def check_pair(root: Path, tpl_rel: str, live_rel: str) -> list[str]:
    """比对一对本体/副本的 pin，返回问题描述列表（空 = 通过）。

    **只对「本体与实际都存在」的配对做判定**——否则「某一对缺失」会连带把
    **另一对明明一致**的配对也判成红（2026-10-23 自测首轮实测：
    6 个用例里 3 个假红，根因就是这里）。
    """
    tpl_path = root / tpl_rel
    live_path = root / live_rel
    if not tpl_path.exists() or not live_path.exists():
        return []

    tpl_pins = pins(tpl_path.read_text(encoding="utf-8"))
    live_pins = pins(live_path.read_text(encoding="utf-8"))
    problems: list[str] = []

    for action, live_ref in sorted(live_pins.items()):
        if action not in tpl_pins:
            problems.append(
                f"{tpl_rel}: 本体缺少 {action} 的 pin"
                f"（实际 workflow 用的是 @{live_ref}）⇒ 同步会把它降级/移除"
            )
            continue
        tpl_ref = tpl_pins[action]
        if tpl_ref == live_ref:
            continue
        t_major, l_major = major(tpl_ref), major(live_ref)
        if l_major > t_major:
            problems.append(
                f"{tpl_rel}: {action} 本体 @{tpl_ref} 落后于 "
                f"{live_rel} 的 @{live_ref} ⇒ 下次同步必然回灌（把升级 revert）"
            )
        # 本体比实际新：不报错（本体现在是权威源，同步方向正确）
    return problems


def check(root: Path) -> list[str]:
    problems: list[str] = []
    for tpl_rel, live_rel in PAIRS:
        problems.extend(check_pair(root, tpl_rel, live_rel))
    return problems


def check_missing_pairs(root: Path) -> list[str]:
    """配对缺失单独判——与 pin 比对**分开**，避免「缺一对」污染「另一对」的结论。

    缺失本身是问题（本体或实际 workflow 不存在 ⇒ 同步脚本的下发/比对会失准），
    但它必须以自己的条目报出来，而不是让别的配对假红。
    """
    problems: list[str] = []
    for tpl_rel, live_rel in PAIRS:
        if not (root / tpl_rel).exists():
            problems.append(f"{tpl_rel}: 本体不存在")
        if not (root / live_rel).exists():
            problems.append(f"{live_rel}: 实际 workflow 不存在")
    return problems


def _selftest() -> int:
    """变异测试：每个用例都必须**红在断言层**（而不是编译/import 层）。"""
    import tempfile

    cases: list[tuple[str, bool]] = []

    # 用例 1：本体落后 → 必须报红
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "workflow-templates").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "workflow-templates/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v5\n", encoding="utf-8")
        (root / ".github/workflows/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        cases.append(("本体落后应报红", bool(check(root))))

    # 用例 2：完全一致 → 必须绿
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "workflow-templates").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "workflow-templates/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        (root / ".github/workflows/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        cases.append(("一致应绿", not check(root)))

    # 用例 3：本体更新（权威源领先）→ 必须绿（同步方向正确）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "workflow-templates").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "workflow-templates/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        (root / ".github/workflows/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v5\n", encoding="utf-8")
        cases.append(("本体领先应绿", not check(root)))

    # 用例 4：本体缺该 action → 必须报红（同步会把实际里的 pin 抹掉）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "workflow-templates").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "workflow-templates/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        (root / ".github/workflows/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n      - uses: actions/setup-go@v7\n", encoding="utf-8")
        cases.append(("本体缺 pin 应报红", bool(check(root))))

    # 用例 5：oekit/.github 自有 pin 不参与比对 → 必须绿
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "workflow-templates").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "workflow-templates/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        (root / ".github/workflows/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n"
            "      - uses: oekit/.github/.github/workflows/go-ci.yml@main\n", encoding="utf-8")
        cases.append(("自有 pin 应忽略", not check(root)))

    # 用例 6：配对缺失单独判——缺一对时，**另一对一致不该假红**
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "workflow-templates").mkdir()
        (root / ".github/workflows").mkdir(parents=True)
        (root / "workflow-templates/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        (root / ".github/workflows/issue-to-project.yml").write_text(
            "      - uses: actions/checkout@v7\n", encoding="utf-8")
        # auto-assign 一对**不存在** ⇒ 缺失要报，但 issue-to-project 一对不该假红
        cases.append(("配对缺失不污染另一对", not check(root)))
        cases.append(("配对缺失本身要报", bool(check_missing_pairs(root))))

    # 用例 7：本仓真实现场必须是绿的（本体与实际一致）
    cases.append(("本仓现场应绿", not check(Path(__file__).resolve().parent.parent)))

    failed = 0
    for name, ok in cases:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed += 1
    print(f"[template-pin-check] selftest: {len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本体/副本 action pin 一致性门禁")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="仓根目录（缺省本仓）")
    ap.add_argument("--selftest", action="store_true", help="跑变异测试")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    root = Path(args.root)
    problems = check(root)
    if problems:
        print("[template-pin-check] 发现问题：")
        for p in problems:
            print(f"  - {p}")
        print("[template-pin-check] 本体是同步脚本的唯一事实源——本体落后 = 下次同步回灌。")
        return 1
    print("[template-pin-check] 本体/副本 pin 一致 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())

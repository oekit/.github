#!/usr/bin/env python3
"""`changelog_check.py` 规则 3 的**嵌套模块**判定自测。

**为什么要有这个自测**：规则 3 原实现是「拿 `git describe --exact-match --tags HEAD`
返回的 tag 名当字符串，去比被检文件里的节标题」⇒ 而

  · **嵌套模块自己的 CHANGELOG 按嵌套约定记短名节**（`## [v0.1.3]`），
  · **tag 名是全名**（`nacosimpl/v0.1.3`），
  · 且**同一 commit 上有多个 tag 时 `describe` 优先返回嵌套 tag 名**，

⇒ 三者叠加 ⇒ **对「有独立 CHANGELOG 的嵌套模块」必然误判**（实测 7 个，全部用短名节）。
⇒ 而这类失败是**静默**的：org CI 只查仓根 CHANGELOG ⇒ **CI 看不到**，只有本地
`validate_changelog` 会撞 ⇒ 「同仓第二个嵌套模块发不出去」。

**本自测覆盖的场景**（每条都对应一个真实形态）：

  S1 嵌套模块 + 同 commit 多 tag + **短名节在位** ⇒ 放行（★修复的核心）
  S2 嵌套模块 + **短名节缺失** ⇒ 判红（★防「顺带放水」：不能只修误判、把真报也放过）
  S3 根 CHANGELOG 记**全名** ⇒ 放行（根仓行为不变）
  S4 根 CHANGELOG **缺同名节** ⇒ 判红（★「该红仍红」的反例：不能只修误判、把根仓门禁放水）
  S5 嵌套文件 + HEAD 在**别的模块**的 tag 上 ⇒ 放行（本文件不适用）
  S6 嵌套文件在**非 git 目录** ⇒ 放行（describe 读不到 ⇒ 跳过；CI 只查仓根）
  S7 嵌套文件 + HEAD 在**根 tag** 上 ⇒ 放行（本文件不适用）

**无副作用、不联网**：全部场景在临时目录里造真实 git 仓（真实 commit + 真实 tag）
⇒ 走被测脚本的**真实** `git describe` 路径，不是 mock。

用法：python3 scripts/changelog_check_selftest.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "changelog_check.py"


def make_repo(root: Path, tags: list[str], files: dict[str, str]) -> Path:
    """造一个真实 git 仓：所有 files 一次 commit，所有 tags 打在同一个 HEAD 上。"""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "fixture"], check=True)
    for t in tags:
        subprocess.run(["git", "-C", str(root), "tag", t], check=True)
    return root


def run_check(cwd: Path, path: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(SCRIPT), path],
                       cwd=str(cwd), capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def main() -> int:
    failures: list[str] = []

    def check(cid: str, cond: bool, msg: str) -> None:
        if not cond:
            failures.append(f"{cid}：{msg}")

    # ── S1：嵌套模块 + 同 commit 多 tag + 短名节在位 ⇒ 放行（★核心）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r1",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, "nacosimpl/CHANGELOG.md")
        check("S1", code == 0, f"嵌套模块短名节在位时应放行，实际 exit={code} 输出={out[:200]!r}")

    # ── S2：嵌套模块 + 短名节缺失 ⇒ 判红（防放水）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r2",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "# 没有版本节\n\n- y\n",
            })
        code, out = run_check(repo, "nacosimpl/CHANGELOG.md")
        check("S2", code == 1, f"嵌套模块缺短名节时必须判红，实际 exit={code}")
        check("S2", "nacosimpl/v0.1.3" in out,
              f"报错应点名 tag，实际={out[:200]!r}")

    # ── S3：根 CHANGELOG 记全名 ⇒ 放行（根仓行为不变）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r3",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": ("# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n\n"
                                 "## [nacosimpl/v0.1.3] — 2026-11-02\n\n- z\n"),
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, "CHANGELOG.md")
        check("S3", code == 0, f"根 CHANGELOG 记全名时应放行，实际 exit={code} 输出={out[:200]!r}")

    # ── S4：根 CHANGELOG 缺同名节 ⇒ 判红（★「该红仍红」的反例）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r4",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, "CHANGELOG.md")
        check("S4", code == 1, f"根 CHANGELOG 缺同名节时必须判红，实际 exit={code}")
        check("S4", "nacosimpl/v0.1.3" in out,
              f"报错应点名 tag，实际={out[:200]!r}")

    # ── S5：嵌套文件 + HEAD 在别的模块的 tag 上 ⇒ 放行（本文件不适用）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r5",
            tags=["other/v1.0.0"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, "nacosimpl/CHANGELOG.md")
        check("S5", code == 0,
              f"HEAD 在别的模块 tag 上时本文件不适用，应放行，实际 exit={code} 输出={out[:200]!r}")

    # ── S6：嵌套文件在非 git 目录 ⇒ 放行（describe 读不到 ⇒ 跳过）──
    with tempfile.TemporaryDirectory() as td:
        outside = Path(td) / "outside"
        outside.mkdir()
        (outside / "CHANGELOG.md").write_text(
            "## [v0.1.3] — 2026-11-02\n\n- y\n", encoding="utf-8")
        code, out = run_check(Path(td), str(outside / "CHANGELOG.md"))
        check("S6", code == 0,
              f"非 git 目录下的文件应跳过规则 3，实际 exit={code} 输出={out[:200]!r}")

    # ── S7：嵌套文件 + HEAD 在根 tag 上 ⇒ 放行（本文件不适用）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r7",
            tags=["v0.5.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, "nacosimpl/CHANGELOG.md")
        check("S7", code == 0,
              f"HEAD 在根 tag 上时嵌套文件不适用，应放行，实际 exit={code} 输出={out[:200]!r}")

    # ── S8：★ 同 commit 多个 tag，其中**另一个模块**缺短名节 ⇒ 必须判红
    #    （★「假绿」防线：旧实现用 `git describe --exact-match` ⇒ 只返回**一个** tag
    #      ⇒ 这个模块**从不被校验** ⇒ 缺了也判绿）
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r8",
            tags=["v0.5.3", "nacos/v0.1.3", "nacosimpl/v0.1.4"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                # nacos 的短名节**在位**
                "nacos/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
                # ★ nacosimpl 的短名节**缺失** ⇒ 必须被抓出来
                "nacosimpl/CHANGELOG.md": "# 没有版本节\n\n- z\n",
            })
        code, out = run_check(repo, "nacosimpl/CHANGELOG.md")
        check("S8", code == 1,
              f"同 commit 多 tag 时，另一模块缺短名节必须判红（假绿防线），实际 exit={code}")
        check("S8", "nacosimpl/v0.1.4" in out,
              f"报错应点名**未被 describe 选中**的那个 tag，实际={out[:300]!r}")

    # ── S9：★ 同 commit 多个 tag，**每个模块各自**都缺短名节 ⇒ 逐个报出
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r9",
            tags=["v0.5.3", "nacos/v0.1.3", "nacosimpl/v0.1.4"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacos/CHANGELOG.md": "# 没有版本节\n\n- y\n",
                "nacosimpl/CHANGELOG.md": "# 没有版本节\n\n- z\n",
            })
        code, out = run_check(repo, "nacos/CHANGELOG.md")
        check("S9", code == 1, f"本模块缺短名节时必须判红，实际 exit={code}")
        check("S9", "nacos/v0.1.3" in out,
              f"报错应点名本模块的 tag，实际={out[:300]!r}")

    # ═══ 2026-11-04：判据**不依赖路径参数形态**（修「绝对路径 ⇒ 根面假绿」）═══
    #
    # 洞的成因：规则 3 用 `os.path.dirname(path)` 是否为空来区分「根面 / 嵌套面」
    #   ⇒ 传 `./CHANGELOG.md` 或**绝对路径**时 dirname 非空 ⇒ 误走嵌套分支
    #   ⇒ 根面文件的「同名（全名）节」检查被**整段静默跳过** ⇒ **假绿**。
    #   而**真实链路**（`release_train.validate_changelog` 传 `str(path)`）就是绝对路径
    #   ⇒ CI 侧（无参调用）判红、本地预校验侧放行 ⇒ 同一份 CHANGELOG 两个结论。
    #
    # ⇒ 这批用例把「**三种调用形态**」都钉住：该红的都红、该绿的都绿。

    # ── S10：根面 + 缺同名节 + **绝对路径** ⇒ 必须判红（★ 本次洞的正面断言）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r10",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, str(repo / "CHANGELOG.md"))
        check("S10", code == 1,
              f"根面缺同名节时，**绝对路径**形态也必须判红（本次洞的正面断言），实际 exit={code}")
        check("S10", "nacosimpl/v0.1.3" in out,
              f"报错应点名 tag，实际={out[:200]!r}")

    # ── S11：根面 + 缺同名节 + `./CHANGELOG.md` ⇒ 必须判红 ──────────────
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r11",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, "./CHANGELOG.md")
        check("S11", code == 1,
              f"根面缺同名节时，`./CHANGELOG.md` 形态也必须判红，实际 exit={code}")

    # ── S12：根面 + 同名节在位 + **绝对路径** ⇒ 放行（防「过度修正」误伤）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r12",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": ("# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n\n"
                                 "## [nacosimpl/v0.1.3] — 2026-11-02\n\n- z\n"),
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, str(repo / "CHANGELOG.md"))
        check("S12", code == 0,
              f"根面同名节在位时，绝对路径形态应放行（不能过度修正），实际 exit={code} 输出={out[:200]!r}")

    # ── S13：嵌套面 + 短名节在位 + **绝对路径** ⇒ 放行（★ 无误伤）────────
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r13",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
            })
        code, out = run_check(repo, str(repo / "nacosimpl" / "CHANGELOG.md"))
        check("S13", code == 0,
              f"嵌套面短名节在位时，绝对路径形态应放行（不能把嵌套面误判成根面），实际 exit={code} 输出={out[:200]!r}")

    # ── S14：嵌套面 + 短名节缺失 + **绝对路径** ⇒ 判红（★ 不能借修正放水）──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r14",
            tags=["v0.5.3", "nacosimpl/v0.1.3"],
            files={
                "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                "nacosimpl/CHANGELOG.md": "# 没有版本节\n\n- y\n",
            })
        code, out = run_check(repo, str(repo / "nacosimpl" / "CHANGELOG.md"))
        check("S14", code == 1,
              f"嵌套面缺短名节时，绝对路径形态也必须判红（不能借修正放水），实际 exit={code}")
        check("S14", "nacosimpl/v0.1.3" in out,
              f"报错应点名 tag，实际={out[:200]!r}")

    # ── S15：非 git 目录 + 绝对路径 ⇒ 放行（边界：读不到仓根 ⇒ 跳过，不误伤）──
    with tempfile.TemporaryDirectory() as td:
        outside = Path(td) / "outside"
        outside.mkdir()
        (outside / "CHANGELOG.md").write_text(
            "## [v0.1.3] — 2026-11-02\n\n- y\n", encoding="utf-8")
        code, out = run_check(Path(td), str(outside / "CHANGELOG.md"))
        check("S15", code == 0,
              f"非 git 目录下的文件应跳过规则 3（绝对路径形态），实际 exit={code} 输出={out[:200]!r}")

    # ── S12（#61 形态 1）：两个版本标题**粘连在同一行** ⇒ 必须判红 ──
    #    旧切节（行首锚定 match）只取行首第一个 ⇒ 第二个标题既不切节也不判重
    #    ⇒ 假绿（concurrency CHANGELOG.md:219 实测）。
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r12",
            tags=[],
            files={"CHANGELOG.md": (
                "# c\n\n## [Unreleased]\n\n- w\n\n"
                "## [v0.15.0] — 2026-10-18## [v0.15.0] — 2026-10-18\n\n- x\n\n"
                "## [v0.14.0] — 2026-10-01\n\n- y\n")},
            )
        code, out = run_check(repo, "CHANGELOG.md")
        check("S12", code == 1, f"同行粘连标题必须判红，实际 exit={code} 输出={out[:200]!r}")
        check("S12", "格式损坏" in out, f"粘连应报「格式损坏」，实际={out[:200]!r}")

    # ── S13（#61 形态 2）：版本标题**跨行断裂**（] 被甩到下一行）⇒ 必须判红 ──
    #    旧切节对断裂行 match 失败 ⇒ 整节连同版本号从门禁视野消失。
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r13",
            tags=[],
            files={"CHANGELOG.md": (
                "# c\n\n## [Unreleased]\n\n- w\n\n"
                "## [- feat(singleflight): add\nv0.7.0] — 2026-10-18\n\n- x\n\n"
                "## [v0.6.0] — 2026-09-01\n\n- y\n")},
            )
        code, out = run_check(repo, "CHANGELOG.md")
        check("S13", code == 1, f"跨行断裂标题必须判红，实际 exit={code} 输出={out[:200]!r}")
        check("S13", "格式损坏" in out, f"断裂应报「格式损坏」，实际={out[:200]!r}")

    # ── S14（对照）：正常日期后缀 + 正文行含 `## [` 字样但**不在行首** ⇒ 放行 ──
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(
            Path(td) / "r14",
            tags=[],
            files={"CHANGELOG.md": (
                "# c\n\n## [Unreleased]\n\n- 修 changelog-check（见 .github#61：`## [` 行首锚定）\n\n"
                "## [v0.15.0] — 2026-10-18\n\n- x\n\n"
                "## [v0.14.0] — 2026-10-01\n\n- y\n")},
            )
        code, out = run_check(repo, "CHANGELOG.md")
        check("S14", code == 0, f"正文提及标题语法不应误报，实际 exit={code} 输出={out[:200]!r}")

    # ── 变异自证（#61）：切节退回行首锚定 match ⇒ S12 必须**转绿（假绿复现）**
    saved_src61 = SCRIPT.read_text(encoding="utf-8")
    mutated61 = saved_src61.replace(
        "    for m in head_re.finditer(l):\n"
        "        sections.append((i, m.group(1).strip()))",
        "    if (m := head_re.match(l)):  # ← 变异：退回行首锚定（只取每行第一个）\n"
        "        sections.append((i, m.group(1).strip()))")
    mutated61 = mutated61.replace(
        'if l.startswith("## [") and (TITLE_LINE_RE.fullmatch(l) is None or l.count("## [") != 1):',
        'if False and l.startswith("## ["):  # ← 变异：硬规则一并退役')
    if mutated61 == saved_src61:
        print("✗ 变异自证失败：没找到 finditer 切节实现（代码形态已变？）")
        return 1
    try:
        SCRIPT.write_text(mutated61, encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(
                Path(td) / "mut61",
                tags=[],
                files={"CHANGELOG.md": (
                    "# c\n\n## [Unreleased]\n\n- w\n\n"
                    "## [v0.15.0] — 2026-10-18## [v0.15.0] — 2026-10-18\n\n- x\n\n"
                    "## [v0.14.0] — 2026-10-01\n\n- y\n")},
                )
            code, out = run_check(repo, "CHANGELOG.md")
        if code != 0:
            print("✗ 变异「切节退回行首锚定」没有复现假绿（变异后仍判红）⇒ S12 证明不了旧口径漏检")
            return 1
        print("✓ 变异「切节退回行首锚定」复现了假绿（旧口径判绿 ⇒ 粘连标题不被抓；新口径判红 ⇒ S12/S13 有牙齿）")
    finally:
        SCRIPT.write_text(saved_src61, encoding="utf-8")

    # ── 变异自证（★）：把实现退回 `describe` 口径 ⇒ S8 必须转红
    #    ⇒ 证明 S8 这条断言**有牙齿**（不是恒绿）。
    #    ⚠️ 变异必须在断言运行期间生效 ⇒ 这里改**源码文件**（被测脚本是 subprocess
    #       调用 ⇒ 每次运行都重新读文件 ⇒ 改动立即生效）⇒ 跑完再还原。
    #    ⚠️ **变异必须是语义等价于旧实现**：光把 `git tag --points-at` 换成
    #       `git describe` **不够** —— 旧代码取的是**单个** tag，而新代码把 stdout
    #       按行拆成列表 ⇒ describe 的单行输出仍会被当成「一个 tag 的列表」⇒
    #       行为不变 ⇒ **变异不生效**（实测踩过：第一次这样写，变异没被抓到）。
    #       ⇒ 正确做法：换成 describe **且只取第一行**。
    saved_src = SCRIPT.read_text(encoding="utf-8")
    mutated = saved_src.replace(
        'r = subprocess.run(["git", "tag", "--points-at", "HEAD"],',
        'r = subprocess.run(["git", "describe", "--exact-match", "--tags", "HEAD"],')
    mutated = mutated.replace(
        'tags = [t.strip() for t in r.stdout.splitlines() if t.strip()]',
        'tags = r.stdout.strip().splitlines()[:1]')
    if mutated == saved_src:
        print("✗ 变异自证失败：没找到 `git tag --points-at HEAD` 实现（代码形态已变？）")
        return 1
    try:
        SCRIPT.write_text(mutated, encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(
                Path(td) / "mut",
                tags=["v0.5.3", "nacos/v0.1.3", "nacosimpl/v0.1.4"],
                files={
                    "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                    "nacos/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
                    "nacosimpl/CHANGELOG.md": "# 没有版本节\n\n- z\n",
                })
            code, out = run_check(repo, "nacosimpl/CHANGELOG.md")
        # ★ **期望的是「变异后判绿（假绿）」**：变异体 = 旧实现（只校验 describe 选中的
        #   那一个模块）⇒ 另一个模块缺短名节 ⇒ **判绿** ⇒ 这正是 **S8 要防的假绿**。
        #   ⇒ 若变异后反而判红 ⇒ 说明旧实现也抓得到 ⇒ **S8 就不是新增防线**（断言无牙齿）。
        if code != 0:
            print("✗ 变异「取 tag 退回 describe（只校验一个模块）」**没有复现假绿**"
                  f"（变异后仍判红，exit={code}）⇒ S8 证明不了「旧口径漏检」这一层")
            return 1
        print("✓ 变异「取 tag 退回 describe（只校验一个模块）」复现了**假绿**"
              "（旧口径判绿 ⇒ 另一模块缺短名节不被抓；新口径判红 ⇒ S8 有牙齿）")
    finally:
        SCRIPT.write_text(saved_src, encoding="utf-8")

    # ── 变异自证（★★）：把「根面判定」退回旧的「看 dirname(path) 是否为空」
    #    ⇒ S10/S11（绝对路径 / `./CHANGELOG.md` 形态的根面缺同名节）必须**转绿**
    #    ⇒ 即「洞被复现」⇒ 证明这两条新断言**有牙齿**（不是恒绿）。
    #    ★ 与上面那条变异互为对照：上面那条证「取 tag 口径」的牙齿，
    #      这条证「根面判定口径」的牙齿 ⇒ 两条独立失效面各有一道断言。
    saved_src2 = SCRIPT.read_text(encoding="utf-8")
    old_expr = '        return os.path.realpath(path) == os.path.join(top, "CHANGELOG.md")'
    new_expr = '        return not os.path.dirname(path)  # ← 变异：退回旧判定（看路径参数形态）'
    mutated2 = saved_src2.replace(old_expr, new_expr)
    if mutated2 == saved_src2:
        print("✗ 变异自证失败：没找到根面判定表达式（代码形态已变？）")
        return 1
    try:
        SCRIPT.write_text(mutated2, encoding="utf-8")
        # S10 夹具：根面缺同名节 + 绝对路径
        with tempfile.TemporaryDirectory() as td:
            repo10 = make_repo(
                Path(td) / "m10",
                tags=["v0.5.3", "nacosimpl/v0.1.3"],
                files={
                    "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                    "nacosimpl/CHANGELOG.md": "## [v0.1.3] — 2026-11-02\n\n- y\n",
                })
            code10, out10 = run_check(repo10, str(repo10 / "CHANGELOG.md"))
            # S11 夹具：根面缺同名节 + ./CHANGELOG.md
            code11, out11 = run_check(repo10, "./CHANGELOG.md")
            # 对照：嵌套面缺短名节 + 绝对路径（这个洞与根面判定无关 ⇒ 仍须判红）
            repo14 = make_repo(
                Path(td) / "m14",
                tags=["v0.5.3", "nacosimpl/v0.1.3"],
                files={
                    "CHANGELOG.md": "# root\n\n## [v0.5.3] — 2026-11-02\n\n- x\n",
                    "nacosimpl/CHANGELOG.md": "# 没有版本节\n\n- y\n",
                })
            code14, _out14 = run_check(repo14, str(repo14 / "nacosimpl" / "CHANGELOG.md"))
        # ★ 期望：变异后 S10/S11 **变绿**（洞复现 ⇒ 旧口径看不见根面同名节缺失）
        if code10 != 0:
            print("✗ 变异「根面判定退回看路径形态」没有复现假绿"
                  f"（绝对路径形态仍判红，exit={code10}）⇒ S10 证明不了这一层")
            return 1
        if code11 != 0:
            print("✗ 变异「根面判定退回看路径形态」没有复现假绿"
                  f"（./ 形态仍判红，exit={code11}）⇒ S11 证明不了这一层")
            return 1
        if code14 == 0:
            print("✗ 变异后嵌套面缺短名节也判绿了 ⇒ 变异体过宽（不是精确退回旧判定）")
            return 1
        print("✓ 变异「根面判定退回看路径形态」复现了**假绿**"
              "（绝对路径 / ./ 形态判绿 ⇒ 根面同名节缺失不被抓；新判定判红 ⇒ S10/S11 有牙齿）")
    finally:
        SCRIPT.write_text(saved_src2, encoding="utf-8")

    if failures:
        print("✗ changelog_check 规则 3（嵌套模块）自测未过：")
        for f in failures:
            print("  -", f)
        return 1
    print("✓ changelog_check 规则 3（嵌套模块）自测通过："
          "嵌套短名节在位⇒放行 / 短名节缺失⇒判红 / 根记全名⇒放行 / 根缺同名节⇒判红 / "
          "别的模块 tag⇒不适用 / 非 git 目录⇒跳过 / 根 tag⇒不适用 / "
          "同 commit 多 tag⇒每个模块都被校验（假绿防线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

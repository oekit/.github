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

    if failures:
        print("✗ changelog_check 规则 3（嵌套模块）自测未过：")
        for f in failures:
            print("  -", f)
        return 1
    print("✓ changelog_check 规则 3（嵌套模块）自测通过："
          "嵌套短名节在位⇒放行 / 短名节缺失⇒判红 / 根记全名⇒放行 / 根缺同名节⇒判红 / "
          "别的模块 tag⇒不适用 / 非 git 目录⇒跳过 / 根 tag⇒不适用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

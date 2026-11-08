#!/usr/bin/env python3
"""`version_line_check.py` 的自测（oekit/.github#64）。

**为什么要有这个自测**：版本行门禁的判定链是
「ls-remote 取最新 tag → 逐行抠版本串 → 与最新 tag 判等」，
三环任何一环静默失效（regex 抠错组、判等写反、远端解析拿错仓），
门禁都会**绿灯放行漂移**——而它防的恰恰是「绿灯下 7 仓版本行落后」这类漂移。
⇒ 判据失效必须能在本地红出来，不能只靠矩阵真的漂了才发现。

**本自测覆盖的场景**（每条对应一个真实形态）：

  V1 全同步（README + AGENTS == 最新 tag）⇒ exit 0，无 ⚠
  V2 README 落后、AGENTS 同步 ⇒ 警告模式 exit 0 且输出 ⚠；--strict ⇒ exit 1
  V3 AGENTS 落后、README 同步 ⇒ 同 V2（两条线独立各管各的）
  V4 嵌套模块仓形态（README 只有嵌套模块安装行）⇒ README 检查跳过（不误伤
     registry/timertask 形态），AGENTS 照查
  V5 远端无 vX.Y.Z tag（未发过版）⇒ 跳过，exit 0
  V6 README **多个**根模块安装行、其中一个落后 ⇒ 逐个命中，落后的那条被抓
  V7 AGENTS.md 缺失 ⇒ 跳过注明；此时 README 落后 ⇒ --strict 仍 exit 1
  V8 版本行**领先**最新 tag（发版前预写）⇒ 同样算不一致（警告模式 ⚠）

**无副作用、不联网**：远端是临时目录里的**本地假 origin**（真 git 仓、真 tag），
走被测脚本的**真实** `git ls-remote` 路径——local path 远端不需要网络，但解析
tag 列表的代码与 CI 里完全同一条，不是 mock。

**变异自证**：把判等谓词退回「恒真」⇒ V2 --strict 必须转绿（exit 0）且 ⚠ 消失
⇒ 证明 V2 的红与 ⚠ 都由判等谓词驱动（断言有牙齿，不是恒红装饰）。

用法：python3 scripts/version_line_check_selftest.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "version_line_check.py"

AGENTS_OK = (
    "# AGENTS\n\n"
    "> 当前版本：`v0.8.14` · module：`github.com/oekit/httpx` "
    "· org：[oekit = Open Easy Kit](https://oekit.github.io)\n\n"
    "## 约定\n\n- x\n")
AGENTS_STALE = AGENTS_OK.replace("v0.8.14", "v0.8.13")
README_OK = (
    "# httpx\n\n"
    "## 安装\n\n"
    "```\n"
    "go get github.com/oekit/httpx@v0.8.14\n"
    "```\n")
README_STALE = README_OK.replace("v0.8.14", "v0.8.13")


def make_origin(root: Path, tags: list[str]) -> Path:
    """造假 origin：真 git 仓 + 真 tag（ls-remote 的注入点 = 本地路径远端）。"""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "go.mod").write_text("module github.com/oekit/httpx\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "fixture"], check=True)
    for t in tags:
        subprocess.run(["git", "-C", str(root), "tag", t], check=True)
    return root


def make_work(root: Path, origin: Path, files: dict[str, str]) -> Path:
    """造假工作仓：README / AGENTS + origin 指向假远端（与 CI 的 checkout 同构）。"""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(origin)],
                   check=True)
    return root


def run_check(work: Path, extra: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(work),
         "--repo", "oekit/httpx", "--remote", str(work.parent / "origin")] + extra,
        capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def main() -> int:
    failures: list[str] = []

    def check(cid: str, cond: bool, msg: str) -> None:
        if not cond:
            failures.append(f"{cid}：{msg}")

    def fixture(files: dict[str, str],
                origin_tags: list[str] | None = None) -> Path:
        """一个用例 = 一对临时目录（假 origin + 假工作仓）。"""
        base = Path(tempfile.mkdtemp(prefix="vlc-selftest-"))
        make_origin(base / "origin", origin_tags if origin_tags is not None
                    else ["v0.8.13", "v0.8.14"])
        return make_work(base / "work", base / "origin", files)

    # ── V1：全同步 ⇒ exit 0，无 ⚠ ──
    code, out = run_check(fixture({"README.md": README_OK, "AGENTS.md": AGENTS_OK}), [])
    check("V1", code == 0, f"版本行与最新 tag 一致时应放行，实际 exit={code} 输出={out[:200]!r}")
    check("V1", "⚠" not in out, f"全同步不应出现 ⚠，实际={out[:300]!r}")
    check("V1", "v0.8.14" in out, f"输出应点名最新 tag，实际={out[:300]!r}")

    # ── V2：README 落后、AGENTS 同步 ⇒ 警告模式 exit 0 + ⚠；--strict ⇒ exit 1 ──
    work = fixture({"README.md": README_STALE, "AGENTS.md": AGENTS_OK})
    code, out = run_check(work, [])
    check("V2", code == 0, f"警告模式不得挡合并（exit 0），实际 exit={code}")
    check("V2", "⚠" in out and "v0.8.13" in out,
          f"落后必须输出 ⚠ 并点名旧版本，实际={out[:300]!r}")
    code, out = run_check(work, ["--strict"])
    check("V2", code == 1, f"--strict 时落后必须 exit 1，实际 exit={code} 输出={out[:200]!r}")

    # ── V3：AGENTS 落后、README 同步 ⇒ 同 V2 ──
    work = fixture({"README.md": README_OK, "AGENTS.md": AGENTS_STALE})
    code, out = run_check(work, [])
    check("V3", code == 0, f"警告模式不得挡合并（exit 0），实际 exit={code}")
    check("V3", "⚠" in out and "AGENTS.md" in out,
          f"AGENTS 落后必须被点名，实际={out[:300]!r}")
    code, out = run_check(work, ["--strict"])
    check("V3", code == 1, f"--strict 时 AGENTS 落后必须 exit 1，实际 exit={code}")

    # ── V4：嵌套模块仓形态（README 只有嵌套安装行）⇒ README 跳过不误伤 ──
    readme_nested = README_OK.replace(
        "go get github.com/oekit/httpx@v0.8.14",
        "go get github.com/oekit/httpx/sub@v0.1.0")
    code, out = run_check(fixture({"README.md": readme_nested, "AGENTS.md": AGENTS_OK}), [])
    check("V4", code == 0, f"嵌套安装行不应触发根模块检查（不误伤），实际 exit={code} 输出={out[:300]!r}")
    check("V4", "⚠" not in out, f"嵌套形态不该有 ⚠，实际={out[:300]!r}")
    check("V4", "跳过 README 检查" in out, f"应注明 README 检查被跳过，实际={out[:300]!r}")

    # ── V5：远端无 vX.Y.Z tag ⇒ 跳过 exit 0 ──
    code, out = run_check(fixture({"README.md": README_STALE, "AGENTS.md": AGENTS_STALE},
                                 origin_tags=["mod/v0.9.9"]), [])
    check("V5", code == 0, f"远端无语义版本 tag 应跳过放行，实际 exit={code} 输出={out[:200]!r}")

    # ── V6：README 多个安装行、其一落后 ⇒ 逐个命中 ──
    readme_multi = ("# httpx\n\n```\ngo get github.com/oekit/httpx@v0.8.14\n```\n\n"
                    "也可以固定到旧版验证：\n\n```\ngo get github.com/oekit/httpx@v0.8.13\n```\n")
    stale_lineno = readme_multi.count("\n", 0, readme_multi.index("v0.8.13")) + 1
    code, out = run_check(fixture({"README.md": readme_multi, "AGENTS.md": AGENTS_OK}), [])
    check("V6", code == 0 and out.count("⚠") >= 1,
          f"多安装行其一落后必须被抓出（警告模式），实际 exit={code} 输出={out[:400]!r}")
    check("V6", f"README.md:{stale_lineno}" in out,
          f"应点名落后安装行的行号（第 {stale_lineno} 行），实际={out[:400]!r}")

    # ── V7：AGENTS.md 缺失 ⇒ 跳过注明；README 落后 ⇒ --strict 仍 exit 1 ──
    work = fixture({"README.md": README_STALE})
    code, out = run_check(work, [])
    check("V7", code == 0 and "跳过 AGENTS 检查" in out,
          f"AGENTS 缺失应注明跳过，实际 exit={code} 输出={out[:300]!r}")
    code, out = run_check(work, ["--strict"])
    check("V7", code == 1, f"AGENTS 缺失时 README 落后仍须 exit 1，实际 exit={code}")

    # ── V8：版本行领先最新 tag（发版前预写）⇒ 同样算不一致 ──
    readme_ahead = README_OK.replace("v0.8.14", "v0.9.0")
    work = fixture({"README.md": readme_ahead, "AGENTS.md": AGENTS_OK.replace("v0.8.14", "v0.9.0")})
    code, out = run_check(work, [])
    check("V8", code == 0 and "⚠" in out and "v0.9.0" in out,
          f"领先 tag 的版本行也要 ⚠（警告模式），实际 exit={code} 输出={out[:400]!r}")
    code, _ = run_check(work, ["--strict"])
    check("V8", code == 1, f"--strict 时领先 tag 也必须 exit 1，实际 exit={code}")

    # ═══ 变异自证：判等谓词退回「恒真」⇒ V2 必须转绿且 ⚠ 消失 ═══
    #   ⇒ 证明 V2 的红/⚠ 由判等谓词驱动（不是恒红装饰），谓词失效时门禁会哑
    #     ——而自测必须能在门禁哑掉的时候红出来。
    saved_src = SCRIPT.read_text(encoding="utf-8")
    mutated = saved_src.replace(
        "    return parsed == latest",
        "    return True  # ← 变异：判等谓词恒真")
    if mutated == saved_src:
        print("✗ 变异自证失败：没找到判等谓词实现（代码形态已变？）")
        return 1
    try:
        SCRIPT.write_text(mutated, encoding="utf-8")
        work = fixture({"README.md": README_STALE, "AGENTS.md": AGENTS_OK})
        code_w, out_w = run_check(work, [])
        code_s, _out_s = run_check(work, ["--strict"])
        if code_w != 0 or "⚠" in out_w:
            print("✗ 变异「判等恒真」后警告模式形态异常"
                  f"（exit={code_w}，输出={out_w[:200]!r}）⇒ ⚠ 不由判等谓词驱动？")
            return 1
        if code_s != 0:
            print("✗ 变异「判等恒真」没有复现放行（--strict 仍判红）"
                  f"⇒ V2 的红不依赖判等谓词，证明不了旧口径漏检这一层")
            return 1
        print("✓ 变异「判等恒真」复现了**放行**（旧口径判绿 ⇒ 落后版本行不被抓；"
              "新口径判红/⚠ ⇒ V2/V3/V6/V8 有牙齿）")
    finally:
        SCRIPT.write_text(saved_src, encoding="utf-8")

    if failures:
        print("✗ version_line_check 自测未过：")
        for f in failures:
            if f:  # 过滤占位空断言
                print("  -", f)
        return 1
    print("✓ version_line_check 自测通过："
          "全同步⇒放行 / README 落后⇒⚠（strict⇒红）/ AGENTS 落后⇒⚠（strict⇒红）/ "
          "嵌套安装行⇒不误伤 / 无语义 tag⇒跳过 / 多安装行逐个命中 / "
          "AGENTS 缺失⇒跳过注明 / 领先 tag⇒同样判不一致 / 判等谓词变异自证有牙齿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

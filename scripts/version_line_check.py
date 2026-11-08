"""README 安装行 / AGENTS 当前版本行 与远端最新 tag 的一致性检查（oekit/.github#64）。

用法:
  python3 version_line_check.py [--strict] [--repo OWNER/NAME] [--remote NAME|URL] [--root DIR]

检查内容（对仓根 module = github.com/<owner>/<name>）:
  1. README.md 里所有 `go get github.com/<owner>/<name>@vX.Y.Z` 安装行 == 最新 vX.Y.Z tag；
  2. AGENTS.md 的「当前版本」行（`> 当前版本：`vX.Y.Z` · ...`）== 最新 vX.Y.Z tag。

  「最新 tag」口径：`git ls-remote --tags` 上的**纯语义版本** tag（vX.Y.Z）取最大；
  嵌套模块 tag（如 nacos/v0.1.3）与本检查无关——嵌套模块的安装行（如
  `go get github.com/oekit/registry/nacos@v...`）**不在检查范围**（它们的
  发版节奏独立，历史上就有滞后，属另一条线索，见 .github#63 的嵌套面）。

模式:
  · 缺省 = **警告模式**：版本行落后输出 ⚠ 但 exit 0。
    为什么要先警告：2026-11-08 全矩阵还有 7 个仓的版本行落后于最新 tag，
    门禁一上就全红 ⇒ 红色失去信号价值。先立规矩不挡路，等 refresh_versions
    轮把全矩阵 ⚠ 清零后，把 go-ci.yml 里的调用行加上 --strict 即切硬门禁。
  · --strict = 硬门禁：任何「版本行 != 最新 tag」⇒ exit 1。

跳过条件（两种模式都放行并注明原因，不误伤）:
  · 远端无 vX.Y.Z 形态 tag（尚未发过版的仓）；
  · README 无根模块安装行（嵌套模块仓形态 / 无安装示例）；
  · AGENTS.md 不存在，或其中无「当前版本」行。

退出码:
  · 警告模式恒 0（版本行漂移只 ⚠）；
  · --strict 且有漂移 ⇒ 1；
  · 脚本自身坏掉（git 不可用、远端不可达等门禁健康问题）⇒ 非 0（大声失败，
    不允许门禁静默失效）。

注入点（自测用，见 version_line_check_selftest.py）:
  · --remote 接受**任意 git URL 含本地路径** ⇒ 自测用临时目录造假 origin
    （真 ls-remote、真 tag 列表，不 mock 网络路径）；
  · --repo 显式给 slug，跳过对 `git remote get-url origin` 的解析；
  · ls_remote_tags() 是独立函数，可被 monkeypatch（如未来要测网络重试）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _semver(tag: str) -> tuple[int, int, int] | None:
    m = TAG_RE.match(tag)
    if not m:
        return None
    return tuple(map(int, m.groups()))  # type: ignore[return-value]


def ls_remote_tags(remote: str, cwd: str) -> list[str]:
    """`git ls-remote --tags` 取全部 tag 名（去 ^{}、去重、排序）。

    带 3 次重试：ls-remote 是这一步唯一的网络动作，偶发失败不该把 CI 标红
    （与 go mod download 步骤同一处理哲学：抖动重试，确定性错误立刻抛）。
    """
    last_err: Exception | str | None = None
    for attempt in range(1, 4):
        try:
            r = subprocess.run(
                ["git", "ls-remote", "--tags", remote, "refs/tags/v*"],
                capture_output=True, text=True, cwd=cwd, timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            last_err = e
            r = None  # type: ignore[assignment]
        if r is not None and r.returncode == 0:
            names = set()
            for line in r.stdout.splitlines():
                ref = line.split("\t", 1)[-1].strip() if "\t" in line else ""
                ref = ref.removeprefix("refs/tags/").removesuffix("^{}")
                if ref:
                    names.add(ref)
            return sorted(names)
        last_err = (r.stderr.strip() if r is not None and r.stderr.strip() else
                    f"exit={getattr(r, 'returncode', '?')}")
        if attempt < 3:
            time.sleep(attempt * 5)
    raise RuntimeError(f"git ls-remote --tags {remote} 连续 3 次失败: {last_err}")


def latest_semver_tag(tags: list[str]) -> str | None:
    """tag 列表里的最大 vX.Y.Z；无语义版本 tag ⇒ None（未发过版）。"""
    best: tuple[int, int, int] | None = None
    best_tag: str | None = None
    for t in tags:
        v = _semver(t)
        if v is None:
            continue  # 嵌套 tag（mod/vX.Y.Z）等非根版本形态不参与
        if best is None or v > best:
            best, best_tag = v, t
    return best_tag


def resolve_repo_slug(args_repo: str | None, root: str) -> str | None:
    """仓 slug（owner/name）：--repo > GITHUB_REPOSITORY > remote get-url 解析。"""
    if args_repo:
        return args_repo.strip().strip("/")
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env:
        return env
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, cwd=root, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    m = re.search(r"[/:]([^/:]+?)(?:\.git)?/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _version_matches(parsed: str | None, latest: str) -> bool:
    """版本行是否与最新 tag 一致（独立成函数：变异自测的锚点）。"""
    return parsed == latest


def scan_file(path: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    """逐行找版本串，返回 (行号, vX.Y.Z) 列表（保留全部命中供逐个判定）。"""
    hits: list[tuple[int, str]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                m = pattern.search(line)
                if m:
                    hits.append((i, m.group(1)))
    except OSError:
        pass  # 文件不存在 ⇒ 上层按「跳过」处理
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="硬门禁：版本行与最新 tag 不一致即 exit 1（缺省只 ⚠）")
    ap.add_argument("--repo", default=None,
                    help="仓 slug（owner/name）；缺省按 GITHUB_REPOSITORY / origin 解析")
    ap.add_argument("--remote", default="origin",
                    help="ls-remote 的远端名或 URL（自测可传本地路径；缺省 origin）")
    ap.add_argument("--root", default=".",
                    help="仓根目录（缺省当前目录；README.md / AGENTS.md 都按它解析）")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    slug = resolve_repo_slug(args.repo, root)
    if not slug:
        print("[version-line-check] ❌ 无法解析仓 slug（--repo / GITHUB_REPOSITORY / origin 均失败）")
        return 2
    owner, name = slug.split("/", 1)
    module_prefix = f"github.com/{owner}/{name}@"

    tags = ls_remote_tags(args.remote, root)
    latest = latest_semver_tag(tags)
    if latest is None:
        print(f"[version-line-check] ⏭ 远端无 vX.Y.Z 形态 tag（{slug} 尚未发版），跳过")
        return 0

    readme_pat = re.compile(re.escape(module_prefix) + r"(v\d+\.\d+\.\d+)")
    agents_pat = re.compile(r"当前版本[：:]\s*\*{0,2}`{0,2}(v\d+\.\d+\.\d+)")

    problems: list[str] = []   # 违规（⚠ / --strict 时 ❌）
    oks: list[str] = []
    skips: list[str] = []

    readme_hits = scan_file(os.path.join(root, "README.md"), readme_pat)
    if not readme_hits:
        skips.append(f"README 无根模块安装行（{module_prefix}vX.Y.Z），跳过 README 检查")
    for lineno, ver in readme_hits:
        if _version_matches(ver, latest):
            oks.append(f"README.md:{lineno} 安装行 {ver} == 最新 tag")
        else:
            problems.append(
                f"README.md:{lineno} 安装行 pin {ver} != 最新 tag {latest} "
                f"（消费方照抄装到的不是最新发布版）")

    agents_hits = scan_file(os.path.join(root, "AGENTS.md"), agents_pat)
    if not agents_hits:
        skips.append("AGENTS.md 不存在或无「当前版本」行，跳过 AGENTS 检查")
    for lineno, ver in agents_hits:
        if _version_matches(ver, latest):
            oks.append(f"AGENTS.md:{lineno} 当前版本 {ver} == 最新 tag")
        else:
            problems.append(
                f"AGENTS.md:{lineno} 当前版本 {ver} != 最新 tag {latest} "
                f"（按 AGENTS 判断兼容性会读到过期基线）")

    if skips:
        for s in skips:
            print(f"[version-line-check] ⏭ {s}")
    for o in oks:
        print(f"[version-line-check] ✓ {o}")
    if problems:
        mark = "❌" if args.strict else "⚠"
        for p in problems:
            print(f"[version-line-check] {mark} {p}")
        if args.strict:
            print(f"[version-line-check] ❌ --strict：{len(problems)} 项版本行与最新 tag {latest} 不一致")
            return 1
        print(f"[version-line-check] ⚠ 警告模式：{len(problems)} 项版本行落后于最新 tag {latest}"
              f"（不挡合并；refresh_versions 轮追平后切 --strict 升硬门禁）")
        return 0
    print(f"[version-line-check] ✓ 版本行与最新 tag 一致（{latest}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

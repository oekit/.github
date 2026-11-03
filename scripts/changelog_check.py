"""CHANGELOG/tag 一致性门禁（oekit/.github#6）。
用法: python3 changelog_check.py [CHANGELOG.md 路径]（缺省 ./CHANGELOG.md）
规则:
  1. 主模块版本节 ## [vX.Y.Z] 按 semver 严格递减，无重复；
  2. [Unreleased] 只允许出现在最顶部一个；任何完整标题不得重复；
  3. HEAD 恰为 tag 时（git describe --exact-match）：必须有同名小节，
     且 [Unreleased]（若存在）不得有正文。
"""
import os
import re
import subprocess
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "CHANGELOG.md"
errors = []
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    print(f"[changelog-check] {path} 不存在，跳过")
    sys.exit(0)

sec_pat = re.compile(r"^## \[([^\]]+)\]")
ver_pat = re.compile(r"^v\d+\.\d+\.\d+$")

sections = [(i, m.group(1).strip()) for i, l in enumerate(lines) if (m := sec_pat.match(l))]

# 规则 2：完整标题不得重复
seen = {}
for lineno, title in sections:
    if title in seen:
        errors.append(f"{path}:{lineno + 1}: 版本节 [{title}] 重复（首次出现于第 {seen[title] + 1} 行）")
    else:
        seen[title] = lineno

# 规则 1：主模块版本节（标题恰为 vX.Y.Z）严格递减
prev = None
prev_lineno = 0
for lineno, title in sections:
    if title == "Unreleased":
        if sections.index((lineno, title)) != 0:
            errors.append(f"{path}:{lineno + 1}: [Unreleased] 只允许出现在最顶部")
        continue
    if not ver_pat.match(title):
        continue  # 嵌套模块标题（hlog/v0.1.3 等）不参与主序列排序
    ver = tuple(map(int, title[1:].split(".")))
    if prev is not None and ver >= prev:
        errors.append(f"{path}:{lineno + 1}: [{title}] 未严格递减（上一主模块节 [{prev_ver}] @ 第 {prev_lineno + 1} 行）")
    prev, prev_ver, prev_lineno = ver, title, lineno

# 规则 3：tag 检出时同名小节 + Unreleased 无正文
# ★★ **必须取 HEAD 上的全部 tag**（`git tag --points-at HEAD`），**不能**只取
#    `git describe --exact-match` 返回的**一个**：
#    同一 commit 上有多个 tag 时 describe **只返回任意一个**（实测优先返回嵌套
#    tag 名）⇒ 另一个模块的版本节**从不被校验** ⇒ 缺了也判绿（**假绿**）。
#    实测：registry 仓 HEAD 停在 `nacos/v0.1.3` + `nacosimpl/v0.1.4` 同 commit 上
#    ⇒ 删掉 `nacosimpl/CHANGELOG.md` 的 `## [v0.1.4]` 短名节
#    ⇒ describe 口径判 ✓（假绿）；全 tag 口径判 ❌（真红）。
tags = []
try:
    r = subprocess.run(["git", "tag", "--points-at", "HEAD"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        tags = [t.strip() for t in r.stdout.splitlines() if t.strip()]
except FileNotFoundError:
    pass
if tags:
    # ★ 嵌套模块的 CHANGELOG 按**嵌套约定**记**短名**节（`## [v0.1.3]`），
    #   而 tag 名是**全名**（`nacosimpl/v0.1.3`）⇒ 拿 tag 名当字符串去比节标题
    #   ⇒ **必然误判**（实测：同 commit 上有多个 tag 时，`git describe --exact-match`
    #   **优先返回嵌套 tag 名** ⇒ 100% 命中）。
    #   ⇒ 精确影响面 = **有独立 CHANGELOG 的嵌套模块**（实测 7 个，**全部用短名节**）；
    #     根 CHANGELOG 记**全名**（`## [nacosimpl/v0.1.3]`）⇒ 根仓**不误判**。
    #   ⇒ 这里按**嵌套约定自己判**：HEAD 在**本模块**的嵌套 tag 上 ⇒ 须有**短名**节；
    #     HEAD 在**别的模块**的 tag 上 ⇒ 本文件**不适用**（跳过同名节检查）。
    #   ⇒ 另：文件在**非 git 目录**时取 tag 失败 ⇒ tag 列表为空 ⇒ 整段跳过（CI 查仓根
    #     ⇒ 看不到这条红 ⇒ 只有本地 validate_changelog 会撞）。
    # ★★ **逐个 tag 判定**（不是只判 describe 返回的那一个）⇒ 同 commit 上多个 tag 时
    #    **每个模块都被校验到** ⇒ 上面的「假绿」不再存在。
    mod_dir = os.path.dirname(path)
    leaf = os.path.basename(mod_dir) if mod_dir else ""
    for tag in sorted(tags):
        m = re.fullmatch(r"(?:(.+)/)?(v\d+\.\d+\.\d+)", tag)
        tag_sub, tag_ver = (m.group(1), m.group(2)) if m else (None, tag)
        if mod_dir:
            belongs = bool(tag_sub) and (tag_sub == leaf or tag_sub.endswith("/" + leaf))
            if belongs:
                # 同名小节匹配用前缀（小节行带日期后缀，如 "## [v0.1.3] — 2026-11-02"）
                if not any(l.startswith(f"## [{tag_ver}]") for l in lines):
                    errors.append(f"{path}: HEAD 处于 tag {tag}（本模块 {leaf}），"
                                  f"但无同名小节 ## [{tag_ver}]（嵌套模块按**短名**记节）")
            # tag 属于**别的模块** ⇒ 本文件不适用 ⇒ 跳过同名节检查（Unreleased 检查仍做）
        else:
            # 同名小节匹配用前缀（小节行带日期后缀，如 "## [v0.13.1] — 2026-10-18"）
            if not any(l.startswith(f"## [{tag}]") for l in lines):
                errors.append(f"{path}: HEAD 处于 tag {tag}，但无同名小节 ## [{tag}]")
    if "Unreleased" in seen:
        u_lineno = seen["Unreleased"]
        next_sec = next((i for i, t in sections if i > u_lineno), len(lines))
        body = [l for l in lines[u_lineno + 1:next_sec] if l.strip() and not l.startswith("## [")]
        if body:
            errors.append(f"{path}: 已发布 {sorted(tags)[0]} 仍残留 [Unreleased] 正文（{len(body)} 行）——"
                          f"版本节未定名，消费方无法归属变更")

if errors:
    print(f"[changelog-check] ❌ {len(errors)} 项：")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print("[changelog-check] ✓")

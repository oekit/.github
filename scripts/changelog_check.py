"""CHANGELOG/tag 一致性门禁（oekit/.github#6）。
用法: python3 changelog_check.py [CHANGELOG.md 路径]（缺省 ./CHANGELOG.md）
规则:
  1. 主模块版本节 ## [vX.Y.Z] 按 semver 严格递减，无重复；
  2. [Unreleased] 只允许出现在最顶部一个；任何完整标题不得重复；
  3. HEAD 恰为 tag 时（git describe --exact-match）：必须有同名小节，
     且 [Unreleased]（若存在）不得有正文。
"""
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
tag = ""
try:
    r = subprocess.run(["git", "describe", "--exact-match", "--tags", "HEAD"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        tag = r.stdout.strip()
except FileNotFoundError:
    pass
if tag and not tag.startswith("wormhole"):
    if f"## [{tag}]" not in lines:
        errors.append(f"{path}: HEAD 处于 tag {tag}，但无同名小节 ## [{tag}]")
    if "Unreleased" in seen:
        u_lineno = seen["Unreleased"]
        next_sec = next((i for i, t in sections if i > u_lineno), len(lines))
        body = [l for l in lines[u_lineno + 1:next_sec] if l.strip() and not l.startswith("## [")]
        if body:
            errors.append(f"{path}: 已发布 {tag} 仍残留 [Unreleased] 正文（{len(body)} 行）——"
                          f"版本节未定名，消费方无法归属变更")

if errors:
    print(f"[changelog-check] ❌ {len(errors)} 项：")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print("[changelog-check] ✓")

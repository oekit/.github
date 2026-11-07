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

ver_pat = re.compile(r"^v\d+\.\d+\.\d+$")

# ★★ 切节改为**枚举行内全部标题**（oekit/.github#61）：行首锚定的 match 只取
#    每行第一个 ⇒「两个版本标题粘连在同一行」时第二个既不被切节也不被判重
#    ⇒ 门禁存在结构性假绿通道（concurrency CHANGELOG.md:219 实测）。
head_re = re.compile(r"## \[([^\]]+)\]")
sections = []
for i, l in enumerate(lines):
    for m in head_re.finditer(l):
        sections.append((i, m.group(1).strip()))

# ★★ 版本标题必须**整行独占**（#61 第 2 形态）：行首是 `## [` 的行必须恰好
#    整行是一个标题——fullmatch 抓「] 被甩到下一行」的断裂形态，
#    count==1 抓「第二个 ## [ 粘在同一行」的粘连形态（fullmatch 的 `.*`
#    后缀会吞掉粘连尾部，所以两条判据缺一不可）。
TITLE_LINE_RE = re.compile(r"## \[[^\]]+\](?: — .*)?")
for i, l in enumerate(lines):
    if l.startswith("## [") and (TITLE_LINE_RE.fullmatch(l) is None or l.count("## [") != 1):
        errors.append(f"{path}:{i + 1}: 版本标题行格式损坏（标题粘连或跨行断裂）: {l[:60]!r}")


def _is_root_changelog(path: str) -> bool:
    """被检文件是不是「当前仓（cwd）的根 CHANGELOG.md」。

    ★★ 判据**必须与调用形态无关**（2026-11-04 实测修的洞）：
      旧写法用 `os.path.dirname(path)` 是否为空来区分「根面 / 嵌套面」⇒
      传 `./CHANGELOG.md` 或**绝对路径**时 dirname 非空 ⇒ **误走嵌套分支** ⇒
      根面文件的「同名（全名）节」检查被**整段静默跳过** ⇒ **假绿**。

      实测（registry @ 29fc80b，HEAD 同带 `nacos/v0.1.3` + `nacosimpl/v0.1.4`，
      删掉根面 `## [nacosimpl/v0.1.4]` 节后）：
        | 调用形态              | 旧判定 | 本判定 |
        |-----------------------|--------|--------|
        | `CHANGELOG.md`（CI）   | 红 ✔   | 红 ✔   |
        | `./CHANGELOG.md`       | 绿 ✘   | 红 ✔   |
        | `<绝对路径>/CHANGELOG.md` | 绿 ✘ | 红 ✔   |

      而**真实链路就是绝对路径**：`release_train.validate_changelog` 传 `str(path)`
      （path = 仓根/CHANGELOG.md ⇒ 绝对）⇒ ⇒ **CI 侧查得到、本地预校验侧查不到**
      ⇒ 同一份 CHANGELOG，CI 判红、本地放行。

    ⇒ 改问 git：取**当前仓**（cwd —— 与上面取 tag 的仓**同一个**，否则两套「仓」
      混用会判出无意义的结论）的 toplevel，再比对这个文件是不是就是
      `<toplevel>/CHANGELOG.md`。
      · cwd 不是 git 仓 ⇒ 读不到 toplevel ⇒ 判 False（退回旧行为，不误伤）；
      · 文件在仓根但**不叫** CHANGELOG.md（如 docs/CHANGELOG.md）⇒ 判 False
        （它不是根面 ⇒ 走嵌套分支 ⇒ 没有匹配的 tag ⇒ 本文件不适用 ⇒ 放行）。
    """
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return False
    if r.returncode != 0 or not r.stdout.strip():
        return False
    top = os.path.realpath(r.stdout.strip())
    try:
        return os.path.realpath(path) == os.path.join(top, "CHANGELOG.md")
    except OSError:
        return False

seen = {}
for lineno, title in sections:
    if title in seen:
        errors.append(f"{path}:{lineno + 1}: 版本节 [{title}] 重复（首次出现于第 {seen[title] + 1} 行）")
    else:
        seen[title] = lineno

# 规则 1：主模块版本节（标题恰为 vX.Y.Z）严格递减
prev = None
prev_lineno = 0
for _idx, (lineno, title) in enumerate(sections):   # 枚举索引：index() 在同名
    if title == "Unreleased":                        # tuple 重复时取第一个（#61）
        if _idx != 0:
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
    # ★★ 判「根面 vs 嵌套面」**不看路径参数形态**（2026-11-04）——
    #   看 `os.path.dirname(path)` 是否为空 ⇒ `./CHANGELOG.md` 与绝对路径都被
    #   误判成嵌套面 ⇒ 根面的「同名（全名）节」检查被静默跳过 ⇒ **假绿**
    #   （真实链路 `release_train.validate_changelog` 传的就是绝对路径）。
    #   ⇒ 改问 git（详见 _is_root_changelog 的 docstring）。
    is_root_file = _is_root_changelog(path)
    # mod_dir / leaf 仍按**原始路径参数**取：嵌套面的模块目录名要与 tag 前缀匹配，
    #   用相对形态的路径参数才与仓内目录名一致（绝对路径的 basename 一样，但
    #   经软链的仓内目录名会不同 ⇒ 以参数形态为准）。
    mod_dir = os.path.dirname(path)
    leaf = os.path.basename(mod_dir) if mod_dir else ""
    for tag in sorted(tags):
        m = re.fullmatch(r"(?:(.+)/)?(v\d+\.\d+\.\d+)", tag)
        tag_sub, tag_ver = (m.group(1), m.group(2)) if m else (None, tag)
        if not is_root_file:
            belongs = bool(tag_sub) and (tag_sub == leaf or tag_sub.endswith("/" + leaf))
            if belongs:
                # 同名小节匹配用切节标题集合（断裂形态对旧 startswith 不可见，#61）
                if not any(t == tag_ver for _, t in sections):
                    errors.append(f"{path}: HEAD 处于 tag {tag}（本模块 {leaf}），"
                                  f"但无同名小节 ## [{tag_ver}]（嵌套模块按**短名**记节）")
            # tag 属于**别的模块** ⇒ 本文件不适用 ⇒ 跳过同名节检查（Unreleased 检查仍做）
        else:
            # 同名小节匹配用切节标题集合（断裂形态对旧 startswith 不可见，#61）
            if not any(t == tag for _, t in sections):
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

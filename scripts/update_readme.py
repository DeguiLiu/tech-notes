#!/usr/bin/env python3
"""Auto-generate README.md from article front matter.

Usage:
    python3 scripts/update_readme.py          # preview to stdout
    python3 scripts/update_readme.py --write  # overwrite README.md
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = ROOT / "content" / "posts"

# Category display order and labels
CATEGORIES = [
    ("architecture", "架构设计"),
    ("performance", "性能优化"),
    ("practice", "工程实践"),
    ("pattern", "设计模式"),
    ("interview", "面试题"),
    ("misc", "杂项"),
]

RELATED_PROJECTS = """\
## 关联项目

| 项目 | 地址 | 说明 |
|------|------|------|
| newosp | [GitHub](https://github.com/DeguiLiu/newosp) | C++17 Header-Only 嵌入式基础设施库 |
| mccc-bus | [Gitee](https://gitee.com/liudegui/mccc-bus) | 高性能无锁消息总线 |
| eventpp (fork) | [Gitee](https://gitee.com/liudegui/eventpp) | eventpp ARM-Linux 优化分支 |
| message_bus | [Gitee](https://gitee.com/liudegui/message_bus) | C++11 非阻塞消息总线 |
| lock-contention-benchmark | [Gitee](https://gitee.com/liudegui/lock-contention-benchmark) | 锁竞争基准测试 |
| ztask-cpp | [Gitee](https://gitee.com/liudegui/ztask-cpp) | C++14 Header-Only 合作式任务调度器 |
"""

TECH_STACK = """\
## 主要技术领域

- C/C++ 系统编程 (C11, C++14/17)
- 嵌入式系统 (ARM-Linux, RTOS, MCU)
- 并发与无锁编程 (CAS, SPSC, MPSC)
- 性能优化与基准测试
- 架构设计与设计模式
"""


def parse_frontmatter(filepath):
    """Extract title, date, draft, categories from YAML front matter."""
    text = filepath.read_text(encoding="utf-8")
    if not text.lstrip().startswith("---"):
        return None

    fm_end = text.index("---", 3) + 3
    fm = text[:fm_end]

    title_m = re.search(r'^title:\s*"(.+?)"', fm, re.MULTILINE)
    date_m = re.search(r"^date:\s*(\S+)", fm, re.MULTILINE)
    draft_m = re.search(r"^draft:\s*(true|false)", fm, re.MULTILINE)
    cat_m = re.search(r'^categories:\s*\["?(.+?)"?\]', fm, re.MULTILINE)

    if not title_m:
        return None

    return {
        "title": title_m.group(1),
        "date": date_m.group(1) if date_m else "unknown",
        "draft": draft_m.group(1) == "true" if draft_m else False,
        "categories": cat_m.group(1).strip('"') if cat_m else "",
        "filename": filepath.name,
        "relpath": str(filepath.relative_to(POSTS_DIR)),
    }


def scan_articles():
    """Scan all categories and return {category: [articles]}."""
    result = {}
    for cat_dir, _ in CATEGORIES:
        cat_path = POSTS_DIR / cat_dir
        if not cat_path.is_dir():
            continue
        articles = []
        for md in sorted(cat_path.glob("*.md")):
            if md.name == "_index.md":
                continue
            info = parse_frontmatter(md)
            if info:
                articles.append(info)
        # Sort by date descending
        articles.sort(key=lambda a: a["date"], reverse=True)
        result[cat_dir] = articles
    return result


def generate_readme(articles_by_cat):
    """Generate README.md content."""
    lines = []
    lines.append("# 编程技术文章集\n")
    lines.append(
        "面向系统编程与软件工程的技术文章，"
        "涵盖架构设计、性能优化、并发编程、设计模式、开发工具等主题。\n"
    )

    # Directory structure
    lines.append("## 目录结构\n")
    lines.append("```")
    for cat_dir, cat_label in CATEGORIES:
        count = len(articles_by_cat.get(cat_dir, []))
        draft_count = sum(
            1 for a in articles_by_cat.get(cat_dir, []) if a["draft"]
        )
        pub = count - draft_count
        suffix = f" ({pub} 篇)" if draft_count == 0 else f" ({pub} 篇公开, {draft_count} 篇草稿)"
        lines.append(f"{cat_dir + '/':20s}-- {cat_label}{suffix}")
    lines.append("```\n")

    # Total count
    total = sum(len(v) for v in articles_by_cat.values())
    total_pub = sum(
        1 for v in articles_by_cat.values() for a in v if not a["draft"]
    )
    lines.append(f"共 **{total}** 篇文章 ({total_pub} 篇公开, {total - total_pub} 篇草稿)\n")

    # Article index by category
    lines.append("## 文章索引\n")
    for cat_dir, cat_label in CATEGORIES:
        arts = articles_by_cat.get(cat_dir, [])
        if not arts:
            continue
        pub_count = sum(1 for a in arts if not a["draft"])
        lines.append(f"### {cat_dir}/ -- {cat_label} ({pub_count} 篇)\n")
        lines.append("| 文件 | 标题 | 日期 |")
        lines.append("|------|------|------|")
        for a in arts:
            fname = a["filename"]
            relpath = a["relpath"]
            title = a["title"]
            date = a["date"]
            draft_tag = " *(草稿)*" if a["draft"] else ""
            lines.append(
                f"| [{fname}]({relpath}) | {title}{draft_tag} | {date} |"
            )
        lines.append("")

    # Related projects and tech stack
    lines.append(RELATED_PROJECTS)
    lines.append(TECH_STACK)

    return "\n".join(lines)


def main():
    articles = scan_articles()
    readme = generate_readme(articles)

    if "--write" in sys.argv:
        readme_path = ROOT / "README.md"
        readme_path.write_text(readme, encoding="utf-8")
        total = sum(len(v) for v in articles.values())
        print(f"README.md updated: {total} articles")
    else:
        print(readme)
        print("\n--- Run with --write to overwrite README.md ---")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Batch inject YAML frontmatter into markdown articles for Hugo."""

import os
import re
from pathlib import Path

CATEGORY_MAP = {
    'blog': 'blog',
    'architecture': 'architecture',
    'mccc': 'mccc',
}

TAG_RULES = [
    (r'C\+\+17', 'C++17'),
    (r'C\+\+14', 'C++14'),
    (r'C\+\+11', 'C++11'),
    (r'lock.?free|无锁|MPSC|SPSC|CAS', 'lock-free'),
    (r'ARM|Cortex|NEON|aarch64|Zynq', 'ARM'),
    (r'RTOS|RT-Thread|FreeRTOS', 'RTOS'),
    (r'嵌入式|embedded', 'embedded'),
    (r'性能|benchmark|基准|吞吐', 'performance'),
    (r'消息总线|message.?bus|AsyncBus|eventpp', 'message-bus'),
    (r'状态机|HSM|state.?machine', 'state-machine'),
    (r'newosp', 'newosp'),
    (r'MCCC|mccc', 'MCCC'),
    (r'MISRA', 'MISRA'),
    (r'nginx', 'nginx'),
    (r'日志|log(?:ging|helper)', 'logging'),
    (r'CRC|校验', 'CRC'),
    (r'FPGA|PL.*PS', 'FPGA'),
    (r'零拷贝|zero.?copy|ShmChannel', 'zero-copy'),
    (r'行为树|BehaviorTree|bt\.hpp', 'behavior-tree'),
    (r'DMA', 'DMA'),
    (r'串口|serial|UART', 'serial'),
    (r'回调|callback|FixedFunction', 'callback'),
    (r'内存池|MemPool|mem_pool', 'memory-pool'),
    (r'死锁|deadlock', 'deadlock'),
    (r'调度|scheduler|executor', 'scheduler'),
    (r'RK3506|异构', 'heterogeneous'),
    (r'激光雷达|LiDAR|点云', 'LiDAR'),
]


def extract_title(content):
    """Extract title from first # heading."""
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('# ') and not line.startswith('## '):
            return line[2:].strip()
    return 'Untitled'


def extract_summary(content):
    """Extract summary from content."""
    lines = content.split('\n')
    # Try first blockquote that's not a link/reference
    for line in lines:
        line = line.strip()
        if line.startswith('> ') and '原文链接' not in line and 'http' not in line:
            text = line[2:].strip()
            if len(text) > 10:
                return text[:200]
    # Fallback: first paragraph after title
    found_title = False
    for line in lines:
        line = line.strip()
        if line.startswith('# ') and not line.startswith('## '):
            found_title = True
            continue
        if found_title and line and not line.startswith('>') and not line.startswith('#') and not line.startswith('---') and not line.startswith('|'):
            return line[:200]
    return ''


def detect_tags(content):
    """Detect tags from article content."""
    tags = set()
    for pattern, tag in TAG_RULES:
        if re.search(pattern, content, re.IGNORECASE):
            tags.add(tag)
    return sorted(tags)


def escape_yaml_string(s):
    """Escape a string for YAML double-quoted value."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def remove_first_heading(content):
    """Remove the first # heading line from content."""
    lines = content.split('\n')
    result = []
    removed = False
    for line in lines:
        if not removed and line.strip().startswith('# ') and not line.strip().startswith('## '):
            removed = True
            # Also remove blank line after title
            continue
        # Skip blank line immediately after removed title
        if removed and not result and not line.strip():
            continue
        result.append(line)
    return '\n'.join(result)


def process_file(filepath, category):
    """Add frontmatter to a single file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Skip if already has frontmatter
    if content.lstrip().startswith('---'):
        print(f'  SKIP (has frontmatter): {filepath}')
        return

    title = extract_title(content)
    summary = extract_summary(content)
    tags = detect_tags(content)

    tags_str = ', '.join(f'"{t}"' for t in tags)
    summary_escaped = escape_yaml_string(summary)

    frontmatter = f'''---
title: "{escape_yaml_string(title)}"
date: 2026-02-15
draft: false
categories: ["{category}"]
tags: [{tags_str}]
summary: "{summary_escaped}"
ShowToc: true
TocOpen: true
---

'''
    new_content = remove_first_heading(content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(frontmatter + new_content)

    print(f'  OK: {filepath} -> title="{title}", tags={tags}')


def main():
    base = Path('content/posts')
    count = 0
    for category in CATEGORY_MAP:
        dirpath = base / category
        if not dirpath.exists():
            print(f'Directory not found: {dirpath}')
            continue
        print(f'\n=== {category} ===')
        for md in sorted(dirpath.glob('*.md')):
            if md.name == '_index.md':
                continue
            process_file(md, category)
            count += 1
    print(f'\nTotal: {count} files processed')


if __name__ == '__main__':
    main()

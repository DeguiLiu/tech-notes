#!/usr/bin/env python3
"""Batch upload Hugo markdown articles to juejin.cn as drafts."""

import os
import re
import sys
import json
import time
import yaml
import requests
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────
COOKIE = os.environ.get("JUEJIN_COOKIE", "")
CONTENT_DIR = Path(__file__).resolve().parent.parent / "content" / "posts"
RESULT_LOG = Path(__file__).resolve().parent / "juejin_upload_log.json"
DELAY_SEC = 3  # seconds between API calls

# juejin category: 后端
CATEGORY_ID = "6809637769959178254"

# juejin API endpoints
API_BASE = "https://api.juejin.cn"
API_CREATE_DRAFT = f"{API_BASE}/content_api/v1/article_draft/create"
API_QUERY_TAG = f"{API_BASE}/tag_api/v1/query_tag_list"

HEADERS = {
    "content-type": "application/json",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Pre-known tag mappings (tag_name -> tag_id)
TAG_CACHE = {
    "C++": "6809640447497994253",
    "嵌入式": "6809640560995860488",
    "Linux": "6809640385980137480",
    "性能优化": "6809641167680962568",
    "架构": "6809640501776482317",
    "设计模式": "6809640467731316749",
    "C语言": "7026219092189118477",
    "消息队列": "6809641194377707534",
    "状态机": "6809640437528133645",
    "后端": "6809640408797167623",
}

# Hugo tag -> juejin tag name mapping
HUGO_TAG_MAP = {
    "C++": "C++", "C++11": "C++", "C++14": "C++", "C++17": "C++",
    "C": "C语言", "C11": "C语言", "C99": "C语言", "C语言": "C语言",
    "embedded": "嵌入式", "嵌入式": "嵌入式",
    "Linux": "Linux", "linux": "Linux", "ARM-Linux": "Linux",
    "ARM": "嵌入式", "Cortex-M": "嵌入式", "MCU": "嵌入式",
    "RTOS": "嵌入式", "RT-Thread": "嵌入式", "bare-metal": "嵌入式",
    "performance": "性能优化", "性能优化": "性能优化",
    "benchmark": "性能优化", "profiling": "性能优化",
    "architecture": "架构", "架构设计": "架构",
    "design-pattern": "设计模式",
    "lock-free": "后端", "concurrency": "后端",
    "message-bus": "消息队列", "消息队列": "消息队列",
    "state-machine": "状态机", "FSM": "状态机", "HSM": "状态机",
    "状态机": "状态机",
}


def query_tag_id(keyword: str) -> str | None:
    """Query juejin API for a tag ID by keyword."""
    if keyword in TAG_CACHE:
        return TAG_CACHE[keyword]
    try:
        resp = requests.post(
            API_QUERY_TAG,
            headers=HEADERS,
            json={"cursor": "0", "key_word": keyword, "limit": 1, "sort_type": 1},
            timeout=10,
        )
        data = resp.json()
        if data.get("err_no") == 0 and data.get("data"):
            tag = data["data"][0]["tag"]
            TAG_CACHE[tag["tag_name"]] = tag["tag_id"]
            return tag["tag_id"]
    except Exception as e:
        print(f"  [WARN] query tag '{keyword}' failed: {e}")
    return None


def resolve_tags(hugo_tags: list[str]) -> list[str]:
    """Map Hugo tags to juejin tag_ids. Limited to 1 tag (account restriction)."""
    for ht in hugo_tags:
        jj_name = HUGO_TAG_MAP.get(ht)
        if not jj_name:
            continue
        tid = query_tag_id(jj_name)
        if tid:
            return [tid]
    # Fallback: C++
    return [TAG_CACHE["C++"]]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and return (metadata, body)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1))
    body = text[m.end():]
    return fm or {}, body


def collect_articles() -> list[dict]:
    """Scan content/posts for all markdown articles."""
    articles = []
    for md_path in sorted(CONTENT_DIR.rglob("*.md")):
        if md_path.name == "_index.md":
            continue
        text = md_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        if fm.get("draft", False):
            continue
        title = fm.get("title", md_path.stem)
        summary = fm.get("summary", "")
        hugo_tags = fm.get("tags", [])
        categories = fm.get("categories", [])
        articles.append({
            "path": str(md_path.relative_to(CONTENT_DIR)),
            "title": title,
            "summary": summary[:100] if summary else "",
            "hugo_tags": hugo_tags,
            "categories": categories,
            "body": body,
        })
    return articles


def create_draft(article: dict) -> dict:
    """Create a draft on juejin and return the API response."""
    tag_ids = resolve_tags(article["hugo_tags"])
    payload = {
        "category_id": CATEGORY_ID,
        "tag_ids": tag_ids,
        "link_url": "",
        "cover_image": "",
        "title": article["title"],
        "brief_content": article["summary"],
        "edit_type": 10,
        "html_content": "deprecated",
        "mark_content": article["body"],
        "theme_ids": [],
    }
    headers = {**HEADERS, "cookie": COOKIE}
    resp = requests.post(API_CREATE_DRAFT, headers=headers, json=payload, timeout=30)
    return resp.json()


def load_log() -> dict:
    """Load upload log to support resume."""
    if RESULT_LOG.exists():
        return json.loads(RESULT_LOG.read_text(encoding="utf-8"))
    return {}


def save_log(log: dict):
    RESULT_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if not COOKIE:
        print("ERROR: set JUEJIN_COOKIE env variable first")
        sys.exit(1)

    articles = collect_articles()
    print(f"Found {len(articles)} articles\n")

    log = load_log()
    done = 0
    fail = 0

    for i, art in enumerate(articles, 1):
        key = art["path"]
        if key in log and log[key].get("err_no") == 0:
            print(f"[{i}/{len(articles)}] SKIP (already uploaded): {art['title']}")
            done += 1
            continue

        print(f"[{i}/{len(articles)}] Uploading: {art['title']}")
        try:
            result = create_draft(art)
            err = result.get("err_no", -1)
            if err == 0:
                draft_id = result["data"]["id"]
                print(f"  OK  draft_id={draft_id}")
                done += 1
            else:
                print(f"  FAIL err_no={err} msg={result.get('err_msg')}")
                fail += 1
            log[key] = {"err_no": err, "data": result.get("data"), "err_msg": result.get("err_msg")}
        except Exception as e:
            print(f"  ERROR: {e}")
            log[key] = {"err_no": -1, "err_msg": str(e)}
            fail += 1

        save_log(log)
        if i < len(articles):
            time.sleep(DELAY_SEC)

    print(f"\nDone: {done} success, {fail} failed")
    print(f"Log saved to {RESULT_LOG}")


if __name__ == "__main__":
    main()

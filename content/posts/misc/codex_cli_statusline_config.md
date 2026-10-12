---
title: "Codex CLI 状态栏配置: 内置字段、试用与持久化"
date: 2026-09-16T08:00:00
draft: false
categories: ["misc"]
tags: ["Codex CLI", "TUI", "configuration"]
summary: "Codex CLI 可直接通过 tui.status_line 配置状态栏，无需外部脚本；本文列出内置字段及交互、单次覆盖和持久化三种调整方式。"
ShowToc: true
TocOpen: true
---

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/165617577)

Codex CLI 0.154.0 及以上版本原生支持状态栏配置，不需要额外脚本。将字段名数组写入 `config.toml` 的 `[tui]` 段即可决定显示内容；临时试验可用命令行覆盖，确认后再持久化。

## 配置状态栏

下面的配置显示当前目录、Git 分支、上下文与令牌用量、模型和线程名，同时启用状态栏颜色：

```toml
[tui]
status_line = [
    "current-dir", "git-branch", "context-used", "used-tokens",
    "context-window-size", "model-with-reasoning", "thread-name",
]
status_line_use_colors = true
screen_reader_detection_done = true
```

字段按数组顺序排列。修改持久化配置后需要重启 Codex，再检查底栏是否符合预期。

## 字段与调整方式

### 内置字段全集

以下字段适用于 Codex CLI 0.154.0：

| 分类 | 字段 |
| --- | --- |
| 位置与仓库 | `current-dir`、`project-name`、`git-branch`、`pull-request-number`、`branch-changes` |
| 上下文与用量 | `context-used`、`context-remaining`、`context-window-size`、`used-tokens`、`total-input-tokens`、`total-output-tokens`、`thread-credits`、`estimated-thread-cost` |
| 会话 | `status`、`run-state`、`task-progress`、`thread-id`、`thread-name`、`thread-title`、`workspace-headline` |
| 模型 | `model`、`model-with-reasoning`、`reasoning`、`fast-mode` |
| 权限与版本 | `permissions`、`approval-mode`、`codex-version` |
| 其他 | `five-hour-limit`、`weekly-limit`、`raw-output` |

字段名拼错时会被忽略或报告 `unknown item`。这通常不会阻止 Codex 启动，但对应内容不会显示。

### 三种调整方式

```bash
# Configure interactively and write the result to config.toml.
/statusline

# Override the field list for one launch.
codex -c 'tui.status_line=["codex-version","permissions","approval-mode"]'

# Persist the final field list in the [tui] section of ~/.codex/config.toml.
```

交互入口会提示 `Use /statusline to configure which items appear in the status line.`。推荐流程是先用单次覆盖验证字段，再编辑 `~/.codex/config.toml` 固化，最后重启 Codex 检查底栏。

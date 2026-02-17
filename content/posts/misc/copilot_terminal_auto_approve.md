---
title: "VS Code Copilot 终端命令自动审批的安全配置"
date: 2026-02-17T10:50:00
draft: false
categories: ["misc"]
tags: ["vscode", "copilot", "security", "automation", "terminal"]
summary: "通过分层白名单策略配置 VS Code Copilot 终端命令自动审批，平衡 AI 编程助手的效率与安全性。"
---

## 问题背景

AI 编程助手频繁请求终端命令审批是效率瓶颈。VS Code 的 `chat.tools.terminal.autoApprove` 提供白名单机制，允许自动批准预定义的安全命令。

## 安全模型

采用白名单策略（默认拒绝），而非黑名单（无法穷举所有危险命令如 `curl | bash`、fork bomb 等）。

## 分层配置策略

完整配置文件可直接下载: [copilot_auto_approve_settings.jsonc](/tech-notes/files/copilot_auto_approve_settings.jsonc)

配置分为五层，从宽松到严格：

| 层级 | 匹配方式 | 覆盖范围 |
|------|---------|---------|
| 第一层 | Substring Match | 无副作用只读命令 (grep/awk/sed/make/git/cat 等) |
| 第二层 | Regex (行首+管道链) | 文件管理/系统监控/编译器/脚本解释器 |
| 第三层 | Regex | 本地脚本执行 (./build.sh) |
| 第四层 | Regex | 安全删除 (构建产物/缓存/临时文件) |
| 第五层 | 兜底拒绝 | 所有未匹配的 rm 操作 |

### 关键正则说明

第二层通用操作白名单，匹配行首或管道/逻辑与/分号之后的命令：

```json
"/^(\\s*|.*[\\|\\&\\;]\\s*)(cd|ls|pwd|cp|mv|mkdir|find|gcc|g\\+\\+|python3?|node|docker|kubectl)(\\s+.*)?$/": {
  "approve": true,
  "matchCommandLine": true
}
```

- `^`：行首匹配
- `.*[\\|\\&\\;]\\s*`：管道链中的命令
- `\\b` 或 `(\\s+.*)?$`：防止误匹配（如 `rm` 不匹配 `rmdir`）

第四层安全删除，仅允许构建产物和缓存：

```json
"/^(\\s*|.*[\\|\\&\\;]\\s*)rm\\s+(-rf\\s+)?
  (build.*|out|dist|target|node_modules|\\.cache|__pycache__|
   .*\\.(o|a|so|tmp|log|bak)|CMakeCache\\.txt)(\\s.*)?$/": {
  "approve": true
}
```

第五层兜底拒绝所有其他 rm：

```json
"rm": false
```

## 风险评估：看似安全实则危险

| 命令 | 风险 | 缓解 |
|------|------|------|
| `curl URL \| bash` | 下载并执行远程脚本 | 先下载审查再执行 |
| `find -exec rm {}` | 匹配规则有误时删除重要文件 | 先预览匹配结果 |
| `xargs cat` | 文件名含特殊字符导致注入 | 使用 `-print0 \| xargs -0` |

## 自定义扩展

根据项目需要添加规则：

```json
{
  // 项目构建脚本
  "^\\./scripts/(build|test|deploy)\\.sh$": {},
  // Docker (只读操作)
  "^docker\\s+(build|run|ps|logs|inspect)\\b": {},
  // Kubernetes (只读操作)
  "^kubectl\\s+(get|describe|logs)\\b": {},
  // 数据库 (只读查询)
  "^psql\\s+.*\\s+-c\\s+\"SELECT\\b": {}
}
```

禁止 `docker rm -f`、`kubectl delete`、`INSERT/UPDATE/DELETE/DROP` 等写操作，需人工确认。

## Claude Code 对比

Claude Code 使用 `.claude/settings.json` 的 `allowedTools` 控制工具类型（如 `Bash`），而非具体命令。粒度更粗，`Bash` 工具允许执行任意 shell 命令。建议生产环境移除 `Bash`，只保留 `Read`、`Grep` 等只读工具。

## 安全建议

- 沙箱隔离：在 devcontainer 中运行 AI Agent，限制网络出站
- Prompt Injection 防御：AI 读取不受信任内容时可能被诱导执行危险操作，需静态分析 + 人工审核
- 团队管理：将配置纳入 `.vscode/settings.json` 版本管理，定期审计规则

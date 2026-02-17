# 文档编写规范

本仓库技术文章的编写偏好和规范，供作者和 AI 辅助工具参考。

## Front Matter 规范

每篇文章必须包含以下 YAML front matter 字段:

```yaml
---
title: "标题: 副标题"
date: 2026-02-17
draft: false
categories: ["architecture"]
tags: ["C++17", "嵌入式", "ARM-Linux"]
summary: "一句话摘要，150 字以内"
---
```

- `title`: 必填，双引号包裹
- `date`: 必填，ISO 格式 (YYYY-MM-DD)
- `draft`: 必填，`true` 表示草稿不公开
- `categories`: 必填，只能是 `architecture` / `performance` / `practice` / `pattern` / `tools` / `interview` / `misc` 之一
- `tags`: 必填，至少 2 个标签
- `summary`: 必填，简明扼要描述文章核心内容

## 标题规范

### 格式

- 采用 "主标题: 副标题" 格式，冒号后加空格
- 主标题点明核心话题，副标题补充技术细节或方法
- 长度控制在 20-40 个字符

### 禁止事项

- 不使用具体硬件型号 (Zynq-7000, RK3506J, STM32F4 等)
  - 用通用描述替代: "FPGA + ARM 双核 SoC"、"三核异构 SoC"、"Cortex-M MCU"
- 不提及具体第三方库品牌作为卖点 (如 "从 Boost.Log 到...")
  - 用功能描述替代: "可插拔后端"、"零依赖架构"
- 不使用 "一个" 等冗余量词
- 不使用感叹号或夸张修饰

### 好的标题示例

```
嵌入式 Telnet 调试 Shell 重构: 纯 POSIX 轻量化实现
C++11 线程安全消息总线: 从零实现 Pub/Sub 模型
FPGA + ARM 双核 SoC 处理激光雷达点云的可行性分析
轻量级 C++14 日志库设计: 可插拔后端与零依赖架构
```

## 文件命名规范

- 全小写 snake_case，不使用 PascalCase 或 camelCase
- 文件名应与标题关键词关联，具有描述性
- 不怕长，优先可读性: `embedded_ab_firmware_upgrade_engine.md`
- 不使用具体硬件型号作为文件名前缀

## 目录分类

文章按 `categories` 字段分类到对应目录:

| 目录 | 分类 | 内容范围 |
|------|------|----------|
| `content/posts/architecture/` | architecture | 系统架构、平台设计、数据流水线 |
| `content/posts/performance/` | performance | 性能基准、优化实战、并发编程 |
| `content/posts/practice/` | practice | 项目实战、框架分析、工程经验 |
| `content/posts/pattern/` | pattern | 设计模式、语言特性、编程范式 |
| `content/posts/tools/` | tools | 开发工具、调试设施、基础设施库 |
| `content/posts/interview/` | interview | 面试题集、技术考察 |
| `content/posts/misc/` | misc | 未归类的技术笔记和杂项内容 |

- 每个目录包含 `_index.md` 作为分类首页
- 文章的 `categories` 字段必须与所在目录一致

## 内容写作原则

- 结论前置: 选型结论/设计决策放首段，读者 30 秒内知道选了什么、为什么
- 基线锚点: 提供最简可行方案作参照，让改进可量化
- 比例控制: 问题/背景 20-30%，方案/设计 70-80%
- 单一焦点: 一篇解一个问题，独立话题拆文档
- 诚实定位: 组合方案定位为工程实践，不自封新范式
- 只论 tradeoff: 讨论技术取舍，不评价项目维护状态
- 公知外链: 教科书内容用链接替代，篇幅留给独有分析和实测数据
- 公平对比: 只有自己能打勾的维度可能是拉偏架，需补充对手优势维度
- 场景匹配: Benchmark 必须匹配目标硬件和负载模型

## 自动化工具

- `scripts/update_readme.py --write`: 扫描所有文章，自动生成 README.md
- `scripts/update_frontmatter.py`: 批量更新文章标题和摘要
- `scripts/add_frontmatter.py`: 为缺少 front matter 的文章添加默认字段

## Draft 状态管理

- `draft: true` 的文章不会在 Hugo 站点上公开显示
- 未经作者确认，不得将 `draft: true` 改为 `draft: false`
- 草稿文章仍会被 `update_readme.py` 收录，但标注 *(草稿)* 标记

#!/usr/bin/env python3
"""Batch update article titles and summaries."""

import re
from pathlib import Path

UPDATES = {
    # architecture/
    "content/posts/architecture/mccc_zero_heap_optimization_benchmark.md": {
        "title": "MCCC 消息总线性能实测: 吞吐量、延迟与优先级背压验证",
        "summary": "MCCC Lock-free MPSC 消息总线在 BARE_METAL 模式下达到 18.7 M/s (54 ns/msg)，FULL_FEATURED 生产模式 5.8 M/s (172 ns/msg)，HIGH 优先级消息在背压测试中实现零丢失，E2E P99 延迟仅 449 ns。本文从对比层级、功能开销分解、端到端延迟分位数三个维度展示完整测试数据。",
    },
    "content/posts/architecture/eventpp_arm_optimization_report.md": {
        "title": "eventpp 性能优化实战: 6 个瓶颈定位与 5 倍吞吐提升",
        "summary": "通过逐行阅读 eventpp v0.1.3 核心代码，定位到回调遍历加锁、双锁入队、排他锁查 map 等 6 个性能瓶颈。逐一实施优化后，Active Object 吞吐量从 1.5 M/s 提升至 8.5 M/s，改善幅度超过 5 倍。",
    },
    "content/posts/architecture/eventpp_processQueueWith_design.md": {
        "title": "零开销事件分发: 用编译期 Visitor 替代 5 层间接调用",
        "summary": "eventpp EventQueue::process() 的分发热路径经过 mutex 读锁、map 查找、shared_ptr 遍历、std::function 类型擦除共 5 层间接调用。processQueueWith 通过编译期 Visitor 模式绕过全部中间层，让编译器将整条分发路径内联为零间接调用。",
    },
    "content/posts/architecture/embedded_streaming_data_architecture.md": {
        "title": "工业嵌入式流式数据处理架构设计: 从传感器采集到网络输出的全链路",
        "summary": "面向激光雷达、工业视觉、机器人等 ARM-Linux 场景，设计一套 C++17 header-only 的流式数据处理架构。覆盖数据流 (10-100 Hz 大块帧) 与控制流 (低频高可靠消息) 的分离处理、零堆分配内存管理、多级流水线调度，基于 newosp 基础设施库实现。",
    },
    "content/posts/architecture/rtos_vs_linux_heterogeneous_soc.md": {
        "title": "RK3506J 三核异构设计: 当 RTOS 与 Linux 跑在同一芯片上",
        "summary": "RK3506J 集成三核 Cortex-A7 (1.0 GHz) + Cortex-M0，支持 Linux + RTOS 异构部署。本文分析 AMP 架构下的核间通信 (RPMsg/共享内存)、实时性保障 (硬件定时器 + 中断隔离)、资源分区策略，面向激光雷达和工业控制器的部署方案。",
    },
    "content/posts/architecture/fpga_arm_soc_lidar_feasibility.md": {
        "title": "Zynq-7000 激光雷达点云处理: FPGA + 双核 ARM 的架构设计与性能分析",
        "summary": "在 Zynq-7000 (双核 Cortex-A9 @ 667 MHz) 上处理 30 万点/秒激光雷达数据流。PL (FPGA) 负责传感器接口和 DMA 搬运，PS (ARM) 运行 Linux 处理点云算法和网络输出，目标端到端延迟 P99 < 5 ms。",
    },
    "content/posts/architecture/dual_core_arm_rtthread_smp.md": {
        "title": "在 Zynq-7000 双核 ARM 上跑 RT-Thread SMP: MMU、Cache 与调度实战",
        "summary": "将 RT-Thread SMP 移植到 Zynq-7000 双核 Cortex-A9 平台，解决 MMU 页表配置、L1/L2 Cache 一致性、双核调度器初始化三个核心问题。实测表明带宽不是瓶颈，CPU 处理延迟和调度抖动才是端到端延迟的主导因素。",
    },
    # blog/
    "content/posts/blog/lockfree_async_log.md": {
        "title": "无锁异步日志设计: Per-Thread SPSC 环形缓冲与分级路由",
        "summary": "在多核 ARM Linux 嵌入式系统中，同步日志的 I/O 阻塞导致控制回路超时和看门狗复位。本文设计一种基于 Per-Thread SPSC 环形缓冲与分级路由的异步日志架构，实现 wait-free 热路径 (~200-300 ns)、零竞争生产者、崩溃安全的关键日志保障。",
    },
    "content/posts/blog/armv8_crc32_hardware_vs_neon_benchmark.md": {
        "title": "ARMv8 CRC 性能实测: 硬件指令快 8 倍, NEON 反而更慢",
        "summary": "对比两组实验: ARMv8 CRC32 硬件指令 (crc32cx) vs 软件查表法，以及 NEON SIMD vs 简单 C 循环的字节累加校验和。结果表明 CRC32 硬件指令比查表快 8 倍以上，而 NEON 手写的字节累加在 -O2 下反而比编译器自动优化的标量代码慢。",
    },
    "content/posts/blog/ztask_scheduler.md": {
        "title": "ztask: 零动态分配的裸机合作式任务调度器设计分析",
    },
    "content/posts/blog/parallel_matmul_benchmark.md": {
        "summary": "以 512x512 矩阵乘法为载体，基于 newosp 基础设施库实测对比单线程、线程池、消息总线、多进程共享内存四种并行方案的性能差异，分析各方案在嵌入式 Linux 平台上的架构取舍与加速比。",
    },
    "content/posts/blog/cpp17_advantages_over_c.md": {
        "summary": "C++17 的模板、variant、constexpr、RAII、强类型系统让编译器在编译期捕获类型不匹配、内存越界、资源泄漏等问题，同时生成比手写 C 更优的机器码。基于 newosp 工业嵌入式库的实践，逐项对比 C11 无法实现的语言级能力。",
    },
    "content/posts/blog/cpp17_claims_in_newosp.md": {
        "title": "newosp 源码中的 C++17 实践: C11 无法实现的能力清单",
        "summary": "从 newosp v0.2.0 源码中提炼 C11 在语言层面无法实现的 C++17 能力: 编译期类型校验、variant 类型路由、constexpr 编译期计算、RAII 资源管理、模板参数化策略选择。每项附带具体代码位置和 C 语言对比。",
    },
    "content/posts/blog/cpp14_pluggable_log_library_design.md": {
        "summary": "在嵌入式 ARM Linux 项目中，基于 Boost.Log 的日志方案因临时对象创建、std::regex 解析和动态链接依赖而成为性能瓶颈。本文以 loghelper 的重构为例，将其改造为 C++14 header-only 架构，支持 spdlog/zlog/fallback 三后端编译期切换，实现 10-100 倍性能提升。",
    },
    "content/posts/blog/newosp_shell_multibackend.md": {
        "summary": "工业嵌入式系统在实验室、早期调试、现场部署、CI 测试等不同阶段，调试环境差异巨大。newosp 的 DebugShell 原本只支持 TCP telnet，本文介绍如何将其扩展为 TCP/串口/stdin/管道四后端统一架构，一套命令在所有环境下可用。",
    },
    "content/posts/blog/embedded_deadlock_prevention_lockfree.md": {
        "summary": "死锁是嵌入式多线程系统中最隐蔽的故障之一。本文从一个典型的双锁死锁场景出发，逐步演示有序锁、lock_guard、try_lock、无锁队列四种防御策略，分析各方案在嵌入式实时系统中的工程权衡。",
    },
    # mccc/
    "content/posts/mccc/cpp11_threadsafe_pubsub_bus.md": {
        "title": "用 C++11 从零实现一个线程安全消息总线",
    },
    "content/posts/mccc/mccc_bus_api_reference.md": {
        "title": "MCCC 消息总线 API 全参考: 类型、接口与配置",
        "summary": "MCCC (Message-Centric Component Communication) 消息总线的完整 API 参考，涵盖 FixedString/FixedVector 容器、MessageEnvelope 消息封装、AsyncBus 总线接口、StaticComponent 编译期组件、优先级与背压配置，每个接口附带签名、参数说明和使用示例。",
    },
    "content/posts/mccc/mccc_bus_cpp17_practice.md": {
        "title": "mccc-bus 源码中的 C++17 实践: C 语言无法实现的能力剖析",
        "summary": "从 mccc-bus 项目 (约 1200 行 header-only) 中提炼 C11 无法实现的 C++17 能力: variant 编译期类型路由、FixedFunction 栈上类型擦除、constexpr if 编译期分支、RAII 自动资源管理、模板约束与 static_assert 编译期校验。",
    },
}


def escape_yaml(s):
    """Escape for YAML double-quoted string."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def update_file(filepath, updates):
    """Update title and/or summary in frontmatter."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    if not content.lstrip().startswith('---'):
        print(f'  SKIP (no frontmatter): {filepath}')
        return False

    # Find frontmatter boundaries
    fm_start = content.index('---')
    fm_end = content.index('---', fm_start + 3) + 3
    frontmatter = content[fm_start:fm_end]
    body = content[fm_end:]

    changed = False

    if 'title' in updates:
        old_title_match = re.search(r'^title:\s*".*?"', frontmatter, re.MULTILINE)
        if old_title_match:
            new_title_line = f'title: "{escape_yaml(updates["title"])}"'
            frontmatter = frontmatter[:old_title_match.start()] + new_title_line + frontmatter[old_title_match.end():]
            changed = True

    if 'summary' in updates:
        old_summary_match = re.search(r'^summary:\s*".*?"', frontmatter, re.MULTILINE)
        if old_summary_match:
            new_summary_line = f'summary: "{escape_yaml(updates["summary"])}"'
            frontmatter = frontmatter[:old_summary_match.start()] + new_summary_line + frontmatter[old_summary_match.end():]
            changed = True

    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(frontmatter + body)
        title_info = updates.get('title', '(unchanged)')
        print(f'  OK: {filepath}')
        if 'title' in updates:
            print(f'       title -> {updates["title"]}')
        if 'summary' in updates:
            summary_preview = updates["summary"][:60] + '...'
            print(f'       summary -> {summary_preview}')
    return changed


def main():
    count = 0
    for filepath, updates in UPDATES.items():
        p = Path(filepath)
        if not p.exists():
            print(f'  NOT FOUND: {filepath}')
            continue
        if update_file(filepath, updates):
            count += 1
    print(f'\nTotal: {count} files updated')


if __name__ == '__main__':
    main()

# Embedded C++ 技术文章集

面向工业级嵌入式平台 (ARM-Linux) 的 C++ 技术文章，涵盖消息总线架构、性能优化、编译期分发等主题。

## 目录结构

```
blog/                -- 技术博客与专题文章
mccc/                -- MCCC 消息总线设计与分析
architecture/        -- 流式架构、性能基准、优化报告、平台评估
interview/           -- 嵌入式面试准备
```

## 文章索引

### blog/ -- 技术博客 (14 篇)

| 文件 | 主题 |
|------|------|
| [blog_newosp.md](blog/blog_newosp.md) | newosp: 面向工业嵌入式的 C++17 Header-Only 基础设施库 |
| [blog_callback_optimization.md](blog/blog_callback_optimization.md) | 回调机制优化: 从 std::function 到零开销 FixedFunction |
| [cpp17_advantages_over_c.md](blog/cpp17_advantages_over_c.md) | 嵌入式场景下 C++17 相对 C 的优势分析 |
| [cpp17_binary_size_vs_c.md](blog/cpp17_binary_size_vs_c.md) | C++17 vs C 二进制体积实测对比 |
| [cpp17_claims_in_newosp.md](blog/cpp17_claims_in_newosp.md) | newosp 中 C++17 技术主张的代码论证 |
| [cpp17_what_c_cannot_do.md](blog/cpp17_what_c_cannot_do.md) | C 语言无法实现的 C++17 能力 |
| [lockfree_async_log.md](blog/lockfree_async_log.md) | ARM Linux 高性能无锁异步日志系统设计与实现 |
| [deadlock_prevention.md](blog/deadlock_prevention.md) | 全局锁策略: 有序获取与超时保护构建无死锁系统 |
| [nginx_oop_in_c.md](blog/nginx_oop_in_c.md) | C 语言面向对象范式: Nginx 模块化架构的设计分析 |
| [ztask_scheduler.md](blog/ztask_scheduler.md) | ztask 轻量级合作式任务调度器 |
| [embedded_cli_msh.md](blog/embedded_cli_msh.md) | 嵌入式 Linux 上用 Embedded CLI 打造 MSH 风格调试命令 |
| [neon_crc32_analysis.md](blog/neon_crc32_analysis.md) | NEON 指令集 CRC32 加速分析 |
| [lock_contention_benchmark.md](blog/lock_contention_benchmark.md) | 锁竞争基准测试: Spinlock vs Mutex vs ConcurrentQueue |
| [loghelper_pluggable_backend.md](blog/loghelper_pluggable_backend.md) | C++14 嵌入式日志库设计: 从 Boost.Log 到可插拔后端架构 |

### mccc/ -- 消息总线 (5 篇)

| 文件 | 主题 |
|------|------|
| [MCCC_Design.md](mccc/MCCC_Design.md) | MCCC 架构设计文档 (Lock-free MPSC, 零堆分配, 优先级控制) |
| [MCCC_Competitive_Analysis.md](mccc/MCCC_Competitive_Analysis.md) | 竞品对标报告 (vs eventpp/EnTT/sigslot/ZeroMQ/QP-C++) |
| [mccc_bus_cpp17_practice.md](mccc/mccc_bus_cpp17_practice.md) | MCCC Bus C++17 工程实践总结 |
| [mccc_bus_api_reference.md](mccc/mccc_bus_api_reference.md) | MCCC Bus API 参考文档 |
| [cpp11_message_bus.md](mccc/cpp11_message_bus.md) | C++11 非阻塞消息总线 message_bus |

### architecture/ -- 架构与性能 (8 篇)

| 文件 | 主题 |
|------|------|
| [Streaming_Architecture_Design.md](architecture/Streaming_Architecture_Design.md) | 流式数据处理架构设计 (eventpp + Active Object) |
| [Performance_Benchmark.md](architecture/Performance_Benchmark.md) | 性能基准测试报告 |
| [Benchmark_Methodology_Detail.md](architecture/Benchmark_Methodology_Detail.md) | 基准测试方法论详解 |
| [Optimization_Summary.md](architecture/Optimization_Summary.md) | 优化手段汇总 (Lock-free, 零拷贝, 批处理等) |
| [eventpp_Optimization_Report.md](architecture/eventpp_Optimization_Report.md) | eventpp ARM-Linux 优化报告 |
| [eventpp_processQueueWith_design.md](architecture/eventpp_processQueueWith_design.md) | eventpp processQueueWith 零开销访问者分发设计 |
| [zynq7000_lidar_feasibility.md](architecture/zynq7000_lidar_feasibility.md) | Zynq-7000 处理30万点/秒激光雷达点云可行性分析 |
| [zynq7000_rtthread_mmu_smp.md](architecture/zynq7000_rtthread_mmu_smp.md) | Zynq-7000 上 RT-Thread 的 MMU 与 SMP 优势分析 |
| [rk3506j_rtos_vs_linux.md](architecture/rk3506j_rtos_vs_linux.md) | RK3506J 架构选型: RT-Thread SMP vs Linux |

### interview/ -- 面试 (2 篇)

| 文件 | 主题 |
|------|------|
| [SeniorEmbeddedSoftwareEngineerInterviewQuestions.md](interview/SeniorEmbeddedSoftwareEngineerInterviewQuestions.md) | 高级嵌入式软件工程师面试题集 |
| [c_language_interview_questions.md](interview/c_language_interview_questions.md) | 中高级软件工程师的 C 语言面试题 (30 题) |

## 关联项目

| 项目 | 地址 | 说明 |
|------|------|------|
| newosp | [GitHub](https://github.com/DeguiLiu/newosp) | C++17 Header-Only 嵌入式基础设施库 |
| mccc-bus | [Gitee](https://gitee.com/liudegui/mccc-bus) | 高性能无锁消息总线 |
| eventpp (fork) | [Gitee](https://gitee.com/liudegui/eventpp) | eventpp ARM-Linux 优化分支 |
| message_bus | [Gitee](https://gitee.com/liudegui/message_bus) | C++11 非阻塞消息总线 |
| lock-contention-benchmark | [Gitee](https://gitee.com/liudegui/lock-contention-benchmark) | 锁竞争基准测试 (Spinlock/Mutex/ConcurrentQueue) |

## 技术栈

- C++17, `-fno-exceptions -fno-rtti`
- Lock-free (CAS, SPSC wait-free)
- MISRA C++ 合规
- ARM-Linux 嵌入式平台 (Zynq-7000, RK3506J, Cortex-A 系列)

# 编程技术文章集

面向系统编程与软件工程的技术文章，涵盖架构设计、性能优化、并发编程、设计模式、开发工具等主题。

## 目录结构

```
architecture/       -- 架构设计 (10 篇)
performance/        -- 性能优化 (27 篇)
practice/           -- 工程实践 (10 篇)
pattern/            -- 设计模式 (8 篇)
interview/          -- 面试题 (2 篇)
misc/               -- 杂项 (17 篇)
```

共 **74** 篇文章 (74 篇公开, 0 篇草稿)

## 文章索引

### architecture/ -- 架构设计 (10 篇)

| 文件 | 标题 | 日期 |
|------|------|------|
| [newosp_concurrency_io_architecture.md](architecture/newosp_concurrency_io_architecture.md) | 如何设计嵌入式并发架构: newosp 的事件驱动 + 固定线程池方案 | 2026-02-17T08:30:00 |
| [lidar_pipeline_newosp.md](architecture/lidar_pipeline_newosp.md) | 激光雷达高吞吐数据处理 Pipeline: 模块化架构与 NEON 向量化 | 2026-02-17T08:20:00 |
| [embedded_streaming_data_architecture.md](architecture/embedded_streaming_data_architecture.md) | 嵌入式流式数据处理架构: 传感器到网络输出的全链路设计 | 2026-02-17T08:10:00 |
| [embedded_ab_firmware_upgrade_engine.md](architecture/embedded_ab_firmware_upgrade_engine.md) | 嵌入式 A/B 分区固件升级引擎: HSM 状态机 + 三层掉电保护 | 2026-02-17T08:00:00 |
| [rtos_ao_cooperative_scheduling.md](architecture/rtos_ao_cooperative_scheduling.md) | RTOS + Active Object 协同调度优化: 从浅层适配到深度融合 | 2026-02-16T08:20:00 |
| [newosp_event_driven_architecture.md](architecture/newosp_event_driven_architecture.md) | 如何设计传感器数据流水线: newosp 事件驱动 + 零堆分配方案 | 2026-02-16T08:10:00 |
| [mcu_secondary_bootloader.md](architecture/mcu_secondary_bootloader.md) | MCU 二级 Bootloader 设计: 状态机驱动的 A/B 分区 OTA 与安全启动 | 2026-02-16T08:00:00 |
| [rtos_vs_linux_heterogeneous_soc.md](architecture/rtos_vs_linux_heterogeneous_soc.md) | RTOS vs Linux 异构选型: 三核 SoC 上的双系统设计 | 2026-02-15T08:20:00 |
| [fpga_arm_soc_lidar_feasibility.md](architecture/fpga_arm_soc_lidar_feasibility.md) | FPGA + ARM 双核 SoC 处理激光雷达点云的可行性分析 | 2026-02-15T08:10:00 |
| [dual_core_arm_rtthread_smp.md](architecture/dual_core_arm_rtthread_smp.md) | 双核 ARM SoC 上跑 RT-Thread SMP: MMU、Cache 与调度实战 | 2026-02-15T08:00:00 |

### performance/ -- 性能优化 (27 篇)

| 文件 | 标题 | 日期 |
|------|------|------|
| [object_pool_hidden_costs.md](performance/object_pool_hidden_costs.md) | 对象池在嵌入式热路径上的三个隐性成本 | 2026-02-17T09:30:00 |
| [memory_barrier_hardware.md](performance/memory_barrier_hardware.md) | 内存屏障的硬件原理: 从 Store Buffer 到 ARM DMB/DSB/ISB | 2026-02-17T09:20:00 |
| [mccc_zero_heap_optimization_benchmark.md](performance/mccc_zero_heap_optimization_benchmark.md) | MCCC 消息总线零堆分配优化与性能实测 | 2026-02-17T09:10:00 |
| [high_performance_system_design_principles.md](performance/high_performance_system_design_principles.md) | 高性能系统设计的五个反直觉原则: 从消息队列优化中提炼的通用方法论 | 2026-02-17T09:00:00 |
| [unix_domain_socket_realtime.md](performance/unix_domain_socket_realtime.md) | Unix Domain Socket 实时性优化: 嵌入式 IPC 全链路调优 | 2026-02-16T11:00:00 |
| [tcp_ringbuffer_short_write.md](performance/tcp_ringbuffer_short_write.md) | TCP 非阻塞发送的 Short Write 问题: 环形缓冲区 + epoll 事件驱动方案 | 2026-02-16T10:50:00 |
| [spsc_ringbuffer_design.md](performance/spsc_ringbuffer_design.md) | SPSC 无锁环形缓冲区设计剖析: 从原理到每一行代码的工程抉择 | 2026-02-16T10:40:00 |
| [shared_memory_ipc_lockfree_ringbuffer.md](performance/shared_memory_ipc_lockfree_ringbuffer.md) | 共享内存 IPC 实践: 从 POSIX shm 到 newosp 无锁 Ring Buffer | 2026-02-16T10:30:00 |
| [mccc_message_passing.md](performance/mccc_message_passing.md) | 嵌入式线程间消息传递重构: 用 MCCC 无锁消息总线替代 mutex + priority_queue | 2026-02-16T10:20:00 |
| [lockfree_programming_fundamentals.md](performance/lockfree_programming_fundamentals.md) | 无锁编程核心原理: 从 CAS 原子操作到三种队列设计模式 | 2026-02-16T10:10:00 |
| [deadlock_priority_inversion_practice.md](performance/deadlock_priority_inversion_practice.md) | 多线程死锁与优先级反转实战: 从问题复现到工程解决方案 | 2026-02-16T10:00:00 |
| [cpp_singleton_thread_safety_dclp.md](performance/cpp_singleton_thread_safety_dclp.md) | C++ 单例模式的线程安全实现: 从 DCLP 的历史缺陷到 C++11 的修复 | 2026-02-16T09:50:00 |
| [cpp_performance_memory_branch_compiler.md](performance/cpp_performance_memory_branch_compiler.md) | C/C++ 性能优化实战: 内存布局、分支预测与编译器调优 | 2026-02-16T09:40:00 |
| [cpp14_message_bus_optimization.md](performance/cpp14_message_bus_optimization.md) | C++14 消息总线的工程优化与性能瓶颈分析 | 2026-02-16T09:30:00 |
| [arm_linux_network_optimization.md](performance/arm_linux_network_optimization.md) | ARM-Linux 网络性能优化实战: 从中断到零拷贝的全链路调优 | 2026-02-16T09:20:00 |
| [parallel_matmul_benchmark.md](performance/parallel_matmul_benchmark.md) | C++17 并行矩阵乘法: 从单线程到多进程共享内存的性能实测 | 2026-02-15T10:30:00 |
| [message_bus_competitive_benchmark.md](performance/message_bus_competitive_benchmark.md) | C++ 消息总线性能实测: 6 个开源方案的吞吐量与延迟对比 | 2026-02-15T10:20:00 |
| [message_bus_benchmark_methodology.md](performance/message_bus_benchmark_methodology.md) | 如何公平地对比消息总线性能: 基准测试方法论与陷阱 | 2026-02-15T10:10:00 |
| [mccc_lockfree_mpsc_design.md](performance/mccc_lockfree_mpsc_design.md) | Lock-free MPSC 消息总线的设计与实现: 从 Ring Buffer 到零堆分配 | 2026-02-15T10:00:00 |
| [lockfree_async_log.md](performance/lockfree_async_log.md) | 无锁异步日志设计: Per-Thread SPSC 环形缓冲与分级路由 | 2026-02-15T09:50:00 |
| [eventpp_arm_optimization_report.md](performance/eventpp_arm_optimization_report.md) | eventpp 性能优化实战: 6 个瓶颈定位与 5 倍吞吐提升 | 2026-02-15T09:40:00 |
| [embedded_deadlock_prevention_lockfree.md](performance/embedded_deadlock_prevention_lockfree.md) | 嵌入式系统死锁防御: 从有序锁到无锁架构的工程实践 | 2026-02-15T09:30:00 |
| [embedded_callback_zero_overhead.md](performance/embedded_callback_zero_overhead.md) | 嵌入式消息总线的回调优化: 从 std::function 到零开销分发 | 2026-02-15T09:20:00 |
| [cpp17_binary_size_vs_c.md](performance/cpp17_binary_size_vs_c.md) | C++17 vs C 二进制体积: 嵌入式场景的实测与分析 | 2026-02-15T09:10:00 |
| [cpp11_threadsafe_pubsub_bus.md](performance/cpp11_threadsafe_pubsub_bus.md) | C++11 线程安全消息总线: 从零实现 Pub/Sub 模型 | 2026-02-15T09:00:00 |
| [armv8_crc32_hardware_vs_neon_benchmark.md](performance/armv8_crc32_hardware_vs_neon_benchmark.md) | ARMv8 CRC 性能实测: 硬件指令快 8 倍, NEON 反而更慢 | 2026-02-15T08:50:00 |
| [arm_linux_lock_contention_benchmark.md](performance/arm_linux_lock_contention_benchmark.md) | ARM-Linux 锁竞争性能实测: Spinlock/Mutex/ConcurrentQueue 对比 | 2026-02-15T08:40:00 |

### practice/ -- 工程实践 (10 篇)

| 文件 | 标题 | 日期 |
|------|------|------|
| [newosp_industrial_embedded_library.md](practice/newosp_industrial_embedded_library.md) | newosp: 面向工业嵌入式的 C++17 Header-Only 基础设施库 | 2026-02-17T10:00:00 |
| [mccc_bus_cpp17_practice.md](practice/mccc_bus_cpp17_practice.md) | 从 C++14 到 C++17: mccc-bus 的四项零堆分配改造 | 2026-02-17T09:50:00 |
| [cpp17_claims_in_newosp.md](practice/cpp17_claims_in_newosp.md) | newosp 源码中的 C++17 实践: 8 项能力的工程落地 | 2026-02-17T09:40:00 |
| [qpc_active_object_hsm.md](practice/qpc_active_object_hsm.md) | QPC 框架深度解析: Active Object 与层次状态机的嵌入式实践 | 2026-02-16T11:40:00 |
| [dbpp_cpp14_database_modernization.md](practice/dbpp_cpp14_database_modernization.md) | 数据库抽象层的 C++14 重写: 从手动内存管理到 RAII | 2026-02-16T11:30:00 |
| [clang_tidy_embedded_cpp17.md](practice/clang_tidy_embedded_cpp17.md) | Clang-Tidy 嵌入式 C++17 实战: 从配置到 CI 集成的完整指南 | 2026-02-16T11:20:00 |
| [behavior_tree_tick_mechanism.md](practice/behavior_tree_tick_mechanism.md) | 行为树 Tick 机制深度解析: 从原理到 bt-cpp 实践 | 2026-02-16T11:10:00 |
| [ztask_scheduler.md](practice/ztask_scheduler.md) | ztask: 零动态分配的裸机合作式任务调度器设计分析 | 2026-02-15T11:00:00 |
| [ztask_cpp_modernization.md](practice/ztask_cpp_modernization.md) | ztask 调度器的 C++14 重写: 类型安全、RAII 与模板化改造 | 2026-02-15T10:50:00 |
| [mccc_bus_api_reference.md](practice/mccc_bus_api_reference.md) | MCCC 消息总线 API 全参考: 类型、接口与配置 | 2026-02-15T10:40:00 |

### pattern/ -- 设计模式 (8 篇)

| 文件 | 标题 | 日期 |
|------|------|------|
| [pimpl_modern_cpp_embedded.md](pattern/pimpl_modern_cpp_embedded.md) | PIMPL 的三种现代实现: 从堆分配到栈内联 | 2026-02-17T08:50:00 |
| [cpp17_what_c_cannot_do.md](pattern/cpp17_what_c_cannot_do.md) | C11 做不到的事: 10 项 C++17 语言级不可替代能力 | 2026-02-17T08:40:00 |
| [smart_pointer_pitfalls_embedded.md](pattern/smart_pointer_pitfalls_embedded.md) | 嵌入式 C++ 智能指针的五个陷阱与零堆分配替代方案 | 2026-02-16T09:10:00 |
| [cpp_design_patterns_embedded.md](pattern/cpp_design_patterns_embedded.md) | 嵌入式 C++17 设计模式实战: 零虚函数、零堆分配的编译期技术 | 2026-02-16T09:00:00 |
| [compile_time_dispatch_optimization.md](pattern/compile_time_dispatch_optimization.md) | 嵌入式系统中的编译期分发: 用模板消除虚函数开销 | 2026-02-16T08:50:00 |
| [c_strategy_state_pattern.md](pattern/c_strategy_state_pattern.md) | C 语言设计模式实战: 策略模式与状态模式的本质差异 | 2026-02-16T08:40:00 |
| [c_hsm_data_driven_framework.md](pattern/c_hsm_data_driven_framework.md) | C 语言层次状态机框架: 从过程驱动到数据驱动的重构实践 | 2026-02-16T08:30:00 |
| [c_oop_nginx_modular_architecture.md](pattern/c_oop_nginx_modular_architecture.md) | C 语言如何实现面向对象: Nginx 模块化架构源码解读 | 2026-02-15T08:30:00 |

### interview/ -- 面试题 (2 篇)

| 文件 | 标题 | 日期 |
|------|------|------|
| [senior_embedded_c_language_interview_questions.md](interview/senior_embedded_c_language_interview_questions.md) | 嵌入式 C 语言深度面试题: 系统与架构 | 2025-01-02T08:00:00 |
| [senior_embedded_software_engineer_interview_questions.md](interview/senior_embedded_software_engineer_interview_questions.md) | 高级嵌入式软件工程师面试题: 架构设计与工程深度 | 2025-01-01T08:00:00 |

### misc/ -- 杂项 (17 篇)

| 文件 | 标题 | 日期 |
|------|------|------|
| [linux_dev_commands_cheatsheet.md](misc/linux_dev_commands_cheatsheet.md) | Linux 开发常用命令与脚本速查手册 | 2026-02-17T11:30:00 |
| [git_advanced_workflow_guide.md](misc/git_advanced_workflow_guide.md) | Git 高级工作流完全指南 | 2026-02-17T11:20:00+08:00 |
| [vmware_sogou_focus_fix.md](misc/vmware_sogou_focus_fix.md) | VMware 虚拟机搜狗输入法焦点跳转问题分析与解决 | 2026-02-17T11:00:00 |
| [copilot_terminal_auto_approve.md](misc/copilot_terminal_auto_approve.md) | VS Code Copilot 终端命令自动审批的安全配置 | 2026-02-17T10:50:00 |
| [vscode_remote_dev_setup.md](misc/vscode_remote_dev_setup.md) | 嵌入式 C++ 远程开发环境: 从 SSH 到交叉调试 | 2026-02-17T10:40:00 |
| [design_diagram_tool_selection.md](misc/design_diagram_tool_selection.md) | 设计文档画图工具选型 | 2026-02-17T10:30:00 |
| [newosp_shell_multibackend.md](misc/newosp_shell_multibackend.md) | newosp 调试 Shell: 多后端架构与运行时控制命令设计 | 2026-02-17T10:20:00 |
| [lmdb_embedded_linux_zero_copy.md](misc/lmdb_embedded_linux_zero_copy.md) | LMDB 在嵌入式 Linux 上的实践: 零拷贝读取与内存映射 I/O | 2026-02-17T10:10:00 |
| [uart_protocol_parsing.md](misc/uart_protocol_parsing.md) | 嵌入式串口协议栈设计: 粘包、缓冲区滑窗与层次状态机 | 2026-02-16T12:40:00 |
| [perf_performance_analysis.md](misc/perf_performance_analysis.md) | perf 性能分析实战: 从硬件计数器到火焰图的完整工作流 | 2026-02-16T12:30:00 |
| [perf_lock_contention_diagnosis.md](misc/perf_lock_contention_diagnosis.md) | perf lock 锁竞争诊断: 从 futex 原理到生产定位实战 | 2026-02-16T12:20:00 |
| [newosp_ospgen_codegen.md](misc/newosp_ospgen_codegen.md) | newosp ospgen: YAML 驱动的嵌入式 C++17 零堆分配消息代码生成 | 2026-02-16T12:10:00 |
| [embedded_ssh_scp_automation.md](misc/embedded_ssh_scp_automation.md) | 告别手动输密码: 嵌入式 SSH/SCP 自动化方案 | 2026-02-16T12:00:00 |
| [embedded_config_serialization.md](misc/embedded_config_serialization.md) | 嵌入式配置序列化选型: struct/TLV/nanopb/capnproto 对比 | 2026-02-16T11:50:00 |
| [telnet_debug_shell_posix_refactoring.md](misc/telnet_debug_shell_posix_refactoring.md) | 嵌入式 Telnet 调试 Shell 重构: 纯 POSIX 轻量化实现 | 2026-02-15T11:30:00 |
| [rtthread_msh_linux_multibackend.md](misc/rtthread_msh_linux_multibackend.md) | 将 RT-Thread MSH 移植到 Linux: 嵌入式调试 Shell 的多后端设计 | 2026-02-15T11:20:00 |
| [cpp14_pluggable_log_library_design.md](misc/cpp14_pluggable_log_library_design.md) | 轻量级 C++14 日志库设计: 可插拔后端与零依赖架构 | 2026-02-15T11:10:00 |

## 关联项目

| 项目 | 地址 | 说明 |
|------|------|------|
| newosp | [GitHub](https://github.com/DeguiLiu/newosp) | C++17 Header-Only 嵌入式基础设施库 |
| mccc-bus | [Gitee](https://gitee.com/liudegui/mccc-bus) | 高性能无锁消息总线 |
| eventpp (fork) | [Gitee](https://gitee.com/liudegui/eventpp) | eventpp ARM-Linux 优化分支 |
| message_bus | [Gitee](https://gitee.com/liudegui/message_bus) | C++11 非阻塞消息总线 |
| lock-contention-benchmark | [Gitee](https://gitee.com/liudegui/lock-contention-benchmark) | 锁竞争基准测试 |
| ztask-cpp | [Gitee](https://gitee.com/liudegui/ztask-cpp) | C++14 Header-Only 合作式任务调度器 |

## 主要技术领域

- C/C++ 系统编程 (C11, C++14/17)
- 嵌入式系统 (ARM-Linux, RTOS, MCU)
- 并发与无锁编程 (CAS, SPSC, MPSC)
- 性能优化与基准测试
- 架构设计与设计模式

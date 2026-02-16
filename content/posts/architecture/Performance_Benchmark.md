---
title: "性能基准测试报告"
date: 2026-02-15
draft: false
categories: ["architecture"]
tags: ["ARM", "C++17", "MCCC", "MISRA", "callback", "embedded", "lock-free", "message-bus", "performance", "scheduler"]
summary: "**MCCC Lock-free 提供优先级保护和背压控制，同时保持高性能。**"
ShowToc: true
TocOpen: true
---

## 核心结论 (Key Takeaways)

> **MCCC Lock-free 提供优先级保护和背压控制，同时保持高性能。**
> **eventpp 优化分支 (OPT-1~8) 显著提升 Active Object 吞吐量。**

| 结论 | 数据支撑 |
|------|----------|
| **MCCC BARE_METAL 极高性能** | 18.7 M/s, 54 ns/msg（Lock-free + 线程安全） |
| **MCCC FULL_FEATURED 生产可用** | 5.8 M/s, 172 ns/msg（全功能 + 线程安全） |
| **eventpp 优化后 AO 大幅提升** | 8.5 M/s, 118 ns/msg（优化前 1.5 M/s, 689 ns） |
| **HIGH 消息零丢失** | 背压测试中 HIGH 优先级丢弃率 **0%** |
| **功能开销可控** | 全功能模式增加 ~118 ns/消息 |
| **尾部延迟稳定** | MCCC E2E P99 仅 449 ns |

---

## 对比层级说明

> **重要**: 不同层级的对比有不同的语义，不能混淆。

### 层级定义

| 层级 | 实现 | 特点 | 适用场景 |
|------|------|------|----------|
| **L1: Raw Queue** | eventpp 值语义 (优化后) | 无堆分配、无线程切换 | 理论上限参考 |
| **L1.5: Raw + PoolQueueList** | eventpp + 池化分配器 | 零 per-node malloc | 小批量最优 |
| **L2: Queue + shared_ptr** | eventpp + shared_ptr | 有堆分配、无线程切换 | AO 封装开销参考 |
| **L3: Active Object** | eventpp + AO + 线程 (优化后) | 有堆分配、有线程切换 | 生产环境 eventpp |
| **L4: MCCC BARE_METAL** | Lock-free Ring Buffer | 无堆分配、有线程切换、无锁 | 公平对比 L3 |
| **L5: MCCC FULL** | L4 + 优先级/背压/统计 | 全功能 | 生产环境 MCCC |

### 性能对比图

```
吞吐量 (M msg/s) - 10K 消息
│
30  ┤ █████████████████████████████ 28.5 L1.5: PoolQueueList (小批量最优)
    │
25  ┤ ██████████████████████ 22.2 L1: Raw eventpp (优化后)
    │
20  ┤ ███████████████████ 18.7 L4: MCCC BARE_METAL
    ┤ █████████████████ 17.0 L2: eventpp + shared_ptr
15  ┤
    │
10  ┤ █████████ 8.5 L3: eventpp + AO (优化后, 原 1.5)
    │
 5  ┤ ██████ 5.8 L5: MCCC FULL_FEATURED
    │
 0  └─────────────────────────────────────────────────────────────────
```

> **关键发现**:
> - L1.5 (PoolQueueList) 在小批量场景下达 28.5 M/s，超越默认 std::list
> - L3 (eventpp + AO) 优化后从 1.5 M/s 提升至 8.5 M/s（**5.7x**），吞吐量超过 L5
> - L4 (MCCC BARE_METAL) 吞吐量 18.7 M/s，接近 Raw eventpp
> - L5 (MCCC FULL) 吞吐量 5.8 M/s，但 E2E 延迟远优于 L3（P50 367 ns vs 11.6 us）

### 数值汇总

| 层级 | 方案 | 吞吐量 | 入队延迟 | 堆分配 | 线程安全 |
|:----:|------|--------|---------|:------:|:--------:|
| L1 | Raw eventpp (优化后) | 22.2 +/- 3.4 M/s | 46 +/- 8 ns | - | - |
| L1.5 | Raw + PoolQueueList | 28.5 +/- 3.1 M/s | 36 +/- 4 ns | - | - |
| L2 | eventpp + shared_ptr | 17.0 +/- 5.7 M/s | 54 +/- 5 ns | 有 | - |
| L3 | eventpp + AO (优化后) | 8.5 +/- 0.6 M/s | 118 +/- 8 ns | 有 | 有 |
| **L4** | **MCCC BARE_METAL** | **18.7 +/- 1.4 M/s** | **54 +/- 4 ns** | **无** | **有** |
| **L5** | **MCCC FULL_FEATURED** | **5.8 +/- 0.3 M/s** | **172 +/- 8 ns** | **无** | **有** |

---

## 开销来源分析

> **优化后 MCCC BARE_METAL 已接近 Raw eventpp 性能**

### 开销分解

| 分类 | 开销来源 | 估算 | 说明 |
|:----:|----------|------|------|
| **已消除** | ~~shared_ptr 堆分配~~ | 0 ns | Envelope 内嵌在 RingBufferNode |
| **已消除** | ~~unordered_map hash 查找~~ | 0 ns | 固定数组 + 编译期类型索引 |
| **已消除** | ~~原子引用计数~~ | 0 ns | 无 shared_ptr |
| **保留** | 优先级检查 + 背压判断 | ~30-70 ns | FULL_FEATURED 模式 |
| **保留** | 统计计数 (atomic fetch_add) | ~20-40 ns | FULL_FEATURED 模式 |
| **保留** | CAS 竞争 | ~10-30 ns | 多生产者场景 |
| | **BARE_METAL 总开销** | ~54 ns | 仅 CAS + 序列号同步 |
| | **FULL 功能开销** | ~118 ns | 优先级 + 背压 + 统计 |

### MCCC 如何避免传统开销

| 开销 | eventpp + AO | MCCC (优化后) | 差异原因 |
|------|:------------:|:----:|----------|
| 堆分配 | 每消息 make_shared | **零** | Envelope 内嵌 Ring Buffer |
| 引用计数 | 原子操作 | **零** | 无 shared_ptr |
| 队列锁 | 互斥锁 | **零 (CAS)** | Lock-free MPSC |
| 回调查找 | unordered_map hash | **O(1) 数组** | 编译期 VariantIndex |
| 线程切换 | 独立线程 | 独立线程 | 两者相同 |

> **总结**：优化后 BARE_METAL 吞吐从 3.1 M/s 提升至 18.7 M/s (6.0x)，
> 主要得益于消除 shared_ptr 堆分配和 unordered_map 查找。

---

## 背压与优先级测试

> **测试目的**: 验证系统过载时，高优先级消息（如紧急停止）不会被低优先级消息阻塞。

### 测试方法

| 步骤 | 操作 |
|------|------|
| 1 | 暂停消费者线程（模拟处理瓶颈） |
| 2 | 突发发送 150,000 条消息（超过队列容量 131,072） |
| 3 | 按优先级分布：HIGH 20%, MEDIUM ~26%, LOW ~26% |
| 4 | 统计各优先级丢弃率 |

### 测试结果

```
丢弃率 (%) - HIGH 应该最低
│
50% ┤                              ████████ 47.6% LOW (优先丢弃)
    │
30% ┤
    │
10% ┤              ████████ 12.6% MEDIUM
    │
 0% ┤ ████ 0.0% HIGH  (完全保护)
    └─────────────────────────────────────────────────────────────────
         HIGH           MEDIUM          LOW
```

| 优先级 | 发送 | 成功 | 丢弃 | 丢弃率 | 状态 |
|--------|------|------|------|--------|:----:|
| **HIGH** | 30,000 | 30,000 | 0 | **0.0%** | 完全保护 |
| MEDIUM | 39,321 | 33,642 | 5,679 | 12.6% | 次级保护 |
| LOW | 39,320 | 3,640 | 35,680 | 47.6% | 优先丢弃 |

**结论**: 优先级准入控制验证通过！

---

## 端到端延迟测试

> **测试目的**: 测量消息从发布到回调执行的完整延迟（包含队列等待时间）。

### MCCC E2E 延迟 (10,000 样本)

| 分位数 | MCCC | 说明 |
|--------|------|------|
| Mean | 380 ns | 平均延迟 |
| StdDev | 334 ns | 标准差 |
| Min | 287 ns | 最小延迟 |
| **P50** | **367 ns** | 中位数 |
| P95 | 396 ns | 95% 分位 |
| **P99** | **449 ns** | 99% 分位 |
| Max | 17,649 ns | 最大延迟 |

### eventpp + AO E2E 延迟 (10,000 样本, 优化后)

| 分位数 | eventpp + AO (优化后) | 说明 |
|--------|----------------------|------|
| Mean | 11,715 ns | 平均延迟 |
| StdDev | 2,888 ns | 标准差 |
| Min | 966 ns | 最小延迟 |
| **P50** | **11,588 ns** | 中位数 |
| P95 | 15,545 ns | 95% 分位 |
| **P99** | **24,289 ns** | 99% 分位 |
| Max | 41,844 ns | 最大延迟 |

### E2E 延迟对比

| 分位数 | MCCC | eventpp + AO (优化后) | 差距 |
|--------|:----:|:--------------------:|:----:|
| P50 | 367 ns | 11,588 ns | 32x |
| P95 | 396 ns | 15,545 ns | 39x |
| P99 | 449 ns | 24,289 ns | 54x |
| Max | 18 us | 42 us | 2.3x |

**分析**:
- MCCC P50 约 367 ns，P99 仅 449 ns，尾部延迟极其稳定（无锁设计）
- eventpp + AO 优化后吞吐量大幅提升（8.5 M/s），但 E2E P50 约 11.6 us（内部批量调度开销）
- MCCC 在 E2E 延迟上优势更加显著（P50 相差 32x）

---

## 详细测试数据

### MCCC 批量测试 (FULL_FEATURED, 10 轮统计)

| 场景 | 消息数 | 吞吐量 | 入队延迟 |
|------|--------|--------|---------|
| Small | 1K | 5.19 +/- 0.50 M/s | 194 +/- 20 ns |
| Medium | 10K | 5.41 +/- 0.17 M/s | 185 +/- 6 ns |
| Large | 100K | 5.52 +/- 0.10 M/s | 181 +/- 3 ns |

> 大批量测试方差更小 (StdDev 0.10 vs 0.50)，说明性能在持续负载下更稳定。

### MCCC 性能模式对比 (100K 消息, 10 轮)

| 模式 | 吞吐量 | 入队延迟 | 说明 |
|------|--------|---------|------|
| FULL_FEATURED | 5.84 +/- 0.28 M/s | 172 +/- 8 ns | 优先级+背压+统计 |
| BARE_METAL | 18.70 +/- 1.38 M/s | 54 +/- 4 ns | 仅队列操作 |
| **功能开销** | - | **~118 ns** | 全功能额外开销 |
| **BARE_METAL 提升** | **220%** | **69% 降低** | 相对 FULL_FEATURED |

### eventpp Raw 测试 (优化后, 值语义, 10 轮统计)

| 场景 | 消息数 | 吞吐量 | 入队延迟 |
|------|--------|--------|---------|
| Small | 1K | 17.6 +/- 2.8 M/s | 58 +/- 9 ns |
| Medium | 10K | 22.2 +/- 3.4 M/s | 46 +/- 8 ns |
| Large | 100K | 26.5 +/- 4.6 M/s | 40 +/- 12 ns |
| VeryLarge | 1M | 24.8 +/- 4.0 M/s | 42 +/- 11 ns |

### eventpp PoolQueueList 测试 (OPT-5 池化分配器, 10 轮统计)

| 场景 | 消息数 | 吞吐量 | 入队延迟 |
|------|--------|--------|---------|
| Small | 1K | 26.1 +/- 3.4 M/s | 39 +/- 6 ns |
| Medium | 10K | 28.5 +/- 3.1 M/s | 36 +/- 4 ns |
| Large | 100K | 25.1 +/- 2.0 M/s | 40 +/- 4 ns |
| VeryLarge | 1M | 23.5 +/- 1.0 M/s | 43 +/- 2 ns |

> PoolQueueList 在 Small/Medium 批量下优势明显（26-29 M/s vs 18-22 M/s），
> 大批量时与默认 std::list 持平（池耗尽后回退到堆分配）。

### eventpp + shared_ptr 测试 (10 轮统计)

| 场景 | 消息数 | 吞吐量 | 入队延迟 |
|------|--------|--------|---------|
| Small | 1K | 16.9 +/- 1.5 M/s | 60 +/- 5 ns |
| Medium | 10K | 17.0 +/- 5.7 M/s | 59 +/- 5 ns |
| Large | 100K | 18.7 +/- 1.6 M/s | 54 +/- 5 ns |
| VeryLarge | 1M | 17.0 +/- 2.1 M/s | 60 +/- 11 ns |

### eventpp + Active Object 测试 (优化后, 10 轮统计)

| 场景 | 消息数 | 吞吐量 | 入队延迟 |
|------|--------|--------|---------|
| Small | 1K | 6.05 +/- 0.46 M/s | 166 +/- 12 ns |
| Medium | 10K | 8.52 +/- 0.58 M/s | 118 +/- 8 ns |
| Large | 100K | 4.22 +/- 0.23 M/s | 238 +/- 13 ns |

> Large 批量方差较大，因为 100K 消息超过 eventpp 内部队列容量时触发锁竞争。

### 持续吞吐测试 (5 秒)

| 指标 | MCCC | eventpp + AO (优化后) |
|------|:----:|:--------------------:|
| 持续时间 | 5.00 秒 | 5.00 秒 |
| 消息发送 | 17,077,858 | 15,626,412 |
| 消息处理 | 17,077,858 | 15,626,412 |
| 消息丢弃 | 3,644,451 | 0 |
| 吞吐量 | 3.42 M/s | 3.13 M/s |

> MCCC 持续测试中生产者速度超过消费者处理速度，优先级准入控制正常工作。
> 丢弃的消息全部为低优先级，高优先级消息零丢失。
> eventpp + AO 无背压机制，持续测试中不丢弃消息。

---

## eventpp 优化前后对比

> eventpp 优化分支实施了 8 项优化 (OPT-1~8)。
> 详见 [eventpp_Optimization_Report.md](eventpp_Optimization_Report.md)。

### eventpp 优化项

| OPT | 优化内容 | 主要收益 |
|:---:|----------|----------|
| 1 | SpinLock ARM YIELD / x86 PAUSE | 降低自旋功耗 |
| 2 | CallbackList 批量预取 (8x 减少锁) | **P99 延迟大幅降低** |
| 3 | EventDispatcher shared_mutex 读写分离 | **多线程 dispatch 不互斥** |
| 4 | doEnqueue try_lock (非阻塞 freeList) | 减少入队锁竞争 |
| 5 | PoolAllocator 池化分配器 | 小批量吞吐提升 |
| 6 | Cache-line 对齐 | 消除 false sharing |
| 7 | 内存序 acq_rel (ARM 屏障降级) | ARM 上减少 dmb 指令 |
| 8 | waitFor 自适应 spin | 减少 futex 系统调用 |

### Active Object 优化前后

| 指标 | 优化前 (vanilla v0.1.3) | 优化后 (OPT-1~8) | 提升 |
|------|:----------------------:|:-----------------:|:----:|
| Small 1K 吞吐量 | ~1.9 M/s | 6.05 M/s | **3.2x** |
| Medium 10K 吞吐量 | ~1.6 M/s | 8.52 M/s | **5.3x** |
| Large 100K 吞吐量 | ~1.5 M/s | 4.22 M/s | **2.8x** |
| E2E P50 | ~1,200 ns | 11,588 ns | 吞吐-延迟权衡 |
| E2E P99 | ~8,953 ns | 24,289 ns | 吞吐-延迟权衡 |
| 持续吞吐 | ~1.25 M/s | 3.13 M/s | **2.5x** |

### Raw eventpp 优化前后

| 指标 | 优化前 (vanilla v0.1.3) | 优化后 (OPT-1~8) | 提升 |
|------|:----------------------:|:-----------------:|:----:|
| Medium 10K | 23.5 M/s | 22.2 M/s | 持平 |
| VeryLarge 1M | 22.2 M/s | 24.8 M/s | +12% |
| PoolQueueList 10K | - | 28.5 M/s | **+28%** (vs 默认) |

> Raw 值语义场景下优化前后持平（单线程无锁竞争，OPT-2/3/4 不生效）。
> PoolQueueList 在小批量场景下提供额外 28% 吞吐提升。

---

## MCCC 优化前后对比

> 本轮优化核心改动：Envelope 内嵌 + 固定回调表 + DataToken 函数指针

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| FULL_FEATURED 吞吐量 | ~2.0 M/s | ~5.8 M/s | **2.9x** |
| BARE_METAL 吞吐量 | ~3.1 M/s | ~18.7 M/s | **6.0x** |
| FULL 入队延迟 | ~505 ns | ~172 ns | **66% 降低** |
| BARE 入队延迟 | ~318 ns | ~54 ns | **83% 降低** |
| 功能开销 | ~187 ns | ~118 ns | **37% 降低** |
| Publish 堆分配 | 1 次 (make_shared) | 0 次 | **消除** |
| Borrow 堆分配 | 1 次 (unique_ptr) | 0 次 | **消除** |

---

## 架构对比

| 特性 | eventpp + Active Object (优化后) | MCCC Lock-free |
|------|--------------------------------|----------------|
| 底层实现 | eventpp::EventQueue (OPT-1~8) | Lock-free MPSC Ring Buffer |
| 同步机制 | shared_mutex + 批量预取 | CAS 原子操作 |
| 内存分配 | 每消息 shared_ptr (可选 PoolAllocator) | 固定 Ring Buffer (Envelope 内嵌) |
| 回调查找 | type_index + unordered_map | 编译期 VariantIndex + 固定数组 |
| 优先级支持 | - | HIGH/MEDIUM/LOW |
| 背压控制 | - | 60%/80%/99% 阈值 |
| 类型安全 | void* 类型擦除 | std::variant 强类型 |
| 外部依赖 | eventpp (优化分支) | 无 |
| MISRA 合规 | 部分 | 大部分合规 |
| 编译期可配置 | PoolQueueList (opt-in) | 队列深度/缓存行/回调表大小 |

---

## 适用场景建议

| 场景 | 推荐方案 | 原因 |
|------|----------|------|
| 安全关键系统 | **MCCC** | 优先级保护 + MISRA 合规 |
| 需要背压控制 | **MCCC** | 分级丢弃验证通过 |
| 尾部延迟敏感 | **MCCC** | P99 449 ns，Max 18 us |
| 零依赖要求 | **MCCC** | 纯 C++17 实现 |
| 高吞吐低延迟 | **MCCC** | BARE_METAL 18.7 M/s, 54 ns |
| 嵌入式/MCU | **MCCC** | 编译宏裁剪，零堆分配 |
| 已有 eventpp 代码 | **eventpp + AO (优化后)** | 迁移成本低，优化后 3.1 M/s 持续吞吐 |
| 单线程高吞吐 | **eventpp PoolQueueList** | 29 M/s (小批量) |

---

## 测试环境

| 项目 | 配置 |
|------|------|
| OS | Ubuntu 24.04 LTS |
| Compiler | GCC 13.3.0 |
| Optimization | -O3 -march=native -faligned-new |
| C++ Standard | C++17 |
| eventpp | 优化版 (OPT-1~8), Gitee: liudegui/eventpp |

---

## 复现测试

```bash
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)

# 运行测试
./mccc_benchmark             # MCCC 性能测试
./eventpp_raw_benchmark      # eventpp Raw + PoolQueueList + shared_ptr 对比
./eventpp_benchmark          # eventpp + Active Object 性能测试
./demo_mccc                  # 功能验证
```

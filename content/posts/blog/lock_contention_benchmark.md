---
title: "ARM-Linux 锁竞争性能实测: Spinlock/Mutex/ConcurrentQueue 对比"
date: 2026-02-15
draft: false
categories: ["blog"]
tags: ["ARM", "RTOS", "embedded", "lock-free", "performance", "scheduler"]
summary: "本文通过严格的基准测试方法，对比多线程高竞争场景下三种同步策略的性能表现：自旋锁 (atomic_flag)、互斥锁 (std::mutex) 和无锁队列 (moodycamel::ConcurrentQueue)。"
ShowToc: true
TocOpen: true
---

> 本文通过严格的基准测试方法，对比多线程高竞争场景下三种同步策略的性能表现：自旋锁 (atomic_flag)、互斥锁 (std::mutex) 和无锁队列 (moodycamel::ConcurrentQueue)。
>
> 相关文章:
> - [perf lock 锁竞争诊断](../perf_lock_contention_diagnosis/) -- 生产环境的锁竞争定位方法
> - [嵌入式系统死锁防御: 从有序锁到无锁架构](../deadlock_prevention/) -- 从架构层面消除锁问题
> - [无锁编程核心原理](../lockfree_programming_fundamentals/) -- 无锁数据结构的理论基础
> - [多线程死锁与优先级反转实战](../deadlock_priority_inversion_practice/) -- 锁使用不当的典型问题
>
> 完整测试代码: [lock-contention-benchmark](https://gitee.com/liudegui/lock-contention-benchmark)

## 1. 背景

多线程数据共享是嵌入式系统的核心问题。常见的同步策略有三类：

| 策略 | 机制 | 适用场景 |
|------|------|----------|
| Spinlock | atomic_flag TAS + pause/yield | 短临界区、线程数 <= 核心数 |
| Mutex | OS futex (Linux) | 长临界区、线程数 > 核心数 |
| Lock-free Queue | CAS 原子操作 | MPMC 生产者-消费者模型 |

一个常见的误区是将 `std::atomic_flag` 自旋锁称为"无锁"。自旋锁本质上仍然是锁 -- 它通过忙等待 (busy-wait) 获取互斥访问权，只是不经过 OS 调度器。真正的 lock-free 数据结构（如 ConcurrentQueue）保证至少一个线程能在有限步内完成操作，不存在互斥等待。

## 2. 旧测试的问题

此前的测试代码存在多个方法论缺陷，导致结果不可信：

| 问题 | 影响 |
|------|------|
| Push/Pop 数量不匹配 | 30 线程各 push 10K = 300K 条；30 线程各 pop 333 = 9,990 条。Pop 仅消费 3.3%，Pop 时间完全失真 |
| CMake 变量名错误 | 检查 `COMPILER_SUPPORTS_CXX14` 但 if 判断 `COMPILER_SUPPORTS_CXX11`，C++ 标准未生效 |
| Debug 构建 (-O0) | 基准测试在无优化模式下运行，结果无参考价值 |
| 无 warmup | 第一个测试承受 CPU cache 冷启动惩罚 |
| 单次运行 | 无法评估方差，结果不可重复 |
| 无线程同步起跑 | 线程创建有先后，不是同时开始竞争 |
| Spinlock 无 pause 指令 | 自旋循环浪费 CPU 流水线资源 |
| std::list 容器 | 每次 push 触发堆分配，测的是"锁 + 分配器"混合开销 |
| try_dequeue 返回值未检查 | ConcurrentQueue 可能空转 |
| 编译器可能优化掉结果 | dequeue 的值未使用，编译器可能消除整个循环 |

## 3. 改进后的测试方法

### 3.1 测试参数

```
Threads:         8
Items/thread:    50,000
Total items:     400,000 (push 和 pop 数量严格相等)
Warmup rounds:   2 (结果丢弃)
Measured rounds: 5 (报告 min/median/max)
SmallItem:       80 bytes (int32_t[20])
LargeItem:       4096 bytes (int32_t[1024])
```

### 3.2 关键改进

**线程同步起跑**: 使用原子 Barrier，所有线程就绪后同时开始竞争：

```cpp
class Barrier {
 public:
    explicit Barrier(int32_t count) : threshold_(count), count_(count), gen_(0) {}

    void wait() {
        uint32_t my_gen = gen_.load(std::memory_order_relaxed);
        if (--count_ == 0) {
            count_ = threshold_;
            gen_.fetch_add(1, std::memory_order_release);
        } else {
            while (gen_.load(std::memory_order_acquire) == my_gen) {
                spin_pause();
            }
        }
    }
};
```

**Spinlock 加 pause 提示**: 减少自旋时的流水线浪费：

```cpp
inline void spin_pause() {
#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__) || defined(_M_IX86)
    __builtin_ia32_pause();
#elif defined(__aarch64__) || defined(__arm__)
    asm volatile("yield" ::: "memory");
#endif
}
```

**编译器屏障**: 防止编译器优化掉 dequeue 结果：

```cpp
template <typename T>
inline void do_not_optimize(const T& val) {
    asm volatile("" : : "r,m"(val) : "memory");
}
```

**容器统一为 std::deque**: 隔离锁竞争成本，避免 `std::list` 的逐元素堆分配干扰。

**ConcurrentQueue 使用 ProducerToken/ConsumerToken**: 利用 per-thread token 获得最佳吞吐。

## 4. 测试结果

### 4.1 测试环境

```
CPU:      AMD Ryzen 7 5800H (8 cores / 16 threads) @ 3.2GHz
RAM:      32GB DDR4
OS:       Ubuntu 24.04, Linux 6.8.0-79-generic x86_64
Compiler: GCC 13.3.0, -O2 -DNDEBUG
```

### 4.2 SmallItem (80 bytes)

| 同步策略 | Push min | Push median | Push max | Pop min | Pop median | Pop max |
|----------|----------|-------------|----------|---------|------------|---------|
| Spinlock + deque | 136.85 ms | 144.45 ms | 150.62 ms | 114.25 ms | 123.02 ms | 128.10 ms |
| Mutex + deque | 154.14 ms | 178.89 ms | 186.68 ms | 198.58 ms | 211.40 ms | 215.61 ms |
| ConcurrentQueue | 2.78 ms | 2.98 ms | 3.33 ms | 4.08 ms | 4.25 ms | 5.07 ms |

吞吐量换算 (基于 median, 400K items):

| 同步策略 | Push ops/s | Pop ops/s |
|----------|-----------|-----------|
| Spinlock + deque | 2.77M | 3.25M |
| Mutex + deque | 2.24M | 1.89M |
| ConcurrentQueue | **134.2M** | **94.1M** |

### 4.3 LargeItem (4096 bytes)

| 同步策略 | Push min | Push median | Push max | Pop min | Pop median | Pop max |
|----------|----------|-------------|----------|---------|------------|---------|
| Spinlock + deque | 1.584 s | 1.631 s | 1.659 s | 261.05 ms | 268.85 ms | 286.62 ms |
| Mutex + deque | 2.591 s | 2.693 s | 2.713 s | 461.52 ms | 477.66 ms | 587.43 ms |
| ConcurrentQueue | 267.54 ms | 283.93 ms | 288.31 ms | 47.03 ms | 48.53 ms | 54.17 ms |

吞吐量换算 (基于 median, 400K items):

| 同步策略 | Push ops/s | Pop ops/s |
|----------|-----------|-----------|
| Spinlock + deque | 245K | 1.49M |
| Mutex + deque | 149K | 837K |
| ConcurrentQueue | **1.41M** | **8.24M** |

## 5. 分析

### 5.1 ConcurrentQueue 为何全面碾压

ConcurrentQueue 在所有场景下都领先 1-2 个数量级，原因：

1. **无互斥等待**: CAS 操作失败后立即重试，不存在线程阻塞或自旋等待
2. **预分配内存块**: 内部使用 block-based 分配，避免每次 enqueue 的堆分配
3. **Per-thread token**: ProducerToken 让每个生产者写入独立的 block，消除 false sharing
4. **批量内存管理**: 内部以 block 为单位分配/回收，摊薄分配器开销

### 5.2 Spinlock vs Mutex

在本测试条件下 (8 线程 / 8 核心)，spinlock 全面优于 mutex：

| 场景 | Spinlock 优势 |
|------|--------------|
| SmallItem Push | 快 ~19% |
| SmallItem Pop | 快 ~42% |
| LargeItem Push | 快 ~39% |
| LargeItem Pop | 快 ~44% |

原因分析：
- **临界区短**: push/pop 操作本身很快（memcpy + 指针调整），锁持有时间短
- **线程数 = 核心数**: 每个线程独占一个核心，自旋不会抢占其他线程的 CPU 时间
- **Mutex 的 futex 开销**: 在高竞争下，mutex 频繁进入内核态 (futex wait/wake)，上下文切换成本显著

### 5.3 Spinlock 的适用边界

Spinlock 并非总是更优。以下场景应优先选择 mutex：

| 场景 | 原因 |
|------|------|
| 线程数 >> 核心数 | 自旋线程占用 CPU，阻止持锁线程运行，导致 lock convoy |
| 临界区包含 I/O | 持锁时间不可预测，自旋浪费大量 CPU 周期 |
| 优先级反转风险 | mutex 支持优先级继承协议 (PI)，spinlock 不支持 |
| 需要公平性 | spinlock 无 FIFO 保证，可能导致线程饥饿 |

### 5.4 数据大小的影响

对比 SmallItem (80B) 和 LargeItem (4096B) 的 push median：

| 同步策略 | 80B -> 4096B | 放大倍数 |
|----------|-------------|---------|
| Spinlock | 144 ms -> 1631 ms | 11.3x |
| Mutex | 179 ms -> 2693 ms | 15.0x |
| ConcurrentQueue | 2.98 ms -> 284 ms | 95.3x |

数据变大 51 倍，但耗时增长远超线性。原因是大数据 memcpy 增加了临界区持有时间，加剧了锁竞争。ConcurrentQueue 的放大倍数最高，因为它的基线极低 (2.98 ms)，大数据场景下 memcpy 成为主要瓶颈而非同步开销。

## 6. 结论与建议

| 场景 | 推荐方案 |
|------|---------|
| MPMC 生产者-消费者队列 | ConcurrentQueue (性能领先 1-2 个数量级) |
| 短临界区、线程数 <= 核心数 | Spinlock (比 mutex 快 20-40%) |
| 长临界区、线程数 > 核心数、需要公平性 | std::mutex |
| RTOS 环境、有优先级反转风险 | mutex + 优先级继承 |

对于嵌入式 ARM-Linux 平台，如果业务模型是多线程数据交换，ConcurrentQueue 是首选。如果需要保护共享状态（非队列场景），在核心数充足时优先考虑 spinlock。

## 7. 参考

- [moodycamel::ConcurrentQueue](https://github.com/cameron314/concurrentqueue)
- [C++ atomic_flag](https://en.cppreference.com/w/cpp/atomic/atomic_flag)
- [std::mutex](https://en.cppreference.com/w/cpp/thread/mutex)
- [Futex overview (Linux man page)](https://man7.org/linux/man-pages/man7/futex.7.html)

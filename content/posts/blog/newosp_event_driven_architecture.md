---
title: "newosp 深度解析: C++17 事件驱动架构、层次状态机与零堆分配消息总线"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["newosp", "C++17", "event-driven", "HSM", "lock-free", "MPSC", "SPSC", "zero-copy", "zero-allocation", "state-machine", "ARM-Linux", "embedded", "CAS", "cache-line", "FixedFunction", "SBO", "RAII"]
summary: "newosp 是面向工业级 ARM-Linux 嵌入式平台的 C++17 header-only 基础设施库。本文从架构设计出发，深入剖析 newosp 的四大核心支柱: 无锁 MPSC 消息总线 (CAS 环形缓冲 + 优先级准入)、层次状态机 (LCA 转换 + Guard 条件)、wait-free SPSC 环形缓冲 (编译期双路径 + FakeTSO)、以及 LifecycleNode 16 状态 HSM 生命周期管理。通过与 QP/C 框架的系统性对比，展示 newosp 如何在 C++17 类型系统下实现同等甚至更优的零堆分配、编译期分发和缓存友好设计。"
ShowToc: true
TocOpen: true
---

> newosp GitHub: [DeguiLiu/newosp](https://github.com/DeguiLiu/newosp)
>
> 本文基于 newosp v0.2.0，1114 test cases (26085 assertions)，ASan/TSan/UBSan 全部通过。
>
> 与 QP/C 框架的对比参考: [QPC 框架深度解析](../qpc_active_object_hsm/)

## 1. 为什么需要 newosp

工业级嵌入式系统 (激光雷达、机器人、边缘计算) 面临一组核心矛盾:

- **性能**: 热路径必须零堆分配、零拷贝，微秒级确定性延迟
- **安全**: 并发模块之间需要隔离，不能靠"约定"而要靠"机制"保证线程安全
- **可维护性**: 状态管理不能退化为 if-else 嵌套，需要可建模、可测试的状态机
- **可移植性**: 核心逻辑不能绑死 Linux，未来要能迁移到 RT-Thread 等 RTOS

QP/C (Quantum Platform in C) 用 Active Object + HSM + 零拷贝事件队列 解决了同类问题，但它是纯 C 框架，受限于语言表达力。newosp 在 C++17 类型系统之上重新设计了完整的事件驱动架构，41 个 header-only 模块覆盖从基础设施到应用层的全栈需求。

### 1.1 核心设计原则

| 原则 | 实现手段 | QP/C 对应 |
|------|---------|-----------|
| **零全局状态** | Bus 依赖注入，非全局单例 | QF 全局框架 |
| **栈优先，零堆分配** | FixedFunction/FixedVector/FixedString/ObjectPool | 固定事件池 |
| **无锁/最小锁** | CAS MPSC + wait-free SPSC + SharedSpinLock | 关中断临界区 |
| **编译期分发** | 模板参数化 + `if constexpr` + `std::visit` | 函数指针 + switch |
| **类型安全** | `std::variant` + `expected<V,E>` + NewType | `void*` + 强制转换 |
| **`-fno-exceptions -fno-rtti`** | 全代码库兼容 | 原生 C，无此问题 |

### 1.2 架构全景

```
┌───────────────────────────────────────────────────────────────────────┐
│                        newosp Architecture                            │
│                                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────┐ │
│  │  AsyncBus     │  │  HSM          │  │  SPSC         │  │Lifecycle │ │
│  │  (MPSC)       │  │  层次状态机   │  │  RingBuffer   │  │  Node    │ │
│  │              │  │              │  │              │  │          │ │
│  │  CAS 无锁    │  │  LCA 转换    │  │  Wait-free   │  │  16 状态 │ │
│  │  优先级准入   │  │  Guard 条件  │  │  编译期路径   │  │  HSM 驱动│ │
│  │  批量处理    │  │  冒泡继承    │  │  FakeTSO     │  │  Fault   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────┘ │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  vocabulary.hpp: expected<V,E> | FixedFunction | FixedVector   │  │
│  │                  FixedString | ScopeGuard | NewType             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Executor: Single | Static | Pinned | Realtime (SCHED_FIFO)   │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Platform: ARM-Linux | GCC/Clang | C++17 | -fno-exceptions    │  │
│  └────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

## 2. 无锁 MPSC 消息总线 (AsyncBus)

### 2.1 从 QP/C QEQueue 到 newosp AsyncBus

QP/C 的事件队列 `QEQueue` 是 SPSC 设计 (单生产者单消费者)，通过关中断临界区保护入队操作。这在 RTOS 裸机环境完全合理: ISR 时间窗极短，关中断开销纳秒级。

但在 ARM-Linux 多核环境下，关中断不可行 (用户态无此权限)，且多个线程同时向同一个 Bus 发布消息是常态。newosp 的 AsyncBus 采用 **CAS (Compare-And-Swap) 无锁 MPSC** 设计:

```
Producer 0 ─┐
Producer 1 ──┼── CAS Publish ──> Ring Buffer ──> ProcessBatch() ──> 类型分发
Producer 2 ─┘   (无锁竞争)      (sequence-based)   (批量消费)      (variant + FixedFunction)
```

### 2.2 CAS 环形缓冲: 数据结构

```cpp
template <typename PayloadVariant,
          uint32_t QueueDepth = 4096,    // 必须是 2 的幂
          uint32_t BatchSize = 256>
class AsyncBus;
```

核心数据结构:

```cpp
// 消息头 (32 字节)
struct MessageHeader {
    uint64_t msg_id;        // 全局递增 ID
    uint64_t timestamp_us;  // 微秒时间戳
    uint32_t sender_id;     // 发送者节点 ID
    uint32_t topic_hash;    // FNV-1a 32-bit hash (0 = 无主题)
    MessagePriority priority;  // kLow / kMedium / kHigh
};

// 信封 = 消息头 + 载荷 (variant)
struct MessageEnvelope<PayloadVariant> {
    MessageHeader header;
    PayloadVariant payload;
};

// 环形缓冲节点 (缓存行对齐)
struct alignas(64) RingBufferNode {
    std::atomic<uint32_t> sequence;  // 序号控制
    MessageEnvelope<PayloadVariant> envelope;
};
```

与 QP/C 的关键区别: QP/C 队列存储的是**事件指针** (`QEvt const *`)，newosp 存储的是**完整信封** (header + variant payload)。QP/C 依赖引用计数实现零拷贝事件共享; newosp 在同一进程内通过 variant 值语义避免了指针管理和引用计数的复杂性。

### 2.3 CAS 发布: 无锁生产者竞争

```cpp
bool Publish(PayloadVariant&& payload, MessagePriority priority = kMedium) {
    // 1. 优先级准入控制 (背压)
    uint32_t depth = producer_pos_.load(relaxed) - cached_consumer_pos_;
    if (depth >= AdmissionThreshold(priority)) {
        // 缓存的消费位置可能已过时，尝试刷新
        cached_consumer_pos_ = consumer_pos_.load(acquire);
        depth = producer_pos_.load(relaxed) - cached_consumer_pos_;
        if (depth >= AdmissionThreshold(priority)) {
            return false;  // 队列已满，丢弃该优先级消息
        }
    }

    // 2. CAS 循环抢占生产者位置
    uint32_t prod_pos;
    RingBufferNode* target;
    do {
        prod_pos = producer_pos_.load(relaxed);
        target = &ring_buffer_[prod_pos & kBufferMask];

        uint32_t seq = target->sequence.load(acquire);
        if (seq != prod_pos) {
            return false;  // 该槽位尚未被消费者释放
        }
    } while (!producer_pos_.compare_exchange_weak(
        prod_pos, prod_pos + 1,
        std::memory_order_acq_rel,    // CAS 成功: Acquire + Release
        std::memory_order_relaxed));  // CAS 失败: Relaxed (立即重试)

    // 3. 填充数据
    target->envelope.header = MakeHeader(priority);
    target->envelope.payload = std::move(payload);

    // 4. 发布: Release 语义确保数据对消费者可见
    target->sequence.store(prod_pos + 1, std::memory_order_release);
    return true;
}
```

**内存序策略解析**:

| 操作 | memory_order | 原因 |
|------|-------------|------|
| 读 `producer_pos_` | `relaxed` | 只是预判，CAS 才是权威 |
| 读 `sequence` | `acquire` | 需要看到消费者对该槽位的释放 |
| CAS 成功 | `acq_rel` | Acquire: 读取最新状态; Release: 对其他生产者可见 |
| CAS 失败 | `relaxed` | 立即重试，无需同步 |
| 写 `sequence` (发布) | `release` | 确保 payload 写入对消费者可见 |

### 2.4 优先级准入控制

QP/C 的 `QActive_post_()` 有 `margin` 参数控制溢出策略，但粒度仅为"通过/拒绝"。newosp 实现了三级优先级准入:

```
队列深度
│
│  ████████████████████████████████████  100%
│  ██████████████████████████████████    99%  ← kHigh 阈值
│  ████████████████████████████          80%  ← kMedium 阈值
│  ██████████████████                    60%  ← kLow 阈值
│
│  当队列压力增大时:
│  1. 先丢弃 kLow (遥测、统计)
│  2. 再丢弃 kMedium (常规数据)
│  3. kHigh (控制命令) 几乎不丢弃
```

**缓存消费位置优化**: 生产者先检查 `cached_consumer_pos_` (relaxed 读，无 cache line 竞争)，只有接近阈值时才重新读取真正的 `consumer_pos_` (acquire 读)。这一优化在低负载时完全避免了对消费者原子变量的争用。

### 2.5 批量消费与编译期分发

消费侧是单线程的，这与 QP/C 的 AO 事件循环完全对应: 一个 AO 从自己的队列中取事件，不存在消费侧竞争。

newosp 提供两种分发模式:

**回调模式** (`ProcessBatch`):

```cpp
uint32_t ProcessBatch() {
    for (uint32_t i = 0; i < kBatchSize; ++i) {
        auto* node = &ring_buffer_[consumer_pos_ & kBufferMask];
        uint32_t seq = node->sequence.load(acquire);
        if (seq != consumer_pos_ + 1) break;  // 无更多消息

        // 预取下一个槽位 (减少 cache miss)
        if (i + 1 < kBatchSize) {
            __builtin_prefetch(&ring_buffer_[(consumer_pos_ + 1) & kBufferMask], 0, 1);
        }

        // 通过回调表分发
        DispatchToCallbacks(node->envelope);

        // 释放槽位: 设置 sequence 为下一轮的生产位置
        node->sequence.store(consumer_pos_ + kQueueDepth, release);
        ++consumer_pos_;
    }
    return processed;
}
```

**编译期访问者模式** (`ProcessBatchWith<Visitor>`):

```cpp
template <typename Visitor>
uint32_t ProcessBatchWith(Visitor& visitor) {
    // 与上述逻辑相同，但分发部分替换为:
    std::visit([&](auto& payload) {
        visitor(payload, envelope.header);
    }, envelope.payload);
    // 编译器为每个 variant alternative 生成直接跳转表
    // 无 SharedSpinLock、无回调表遍历、无 FixedFunction 间接调用
}
```

**性能对比** (1000 条 SmallMsg, P50):

| 模式 | ns/msg | 加速比 |
|------|-------:|-------:|
| 直接分发 (ProcessBatchWith) | ~2 | **15x** |
| 回调 (ProcessBatch + FixedFunction) | ~30 | 1x |

### 2.6 零堆分配回调: FixedFunction SBO

QP/C 不需要回调机制 (AO 的状态机直接处理事件)。newosp 需要支持运行时动态订阅，但不能引入 `std::function` 的堆分配:

```cpp
// SBO (Small Buffer Optimization) 固定函数
template <typename Signature, size_t BufferSize = 2 * sizeof(void*)>
class FixedFunction;

// AsyncBus 订阅回调: 32 字节 SBO 缓冲
static constexpr size_t kCallbackBufSize = 4 * sizeof(void*);  // 32B (64-bit)
using CallbackType = FixedFunction<void(const EnvelopeType&), kCallbackBufSize>;
```

核心实现:

```cpp
template <typename Ret, typename... Args, size_t BufferSize>
class FixedFunction<Ret(Args...), BufferSize> {
    using Storage = typename std::aligned_storage<BufferSize, alignof(void*)>::type;
    using Invoker = Ret(*)(const Storage&, Args...);
    using Destroyer = void(*)(Storage&);

    Storage storage_;
    Invoker invoker_ = nullptr;
    Destroyer destroyer_ = nullptr;

    template <typename Callable>
    FixedFunction(Callable&& fn) {
        using Decay = std::decay_t<Callable>;
        // 编译期拒绝超限捕获
        static_assert(sizeof(Decay) <= BufferSize,
                      "Callable too large for FixedFunction buffer");
        ::new (&storage_) Decay(std::forward<Callable>(fn));
        invoker_ = &InvokeImpl<Decay>;
        destroyer_ = &DestroyImpl<Decay>;
    }
};
```

与 `std::function` 的关键区别:

| 特性 | `std::function` | `FixedFunction` |
|------|:---:|:---:|
| 大 lambda 处理 | 堆分配 | **编译期拒绝** |
| 小 lambda 处理 | SBO (实现依赖) | **SBO (保证)** |
| 拷贝语义 | 可拷贝 | Move-only |
| 类型擦除大小 | ~32-48B (实现依赖) | 精确 `BufferSize + 16B` |
| 热路径堆分配 | 可能 | **不可能** |

### 2.7 与 QP/C QEQueue 的系统性对比

| 维度 | QP/C QEQueue | newosp AsyncBus |
|------|:---:|:---:|
| 并发模型 | SPSC (关中断保护) | **MPSC (CAS 无锁)** |
| 存储内容 | 事件指针 (4/8B) | 完整信封 (header + variant) |
| 入队复杂度 | O(1) 指针写 | O(1) CAS + 数据拷贝 |
| 出队复杂度 | O(1) 指针读 | O(1) sequence 检查 |
| 优先级控制 | 无 (margin 参数) | **三级准入 (60/80/99%)** |
| 批量处理 | 无 | **BatchSize 可配置 + prefetch** |
| 分发方式 | 状态机 switch | **variant visit / 回调表** |
| 编译期优化 | 无 | **ProcessBatchWith 零间接** |
| 多实例 | 每 AO 一个队列 | 支持多 Bus 实例隔离 |
| QP/C frontEvt 优化 | 有 (空队列快速路径) | 无 (sequence 统一路径) |

**设计取舍**: QP/C 的 `frontEvt` 优化在低负载时 (队列空) 完全跳过环形缓冲操作，这在嵌入式系统常态下极具价值。newosp 使用 sequence number 统一入队路径，牺牲了低负载快速路径，但换来了 MPSC 并发支持和更高吞吐量上限。

## 3. 层次状态机 (HSM)

### 3.1 从平面 FSM 到层次 HSM: 相同的问题，不同的解法

newosp 和 QP/C 对 HSM 的需求完全一致: 解决平面状态机的**状态爆炸**问题。但实现路径截然不同。

QP/C 的 HSM 以 `QMState` 结构体为核心，状态处理函数返回 `Q_HANDLED()` 或 `Q_SUPER()` 来实现冒泡:

```c
// QP/C: 子状态通过 Q_SUPER 委托给父状态
static QState Child_state(MyAO *me, QEvt const *e) {
    switch (e->sig) {
    case SPECIFIC_SIG:
        handle_specific();
        return Q_HANDLED();
    }
    return Q_SUPER(&Parent_state);  // 冒泡
}
```

newosp 的 HSM 以模板化 `StateMachine<Context, MaxStates>` 为核心，状态信息存储在固定大小数组中:

```cpp
// newosp: 状态配置 (编译期绑定)
template <typename Context>
struct StateConfig {
    const char* name;              // 调试用名称 (静态生命周期)
    int32_t parent_index;          // 父状态索引 (-1 = 根)
    TransitionResult (*handler)(Context&, Event&);  // 处理函数
    void (*on_entry)(Context&);    // Entry 动作
    void (*on_exit)(Context&);     // Exit 动作
    bool (*guard)(const Context&, Event&);  // Guard 条件
};

// 三值返回
enum class TransitionResult : uint8_t {
    kHandled,     // 事件已处理
    kUnhandled,   // 冒泡到父状态
    kTransition   // 请求状态转换
};
```

### 3.2 LCA 算法: 手工路径 vs 编译期计算

QP/C 提供两种 HSM 变体:

- **QHsm**: 运行时沿 superstate 链递归查找 LCA
- **QMsm**: QM 工具在编译期预计算 LCA，生成静态转换动作表

newosp 的 LCA 算法是运行时计算，但基于**深度归一化**的高效实现:

```cpp
void ExecuteTransition(int32_t source_idx, int32_t target_idx) {
    // Step 1: 计算源和目标的深度
    int32_t src_depth = ComputeDepth(source_idx);
    int32_t tgt_depth = ComputeDepth(target_idx);

    int32_t s = source_idx;
    int32_t t = target_idx;

    // Step 2: 归一化深度 (将较深的状态上移)
    while (src_depth > tgt_depth) {
        s = states_[s].parent_index;
        --src_depth;
    }
    while (tgt_depth > src_depth) {
        t = states_[t].parent_index;
        --tgt_depth;
    }

    // Step 3: 同步上移直到找到公共祖先
    while (s != t) {
        s = states_[s].parent_index;
        t = states_[t].parent_index;
    }
    int32_t lca = s;  // LCA 找到

    // Step 4: 执行 Exit 路径 (source -> LCA)
    int32_t exit_path[OSP_HSM_MAX_DEPTH];
    int32_t exit_count = BuildExitPath(source_idx, lca, exit_path);
    for (int32_t i = 0; i < exit_count; ++i) {
        if (states_[exit_path[i]].on_exit) {
            states_[exit_path[i]].on_exit(context_);
        }
    }

    // Step 5: 执行 Entry 路径 (LCA -> target，需要反转)
    int32_t entry_path[OSP_HSM_MAX_DEPTH];
    int32_t entry_count = BuildEntryPath(target_idx, lca, entry_path);
    // entry_path 是 bottom-up，需要 top-down 执行
    for (int32_t i = entry_count - 1; i >= 0; --i) {
        if (states_[entry_path[i]].on_entry) {
            states_[entry_path[i]].on_entry(context_);
        }
    }

    current_state_ = target_idx;
}
```

**关键设计决策**: Exit/Entry 路径使用栈上固定数组 (`int32_t path[OSP_HSM_MAX_DEPTH]`, 32 层深度上限)，整个转换过程零堆分配。

### 3.3 Guard 条件: newosp 的扩展

QP/C 的 HSM 没有原生 Guard 条件 (需要在状态处理函数内手动实现)。newosp 在状态配置中内置 Guard:

```cpp
// Guard 条件在事件派发时自动检查
TransitionResult Dispatch(Event& event) {
    int32_t state = current_state_;

    while (state >= 0) {
        auto& cfg = states_[state];

        // Guard 检查: 如果 guard 返回 false，跳过该状态
        if (cfg.guard != nullptr && !cfg.guard(context_, event)) {
            state = cfg.parent_index;  // 继续冒泡
            continue;
        }

        TransitionResult result = cfg.handler(context_, event);

        if (result == TransitionResult::kHandled) {
            return result;
        }
        if (result == TransitionResult::kTransition) {
            ExecuteTransition(current_state_, pending_target_);
            return result;
        }
        // kUnhandled: 冒泡到父状态
        state = cfg.parent_index;
    }

    return TransitionResult::kUnhandled;  // 到达根状态仍未处理
}
```

### 3.4 事件冒泡-继承-覆盖: 思想一致，表达不同

newosp 和 QP/C 的冒泡机制在概念上完全一致:

```
事件到达当前状态
  │
  ▼
当前状态 handler 处理?
  ├─ kHandled  → 完成
  ├─ kTransition → 执行 LCA 转换 → 完成
  └─ kUnhandled → 冒泡到 parent_index → 重复
```

但 C++ 的类型系统带来了更强的安全性:

```cpp
// newosp: Event 携带类型化数据
struct Event {
    uint32_t id;
    const void* data;  // 与 variant 配合使用时可以静态转换
};

// QP/C: 需要手动强制转换
SensorEvt const *se = (SensorEvt const *)e;  // 无编译期检查
```

### 3.5 零堆分配保证

| 组件 | QP/C | newosp |
|------|------|--------|
| 状态存储 | `QMState` 静态数组 | `std::array<StateConfig, MaxStates>` |
| 转换路径 | 编译期表 (QMsm) / 递归栈 (QHsm) | 栈上 `int32_t path[32]` |
| 事件 | 固定事件池 `Q_NEW()` | 值语义 Event (栈分配) |
| Handler | 函数指针 | 函数指针 (无 FixedFunction/std::function) |

两个框架在 HSM 实现上都达到了**零堆分配**目标，但路径不同: QP/C 靠事件池 + 引用计数管理事件生命周期; newosp 靠 C++ 值语义和 RAII 自动管理。

### 3.6 newosp vs QP/C HSM 对比总结

| 维度 | QP/C QHsm/QMsm | newosp StateMachine |
|------|:---:|:---:|
| 语言 | C | C++17 |
| 状态容量 | 无限制 (链表) | 模板参数 MaxStates (默认 16) |
| LCA 计算 | 运行时递归 (QHsm) / 编译期表 (QMsm) | 运行时深度归一化 |
| Guard 条件 | 手动实现 | **内置 `guard` 函数指针** |
| Entry/Exit | 函数指针 | 函数指针 |
| 冒泡机制 | `Q_SUPER()` 返回值 | `kUnhandled` + parent_index |
| 双实现策略 | QHsm (手工) / QMsm (代码生成) | 单一实现 (运行时 LCA) |
| 代码生成支持 | QM 图形化工具 | ospgen YAML 工具 (消息/拓扑) |
| 线程安全 | 单线程 Dispatch (RTC) | 单线程 Dispatch |
| 内存开销 | 取决于状态数 | ~500B (16 状态) |

## 4. Wait-free SPSC 环形缓冲

### 4.1 QP/C 与 newosp 的 SPSC 设计

QP/C 的 `QEQueue` 本质上也是一个 SPSC 队列 (一个生产者向一个 AO 投递事件)。newosp 的 `SpscRingbuffer` 则是一个通用的、类型化的 SPSC 组件，在多个模块中复用:

| 集成点 | 元素类型 | 容量 |
|--------|---------|------|
| WorkerPool 工作线程 | `MessageEnvelope<PayloadVariant>` | 1024 |
| 串口字节缓冲 | `uint8_t` | 4096 |
| 网络帧缓冲 | `RecvFrameSlot` | 32 |
| 统计通道 | `ShmStats` (48B) | 16 |

### 4.2 模板参数化与编译期双路径

```cpp
template <typename T,
          size_t BufferSize,         // 必须是 2 的幂
          bool FakeTSO = false,      // 单核 relaxed ordering
          typename IndexT = size_t>
class SpscRingbuffer {
    static constexpr bool kTriviallyCopyable =
        std::is_trivially_copyable<T>::value;

    // 编译期选择拷贝策略
    bool PushBatch(const T* buf, size_t count) {
        if constexpr (kTriviallyCopyable) {
            // POD 类型: memcpy 批量拷贝 (可能分两段处理 wrap-around)
            const size_t first_part = std::min(count, BufferSize - head_offset);
            std::memcpy(&data_buff_[head_offset], buf, first_part * sizeof(T));
            if (count > first_part) {
                std::memcpy(&data_buff_[0], buf + first_part,
                           (count - first_part) * sizeof(T));
            }
        } else {
            // 非 POD 类型: 逐元素 move
            for (size_t i = 0; i < count; ++i) {
                data_buff_[(head + i) & kMask] = std::move(buf[i]);
            }
        }
        head_.value.store(head + count, ReleaseOrder());
        return true;
    }
};
```

这是 QP/C 无法实现的: C 语言没有 `if constexpr`，不能在编译期根据类型属性选择不同的代码路径。

### 4.3 FakeTSO: 单核 MCU 优化

newosp 为未来移植到 RT-Thread 单核 MCU 准备了 `FakeTSO` 模式:

```cpp
// 内存序选择 (编译期)
static constexpr std::memory_order AcquireOrder() {
    return FakeTSO ? std::memory_order_relaxed : std::memory_order_acquire;
}
static constexpr std::memory_order ReleaseOrder() {
    return FakeTSO ? std::memory_order_relaxed : std::memory_order_release;
}
```

| FakeTSO | Acquire | Release | 适用场景 |
|---------|---------|---------|----------|
| false | `acquire` | `release` | 多核 ARM-Linux / RISC-V |
| true | `relaxed` | `relaxed` | 单核 MCU (RT-Thread) / x86 TSO |

**原理**: 单核 MCU 上，ISR 和线程之间的内存可见性由 `ISB` (指令同步屏障) 隐式保证，不需要硬件内存屏障。将 acquire/release 降级为 relaxed 可以节省 ARM 上 `DMB` 指令的开销。

QP/C 在 RTOS 上使用关中断达到相同效果: 关中断本身就是一个隐式的全屏障。两种方案在单核场景下性能等价。

### 4.4 缓存行对齐: 消除 False Sharing

```cpp
static constexpr size_t kCacheLineSize = 64;

struct alignas(kCacheLineSize) PaddedIndex {
    std::atomic<IndexT> value{0};
};

PaddedIndex head_;  // 生产者独占写入
// --- 64 字节边界 ---
PaddedIndex tail_;  // 消费者独占写入
```

QP/C 的 QEQueue 没有缓存行对齐 (RTOS 环境下 L1 cache 通常较小或无 D-Cache)。newosp 针对 ARM Cortex-A 的 64 字节缓存行做了显式分离，避免生产者和消费者因缓存行乒乓 (false sharing) 导致性能退化。

### 4.5 零拷贝查看: Peek 与 At

QP/C 的 `frontEvt` 快速路径允许直接读取最新事件而不出队。newosp 提供了更通用的零拷贝查看:

```cpp
// Peek: 查看队首元素 (不出队)
const T* Peek() const {
    IndexT head = head_.value.load(AcquireOrder());
    IndexT tail = tail_.value.load(std::memory_order_relaxed);
    if (head == tail) return nullptr;
    return &data_buff_[tail & kMask];
}

// At: 随机访问第 n 个元素
const T* At(size_t n) const;

// Discard: 跳过 n 个元素 (不拷贝)
size_t Discard(size_t count);
```

### 4.6 PushFromCallback: 延迟计算优化

```cpp
// 仅在队列有空间时才执行 callback 计算
template <typename Callable>
bool PushFromCallback(Callable&& callback) {
    if (IsFull()) return false;  // 避免执行昂贵的 callback
    data_buff_[head & kMask] = callback();  // 延迟计算
    head_.value.store(head + 1, ReleaseOrder());
    return true;
}
```

这一模式在传感器数据采集场景极为实用: 如果下游消费者跟不上，直接跳过 ADC 读取，避免无意义的硬件操作。

## 5. LifecycleNode: HSM 驱动的生命周期管理

### 5.1 从 QP/C QActive 到 newosp LifecycleNode

QP/C 的 `QActive` 生命周期是线性的: `ctor → start → 事件循环 (永不退出)`。状态管理完全交给用户定义的 HSM。

newosp 的 `LifecycleNode` 则内置了一个 **16 状态的层次状态机**，借鉴 ROS2 Lifecycle Node 的设计，将节点生命周期本身建模为 HSM:

```
Alive (根状态)
├── Unconfigured
│   ├── Initializing     ← 加载默认配置
│   └── WaitingConfig    ← 等待 Configure 触发
├── Configured
│   ├── Inactive
│   │   ├── Standby      ← 就绪，等待 Activate
│   │   └── Paused       ← 曾经 Active，可 Resume
│   └── Active
│       ├── Starting     ← 过渡态，执行 on_activate
│       ├── Running      ← 正常运行
│       └── Degraded     ← 运行中降级 (告警/部分功能失效)
├── Error
│   ├── Recoverable      ← 可恢复，重试 Configure
│   └── Fatal            ← 不可恢复，必须 Shutdown
└── Finalized (终态，独立根)
```

### 5.2 HSM 实例的零堆分配构造

LifecycleNode 内部使用 placement new 在预分配的对齐存储中构造 HSM:

```cpp
template <typename PayloadVariant>
class LifecycleNode : public Node<PayloadVariant> {
private:
    LifecycleHsmContext ctx_;
    bool hsm_initialized_;
    // 对齐存储: 避免堆分配
    alignas(HsmType) uint8_t hsm_storage_[sizeof(HsmType)];

    void InitHsm() noexcept {
        auto* hsm = new (hsm_storage_) HsmType(ctx_);
        hsm_initialized_ = true;
        ctx_.sm = hsm;

        // 注册 16 个状态 (全部编译期确定)
        ctx_.idx[DS::kAlive] =
            hsm->AddState({"Alive", -1, HandleAlive, nullptr, nullptr, nullptr});

        int32_t alive = ctx_.I(DS::kAlive);
        ctx_.idx[DS::kUnconfigured] =
            hsm->AddState({"Unconfigured", alive, HandleNoop, nullptr, nullptr, nullptr});
        // ... 注册全部 16 个状态 ...

        hsm->SetInitialState(ctx_.I(DS::kWaitingConfig));
        hsm->Start();
    }

    HsmType* GetHsm() noexcept {
        return reinterpret_cast<HsmType*>(hsm_storage_);
    }
};
```

### 5.3 粗粒度与细粒度状态映射

为了向后兼容简单场景，LifecycleNode 提供双层状态视图:

```cpp
// 粗粒度 (4 状态，向后兼容)
enum class LifecycleState : uint8_t {
    kUnconfigured,  // Unconfigured/Initializing/WaitingConfig
    kInactive,      // Standby/Paused
    kActive,        // Starting/Running/Degraded
    kFinalized      // 终态
};

// 细粒度 (16 状态，精确控制)
enum class LifecycleDetailedState : uint8_t {
    kAlive, kUnconfigured, kInitializing, kWaitingConfig,
    kConfigured, kInactive, kStandby, kPaused,
    kActive, kStarting, kRunning, kDegraded,
    kError, kRecoverable, kFatal, kFinalized
};
```

### 5.4 内置故障上报

与 QP/C 不同，newosp 的 LifecycleNode 内置了 FaultReporter 注入点:

```cpp
// 16 字节 POD 结构 (零开销注入)
struct FaultReporter {
    void (*fn)(uint16_t fault_index, uint32_t detail,
               FaultPriority priority, void* ctx) = nullptr;
    void* ctx = nullptr;

    void Report(uint16_t fault_index, uint32_t detail,
                FaultPriority priority) const noexcept {
        if (fn != nullptr) { fn(fault_index, detail, priority, ctx); }
    }
};
```

HSM 状态处理函数中自动上报:

```cpp
inline TransitionResult HandleWaitingConfig(Ctx& ctx, const Event& event) {
    if (event.id == kLcEvtConfigure) {
        if (ctx.on_configure != nullptr && !ctx.on_configure()) {
            ctx.transition_failed = true;
            // 自动上报故障
            ctx.fault_reporter.Report(kFaultConfigureFailed, 0,
                                      FaultPriority::kHigh);
            return TransitionResult::kHandled;  // 留在当前状态
        }
        return ctx.sm->RequestTransition(ctx.I(DS::kStandby));
    }
    return TransitionResult::kUnhandled;
}
```

`fn == nullptr` 时 `Report()` 为空操作，零运行时开销。这比 QP/C 中手动在每个状态函数里加 assert 或日志更系统化。

### 5.5 与 QP/C QActive 生命周期对比

| 维度 | QP/C QActive | newosp LifecycleNode |
|------|:---:|:---:|
| 生命周期模型 | `ctor → start → 永不退出` | **16 状态 HSM** |
| 状态管理 | 用户自定义 HSM | **内置标准生命周期 + 用户 HSM** |
| Configure/Activate | 无 | **标准转换 API** |
| Degraded 降级 | 无 | **Running → Degraded 子状态** |
| 错误恢复 | 用户自行处理 | **Recoverable/Fatal 分层** |
| 故障上报 | 无 | **内置 FaultReporter** |
| 线程映射 | 每 AO 一个 RTOS 线程 | Node + Executor 解耦 |

## 6. 调度与实时性

### 6.1 QP/C 的调度模型

QP/C 在 RT-Thread 上的调度模型: 每个 QActive 对应一个 RT-Thread 线程，`qf_thread_function()` 是永不退出的事件循环:

```c
// QP/C: 每个 AO 的事件循环
static void qf_thread_function(void *arg) {
    QActive *me = (QActive *)arg;
    for (;;) {
        QEvt const *e = QActive_get_(me);   // 阻塞等待
        QHSM_DISPATCH(&me->super, e);       // 派发给 HSM
        QF_gc(e);                            // 回收事件
    }
}
```

### 6.2 newosp 的多层级 Executor

newosp 提供四种执行模型，通过模板参数在编译期选择:

```cpp
// 1. 单线程轮询 (最简单，调试用)
osp::SingleThreadExecutor<Payload> exec;
exec.Spin();  // 阻塞在调用线程

// 2. 后台线程 + 休眠策略 (通用)
osp::StaticExecutor<Payload, osp::YieldSleepStrategy> exec;
exec.Start();  // 创建后台线程

// 3. CPU 绑核 (确定性调度)
osp::PinnedExecutor<Payload, osp::PreciseSleepStrategy> exec(/*cpu_core=*/2);
exec.Start();

// 4. 实时调度 (工业级)
osp::RealtimeConfig cfg;
cfg.sched_policy = SCHED_FIFO;
cfg.sched_priority = 80;
cfg.lock_memory = true;     // mlockall(MCL_CURRENT | MCL_FUTURE)
cfg.cpu_affinity = 3;       // 绑定到 CPU 3
cfg.stack_size = 65536;     // 64KB 自定义栈

osp::RealtimeExecutor<Payload, osp::PreciseSleepStrategy> exec(cfg);
exec.Start();
```

**RealtimeExecutor 初始化序列**:

```
线程启动
  │
  ├── 1. mlockall()        ← 锁定所有内存页，防止 page fault
  ├── 2. CPU affinity      ← pthread_setaffinity_np()
  ├── 3. SCHED_FIFO        ← pthread_setschedparam()
  ├── 4. 自定义栈大小       ← pthread_attr_setstacksize()
  └── 5. 进入 ProcessBatch 循环
```

### 6.3 PreciseSleepStrategy: 高精度休眠

QP/C 在 RT-Thread 上通过 `QTimeEvt` (SysTick 驱动) 实现周期性唤醒。newosp 使用 `clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME)` 实现亚毫秒级精确休眠:

```cpp
struct PreciseSleepStrategy {
    uint64_t default_sleep_ns_;  // 默认休眠时长
    uint64_t min_sleep_ns_;      // 最小休眠 (避免忙等)
    uint64_t max_sleep_ns_;      // 最大休眠 (避免过长)
    uint64_t next_wakeup_ns_ = 0;

    void OnIdle() {
        uint64_t now = SteadyNowNs();
        uint64_t sleep_ns = (next_wakeup_ns_ > now)
            ? next_wakeup_ns_ - now
            : default_sleep_ns_;

        // 限幅
        sleep_ns = std::clamp(sleep_ns, min_sleep_ns_, max_sleep_ns_);

        struct timespec ts;
        ts.tv_sec  = static_cast<time_t>((now + sleep_ns) / 1000000000ULL);
        ts.tv_nsec = static_cast<long>((now + sleep_ns) % 1000000000ULL);
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr);
    }

    void SetNextWakeup(uint64_t abs_ns) { next_wakeup_ns_ = abs_ns; }
};
```

**与 QP/C QTimeEvt 的对比**:

| 维度 | QP/C QTimeEvt | newosp PreciseSleepStrategy |
|------|:---:|:---:|
| 时基 | SysTick (通常 1kHz) | CLOCK_MONOTONIC (纳秒级) |
| 精度 | 1 tick (1ms @ 1kHz) | 亚毫秒 (受内核调度影响) |
| 驱动方式 | ISR 定时器中断 | clock_nanosleep 绝对时间 |
| 周期性任务 | `QTimeEvt_armX(1U, 1U)` | `SetNextWakeup(now + period)` |
| 适用平台 | RTOS (裸机/FreeRTOS/RT-Thread) | Linux (需要 CLOCK_MONOTONIC) |

## 7. 跨模块集成: 从单点到系统

### 7.1 数据流全景

newosp 的核心数据流与 QP/C 的 AO 通信模型在概念上等价，但实现更为灵活:

```
                    newosp 数据流
                    ============

传感器线程 ─┐
控制线程  ──┼── AsyncBus::Publish() ─── [CAS MPSC Ring Buffer] ───┐
网络线程  ─┘   (无锁竞争)              (4096 slots, 缓存行对齐)      │
                                                                     │
                              ┌──── ProcessBatch() 单消费者 ─────────┘
                              │
                              ▼
                    Node 类型路由 (FNV-1a topic hash)
                              │
                    ┌─────────┼─────────┐
                    │         │         │
                    ▼         ▼         ▼
              StaticNode   Node     WorkerPool
              (零开销)   (动态订阅)  (SPSC 分发)
                    │         │         │
                    ▼         ▼         ▼
                HSM/BT    回调处理    并行计算
```

**与 QP/C 的对应关系**:

```
QP/C                              newosp
====                              ======
QActive (AO 对象)          ←→     Node / StaticNode / LifecycleNode
QEQueue (事件队列)          ←→     AsyncBus (MPSC) + SpscRingbuffer (SPSC)
QHsm_dispatch()            ←→     StateMachine::Dispatch()
QF_publish()               ←→     Node::Publish() / AsyncBus::Publish()
QF_TICK_X()                ←→     TimerScheduler + PreciseSleepStrategy
QActive on RT-Thread       ←→     RealtimeExecutor (SCHED_FIFO + mlockall)
Q_NEW() / QF_gc()          ←→     variant 值语义 (无需引用计数)
```

### 7.2 Node 双模式: 动态订阅 vs 编译期绑定

```cpp
// 模式 1: Node (运行时动态订阅)
osp::Node<Payload> sensor("sensor", 1, bus);
sensor.Subscribe<SensorData>([](const SensorData& d, const osp::MessageHeader& h) {
    process(d.temp);
});
sensor.Subscribe<SensorData>("imu/temperature", on_imu_temp);
sensor.SpinOnce();

// 模式 2: StaticNode (编译期绑定, 15x 快于 Node)
struct StreamHandler {
    ProtocolState* state;
    void operator()(const StreamData& sd, const osp::MessageHeader&) {
        ++state->stream_count;  // 编译器可完全内联
    }
    template <typename T>
    void operator()(const T&, const osp::MessageHeader&) {}  // catch-all: 零开销
};

osp::StaticNode<Payload, StreamHandler> ctrl("ctrl", 3, StreamHandler{&state});
ctrl.SpinOnce();  // ProcessBatchWith: 编译期直接跳转，无回调表
```

### 7.3 WorkerPool: 二级排队架构

```
Submit(job) ──> AsyncBus::Publish() [MPSC, 无锁]
                       │
               DispatcherThread (ProcessBatch, round-robin 分发)
                       │
            ┌──────────┼──────────┐
            ▼          ▼          ▼
      Worker[0]    Worker[1]  Worker[N-1]
      SpscRingbuffer (wait-free, 1024 深度)
      └── 独立线程，CPU 亲和性可配置
```

这一设计将无锁 MPSC 入口和 wait-free SPSC 工作分发组合起来，前者承受多生产者并发写入，后者保证工作线程零竞争。

### 7.4 可靠性闭环: Watchdog + FaultCollector

```
ThreadWatchdog                          FaultCollector
(检测问题)                              (处理问题)
    │                                       │
    │ timeout 回调                          │ Hook 决策
    ├──────────────> ReportFault ───────────>│
    │                                       │ kHandled / kEscalate
    │                                       │ kDefer / kShutdown
    │       Beat()                          │
    │<──── ThreadHeartbeat <────── 消费者线程│
    │  (也被 Watchdog 监控)                 │
```

核心原则: Watchdog 和 FaultCollector 职责正交。Watchdog 只发现问题 (线程挂死)，FaultCollector 只处理问题 (分级上报 + hook 决策)。两者通过应用层 wiring 组合，没有内部依赖。

## 8. 性能与资源预算

### 8.1 内存占用

| 模块 | 典型配置 | 内存 |
|------|---------|------|
| AsyncBus<Payload, 4096, 256> | 高吞吐 | ~320 KB |
| AsyncBus<Payload, 256, 64> | 嵌入式低内存 | ~20 KB |
| StateMachine<Ctx, 16> | 16 状态 HSM | ~500 B |
| SpscRingbuffer<uint8_t, 4096> | 串口字节缓冲 | ~4 KB |
| LifecycleNode (含 HSM) | 16 状态生命周期 | ~2 KB |
| ThreadWatchdog<32> | 32 线程监控 | ~2 KB |
| FaultCollector<64, 256> | 默认配置 | ~36 KB |

### 8.2 热路径性能

| 操作 | 延迟 | 堆分配 |
|------|------|--------|
| AsyncBus::Publish (CAS 成功) | < 100 ns | 0 |
| ProcessBatchWith (编译期分发) | ~2 ns/msg | 0 |
| HSM Dispatch (3 层冒泡) | < 200 ns | 0 |
| HSM Transition (LCA 2 层) | < 500 ns | 0 |
| SPSC Push/Pop (单元素) | < 50 ns | 0 |
| SPSC PushBatch (memcpy 路径) | 批量吞吐优 | 0 |

### 8.3 测试覆盖

- 正常模式: 1114 test cases (26085 assertions)
- `-fno-exceptions` 模式: 393 test cases
- Sanitizer: ASan + UBSan + TSan 全部通过
- 每个模块独立测试文件，集成测试验证跨模块交互

## 9. 方案对比总结

| 维度 | QP/C (Active Object on RTOS) | newosp (C++17 on ARM-Linux) |
|------|:---:|:---:|
| **语言** | C11 | C++17 |
| **目标平台** | MCU (FreeRTOS/RT-Thread/裸机) | ARM-Linux (工业嵌入式) |
| **并发模型** | AO + 关中断临界区 | MPSC CAS + SPSC wait-free |
| **状态机** | QHsm/QMsm (两种变体) | StateMachine + LifecycleNode |
| **事件队列** | QEQueue (SPSC + frontEvt) | AsyncBus (MPSC + 优先级准入) |
| **零拷贝机制** | 事件指针 + 引用计数 | variant 值语义 + FixedFunction SBO |
| **动态分配** | 固定事件池 `Q_NEW/QF_gc` | ObjectPool + FixedVector + 栈优先 |
| **编译期优化** | QMsm 转换表 (QM 工具生成) | `if constexpr` + `std::visit` + 模板参数化 |
| **类型安全** | `void*` 手动转换 | `std::variant` + `expected<V,E>` + NewType |
| **调度** | RTOS 线程 / QXK 内核 | Single/Static/Pinned/Realtime Executor |
| **生命周期** | ctor → start → 永不退出 | 16 状态 HSM (Configure/Activate/Degraded/...) |
| **故障处理** | 用户自定义 | 内置 FaultReporter + FaultCollector |
| **测试** | 用户自行编写 | 1114 tests, ASan/TSan/UBSan |
| **代码风格** | 函数指针 + switch/case | Header-only + RAII + 模板 |

## 10. 最佳实践

### 10.1 模块选择指南

| 场景 | 推荐方案 |
|------|---------|
| 简单传感器数据流 | Node + AsyncBus + SingleThreadExecutor |
| 高吞吐多生产者 | StaticNode + AsyncBus<PV, 4096, 256> + PinnedExecutor |
| 复杂状态管理 | StateMachine (手动) 或 LifecycleNode (标准生命周期) |
| 实时控制 | RealtimeExecutor (SCHED_FIFO) + PreciseSleepStrategy |
| 跨进程 IPC | ShmChannel + ShmRingBuffer (零拷贝共享内存) |
| 工业串口通信 | SerialTransport + SpscRingbuffer + CRC16 |

### 10.2 性能调优

- **Bus 队列深度**: 根据峰值突发估算。低频控制用 256，高频传感器用 4096
- **BatchSize**: 与生产频率匹配。批量越大吞吐越高，但单次延迟增加
- **StaticNode vs Node**: 编译期确定的处理逻辑用 StaticNode (15x 性能提升)
- **FakeTSO**: 确认为单核 MCU 后启用，减少内存屏障指令
- **SleepStrategy**: 低功耗场景用 PreciseSleepStrategy，高吞吐用 YieldSleepStrategy

### 10.3 迁移路径: Linux → RT-Thread

newosp 的核心模块 (HSM, SPSC, Bus, Executor) 保持 POSIX API 边界清晰，为未来迁移到 RT-Thread (C++17 RTOS) 做好准备:

| 模块 | Linux 依赖 | RT-Thread 适配方案 |
|------|-----------|-------------------|
| SPSC | 无 (纯 C++ atomic) | `FakeTSO = true` |
| HSM | 无 (纯 C++) | 直接使用 |
| AsyncBus | 无 (CAS 原子操作) | 直接使用 |
| Executor | pthread, SCHED_FIFO | 映射到 RT-Thread 线程 API |
| Timer | clock_gettime | 映射到 rt_tick |
| ShmTransport | shm_open, mmap | 不适用 (单进程) |

## 参考资料

1. [newosp GitHub 仓库](https://github.com/DeguiLiu/newosp) -- C++17 header-only 嵌入式基础设施库
2. [QP/C 官方文档](https://www.state-machine.com/qpc/) -- Quantum Leaps Active Object 框架
3. [QPC 层次状态机设计与优势分析](https://blog.csdn.net/stallion5632/article/details/149359525)
4. [QPC 框架中状态机的设计优势和特殊之处](https://blog.csdn.net/stallion5632/article/details/149260812)
5. [QPC QActive 零拷贝 & 无锁数据传输解析](https://blog.csdn.net/stallion5632/article/details/149374727)
6. [QPC QActive 在 RT-Thread 上的实现原理详述](https://blog.csdn.net/stallion5632/article/details/149604623)
7. Miro Samek, *Practical UML Statecharts in C/C++*, 2nd Edition
8. [ROS2 Lifecycle Node Design](https://design.ros2.org/articles/node_lifecycle.html)

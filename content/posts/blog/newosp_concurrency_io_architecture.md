---
title: "如何设计嵌入式并发架构: newosp 的事件驱动 + 固定线程池方案"
date: 2026-02-17
draft: false
categories: ["blog"]
tags: ["newosp", "C++17", "concurrency", "event-driven", "lock-free", "MPSC", "SPSC", "epoll", "poll", "embedded", "ARM-Linux", "zero-allocation", "WorkerPool", "Executor", "IoPoller"]
summary: "面向 4 核 ARM-Linux、32-256MB RAM、-fno-exceptions 的工业嵌入式场景，newosp 选择了事件驱动消息总线 + 固定线程预算 + 可移植 I/O 抽象的并发架构。本文从约束出发，展开 AsyncBus (CAS 无锁 MPSC + 优先级准入)、Executor 家族 (SingleThread/Pinned/Realtime)、IoPoller (epoll/poll 编译期选择) 三层设计，详解零堆分配保证、线程预算计算、背压控制机制和 I/O 线程解耦模式，附完整端到端示例和性能数据。"
ShowToc: true
TocOpen: true
---

> 配套代码: [DeguiLiu/newosp](https://github.com/DeguiLiu/newosp) -- header-only C++17 嵌入式基础设施库
>
> 相关文章:
> - [工业传感器数据流水线: newosp C++17 零堆分配事件驱动架构实战](../newosp_event_driven_architecture/) -- AsyncBus/HSM/SPSC 核心实现 + 流水线案例
> - [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- WorkerPool 内部的 SPSC 队列
> - [无锁编程核心原理: 从 CAS 到三种队列模式](../lockfree_programming_fundamentals/) -- CAS/MPSC/SPSC 原理
> - [共享内存进程间通信](../shm_ipc_newosp/) -- ShmRingBuffer I/O 集成
>
> CSDN 原文:
> - [C++ 多线程与协程优化阻塞型任务](https://blog.csdn.net/stallion5632/article/details/143887766)
> - [Linux I/O 多路复用与异步 I/O 对比](https://blog.csdn.net/stallion5632/article/details/143675999)

## 1. 约束与方案选择

newosp 面向的典型硬件是 4 核 ARM Cortex-A (激光雷达、机器人、边缘计算)，32-256MB RAM，编译选项 `-fno-exceptions -fno-rtti`，未来需要移植到 RT-Thread MCU。在这个约束下，并发架构必须满足: **线程数编译期确定** (4-8 个，不随任务数增长)、**热路径零堆分配** (Publish → Dispatch → 回调全程无 malloc)、**I/O 层可移植** (不绑定 epoll)。

newosp 的回答是三层解耦:

- **AsyncBus**: CAS 无锁 MPSC 环形缓冲，替代 mutex + queue 的线程间通信
- **Executor/WorkerPool**: 固定线程预算的调度层，替代"每任务一线程"
- **IoPoller**: epoll/poll 编译期选择，I/O 就绪通知与业务逻辑解耦

### 为什么不用协程

state-threads 实验 (1000x1000 矩阵乘法 + 1us 阻塞) 显示协程在阻塞密集场景下比纯多线程快 4.5 倍 (3.3s vs 14.9s)。但工业嵌入式有两个硬伤: **栈内存不可控** -- 有栈协程需要 4-64KB/协程，1000 协程 = 4-64MB，在 32-256MB 系统中无法接受，且栈深度编译期无法预测; **`-fno-exceptions` 冲突** -- Boost.Fiber/Coroutine2 不兼容，兼容的 state-threads/libco 是纯 C 库，与 C++17 类型系统 (variant, optional, constexpr) 没有集成。

### 为什么不直接用 epoll

epoll 是网络 I/O 的最优方案，但直接在 epoll 回调中处理业务会导致三个问题: **不可移植** (RT-Thread 只有 POSIX poll); **回调耦合** (epoll_wait → recv → parse → dispatch → process 层层嵌套，难以独立测试); **缺少消息语义** (epoll 只管 fd 就绪，不提供类型路由、优先级准入、背压控制)。newosp 保留 epoll 做 I/O 通知，但将业务逻辑抽离到消息总线层。

## 2. 架构设计

### 2.1 三层职责

```
                        ┌──────────────────────────────────┐
                        │         应用层 (Node/StaticNode)  │
                        │  Publish()  Subscribe<T>()       │
                        └──────────┬───────────────────────┘
                                   │ std::variant<Types...>
                        ┌──────────▼───────────────────────┐
                        │     AsyncBus (无锁 MPSC 环形缓冲) │
                        │  CAS publish → batch consume     │
                        │  优先级准入 + 背压控制             │
                        └──────────┬───────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
    ┌─────────▼────────┐ ┌────────▼────────┐ ┌─────────▼────────┐
    │  SingleThread     │ │  PinnedExecutor │ │   WorkerPool     │
    │  Executor         │ │  RealtimeExec   │ │  (Dispatcher + N │
    │  (调试/单核)      │ │  (SCHED_FIFO)   │ │   Worker SPSC)   │
    └──────────────────┘ └─────────────────┘ └──────────────────┘
                                   │
                        ┌──────────▼───────────────────────┐
                        │       IoPoller (I/O 事件循环)     │
                        │  Linux: epoll  │  通用: poll()    │
                        └──────────────────────────────────┘
```

**AsyncBus** 是消息骨干: 多个生产者 (传感器线程、网络线程、定时器) 通过 CAS 无锁发布消息到共享环形缓冲; 单消费者 (Executor) 批量取出并分发到 Node/StaticNode 的回调。

**Executor** 是调度层: 决定"哪个线程执行 ProcessBatch"。SingleThread 在调用线程阻塞执行; PinnedExecutor 绑定到指定 CPU 核心; RealtimeExecutor 启用 SCHED_FIFO + mlockall + 自定义栈。

**IoPoller** 是 I/O 适配层: 将 fd 就绪事件转化为消息发布，自身不处理业务逻辑。

### 2.2 AsyncBus: CAS 发布与批量消费

```cpp
template <typename PayloadVariant,
          uint32_t QueueDepth = 4096,    // 2 的幂
          uint32_t BatchSize = 256>
class AsyncBus;
```

消息以**信封** (32B header + variant payload) 存储在缓存行对齐的环形缓冲中:

```cpp
struct alignas(64) RingBufferNode {
    std::atomic<uint32_t> sequence;          // CAS 序号控制
    MessageEnvelope<PayloadVariant> envelope; // header + payload
};
```

**发布路径** (多生产者，lock-free):

```
Producer 0 ─┐
Producer 1 ──┼── CAS(producer_pos_) ──> 写入 Envelope ──> release store(sequence)
Producer 2 ─┘
```

生产者通过 `compare_exchange_weak(acq_rel)` 竞争序列号，获胜者写入对应 slot，最后用 `release store` 发布。无 mutex、无 spinlock。

**消费路径** (单消费者，Run-to-Completion):

```cpp
uint32_t ProcessBatch() {
    for (uint32_t i = 0; i < kBatchSize; ++i) {
        auto* node = &ring_[consumer_pos_ & kMask];
        if (node->sequence.load(acquire) != consumer_pos_ + 1) break;

        __builtin_prefetch(&ring_[(consumer_pos_ + 1) & kMask], 0, 1);
        DispatchToCallbacks(node->envelope);

        node->sequence.store(consumer_pos_ + kQueueDepth, release);
        ++consumer_pos_;
    }
}
```

批量消费减少原子操作频率，prefetch 预取下一 slot 减少 cache miss。

**两种分发模式**:

| 模式 | 机制 | ns/msg (P50) |
|------|------|---:|
| StaticNode (编译期) | `std::visit` 跳转表，编译器可内联 | ~2 |
| Node (运行时) | FixedFunction 回调表，SharedSpinLock | ~30 |

StaticNode 的 Handler 在编译期确定，编译器为每个 variant alternative 生成直接调用。Node 适合需要运行时动态订阅的场景，回调存储在 `FixedFunction<Sig, 32>` (32 字节 SBO，编译期 `static_assert` 拒绝超限 lambda)。

### 2.3 零堆分配保证

热路径 (Publish → ProcessBatch → 回调执行) 中零 `malloc`/`free`，通过三个机制:

**环形缓冲预分配**: Bus 构造时一次性分配 `RingBufferNode[QueueDepth]` 数组，后续消息写入只是覆盖已有 slot:

```
内存布局 (QueueDepth=4096):
┌───────────────────────────────────────────────┐
│  RingBufferNode[0]  │  [1]  │  ...  │  [4095] │  ← 构造时分配，运行时零分配
│  alignas(64)        │       │       │         │
└───────────────────────────────────────────────┘
```

**FixedFunction SBO**: 替代 `std::function`，32 字节内联缓冲，超限捕获**编译期报错**:

```cpp
template <typename Signature, size_t BufferSize = 2 * sizeof(void*)>
class FixedFunction;

// Bus 内部使用 32B SBO
using CallbackType = FixedFunction<void(const EnvelopeType&), 4 * sizeof(void*)>;
// sizeof(void*) = 8 → 32B，足够存储 [this + 1 指针] 的 lambda
```

`std::function` 在 lambda 超过 SBO 阈值 (通常 16-32B，实现依赖) 时会隐式堆分配。FixedFunction 将这个"可能"变成"编译期拒绝"。

**std::variant 值语义**: 消息作为 variant 直接 move 进 Envelope，无间接指针，无引用计数:

```cpp
// 传统方案: shared_ptr<Frame> → control block 堆分配 + 原子引用计数
// newosp: variant<RawFrame, ControlCmd, ...> → 直接嵌入 Envelope，sizeof 编译期确定
```

### 2.4 线程预算

newosp 典型部署的线程分布:

| 组件 | 线程数 | CPU 亲和性 | 职责 |
|------|--------|-----------|------|
| TimerScheduler | 1 | -- | 定时任务调度 |
| DebugShell | 1+2 | -- | TCP telnet 监听 + 最多 2 会话 |
| AsyncLog | 1 | -- | 异步日志写盘 |
| Executor | 1 | CPU 2 (Pinned) | 消息调度 (SpinOnce 循环) |
| WorkerPool | 1+N | CPU 3+ | Dispatcher + N Worker |
| **合计** | **6+N** | 确定性 | N 通常 = 2-4 |

对比:

```
传统多线程:     线程数 = 任务数 (10 个传感器 = 10 个线程)
协程:           线程数固定，但协程数/栈内存不可控
newosp:         线程数 = 编译期常量，内存预算可精确计算
```

### 2.5 内存预算

| 组件 | 内存占用 | 分配方式 |
|------|----------|----------|
| AsyncBus (4096 slots) | ~320 KB | 预分配环形缓冲 (取决于 variant sizeof) |
| WorkerPool (4 workers x 1024 SPSC) | ~64 KB | 预分配 SPSC 队列 |
| Node (16 subscriptions) | ~1 KB | 栈数组 |
| IoPoller (64 events) | ~512 B | 内部缓冲 |
| **热路径合计** | **~386 KB** | **零 malloc** |

### 2.6 背压控制

当 Bus 队列压力增大时，低优先级消息被拒绝发布:

```
队列占用 >= 60%: 拒绝 kLow (遥测、统计)
队列占用 >= 80%: 拒绝 kMedium (常规数据)
队列占用 >= 99%: 拒绝 kHigh (仅保留控制命令)
```

实现上，生产者先检查**本地缓存**的消费位置 (relaxed 读，无 cache line 竞争):

```cpp
uint32_t depth = producer_pos_.load(relaxed) - cached_consumer_pos_;
if (depth >= AdmissionThreshold(priority)) {
    // 缓存可能过时，重新读取真实消费位置 (acquire)
    cached_consumer_pos_ = consumer_pos_.load(acquire);
    depth = producer_pos_.load(relaxed) - cached_consumer_pos_;
    if (depth >= AdmissionThreshold(priority)) {
        return false;  // 丢弃
    }
}
```

低负载时完全避免对消费者原子变量的争用 (cached hit)。高负载时按优先级逐级丢弃，控制命令几乎不受影响。

### 2.7 Executor 家族

四种执行模型覆盖从调试到工业实时的全场景:

```cpp
// 1. 单线程阻塞 (调试/单核)
osp::SingleThreadExecutor<Payload> exec;
exec.Spin();

// 2. 后台线程 + 休眠策略
osp::StaticExecutor<Payload, osp::PreciseSleepStrategy> exec(strategy);
exec.Start();

// 3. CPU 绑核
osp::PinnedExecutor<Payload, osp::YieldSleepStrategy> exec(/*cpu=*/2);
exec.Start();

// 4. 工业实时
osp::RealtimeConfig cfg;
cfg.sched_policy = SCHED_FIFO;
cfg.sched_priority = 80;
cfg.lock_memory = true;     // mlockall
cfg.cpu_affinity = 3;
osp::RealtimeExecutor<Payload, osp::PreciseSleepStrategy> exec(cfg);
exec.Start();
```

RealtimeExecutor 初始化序列: `mlockall()` → CPU affinity → SCHED_FIFO → 自定义栈 → 进入 ProcessBatch 循环。高精度休眠使用 `clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME)` 实现亚毫秒精度。

**PreciseSleepStrategy**: 三参数自适应 (default/min/max sleep_ns)，有消息时快速唤醒，空闲时降低功耗。

### 2.8 WorkerPool: 两级无锁流水线

当单消费者线程处理不过来时，WorkerPool 提供多 Worker 并行:

```
AsyncBus (MPSC)
    │
    ▼
Dispatcher Thread (ProcessBatch → Round-Robin 分发)
    │
    ├──→ Worker[0] SPSC (1024 depth, wait-free)
    ├──→ Worker[1] SPSC
    └──→ Worker[N] SPSC
```

**第一级 MPSC**: 所有生产者 CAS 发布到共享 Bus。

**第二级 Per-Worker SPSC**: Dispatcher 从 Bus 取出消息，Round-Robin 分发到各 Worker 的 SPSC 队列。Worker 线程从自己的 SPSC 消费，**无任何锁竞争**。

Worker 空闲时三阶段退避: Spin (1-64 次 CPU relax) → Yield (4 次) → Sleep (50us)。有消息到来立即回退到 Spin。

```cpp
osp::WorkerPool<Payload> pool({.name = "sensor", .worker_num = 4});
pool.RegisterHandler<SensorData>([](const SensorData& d, const auto&) {
    heavy_computation(d);
});
pool.Start();  // 4 Worker + 1 Dispatcher = 5 线程
```

## 3. I/O 集成

### 3.1 IoPoller: 编译期后端选择

IoPoller 在编译期根据平台选择后端，API 完全统一:

```cpp
osp::IoPoller poller;
poller.Add(tcp_fd, osp::IoEvent::kReadable);
poller.Add(serial_fd, osp::IoEvent::kReadable);

while (running) {
    auto result = poller.Wait(/*timeout_ms=*/100);
    if (!result.has_value()) continue;
    for (uint32_t i = 0; i < result.value(); ++i) {
        auto& ev = poller.Results()[i];
        if (ev.events & osp::IoEvent::kReadable) {
            handle_fd(ev.fd);
        }
    }
}
```

| 平台 | 后端 | 复杂度 | 选择条件 |
|------|------|--------|---------|
| Linux | epoll | O(1) | `__linux__` 定义时 |
| macOS/BSD | kqueue | O(1) | `__APPLE__` 定义时 |
| 通用 | poll | O(n) | fallback |

嵌入式 fd 数量通常 < 100，O(n) poll 在此规模下与 O(1) epoll 差异可忽略 (<1us)。IoPoller 保证核心逻辑可移植到 RT-Thread (支持 POSIX poll)，同时在 Linux 自动使用 epoll。

### 3.2 I/O 线程与业务线程解耦

I/O 线程只做三件事: **等待 fd 就绪 → 读取数据 → 发布消息**。不持有业务状态，不需要锁:

```
I/O 线程                               Executor 线程
─────────                              ────────────
IoPoller.Wait()                        ProcessBatch()
    │                                      │
    ├── recv(fd, buf, len)                 ├── StaticNode::Handler
    ├── ParseFrame(buf)                    │   void operator()(const SensorData& d, ...)
    ├── Deserialize → SensorData           │   {  process(d);  }
    └── bus.Publish(SensorData{...})  ────>│
                                           │
    (零业务逻辑，纯 I/O)                  (零 I/O，纯业务)
```

分离带来两个好处:
1. **I/O 线程极轻量**: recv + publish，无阻塞风险
2. **业务可独立测试**: StaticNode Handler 接收消息和 Header，不依赖 socket/fd

完整的 Transport 接收路径:

```cpp
void TransportReceiver::Run() {
    osp::IoPoller poller;
    poller.Add(socket_fd_, osp::IoEvent::kReadable);

    while (running_) {
        auto result = poller.Wait(100);
        if (!result.has_value() || result.value() == 0) continue;

        uint8_t buf[kMaxFrameSize];
        ssize_t n = ::recv(socket_fd_, buf, sizeof(buf), 0);
        if (n <= 0) continue;

        FrameHeaderV1 header;
        if (!ParseFrame(buf, n, &header)) continue;

        auto payload = Deserialize(buf + sizeof(header), header.type_idx);
        if (payload.has_value()) {
            bus_.Publish(std::move(payload.value()), header.sender_id);
        }
    }
}
```

## 4. 端到端示例: 传感器采集系统

```cpp
#include <osp/bus.hpp>
#include <osp/static_node.hpp>
#include <osp/executor.hpp>
#include <osp/io_poller.hpp>
#include <osp/shutdown.hpp>

// ── 消息类型 (POD, trivially_copyable) ────────────────────────
struct SensorReading {
    uint32_t sensor_id;
    float value;
    uint64_t timestamp_ns;
};
struct ProcessedResult {
    uint32_t sensor_id;
    float filtered_value;
    uint8_t quality;
};
struct AlarmEvent {
    uint32_t sensor_id;
    uint8_t level;
    char message[64];
};

using Payload = std::variant<SensorReading, ProcessedResult, AlarmEvent>;
using Bus = osp::AsyncBus<Payload>;

static_assert(std::is_trivially_copyable_v<SensorReading>);
static_assert(std::is_trivially_copyable_v<ProcessedResult>);
static_assert(std::is_trivially_copyable_v<AlarmEvent>);

// ── Handler (编译期绑定) ──────────────────────────────────────
struct ProcessingHandler {
    Bus& bus;
    float ema_alpha = 0.3f;
    float ema_value = 0.0f;

    void operator()(const SensorReading& r, const osp::MessageHeader&) {
        // 指数移动平均滤波
        ema_value = ema_alpha * r.value + (1.0f - ema_alpha) * ema_value;
        uint8_t quality = (std::abs(r.value - ema_value) < 1.0f) ? 95 : 60;

        ProcessedResult result{r.sensor_id, ema_value, quality};
        bus.Publish(Payload(result), /*sender_id=*/2);
    }

    void operator()(const ProcessedResult& r, const osp::MessageHeader&) {
        // 质量低于阈值时报警
        if (r.quality < 50) {
            AlarmEvent alarm{};
            alarm.sensor_id = r.sensor_id;
            alarm.level = 1;
            std::snprintf(alarm.message, sizeof(alarm.message),
                         "quality=%u below threshold", r.quality);
            bus.Publish(Payload(alarm), /*sender_id=*/2);
        }
    }

    void operator()(const AlarmEvent& a, const osp::MessageHeader&) {
        OSP_LOG_WARN("Alarm", "sensor=%u level=%u: %s",
                     a.sensor_id, a.level, a.message);
    }
};

// ── Main ──────────────────────────────────────────────────────
int main() {
    auto& shutdown = osp::ShutdownManager::Instance();
    auto& bus = Bus::Instance();

    // 处理节点 (StaticNode, 编译期分发)
    ProcessingHandler handler{bus};
    osp::StaticNode<Payload, ProcessingHandler> processor("processor", 2, handler);

    // I/O 采集节点
    osp::Node<Payload> sensor_io("sensor_io", 1);

    // I/O 线程: IoPoller → recv → Publish
    std::thread io_thread([&]() {
        osp::IoPoller poller;
        poller.Add(sensor_fd, osp::IoEvent::kReadable);

        while (!shutdown.IsShutdown()) {
            auto result = poller.Wait(100);
            if (!result.has_value() || result.value() == 0) continue;

            for (uint32_t i = 0; i < result.value(); ++i) {
                auto& ev = poller.Results()[i];
                if (ev.events & osp::IoEvent::kReadable) {
                    SensorReading reading{};
                    ssize_t n = ::read(ev.fd, &reading, sizeof(reading));
                    if (n == sizeof(reading)) {
                        reading.timestamp_ns = osp::SteadyNowNs();
                        sensor_io.Publish(reading);
                    }
                }
            }
        }
    });

    // 消息调度: CPU 2 绑核，PreciseSleep 降低空闲功耗
    osp::PreciseSleepStrategy sleep_strategy(
        1'000'000ULL,   // 默认 1ms
        100'000ULL,     // 最小 100us
        10'000'000ULL   // 最大 10ms
    );
    osp::PinnedExecutor<Payload, osp::PreciseSleepStrategy> executor(2, sleep_strategy);
    executor.Start();

    shutdown.WaitForShutdown();
    executor.Stop();
    io_thread.join();
    return 0;
}
```

**线程分布**:

```
CPU 0: Linux 内核 + 系统服务
CPU 1: I/O 线程 (IoPoller + recv + Publish)
CPU 2: 消息调度 (PinnedExecutor, ProcessBatch → StaticNode)
CPU 3: 备用 (WorkerPool Worker / 日志 / Shell)

合计: 3 线程，确定性
```

**消息流**:

```
sensor_fd (硬件)
    │
    ▼
IoPoller.Wait() → recv → SensorReading → bus.Publish()
                                              │
    processor (StaticNode) ←──────────────────┘
        │
        ├── operator()(SensorReading) → EMA 滤波 → ProcessedResult → Publish
        │
        ├── operator()(ProcessedResult) → 质量检查 → AlarmEvent → Publish
        │
        └── operator()(AlarmEvent) → OSP_LOG_WARN
```

**性能数据** (ARM Cortex-A72, 4 核 1.5GHz):

| 指标 | 数值 | 说明 |
|------|------|------|
| StaticNode 分发延迟 (P50) | ~2 ns | std::visit 直接跳转 |
| Node 回调延迟 (P50) | ~30 ns | FixedFunction 间接调用 |
| Publish 延迟 (P50) | ~15 ns | CAS + memcpy envelope |
| 端到端延迟 (Publish → Handler) | ~50 ns | 含 ProcessBatch 调度 |
| Bus 吞吐 (单生产者) | ~60M msg/s | QueueDepth=4096 |
| Bus 吞吐 (4 生产者) | ~40M msg/s | CAS 竞争开销 |
| 热路径堆分配 | 0 | FixedFunction SBO + variant 值语义 |

## 5. 与传统方案对比

| 维度 | mutex + queue | epoll 回调 | newosp 事件总线 |
|------|:------------:|:----------:|:--------------:|
| 线程间同步 | mutex (futex slow path) | 单线程无需 | CAS 无锁 MPSC |
| 消息存储 | std::queue (堆分配) | 自行管理 | 预分配环形缓冲 |
| 回调机制 | std::function (可能堆分配) | 裸函数指针/回调 | FixedFunction SBO / std::visit |
| 类型安全 | void* 或 std::any | 自行管理 | std::variant 编译期路由 |
| 背压 | 无界队列或阻塞 | 自行实现 | 优先级准入 (60/80/99%) |
| I/O 可移植 | N/A | epoll 专有 | epoll/kqueue/poll 自动选择 |
| 可测试性 | 线程竞态难测 | 依赖 fd mock | Node 纯消息测试 |
| 延迟 (P50) | ~1-5 us | ~100-500 ns | ~2 ns (StaticNode) |

核心差异: 传统方案的 `std::mutex` futex slow path 和 `std::queue` 堆分配是热路径上的两个主要不确定性来源。newosp 用 CAS 环形缓冲消除了前者，用 variant 值语义消除了后者。

## 参考资料

1. [newosp GitHub 仓库](https://github.com/DeguiLiu/newosp) -- C++17 header-only 嵌入式基础设施库
2. [C++ 多线程与协程优化阻塞型任务](https://blog.csdn.net/stallion5632/article/details/143887766) -- state-threads 实验
3. [Linux I/O 多路复用与异步 I/O 对比](https://blog.csdn.net/stallion5632/article/details/143675999) -- 五种 I/O 模型
4. [Cyber RT 协程实现](https://zhuanlan.zhihu.com/p/365838048) -- Baidu Apollo 协程架构

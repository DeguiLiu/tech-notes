---
title: "newosp 嵌入式并发与 I/O 架构: 从线程/协程/IO多路复用到事件驱动消息总线"
date: 2026-02-17
draft: false
categories: ["blog"]
tags: ["newosp", "C++17", "concurrency", "I/O", "epoll", "poll", "coroutine", "thread-pool", "event-driven", "lock-free", "MPSC", "SPSC", "embedded", "ARM-Linux", "zero-allocation", "WorkerPool", "Executor"]
summary: "嵌入式并发有三条经典路径: 多线程+协程、I/O 多路复用、异步 I/O。它们各自解决了部分问题，但在资源受限的 ARM-Linux 嵌入式场景中，线程数不可控、协程生态碎片化、epoll 不可移植、aio 仅限块设备等问题依然突出。newosp 选择了第四条路: 事件驱动消息总线 (无锁 MPSC AsyncBus) + 固定线程预算 (Executor/WorkerPool) + 可移植 I/O 抽象 (IoPoller poll/epoll 双后端)，在 4-8 个线程内实现确定性微秒级延迟和零堆分配热路径。本文从传统并发与 I/O 模型的局限出发，逐层剖析 newosp 的线程架构、消息流水线和 I/O 集成设计，展示嵌入式系统如何用事件驱动替代阻塞并发。"
ShowToc: true
TocOpen: true
---

> 配套代码: [DeguiLiu/newosp](https://github.com/DeguiLiu/newosp) -- header-only C++17 嵌入式基础设施库
>
> 设计文档: [design_zh.md](https://github.com/DeguiLiu/newosp/blob/main/docs/design_zh.md)
>
> 相关文章:
> - [newosp 深度解析: C++17 事件驱动架构](../newosp_event_driven_architecture/) -- AsyncBus/Node/HSM 核心架构
> - [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- WorkerPool 内部的 SPSC 队列
> - [无锁编程核心原理: 从 CAS 到三种队列模式](../lockfree_programming_fundamentals/) -- AsyncBus 底层的无锁原理
> - [共享内存进程间通信](../shm_ipc_newosp/) -- ShmRingBuffer I/O 集成
> - [嵌入式线程间消息传递重构: MCCC 无锁消息总线](../mccc_message_passing/) -- newosp AsyncBus 的前身
> - [newosp ospgen: YAML 驱动的 C++17 零堆消息代码生成](../newosp_ospgen_codegen/) -- 消息类型自动生成
>
> CSDN 原文: [newosp 嵌入式并发与 I/O 架构](https://blog.csdn.net/stallion5632)

## 1. 问题: 嵌入式并发的三条路和它们的局限

嵌入式 ARM-Linux 系统 (激光雷达、机器人、边缘计算) 需要同时处理传感器采集、协议解析、控制逻辑、网络通信等多个并发任务。业界有三条经典路径:

### 1.1 路径一: 多线程 + 协程

**多线程** 是最直观的并发模型: 每个任务一个线程，阻塞等待 I/O 或计算。但线程创建/销毁和上下文切换的开销在嵌入式场景中不可忽视:

```cpp
// 传统多线程: 每个传感器一个线程
void sensor_thread(int sensor_id) {
  while (running) {
    auto data = blocking_read(sensor_fd);  // 阻塞等待
    process(data);                          // 处理
    std::this_thread::sleep_for(1ms);       // 让出 CPU
  }
}
// 问题: 10 个传感器 = 10 个线程 = 不可控的上下文切换
```

**协程** (如 state-threads、libco、Boost.Fiber) 通过用户态上下文切换减少开销。在矩阵乘法阻塞实验中，4 线程 + 10 协程/线程 比纯 4 线程快 4-5 倍 (14.9s vs 3.3s)，因为协程在阻塞时主动让出 CPU 而非被动等待调度器:

```
纯多线程 (4 线程):      14.91s -- 每次 sleep_for(1us) 触发内核调度
多线程+协程 (4x10):      3.32s -- st_usleep(1) 用户态切换，无内核介入
```

但协程在嵌入式场景有三个根本问题:

| 问题 | 说明 |
|------|------|
| 生态碎片化 | state-threads (epoll+longjmp)、libco (汇编)、Boost.Fiber (重依赖) 互不兼容 |
| 栈内存不可控 | 有栈协程需要 4-64KB 栈/协程，1000 协程 = 4-64MB，嵌入式内存预算不允许 |
| 与 `-fno-exceptions` 冲突 | 多数协程库依赖异常或 RTTI，嵌入式编译选项下不可用 |

### 1.2 路径二: I/O 多路复用

Linux 提供五种 I/O 模型: 阻塞、非阻塞、信号驱动、多路复用 (select/poll/epoll)、异步 I/O (aio/io_uring)。对网络 I/O，epoll 是公认的最优方案:

```
select()   → O(n) 扫描 fd_set，fd 数量上限 1024
poll()     → O(n) 扫描 pollfd 数组，无 fd 上限
epoll()    → O(1) 事件驱动，内核红黑树维护 fd，高并发首选
```

但 epoll 在嵌入式场景的局限:

| 问题 | 说明 |
|------|------|
| Linux 专有 | 不可移植到 RT-Thread/FreeRTOS 等 RTOS (它们只支持 POSIX poll/select) |
| 仅管 I/O 就绪 | 不解决消息路由、类型安全、背压控制等应用层问题 |
| 回调地狱 | 复杂业务逻辑在 epoll 回调中层层嵌套，可维护性差 |

### 1.3 路径三: 异步 I/O

Linux 异步 I/O 有两种实现: POSIX aio (用户态模拟) 和 libaio/io_uring (内核态真异步)。但:

| 方案 | 适用范围 | 嵌入式问题 |
|------|----------|-----------|
| POSIX aio | 文件/块设备 | 内部创建线程池，线程数不可控 |
| libaio | 块设备 (O_DIRECT) | 不支持网络 socket，不支持 buffered I/O |
| io_uring | 通用 (5.1+) | 需要 Linux 5.1+，嵌入式内核版本普遍 4.x |

**结论**: 对网络 I/O (UDP/TCP)，多路复用仍是最佳选择; 异步 I/O 仅建议用于磁盘/块设备操作。

### 1.4 嵌入式的核心需求

上述三条路径各解决了部分问题，但嵌入式系统需要的是一个**融合方案**:

```
确定性线程数 (4-8 个)  ←  线程/协程模型不提供
零堆分配热路径         ←  协程栈分配、std::function 不保证
跨平台 I/O 抽象       ←  epoll 不可移植
类型安全消息路由       ←  I/O 多路复用不涉及
编译期分发             ←  以上均不涉及
```

newosp 的回答: **事件驱动消息总线 + 固定线程预算 + 可移植 I/O 抽象**。

## 2. newosp 并发架构总览

newosp 的并发模型可以用一句话概括: **所有并发通过消息总线解耦，所有 I/O 通过事件循环驱动，线程数编译期确定**。

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
    │  SingleThread     │ │  StaticExecutor │ │   WorkerPool     │
    │  Executor         │ │  (专用调度线程)  │ │  (Dispatcher + N │
    │  (阻塞当前线程)   │ │  PinnedExecutor │ │   Worker 线程)   │
    └──────────────────┘ │  (CPU 亲和性)    │ └──────────────────┘
                         └─────────────────┘
                                   │
                        ┌──────────▼───────────────────────┐
                        │       IoPoller (I/O 事件循环)     │
                        │  Linux: epoll  │  通用: poll()    │
                        │  macOS: kqueue │  RT-Thread: poll │
                        └──────────────────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
    ┌─────────▼────────┐ ┌────────▼────────┐ ┌─────────▼────────┐
    │   TCP Transport  │ │  ShmTransport   │ │  SerialTransport │
    │   (socket I/O)   │ │  (共享内存 IPC)  │ │  (UART/RS-485)  │
    └──────────────────┘ └─────────────────┘ └──────────────────┘
```

### 2.1 线程预算: 编译期确定，运行时不膨胀

newosp 的典型线程分布:

| 组件 | 线程数 | 职责 |
|------|--------|------|
| TimerScheduler | 1 | 定时任务调度 |
| DebugShell | 1+2 | TCP telnet 监听 + 会话 |
| AsyncLog | 1 | 异步日志写盘 |
| Executor | 1 | 消息调度 (SpinOnce 循环) |
| WorkerPool | 1+N | Dispatcher + N Worker |
| **合计** | **4-8** | 全部确定性 |

对比传统方案:

```
传统多线程:     线程数 = 任务数 (不可控)
协程模型:       线程数确定，但协程数/栈内存不可控
newosp:         线程数 = 编译期常量，内存预算可计算
```

### 2.2 内存预算: 零堆分配保证

| 组件 | 内存占用 | 分配方式 |
|------|----------|----------|
| AsyncBus (4096 slots) | ~320 KB | 预分配环形缓冲 |
| WorkerPool (4 workers) | ~64 KB | 预分配 SPSC 队列 |
| Node (16 subscriptions) | ~1 KB | 栈数组 |
| IoPoller (64 events) | ~512 B | 内部缓冲 |
| **热路径合计** | **~386 KB** | **零 malloc** |

关键: `Publish()` → `ProcessBatch()` → 回调执行的整条消息热路径中，没有一次 `malloc`/`free` 调用。这是通过三个机制保证的:

1. **环形缓冲预分配**: `Envelope` 数组在 Bus 构造时一次性分配
2. **FixedFunction SBO**: 32 字节内联缓冲替代 `std::function` 的堆分配
3. **std::variant 值语义**: 消息作为 variant 直接 move 进 Envelope，无间接指针

## 3. 消息总线: 替代线程间共享状态

### 3.1 为什么用消息传递替代锁

传统并发使用共享内存 + 锁:

```cpp
// 传统方式: 共享状态 + mutex
struct SensorData { float x, y, z; };
std::mutex mtx;
std::queue<SensorData> shared_queue;  // 共享队列

// 生产者线程
void producer() {
  SensorData data = read_sensor();
  std::lock_guard<std::mutex> lock(mtx);  // 加锁
  shared_queue.push(data);                 // 堆分配 (queue node)
}

// 消费者线程
void consumer() {
  std::lock_guard<std::mutex> lock(mtx);  // 竞争同一把锁
  auto data = shared_queue.front();
  shared_queue.pop();
  process(data);
}
```

问题: mutex 竞争引入不确定性延迟; `std::queue` 的每次 push 都可能触发堆分配; 多消费者需要更复杂的同步。

newosp 的消息传递模型:

```cpp
// newosp 方式: 消息总线解耦
using Payload = std::variant<SensorData, ControlCmd, StatusReport>;
using Bus = osp::AsyncBus<Payload>;

// 生产者 (任意线程，lock-free)
osp::Node<Payload> sensor_node("sensor", 1);
sensor_node.Publish(SensorData{1.0f, 2.0f, 3.0f});  // CAS 发布，零堆分配

// 消费者 (单线程调度)
osp::Node<Payload> processor("processor", 2);
processor.Subscribe<SensorData>([](const SensorData& data, const auto&) {
    process(data);  // 类型安全，编译期路由
});

// 调度循环
while (running) {
  processor.SpinOnce();  // 批量处理，单消费者无锁
}
```

### 3.2 AsyncBus: 无锁 MPSC 的四个关键设计

**CAS 序列号排序**: 多个生产者通过 `compare_exchange_weak` 竞争序列号，获胜者将消息写入对应 slot。无 mutex，无 spinlock (发布路径)。

```cpp
// 简化的发布流程 (实际代码见 bus.hpp)
bool Publish(PayloadVariant&& payload, uint32_t sender_id) noexcept {
  uint64_t seq = prod_seq_.load(std::memory_order_relaxed);
  while (!prod_seq_.compare_exchange_weak(seq, seq + 1,
         std::memory_order_acq_rel)) { /* retry */ }
  // seq 位置已获得，写入 Envelope
  auto& env = ring_[seq & kBufferMask];
  env.header.id = seq;
  env.header.sender_id = sender_id;
  env.payload = std::move(payload);
  env.ready.store(true, std::memory_order_release);
  return true;
}
```

**批量消费**: `ProcessBatch()` 一次消费最多 256 条消息，减少原子操作频率:

```cpp
uint32_t ProcessBatch() noexcept {
  uint32_t count = 0;
  while (count < kBatchSize) {
    auto& env = ring_[cons_seq_ & kBufferMask];
    if (!env.ready.load(std::memory_order_acquire)) break;
    Dispatch(env);          // 类型路由 + 回调执行
    env.ready.store(false, std::memory_order_release);
    ++cons_seq_;
    ++count;
  }
  return count;
}
```

**优先级准入控制**: 队列压力大时，低优先级消息被拒绝:

```
队列占用 >= 60%: 拒绝 LOW 优先级
队列占用 >= 80%: 拒绝 MEDIUM 优先级
队列占用 >= 99%: 拒绝 HIGH 优先级 (仅保留 CRITICAL)
```

**缓存行隔离**: 生产者计数器和消费者计数器分别放在不同的 64 字节缓存行，避免 ARM 多核场景的 false sharing:

```cpp
alignas(64) std::atomic<uint64_t> prod_seq_{0};  // 生产者独占缓存行
alignas(64) uint64_t cons_seq_{0};                // 消费者独占缓存行
```

### 3.3 StaticNode: 编译期分发，15x 性能提升

对性能敏感的消息处理，newosp 提供 `StaticNode`: 通过 `std::visit` + 编译期 Visitor 替代运行时回调分发:

```cpp
// Handler 在编译期绑定 (零间接调用)
struct SensorHandler {
  void operator()(const SensorData& data, const osp::MessageHeader& hdr) {
    process(data);
  }
  void operator()(const ControlCmd& cmd, const osp::MessageHeader& hdr) {
    execute(cmd);
  }
};

osp::StaticNode<Payload, SensorHandler> sensor_node("sensor", 1, handler);
// ProcessBatch 直接 std::visit，编译器可内联整个调用链
```

性能对比 (基准测试，ARM Cortex-A72):

| 方式 | 延迟/消息 | 吞吐量 |
|------|----------|--------|
| Node + lambda 回调 | ~30 ns | ~33M msg/s |
| StaticNode + Visitor | ~2 ns | ~500M msg/s |
| std::function 回调 | ~45 ns | ~22M msg/s |

StaticNode 的 15x 提升来自: 无虚函数、无间接调用、编译器可完全内联。

## 4. 线程调度: Executor 家族

newosp 提供三种 Executor，覆盖不同的嵌入式调度需求:

### 4.1 SingleThreadExecutor: 阻塞调度

最简单的模式: 在调用线程上阻塞运行消息循环。适合 main() 或专用线程:

```cpp
osp::SingleThreadExecutor<Payload> executor;
executor.AddNode(sensor_node);
executor.AddNode(processor_node);

// 阻塞当前线程，持续调度消息
executor.Spin();  // 直到 Stop() 被调用
```

### 4.2 StaticExecutor: 专用调度线程 + 精确休眠

创建独立调度线程，支持两种休眠策略:

```cpp
// 策略一: Yield (低延迟，高 CPU)
osp::StaticExecutor<Payload, osp::YieldSleepStrategy> executor;

// 策略二: PreciseSleep (低 CPU，可调延迟)
// 使用 clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME) 实现精确唤醒
osp::PreciseSleepStrategy strategy(
    1'000'000ULL,    // 默认休眠 1ms
    100'000ULL,      // 最小 100us
    10'000'000ULL    // 最大 10ms
);
osp::StaticExecutor<Payload, osp::PreciseSleepStrategy> executor(strategy);
executor.Start();  // 后台线程
```

### 4.3 PinnedExecutor: CPU 亲和性绑定

在指定 CPU 核心上运行调度线程，减少缓存失效和调度抖动:

```cpp
// 将消息调度线程绑定到 CPU 核心 2
osp::PinnedExecutor<Payload, osp::YieldSleepStrategy> executor(/*cpu_core=*/2);
executor.Start();
```

适合实时性要求高的场景 (激光雷达点云处理、运动控制):

```
CPU 0: Linux 内核 + 系统服务
CPU 1: 网络 I/O + Transport
CPU 2: 消息调度 (PinnedExecutor)  ← 独占核心
CPU 3: WorkerPool Worker
```

## 5. WorkerPool: 两级无锁流水线

当单个消费者线程处理不过来时，WorkerPool 提供**多工 Worker 并行处理**:

```
              ┌─────────────────────────┐
              │      AsyncBus (MPSC)     │
              │   多生产者 lock-free      │
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │     Dispatcher Thread    │
              │  轮询 Bus → Round-Robin  │
              │  分发到 Worker SPSC 队列  │
              └──┬──────┬──────┬────────┘
                 │      │      │
           ┌─────▼──┐ ┌▼────┐ ┌▼─────┐
           │Worker 0│ │W 1  │ │W N-1 │
           │ SPSC   │ │SPSC │ │SPSC  │
           │ 1024   │ │1024 │ │1024  │
           └────────┘ └─────┘ └──────┘
```

### 5.1 两级队列设计

**第一级: MPSC AsyncBus** -- 所有生产者 (传感器、网络、定时器) 通过 CAS 发布消息到共享 Bus。

**第二级: Per-Worker SPSC** -- Dispatcher 从 Bus 取出消息，Round-Robin 分发到各 Worker 的 SPSC 队列。Worker 线程从自己的 SPSC 队列消费。

```cpp
osp::WorkerPool<Payload> pool(osp::WorkerPoolConfig{
    .name = "sensor_pool",
    .worker_num = 4,
    .priority = 0,
});

// 注册 Handler (必须在 Start() 前)
pool.RegisterHandler<SensorData>([](const SensorData& data, const auto& hdr) {
    heavy_computation(data);  // CPU 密集型处理
});

pool.Start();
// 4 Worker + 1 Dispatcher = 5 线程
```

### 5.2 AdaptiveBackoff: 三阶段退避

Worker 空闲时的退避策略:

```
阶段 1: Spin (1-64 次)   -- CPU relax 指令 (x86: pause, ARM: yield)
阶段 2: Yield (4 次)     -- std::this_thread::yield()
阶段 3: Sleep (50us)     -- 粗粒度休眠
```

有消息到来时，立即回退到阶段 1。这个三阶段设计在低延迟和低功耗之间取得平衡:

```
有负载时: Spin 阶段，微秒级响应
负载降低: 自动过渡到 Yield，让出 CPU
持续空闲: Sleep 阶段，降低功耗
```

### 5.3 与传统线程池的对比

| 特性 | std::thread 池 | newosp WorkerPool |
|------|---------------|-------------------|
| 任务提交 | mutex + condition_variable | lock-free MPSC + SPSC |
| 任务分发 | 全局队列竞争 | Per-Worker 无锁队列 |
| 堆分配 | std::function (每次提交) | FixedFunction SBO (零分配) |
| 类型安全 | void* 或 std::any | std::variant 编译期路由 |
| 背压控制 | 无 (无界队列或阻塞) | 优先级准入 + 队列深度监控 |
| 健康监控 | 无 | GetStats() + Heartbeat |

## 6. I/O 集成: IoPoller 的可移植设计

### 6.1 为什么不直接用 epoll

newosp 目标是 ARM-Linux 嵌入式平台，但未来可能移植到 RT-Thread MCU (支持 POSIX 层)。设计决策:

```
epoll     → Linux 专有，高性能，O(1) -- 作为 Linux 后端
kqueue    → macOS/BSD 专有，O(1) -- 作为 macOS 后端
poll()    → POSIX 标准，O(n)，可移植 -- 作为通用后端
```

IoPoller 在编译期选择后端，API 完全统一:

```cpp
osp::IoPoller poller;

// 注册 socket fd (可读事件)
poller.Add(tcp_fd, osp::IoEvent::kReadable);
poller.Add(serial_fd, osp::IoEvent::kReadable);

// 统一事件循环
while (running) {
  auto result = poller.Wait(/*timeout_ms=*/100);
  if (!result.has_value()) continue;

  for (uint32_t i = 0; i < result.value(); ++i) {
    auto& ev = poller.Results()[i];
    if (ev.fd == tcp_fd && (ev.events & osp::IoEvent::kReadable)) {
      handle_tcp_data();
    }
    if (ev.fd == serial_fd && (ev.events & osp::IoEvent::kReadable)) {
      handle_serial_data();
    }
  }
}
```

### 6.2 IoPoller 与消息总线的集成

IoPoller 负责 I/O 就绪通知，消息总线负责数据路由。两者的集成模式:

```
IoPoller                           AsyncBus
   │                                  │
   │ Wait() → fd readable             │
   │                                  │
   ├──→ recv(fd, buf, len)            │
   │                                  │
   ├──→ 帧解析 (FrameHeader)          │
   │                                  │
   ├──→ 反序列化为 SensorData          │
   │                                  │
   └──→ node.Publish(SensorData{...}) ─┤
                                       │
                              ProcessBatch() → 回调
```

具体代码示例 (Transport 接收路径):

```cpp
// Transport 接收线程: I/O 事件 → 消息总线
void TransportReceiver::Run() {
  osp::IoPoller poller;
  poller.Add(socket_fd_, osp::IoEvent::kReadable);

  while (running_) {
    auto result = poller.Wait(100);  // 100ms 超时
    if (!result.has_value() || result.value() == 0) continue;

    // I/O 就绪: 读取帧数据
    uint8_t buf[kMaxFrameSize];
    ssize_t n = ::recv(socket_fd_, buf, sizeof(buf), 0);
    if (n <= 0) continue;

    // 帧解析 + 反序列化
    FrameHeaderV1 header;
    if (!ParseFrame(buf, n, &header)) continue;

    // 反序列化为具体消息类型
    auto payload = Deserialize(buf + sizeof(header), header.type_idx);
    if (!payload.has_value()) continue;

    // 发布到消息总线 (零堆分配)
    bus_.Publish(std::move(payload.value()), header.sender_id);
  }
}
```

### 6.3 与传统 I/O 模型的对比

| 维度 | 传统 epoll 回调 | newosp IoPoller + Bus |
|------|----------------|----------------------|
| I/O 通知 | epoll_wait → 直接回调 | IoPoller.Wait → Publish → 批量回调 |
| 线程模型 | 回调在 I/O 线程执行 | 回调在 Executor/Worker 线程执行 |
| I/O 与业务耦合 | 紧耦合 (回调中处理业务) | 解耦 (I/O 线程只做 recv + publish) |
| 背压 | 无 (回调积压在内存) | Bus 优先级准入控制 |
| 可测试性 | 依赖 socket/fd mock | Node 可独立测试 (纯消息) |

## 7. 完整示例: 传感器采集系统

将以上组件组合为一个典型的嵌入式传感器采集系统:

```cpp
#include <osp/bus.hpp>
#include <osp/node.hpp>
#include <osp/static_node.hpp>
#include <osp/executor.hpp>
#include <osp/worker_pool.hpp>
#include <osp/io_poller.hpp>
#include <osp/shutdown.hpp>
#include <osp/timer.hpp>

// ── 消息类型 (POD, trivially_copyable) ────────────────────────
struct SensorReading {
  uint32_t sensor_id;
  float value;
  uint64_t timestamp_ns;
};

struct ProcessedResult {
  uint32_t sensor_id;
  float filtered_value;
  uint8_t quality;  // 0-100
};

struct AlarmEvent {
  uint32_t sensor_id;
  uint8_t level;    // 0=info, 1=warn, 2=critical
  char message[64];
};

using Payload = std::variant<SensorReading, ProcessedResult, AlarmEvent>;
using Bus = osp::AsyncBus<Payload>;

// ── 编译期断言 ────────────────────────────────────────────────
static_assert(std::is_trivially_copyable_v<SensorReading>);
static_assert(std::is_trivially_copyable_v<ProcessedResult>);
static_assert(std::is_trivially_copyable_v<AlarmEvent>);

// ── Handler (编译期绑定) ──────────────────────────────────────
struct ProcessingHandler {
  Bus& bus;
  void operator()(const SensorReading& r, const osp::MessageHeader&) {
    // 滤波处理
    ProcessedResult result{r.sensor_id, filter(r.value), 95};
    bus.Publish(Payload(result), /*sender_id=*/2);
  }
  void operator()(const ProcessedResult&, const osp::MessageHeader&) {}
  void operator()(const AlarmEvent&, const osp::MessageHeader&) {}
};

struct AlarmHandler {
  void operator()(const ProcessedResult& r, const osp::MessageHeader&) {
    if (r.quality < 50) {
      AlarmEvent alarm{r.sensor_id, 1, "Low quality reading"};
      // 报警处理...
    }
  }
  void operator()(const SensorReading&, const osp::MessageHeader&) {}
  void operator()(const AlarmEvent&, const osp::MessageHeader&) {}
};

// ── Main ──────────────────────────────────────────────────────
int main() {
  // 1. 优雅关闭
  auto& shutdown = osp::ShutdownManager::Instance();

  // 2. 消息节点
  osp::Node<Payload> sensor_node("sensor_io", 1);
  ProcessingHandler proc_handler{Bus::Instance()};
  osp::StaticNode<Payload, ProcessingHandler> processor("processor", 2, proc_handler);
  AlarmHandler alarm_handler;
  osp::StaticNode<Payload, AlarmHandler> alarm("alarm", 3, alarm_handler);

  // 3. I/O 事件循环 (独立线程)
  std::thread io_thread([&]() {
    osp::IoPoller poller;
    poller.Add(sensor_fd, osp::IoEvent::kReadable);
    while (!shutdown.IsShutdown()) {
      auto result = poller.Wait(100);
      if (result.has_value() && result.value() > 0) {
        SensorReading reading = read_sensor(sensor_fd);
        sensor_node.Publish(reading);  // I/O → Bus
      }
    }
  });

  // 4. 消息调度 (CPU 核心 2 绑定)
  osp::PinnedExecutor<Payload, osp::YieldSleepStrategy> executor(2);
  executor.Start();

  // 5. 等待关闭信号
  shutdown.WaitForShutdown();
  executor.Stop();
  io_thread.join();
  return 0;
}
```

**线程分布**:

```
线程 0 (main):       等待 shutdown
线程 1 (io_thread):  IoPoller + sensor recv + Publish
线程 2 (executor):   ProcessBatch → StaticNode dispatch (CPU 2 绑定)
线程 3 (shell):      DebugShell telnet (可选)
合计: 3-4 个线程，确定性
```

**消息流**:

```
sensor_fd (硬件) → IoPoller → SensorReading → AsyncBus
                                                │
    processor (StaticNode) ←────────────────────┘
        │
        └→ ProcessedResult → AsyncBus
                                │
    alarm (StaticNode) ←────────┘
        │
        └→ AlarmEvent (如果质量低)
```

## 8. 与传统方案的完整对比

| 维度 | 多线程+协程 | epoll 回调 | newosp 事件总线 |
|------|-----------|-----------|----------------|
| 线程数 | 不可控 | 1 (事件循环) + N (处理) | 编译期确定 (4-8) |
| 堆分配 | 协程栈 + std::function | 回调闭包 | 零 (FixedFunction SBO) |
| 类型安全 | void* 或 variant | 自行管理 | std::variant 编译期路由 |
| 背压 | 无 | 自行实现 | 优先级准入控制 |
| 可移植 | 依赖协程库 | Linux 专有 | poll/epoll/kqueue 自动选择 |
| 可测试 | 线程竞态难测 | 依赖 fd mock | Node 纯消息测试 |
| 编译选项 | 需要异常/RTTI | 无限制 | `-fno-exceptions -fno-rtti` |
| 延迟 (P50) | ~1-10 us (上下文切换) | ~1 us (回调) | ~2 ns (StaticNode) |
| 调试 | gdb 多线程 | strace + ltrace | DebugShell + Dump() |

## 9. 总结: 嵌入式并发的第四条路

传统并发模型各有其适用领域:

- **多线程 + 协程**: 适合 Web 服务器、网络代理等高并发 I/O 密集场景
- **I/O 多路复用 (epoll)**: 适合网络服务器的海量连接管理
- **异步 I/O (libaio/io_uring)**: 适合存储系统的块设备高吞吐

但嵌入式系统的约束集 (确定性线程数 + 零堆分配 + 跨平台 + `-fno-exceptions`) 需要**第四条路**: 事件驱动消息总线。

newosp 的核心设计选择:

| 嵌入式约束 | newosp 的回答 |
|-----------|--------------|
| 确定性线程数 | Executor/WorkerPool 编译期线程预算 |
| 零堆分配热路径 | CAS 预分配环形缓冲 + FixedFunction SBO |
| 跨平台 I/O | IoPoller (epoll/kqueue/poll 编译期选择) |
| 类型安全消息 | std::variant + 编译期类型路由 |
| 编译期分发 | StaticNode + std::visit 内联 |
| 背压控制 | 优先级准入 (LOW/MEDIUM/HIGH 阈值) |
| 最小锁粒度 | MPSC CAS (发布无锁) + SPSC (工人无锁) |
| 可调试性 | DebugShell + Dump() + GetStats() |

这不是"替代"传统方案，而是针对嵌入式约束的**特化融合**: 用消息总线替代共享锁、用固定线程池替代动态协程、用编译期分发替代运行时回调。最终在 4-8 个线程内，实现了确定性微秒级消息延迟和可计算的内存预算。

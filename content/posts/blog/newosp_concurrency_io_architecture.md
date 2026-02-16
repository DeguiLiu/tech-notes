---
title: "嵌入式并发的第四条路: 为什么多线程、协程和 epoll 都不够用"
date: 2026-02-17
draft: false
categories: ["blog"]
tags: ["C++17", "concurrency", "I/O", "epoll", "poll", "select", "coroutine", "state-threads", "aio", "io_uring", "event-driven", "embedded", "ARM-Linux", "newosp"]
summary: "嵌入式 ARM-Linux 系统需要同时处理传感器、协议、控制、网络等并发任务。业界有三条经典路径: 多线程+协程、I/O 多路复用、异步 I/O。本文逐一分析它们在嵌入式场景中的局限: 协程的栈内存不可控与 -fno-exceptions 冲突、epoll 的 Linux 专有与回调耦合、异步 I/O 仅限块设备。在此基础上，提出嵌入式并发的第四条路: 事件驱动消息总线 + 固定线程预算 + 可移植 I/O 抽象，并以 newosp 架构为例展示这一方案如何在 4-8 个线程内实现零堆分配和微秒级确定性延迟。"
ShowToc: true
TocOpen: true
---

> 配套代码: [DeguiLiu/newosp](https://github.com/DeguiLiu/newosp) -- header-only C++17 嵌入式基础设施库
>
> 相关文章:
> - [工业传感器数据流水线: newosp C++17 零堆分配事件驱动架构实战](../newosp_event_driven_architecture/) -- AsyncBus/HSM/SPSC 核心实现详解
> - [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- newosp SPSC 的逐行代码分析
> - [无锁编程核心原理: 从 CAS 到三种队列模式](../lockfree_programming_fundamentals/) -- CAS/MPSC/SPSC 原理
> - [共享内存进程间通信](../shm_ipc_newosp/) -- ShmRingBuffer I/O 集成
>
> CSDN 原文:
> - [C++ 多线程与协程优化阻塞型任务](https://blog.csdn.net/stallion5632/article/details/143887766)
> - [Linux I/O 多路复用与异步 I/O 对比](https://blog.csdn.net/stallion5632/article/details/143675999)

## 1. 问题: 嵌入式并发的三条路

嵌入式 ARM-Linux 系统 (激光雷达、机器人、边缘计算) 需要同时处理传感器采集、协议解析、控制逻辑、网络通信等并发任务。业界有三条经典路径，它们各解决了部分问题，但都留下了嵌入式特有的缺口。

### 1.1 路径一: 多线程 + 协程

#### 多线程的代价

多线程是最直观的并发模型: 每个任务一个线程，阻塞等待 I/O 或计算。但线程创建/销毁的开销和上下文切换的不确定性在嵌入式场景中不可接受:

```cpp
// 传统多线程: 每个传感器一个线程
void sensor_thread(int sensor_id) {
    while (running) {
        auto data = blocking_read(sensor_fd);  // 阻塞等待
        process(data);
        std::this_thread::sleep_for(1ms);       // 让出 CPU → 触发内核调度
    }
}
// 10 个传感器 = 10 个线程 = 不可控的上下文切换
```

问题不在于线程本身，而在于**线程数随任务数增长**。嵌入式系统的 CPU 核心数通常是 2-4 个，10+ 线程意味着频繁的上下文切换，每次切换 ~1-5us，累积后直接影响实时性。

#### 协程的优势

协程通过用户态上下文切换减少内核介入。使用 state-threads 库 (基于 `setjmp`/`longjmp` + `epoll` 实现的有栈协程) 进行的矩阵乘法实验展示了这一优势:

**实验设计**: 1000x1000 矩阵乘法，每次内积计算后插入 1us 阻塞 (`sleep_for(1us)` vs `st_usleep(1)`) 模拟传感器采集中的 I/O 等待。

```
纯多线程 (4 线程):              14.91 秒
多线程 + 协程 (4 线程 x 10 协程): 3.32 秒   ← 4.5x 加速
```

4.5 倍加速的来源: `std::this_thread::sleep_for(1us)` 每次都触发内核调度器 (futex → schedule → context_switch)，而 `st_usleep(1)` 仅在用户态切换协程上下文 (保存/恢复寄存器，无内核介入)。当阻塞操作频繁时，内核调度的累积开销远超用户态切换。

#### 协程在嵌入式的三个根本问题

尽管协程在阻塞密集场景下性能优异，但在 ARM-Linux 嵌入式系统中有三个根本障碍:

**问题一: 栈内存不可控**

有栈协程 (stackful coroutine) 需要为每个协程预分配独立的栈空间。state-threads 默认 64KB/协程，即使缩小到 4KB:

```
1000 个协程 x 4KB/栈 = 4MB 栈空间
```

嵌入式系统通常只有 32-256MB RAM，其中大部分被应用和 OS 占用。4MB 仅用于协程栈，这在内存预算中无法被接受。更关键的是，**栈深度在编译期无法精确预测** -- 如果某个协程的调用链比预期深，就会栈溢出，且调试困难。

C++20 无栈协程 (stackless, `co_await`) 不分配独立栈，但:
- 需要 `-std=c++20`，嵌入式工具链 (GCC 9/10) 支持不完整
- coroutine frame 仍然可能堆分配 (compiler-dependent)
- 生态碎片化严重，缺乏嵌入式验证的协程运行时

**问题二: 与 `-fno-exceptions` 冲突**

嵌入式 C++ 编译通常带 `-fno-exceptions -fno-rtti` 以减小二进制体积和避免不确定性。但协程库的兼容性:

| 协程库 | 实现方式 | `-fno-exceptions` |
|--------|---------|-------------------|
| state-threads | `setjmp`/`longjmp` + epoll | 兼容 (纯 C) |
| libco (腾讯) | 汇编上下文切换 | 基本兼容 |
| Boost.Fiber | C++ 模板 + 异常 | **不兼容** |
| Boost.Coroutine2 | C++ 模板 + 异常 | **不兼容** |
| C++20 coroutines | 编译器内置 | 依赖实现 |

兼容的选项 (state-threads, libco) 都是纯 C 库，与 C++17 类型系统 (variant, optional, constexpr) 没有集成。

**问题三: 生态碎片化**

Baidu Apollo 的 Cyber RT 使用了自研的非共享栈、非对称、汇编实现的协程。协程库的实现方式差异巨大:

| 维度 | 选项 |
|------|------|
| 栈模型 | 共享栈 (stackless) vs 非共享栈 (stackful) |
| 对称性 | 对称 (协程间互相让渡) vs 非对称 (只能让渡给调用者) |
| 上下文切换 | ucontext (有系统调用) vs 汇编 (零内核介入) vs setjmp/longjmp |

每种组合有不同的 API、不同的内存模型、不同的调试方式。嵌入式项目一旦选择了某个协程库，就很难迁移。

### 1.2 路径二: I/O 多路复用

#### Linux 五种 I/O 模型

Linux 提供了五种 I/O 模型，理解它们的区别是选择并发策略的基础:

**阻塞 I/O (Blocking I/O)**: 进程发起 `read()` 后被挂起，直到数据就绪并拷贝到用户空间。最简单，但单线程只能服务一个 fd。

**非阻塞 I/O (Non-Blocking I/O)**: `read()` 在数据未就绪时立即返回 `EAGAIN`，进程需要不断轮询。避免了阻塞，但忙轮询浪费 CPU。

**信号驱动 I/O (Signal-Driven I/O)**: 通过 `SIGIO` 信号通知数据就绪，进程在信号处理函数中读取数据。减少了轮询，但信号处理的可重入性和优先级反转问题使其在复杂系统中不可靠。

**I/O 多路复用 (I/O Multiplexing)**: 通过 `select()`/`poll()`/`epoll()` 同时监控多个 fd，当某个 fd 就绪时通知进程。**这是网络 I/O 的主流方案**。

**异步 I/O (Asynchronous I/O)**: 进程发起 `aio_read()` 后立即返回，数据就绪后内核直接拷贝到用户空间并通知进程。进程不参与数据拷贝。

```
             阻塞I/O     非阻塞I/O   信号驱动    多路复用     异步I/O
等待数据:    阻塞        轮询        信号通知    select/poll  不参与
拷贝数据:    阻塞        阻塞        阻塞        阻塞         不参与
进程状态:    全程挂起    忙等        被动通知    批量等待     全程非阻塞
```

前四种模型中，数据拷贝阶段进程都是阻塞的 (区别只在"等待数据"阶段)。真正的异步 I/O 是第五种，进程完全不参与等待和拷贝。

#### select / poll / epoll 对比

```cpp
// select: 位图扫描，fd 上限 1024
fd_set read_fds;
FD_ZERO(&read_fds);
FD_SET(fd1, &read_fds);
FD_SET(fd2, &read_fds);
select(max_fd + 1, &read_fds, NULL, NULL, &timeout);
// 每次调用都要重建 fd_set，O(n) 扫描

// poll: pollfd 数组，无 fd 上限，但仍 O(n) 扫描
struct pollfd fds[2] = {
    {fd1, POLLIN, 0},
    {fd2, POLLIN, 0}
};
poll(fds, 2, timeout_ms);

// epoll: 内核红黑树维护 fd，O(1) 事件通知
int epfd = epoll_create1(0);
struct epoll_event ev = {.events = EPOLLIN, .data.fd = fd1};
epoll_ctl(epfd, EPOLL_CTL_ADD, fd1, &ev);
epoll_wait(epfd, events, max_events, timeout_ms);
// 只返回就绪的 fd，不扫描全量
```

| 特性 | select | poll | epoll |
|------|--------|------|-------|
| fd 上限 | 1024 (FD_SETSIZE) | 无限制 | 无限制 |
| 就绪检测 | O(n) 扫描位图 | O(n) 扫描数组 | O(1) 就绪链表 |
| fd 传递 | 每次拷贝 fd_set 到内核 | 每次拷贝 pollfd 到内核 | 注册一次，内核维护 |
| 水平/边沿触发 | 仅水平触发 | 仅水平触发 | 支持 ET (边沿触发) |
| 可移植性 | POSIX 标准 | POSIX 标准 | **Linux 专有** |

对高并发网络 I/O，epoll 是公认最优方案。但嵌入式场景的问题不在于并发连接数 (通常 < 100)，而在于:

#### epoll 在嵌入式的局限

**不可移植**: RT-Thread、FreeRTOS 等 RTOS 只支持 POSIX `poll()`/`select()`，不支持 `epoll`。如果核心逻辑绑定 epoll，就无法移植到 MCU 平台。

**仅管 I/O 就绪**: epoll 告诉你"fd 可读了"，但不解决消息路由、类型安全、背压控制等应用层问题。你仍然需要在 epoll 回调里手写消息分发和队列管理。

**回调耦合**: 复杂业务逻辑在 epoll 回调中层层嵌套 (epoll_wait → recv → parse → dispatch → process)，难以测试和维护。

### 1.3 路径三: 异步 I/O

Linux 异步 I/O 有三种实现:

**POSIX aio** (`aio_read`/`aio_write`): 用户空间模拟，内部创建线程池执行同步 I/O。代码示例:

```cpp
struct aiocb aio;
memset(&aio, 0, sizeof(aio));
aio.aio_fildes = fd;
aio.aio_buf = buffer;
aio.aio_nbytes = 1024;
aio.aio_offset = 0;

aio_read(&aio);  // 立即返回

// 轮询等待完成
while (aio_error(&aio) == EINPROGRESS) {
    // 可以做其他事情
}
ssize_t ret = aio_return(&aio);  // 获取结果
```

POSIX aio 看起来是异步的，但实际上内部创建了线程池来执行同步 `read()`。线程数不可控，且在某些 glibc 实现中性能并不比同步 I/O + 线程池好。

**libaio** (`io_submit`/`io_getevents`): 内核态真异步，直接与块设备层交互。但:
- **仅支持块设备** (需要 `O_DIRECT` 打开文件)
- 不支持网络 socket
- 不支持 buffered I/O

**io_uring** (Linux 5.1+): 通用的异步 I/O 框架，支持网络、文件、定时器。性能优异，但:
- 需要 Linux 5.1+ 内核
- 嵌入式内核版本普遍 4.x
- API 复杂 (submission/completion queue pair)
- 安全漏洞频繁 (多个 CVE)

| 方案 | 适用范围 | 嵌入式问题 |
|------|----------|-----------|
| POSIX aio | 文件/块设备 | 内部线程池，线程数不可控 |
| libaio | 块设备 (O_DIRECT) | 不支持 socket，不支持 buffered I/O |
| io_uring | 通用 (5.1+) | 内核版本要求高，API 复杂，安全漏洞多 |

**结论**: 对网络 I/O (UDP/TCP)，I/O 多路复用仍是最佳选择; 异步 I/O 仅建议用于磁盘/块设备操作。

### 1.4 缺口分析: 嵌入式需要什么

三条路径各解决了部分问题，但嵌入式需要的融合特性没有任何一条路径单独提供:

| 嵌入式需求 | 多线程+协程 | I/O 多路复用 | 异步 I/O |
|-----------|:---------:|:---------:|:-------:|
| 确定性线程数 (4-8) | 协程数不可控 | 需手动管理 | 线程池不可控 |
| 零堆分配热路径 | 协程栈分配 | 不涉及 | aio 控制块 |
| 跨平台 I/O | 协程库不可移植 | epoll 不可移植 | io_uring 不可移植 |
| 类型安全消息路由 | 不涉及 | 不涉及 | 不涉及 |
| `-fno-exceptions` | 多数库不兼容 | 兼容 | 兼容 |
| 编译期分发 | 不涉及 | 不涉及 | 不涉及 |

需要的是**第四条路**: 将 I/O 就绪通知 (多路复用的优势) 与类型安全的消息传递 (无锁队列 + variant) 解耦组合，在编译期确定的线程预算内完成。

## 2. 第四条路: 事件驱动消息总线

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
    │  SingleThread     │ │  PinnedExecutor │ │   WorkerPool     │
    │  Executor         │ │  (CPU 亲和性)   │ │  (Dispatcher + N │
    │  (阻塞当前线程)   │ │  RealtimeExec   │ │   Worker 线程)   │
    └──────────────────┘ └─────────────────┘ └──────────────────┘
                                   │
                        ┌──────────▼───────────────────────┐
                        │       IoPoller (I/O 事件循环)     │
                        │  Linux: epoll  │  通用: poll()    │
                        │  macOS: kqueue │  RT-Thread: poll │
                        └──────────────────────────────────┘
```

### 2.1 设计选择

| 问题 | 传统方案 | newosp 方案 |
|------|---------|------------|
| 线程间通信 | `mutex` + `std::queue` (堆分配) | CAS 无锁 MPSC + 预分配环形缓冲 |
| 回调注册 | `std::function` (可能堆分配) | FixedFunction SBO (编译期拒绝超限) |
| 消息类型安全 | `void*` 或 `std::any` | `std::variant` + `std::visit` 编译期路由 |
| I/O 抽象 | 直接用 epoll | IoPoller (epoll/kqueue/poll 编译期选择) |
| 线程数 | 随任务增长 | 编译期常量 (Executor + WorkerPool 配置) |
| 背压控制 | 无界队列或阻塞 | 优先级准入 (60%/80%/99% 阈值) |

### 2.2 线程预算

| 组件 | 线程数 | 职责 |
|------|--------|------|
| TimerScheduler | 1 | 定时任务调度 |
| DebugShell | 1+2 | TCP telnet 监听 + 会话 |
| Executor | 1 | 消息调度 (SpinOnce 循环) |
| WorkerPool | 1+N | Dispatcher + N Worker |
| **合计** | **4-8** | 全部确定性，编译期可计算 |

关键: `Publish()` → `ProcessBatch()` → 回调执行的整条热路径中，没有一次 `malloc`/`free` 调用。这通过三个机制保证: (1) 环形缓冲预分配 (2) FixedFunction SBO (3) `std::variant` 值语义。

AsyncBus、HSM、SPSC 的实现细节见 [工业传感器数据流水线: newosp 事件驱动架构实战](../newosp_event_driven_architecture/)。

## 3. I/O 集成: 可移植的事件循环

### 3.1 IoPoller: 编译期选择后端

前面分析了 epoll 的不可移植问题。newosp 的 IoPoller 通过编译期条件选择后端，API 完全统一:

```cpp
osp::IoPoller poller;

// 注册 fd (可读事件)
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
    }
}
```

后端选择:

| 平台 | 后端 | 复杂度 |
|------|------|--------|
| Linux | epoll | O(1) |
| macOS/BSD | kqueue | O(1) |
| 通用 POSIX | poll | O(n) |
| RT-Thread | poll (POSIX 层) | O(n) |

嵌入式 fd 数量通常 < 100，O(n) 的 poll 在这个规模下与 O(1) 的 epoll 差异可忽略。IoPoller 保证了核心逻辑可移植到 RT-Thread，同时在 Linux 上自动使用 epoll 获得最佳性能。

### 3.2 IoPoller + 消息总线: 职责分离

I/O 线程只负责**数据接收和消息发布**，不处理业务逻辑:

```
IoPoller                           AsyncBus
   │                                  │
   │ Wait() → fd readable             │
   │                                  │
   ├──→ recv(fd, buf, len)            │
   ├──→ 帧解析 (FrameHeader)          │
   ├──→ 反序列化为 SensorData          │
   └──→ node.Publish(SensorData{...}) ─┤
                                       │
                              ProcessBatch() → StaticNode 回调
```

这个分离带来两个好处:
1. **I/O 线程极轻量**: 只做 recv + publish，不持有业务状态，不需要锁
2. **业务逻辑可测试**: StaticNode 的 Handler 接收消息和 Header，完全不依赖 socket/fd，可以用纯消息驱动测试

## 4. 端到端示例: 传感器采集系统

```cpp
// 消息类型 (POD, trivially_copyable)
struct SensorReading { uint32_t sensor_id; float value; uint64_t timestamp_ns; };
struct ProcessedResult { uint32_t sensor_id; float filtered_value; uint8_t quality; };
struct AlarmEvent { uint32_t sensor_id; uint8_t level; char message[64]; };

using Payload = std::variant<SensorReading, ProcessedResult, AlarmEvent>;

// Handler (编译期绑定，零间接调用)
struct ProcessingHandler {
    osp::AsyncBus<Payload>& bus;
    void operator()(const SensorReading& r, const osp::MessageHeader&) {
        ProcessedResult result{r.sensor_id, filter(r.value), 95};
        bus.Publish(Payload(result), /*sender_id=*/2);
    }
    template <typename T>
    void operator()(const T&, const osp::MessageHeader&) {}
};

int main() {
    auto& shutdown = osp::ShutdownManager::Instance();

    // StaticNode: 编译期 Visitor，std::visit 直接跳转 (~2ns/msg)
    ProcessingHandler handler{osp::AsyncBus<Payload>::Instance()};
    osp::StaticNode<Payload, ProcessingHandler> processor("processor", 2, handler);

    // I/O 线程: IoPoller → recv → Publish (与业务解耦)
    std::thread io_thread([&]() {
        osp::IoPoller poller;
        poller.Add(sensor_fd, osp::IoEvent::kReadable);
        while (!shutdown.IsShutdown()) {
            auto result = poller.Wait(100);
            if (result.has_value() && result.value() > 0) {
                SensorReading reading = read_sensor(sensor_fd);
                osp::Node<Payload>("sensor_io", 1).Publish(reading);
            }
        }
    });

    // 消息调度: CPU 2 绑核
    osp::PinnedExecutor<Payload, osp::YieldSleepStrategy> executor(2);
    executor.Start();

    shutdown.WaitForShutdown();
    executor.Stop();
    io_thread.join();
}
```

线程分布:

```
线程 0 (main):       等待 shutdown
线程 1 (io_thread):  IoPoller + sensor recv + Publish
线程 2 (executor):   ProcessBatch → StaticNode dispatch (CPU 2 绑定)
合计: 3 个线程，确定性
```

## 5. 对比总结

| 维度 | 多线程+协程 | epoll 回调 | newosp 事件总线 |
|------|-----------|-----------|----------------|
| 线程数 | 不可控 | 1 (事件循环) + N (处理) | 编译期确定 (4-8) |
| 堆分配 | 协程栈 + std::function | 回调闭包 | 零 (FixedFunction SBO) |
| 类型安全 | void* 或 variant | 自行管理 | std::variant 编译期路由 |
| 背压 | 无 | 自行实现 | 优先级准入控制 |
| 可移植 | 依赖协程库 | Linux 专有 | poll/epoll/kqueue 自动选择 |
| 可测试 | 线程竞态难测 | 依赖 fd mock | Node 纯消息测试 |
| 编译选项 | 多数需要异常/RTTI | 无限制 | `-fno-exceptions -fno-rtti` |
| 延迟 (P50) | ~1-10 us (上下文切换) | ~1 us (回调) | ~2 ns (StaticNode visit) |

这不是"替代"传统方案，而是针对嵌入式约束的**特化融合**: 用消息总线替代共享锁、用固定线程池替代动态协程、用编译期分发替代运行时回调、用 IoPoller 替代 epoll 直调。最终在 4-8 个线程内实现确定性微秒级消息延迟和可计算的内存预算。

## 参考资料

1. [newosp GitHub 仓库](https://github.com/DeguiLiu/newosp) -- C++17 header-only 嵌入式基础设施库
2. [C++ 多线程与协程优化阻塞型任务](https://blog.csdn.net/stallion5632/article/details/143887766) -- state-threads 矩阵乘法实验
3. [Linux I/O 多路复用与异步 I/O 对比](https://blog.csdn.net/stallion5632/article/details/143675999) -- 五种 I/O 模型分析
4. [一顿饭的事儿，搞懂了 Linux 5 种 IO 模型](https://www.cnblogs.com/jay-huaxiao/p/12615760.html)
5. [Cyber RT 协程实现](https://zhuanlan.zhihu.com/p/365838048) -- Baidu Apollo 协程架构
6. [state-threads for Internet Applications](http://state-threads.sourceforge.net/docs/st.html)

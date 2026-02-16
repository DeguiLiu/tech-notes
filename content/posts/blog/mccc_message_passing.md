---
title: "嵌入式线程间消息传递重构: 用 MCCC 无锁消息总线替代 mutex + priority_queue"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["C++17", "MCCC", "MPSC", "lock-free", "message-bus", "SendMessage", "PostMessage", "embedded", "ring-buffer", "priority", "CAS", "zero-heap"]
summary: "本文基于一个实际的线程间消息传递需求（Windows 风格的 SendMessage/PostMessage），分析传统 mutex + priority_queue + promise/future 方案的工程缺陷，然后用 MCCC 无锁消息总线重新实现，并通过完整的测试和 Sanitizer 验证。"
ShowToc: true
TocOpen: true
---

> 原文链接: [Linux 实现一个简单的 SendMessage 和 PostMessage](https://blog.csdn.net/stallion5632/article/details/144097813)
>
> 相关文章:
> - [newosp 深度解析: C++17 事件驱动架构](../newosp_event_driven_architecture/) -- MCCC 的 C++17 演进: newosp AsyncBus
> - [无锁编程核心原理](../lockfree_programming_fundamentals/) -- MPSC 无锁队列的理论基础
> - [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- 底层环形缓冲区的设计详解
>
> 配套代码: [mccc-message-cpp](https://gitee.com/liudegui/mccc-message-cpp) -- C++17 实现，8 个 Catch2 测试，ASan+UBSan clean
>
> 依赖库: [mccc-bus](https://gitee.com/liudegui/mccc-bus) -- 无锁 MPSC 消息总线，171 个单元测试

## 1. 问题回顾：为什么需要 SendMessage / PostMessage

在嵌入式 Linux 系统和工业控制软件中，线程间消息传递是最基本的协作机制。Windows 提供了两个经典 API：

- **SendMessage**：同步消息。调用线程阻塞，直到目标线程处理完消息并返回结果。
- **PostMessage**：异步消息。消息入队后调用线程立即返回，不等待处理结果。

在 Linux 上没有原生对应物，需要自行实现。原文给出了一个基于 `std::mutex` + `std::priority_queue` + `std::promise/future` 的方案。

## 2. 原始方案分析

原文的核心数据结构：

```cpp
struct Message {
    int msg_id;
    int w_param;
    int l_param;
    std::shared_ptr<std::promise<int>> promise_ptr;  // 同步返回值
    MessageType type;  // kSync / kAsync

    // 优先级排序：同步消息优先
    bool operator<(const Message& other) const {
        return type > other.type;
    }
};

class MessageQueue {
    std::priority_queue<Message> queue_;
    std::mutex mtx_;
    std::condition_variable cv_;
    bool terminate_flag_ = false;
};
```

消息处理线程的运行方式：

```cpp
void MessageHandler() {
    while (true) {
        Message msg = g_message_queue.Dequeue();  // 阻塞等待
        int result = msg.w_param + msg.l_param;   // 处理
        if (msg.promise_ptr) {
            msg.promise_ptr->set_value(result);    // 同步返回
        }
    }
}
```

### 2.1 问题诊断

这个方案在功能上是正确的，但在以下维度存在工程问题：

| 维度 | 问题 | 影响 |
|------|------|------|
| 锁竞争 | 每次 Enqueue/Dequeue 都加 `std::mutex` | 多生产者场景下吞吐量受限于锁粒度 |
| 堆分配 | 每个同步消息创建 `std::shared_ptr<std::promise<int>>` | 热路径堆分配，延迟不可预测；嵌入式系统 heap 碎片化 |
| 优先级 | `std::priority_queue` 的 O(log n) 插入 | 队列深时开销增加；且只有"同步优先"一种策略 |
| 背压 | 无队列深度限制 | 生产者速率 > 消费者速率时，内存无限增长 |
| 类型安全 | `int msg_id` + `int w_param` | 编译期无法检查消息类型匹配 |
| 全局单例 | `MessageQueue g_message_queue` | 无法隔离多个消息域 |

### 2.2 锁竞争的具体表现

当 4 个生产者线程同时向队列 `Enqueue` 消息时，`std::mutex` 导致序列化：

```
Thread 0: [lock] [push] [unlock] ----wait---- [lock] ...
Thread 1: ----wait---- [lock] [push] [unlock] ----wait----
Thread 2: ----wait----wait---- [lock] [push] [unlock] ...
Thread 3: ----wait----wait----wait---- [lock] [push] ...
```

锁持有时间虽短（push 操作本身很快），但锁的获取和释放涉及系统调用（futex），上下文切换开销在高频场景下成为瓶颈。

### 2.3 堆分配的具体开销

```cpp
auto promise_ptr = std::make_shared<std::promise<int>>();
```

这一行代码的实际开销：

1. `std::make_shared` 调用 `operator new`（一次堆分配，约 50-200 ns）
2. 构造 `shared_ptr` 控制块（引用计数、weak 计数、deleter）
3. 构造 `std::promise`（内部包含 shared state、mutex、condition variable）
4. 析构时再次调用 `operator delete`

在嵌入式系统中，`malloc/free` 的延迟不确定性是实时性的天敌。

## 3. MCCC 消息总线

[MCCC](https://gitee.com/liudegui/mccc-bus)（Message-Centric Component Communication）是一个 header-only 的 C++17 无锁消息总线，专为嵌入式系统设计。

### 3.1 架构要点

```
Producer 0 ──┐
Producer 1 ──┤  lock-free CAS
Producer 2 ──┤  enqueue
Producer N ──┘
              ↓
+-----------------------------------------------+
|        MPSC Ring Buffer (pre-allocated)        |
|  [seq|envelope] [seq|envelope] ... [seq|env]   |
|  cache-line aligned, power-of-2 size           |
+-----------------------------------------------+
              ↓
         Consumer Thread
         ProcessBatch()
              ↓
     variant dispatch → callbacks
```

关键特性：

| 特性 | 说明 |
|------|------|
| 无锁 MPSC | 多生产者通过 CAS 原子操作入队，无 mutex |
| 零堆分配 | 消息内嵌在预分配的 Ring Buffer 中，热路径无 malloc |
| 优先级准入 | 3 级：LOW（60% 丢弃）、MEDIUM（80% 丢弃）、HIGH（99% 才丢弃） |
| 类型安全 | `std::variant` 编译期类型检查 |
| 批处理消费 | `ProcessBatch()` 一次最多处理 1024 条消息 |

### 3.2 为什么 MCCC 适合替代原始方案

| 原始方案 | MCCC 对应 |
|----------|-----------|
| `std::mutex` 保护队列 | CAS-based lock-free ring buffer |
| `std::priority_queue` 排序 | 准入控制（HIGH 优先级 ≈ 零丢弃保证） |
| `std::shared_ptr<promise>` 同步返回 | 预分配 `ResponseSlot` 池 |
| `int msg_id` 手动分发 | `std::variant` + `Subscribe<T>` 类型分发 |
| 无背压控制 | 3 级优先级准入阈值 |

## 4. 方案重构

### 4.1 消息类型定义

```cpp
// 异步消息 (PostMessage)
struct AsyncMessage {
    uint32_t msg_id;
    int32_t w_param;
    int32_t l_param;
};

// 同步请求 (SendMessage) -- 携带响应槽索引
struct SyncRequest {
    uint32_t msg_id;
    int32_t w_param;
    int32_t l_param;
    uint32_t reply_slot;  // 预分配响应槽的索引
};

using MsgPayload = std::variant<AsyncMessage, SyncRequest>;
using MsgBus = mccc::AsyncBus<MsgPayload>;
```

两种消息类型通过 `std::variant` 区分，编译期确定类型索引，分发无需运行时 switch。

### 4.2 ResponsePool -- 零堆分配的同步响应机制

MCCC 是纯异步总线（fire-and-forget），不提供内建的请求-应答模式。为实现 `SendMessage` 的同步语义，需要一个响应通道。

传统方案使用 `std::promise/std::future`，每次调用产生堆分配。重构方案使用**预分配的响应槽池**：

```cpp
// 单个响应槽：缓存行对齐，防止 false sharing
struct alignas(64) ResponseSlot {
    static constexpr uint32_t kEmpty = 0U;
    static constexpr uint32_t kPending = 1U;
    static constexpr uint32_t kReady = 2U;

    std::atomic<uint32_t> state{kEmpty};
    int32_t result{0};
};
```

状态转换流程：

```
kEmpty ──(Acquire)──> kPending ──(SetResult)──> kReady ──(WaitResult)──> kEmpty
  ^                                                                        |
  └────────────────────────────────────────────────────────────────────────┘
```

响应槽池的完整实现：

```cpp
template <uint32_t MaxSlots = 64U>
class ResponsePool {
 public:
    // 获取一个空闲槽（生产者线程调用）
    uint32_t Acquire() noexcept {
        uint32_t idx =
            next_.fetch_add(1U, std::memory_order_relaxed) & (MaxSlots - 1U);

        uint32_t expected = ResponseSlot::kEmpty;
        while (!slots_[idx].state.compare_exchange_weak(
            expected, ResponseSlot::kPending,
            std::memory_order_acquire, std::memory_order_relaxed)) {
            expected = ResponseSlot::kEmpty;
            std::this_thread::yield();
        }
        return idx;
    }

    // 写入结果并标记完成（消费者线程调用）
    void SetResult(uint32_t idx, int32_t result) noexcept {
        slots_[idx].result = result;
        slots_[idx].state.store(ResponseSlot::kReady,
                                std::memory_order_release);
    }

    // 等待结果并释放槽（生产者线程调用）
    int32_t WaitResult(uint32_t idx) noexcept {
        while (slots_[idx].state.load(std::memory_order_acquire)
               != ResponseSlot::kReady) {
            std::this_thread::yield();
        }
        int32_t result = slots_[idx].result;
        slots_[idx].state.store(ResponseSlot::kEmpty,
                                std::memory_order_release);
        return result;
    }

 private:
    ResponseSlot slots_[MaxSlots]{};
    std::atomic<uint32_t> next_{0U};
};
```

关键设计决策：

| 决策 | 原因 |
|------|------|
| 固定大小数组 | 零堆分配，编译期确定内存占用 |
| `alignas(64)` 缓存行对齐 | 不同槽位于不同缓存行，消除 false sharing |
| Round-robin 分配 | `fetch_add` 无锁递增，取模用位与（2 的幂） |
| CAS 获取空闲槽 | 多生产者竞争同一槽时自旋等待，无需全局锁 |
| `yield()` 而非 busy-wait | 让出 CPU 时间片，避免浪费 CPU 资源 |

### 4.3 PostMessage 实现

```cpp
bool PostMessage(uint32_t msg_id, int32_t w_param,
                 int32_t l_param) noexcept {
    AsyncMessage msg{msg_id, w_param, l_param};
    return MsgBus::Instance().PublishWithPriority(
        std::move(msg), sender_id_, mccc::MessagePriority::MEDIUM);
}
```

直接映射到 MCCC 的 `Publish`：消息通过 CAS 操作入队到预分配的 Ring Buffer，零堆分配，返回值表示是否入队成功（背压控制）。

### 4.4 SendMessage 实现

```cpp
int32_t SendMessage(uint32_t msg_id, int32_t w_param,
                    int32_t l_param) noexcept {
    // 1. 获取预分配的响应槽
    uint32_t slot = response_pool_.Acquire();

    // 2. 发布同步请求（HIGH 优先级 -> 99% 队列深度才丢弃）
    SyncRequest req{msg_id, w_param, l_param, slot};
    MsgBus::Instance().PublishWithPriority(
        std::move(req), sender_id_, mccc::MessagePriority::HIGH);

    // 3. 阻塞等待消费者写入结果
    return response_pool_.WaitResult(slot);
}
```

对比原始方案的 `SendMessage`：

```cpp
// 原始方案 -- 每次堆分配
int SendMessage(int msg_id, int w_param, int l_param) {
    auto promise_ptr = std::make_shared<std::promise<int>>();  // 堆分配
    std::future<int> fut = promise_ptr->get_future();
    // ... enqueue ...
    return fut.get();  // 阻塞
}

// MCCC 方案 -- 零堆分配
int32_t SendMessage(uint32_t msg_id, int32_t w_param,
                    int32_t l_param) noexcept {
    uint32_t slot = response_pool_.Acquire();  // 预分配槽
    // ... publish ...
    return response_pool_.WaitResult(slot);    // 原子等待
}
```

### 4.5 消费者端处理

```cpp
void RegisterHandler(MessageHandlerFn handler) noexcept {
    handler_ = handler;

    // 订阅异步消息
    MsgBus::Instance().Subscribe<AsyncMessage>(
        [this](const MsgEnvelope& env) {
            const auto* msg = std::get_if<AsyncMessage>(&env.payload);
            if (msg != nullptr && handler_ != nullptr) {
                handler_(msg->msg_id, msg->w_param, msg->l_param);
            }
        });

    // 订阅同步请求（处理后写回响应槽）
    MsgBus::Instance().Subscribe<SyncRequest>(
        [this](const MsgEnvelope& env) {
            const auto* req = std::get_if<SyncRequest>(&env.payload);
            if (req != nullptr && handler_ != nullptr) {
                int32_t result =
                    handler_(req->msg_id, req->w_param, req->l_param);
                response_pool_.SetResult(req->reply_slot, result);
            }
        });
}
```

消费者线程通过 `ProcessBatch()` 批量处理：

```cpp
void Start() noexcept {
    running_.store(true, std::memory_order_release);
    consumer_thread_ = std::thread([this]() {
        while (running_.load(std::memory_order_acquire)) {
            uint32_t processed = MsgBus::Instance().ProcessBatch();
            if (processed == 0U) {
                std::this_thread::sleep_for(std::chrono::microseconds(100));
            }
        }
        // 退出前清空残留消息
        while (MsgBus::Instance().ProcessBatch() > 0U) {}
    });
}
```

## 5. 完整使用示例

### 5.1 基础用法（对标原文 main()）

```cpp
#include <mccc_message/message_service.hpp>

static int32_t MessageHandler(uint32_t msg_id, int32_t w_param,
                               int32_t l_param) {
    printf("Processing Message ID: %u, wParam: %d, lParam: %d\n",
           msg_id, w_param, l_param);
    return w_param + l_param;
}

int main() {
    mccc_msg::MessageService<> service(1U);
    service.RegisterHandler(MessageHandler);
    service.Start();

    // 同步消息（SendMessage）
    int32_t result = service.SendMessage(1U, 10, 20);
    printf("SendMessage result: %d\n", result);  // 输出: 30

    // 异步消息（PostMessage）
    service.PostMessage(2U, 30, 40);

    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    service.Stop();
    return 0;
}
```

输出：

```
Processing Message ID: 1, wParam: 10, lParam: 20
SendMessage result: 30
Processing Message ID: 2, wParam: 30, lParam: 40
```

### 5.2 多生产者并发

```cpp
constexpr uint32_t kNumProducers = 4U;
constexpr uint32_t kMessagesPerProducer = 100U;

std::vector<std::thread> producers;
for (uint32_t p = 0U; p < kNumProducers; ++p) {
    producers.emplace_back([&service, p]() {
        for (uint32_t i = 0U; i < kMessagesPerProducer; ++i) {
            int32_t wp = static_cast<int32_t>(p * 1000U + i);
            if (i % 5U == 0U) {
                // 每 5 条消息发一次同步请求
                int32_t r = service.SendMessage(2U, wp, i);
                assert(r == wp + static_cast<int32_t>(i));
            } else {
                service.PostMessage(1U, wp, i);
            }
        }
    });
}
```

4 个生产者线程同时向同一个 MCCC Bus 发送消息，无锁竞争。

### 5.3 优先级准入控制

```cpp
// HIGH: 99% 队列深度才丢弃
service.PostMessageWithPriority(1U, 0, 0, mccc::MessagePriority::HIGH);

// MEDIUM: 80% 队列深度丢弃（默认）
service.PostMessage(1U, 0, 0);

// LOW: 60% 队列深度丢弃
service.PostMessageWithPriority(1U, 0, 0, mccc::MessagePriority::LOW);
```

## 6. 数据流对比

### 6.1 原始方案的数据流

```
Producer                       Consumer
   |                              |
   | mutex.lock()                 |
   | priority_queue.push()        |
   | mutex.unlock()               |
   | cv.notify_one()              |
   |                              | cv.wait()
   |                              | mutex.lock()
   |                              | priority_queue.top()
   |                              | priority_queue.pop()
   |                              | mutex.unlock()
   |                              | handler()
   |                              | promise.set_value()
   | future.get()                 |
   |   (context switch)           |
```

关键开销点：

1. `mutex.lock()` / `unlock()` -- 2 次系统调用（futex）
2. `cv.notify_one()` -- 唤醒阻塞线程（上下文切换）
3. `priority_queue.push()` -- O(log n) 堆调整
4. `promise.set_value()` -- 内部包含 mutex + cv

### 6.2 MCCC 方案的数据流

```
Producer                       Consumer
   |                              |
   | CAS enqueue (lock-free)      |
   |                              | ProcessBatch()
   |                              |   load sequence (acquire)
   |                              |   variant dispatch
   |                              |   handler()
   |                              |   store sequence (release)
   |                              |
   | [SendMessage only:]          | [SyncRequest only:]
   | response_pool_.WaitResult()  | response_pool_.SetResult()
   |   atomic load (spin)         |   atomic store (release)
```

关键优势：

1. CAS 入队 -- 无系统调用，无上下文切换
2. `ProcessBatch()` -- 批量处理最多 1024 条，摊薄开销
3. `ResponseSlot` -- 纯原子操作，无 mutex/cv 开销

## 7. 测试验证

### 7.1 测试覆盖

| 测试文件 | 测试内容 | 用例数 |
|----------|----------|--------|
| `test_response_pool.cpp` | ResponseSlot 获取/释放/并发 | 2 |
| `test_post_message.cpp` | 异步消息投递和接收 | 1 |
| `test_send_message.cpp` | 同步请求-应答/并发正确性 | 3 |
| `test_multi_producer.cpp` | 多生产者混合同步异步/生命周期 | 2 |

### 7.2 测试结果

```
$ ctest --output-on-failure
1/8 ResponsePool basic acquire/release ......... Passed   0.00 sec
2/8 ResponsePool concurrent acquire/release .... Passed   0.00 sec
3/8 PostMessage delivers async messages ........ Passed   0.17 sec
4/8 SendMessage returns handler result ......... Passed   0.02 sec
5/8 SendMessage with different handlers ........ Passed   0.02 sec
6/8 SendMessage concurrent from multiple threads Passed   0.02 sec
7/8 Multi-producer mixed sync/async ............ Passed   0.22 sec
8/8 Service lifecycle .......................... Passed   0.02 sec

100% tests passed, 0 tests failed out of 8
```

### 7.3 Sanitizer 验证

```
$ cmake .. -DCMAKE_CXX_FLAGS="-fsanitize=address,undefined"
$ make && ctest
100% tests passed (ASan + UBSan clean)
```

## 8. 对比总结

| 维度 | 原始方案 | MCCC 方案 |
|------|----------|-----------|
| 入队复杂度 | O(log n)（priority_queue push） | O(1)（CAS + 位与） |
| 同步机制 | `mutex` + `condition_variable` | 无锁 CAS（MPSC） |
| 同步返回值 | `shared_ptr<promise<int>>`（堆分配） | `ResponseSlot`（预分配，零堆） |
| 优先级策略 | 二元（sync > async） | 三级准入控制（HIGH/MEDIUM/LOW） |
| 背压控制 | 无（无限增长） | 队列深度阈值自动丢弃 |
| 类型安全 | `int` 手动匹配 | `std::variant` 编译期检查 |
| 多生产者扩展性 | 受 mutex 锁粒度限制 | CAS 无锁，线性扩展 |
| 缓存友好性 | `priority_queue` 堆结构（随机访问） | Ring Buffer 顺序访问（缓存行对齐） |
| 内存布局 | 堆碎片化风险 | 固定连续内存块 |

## 9. 移植到嵌入式目标

MCCC 设计时就考虑了嵌入式目标。通过编译期宏可以适配不同硬件：

```bash
# 单生产者 SPSC 模式（跳过 CAS，更低延迟）
cmake .. -DCMAKE_CXX_FLAGS="-DMCCC_SINGLE_PRODUCER=1"

# 单核 MCU 模式（relaxed 原子 + signal_fence）
cmake .. -DCMAKE_CXX_FLAGS="-DMCCC_SINGLE_CORE=1 \
  -DMCCC_I_KNOW_SINGLE_CORE_IS_UNSAFE=1"

# 缩小队列深度（节省 RAM）
cmake .. -DCMAKE_CXX_FLAGS="-DMCCC_QUEUE_DEPTH=4096"
```

| 配置 | 适用场景 | 队列 RAM |
|------|----------|----------|
| 默认 MPSC | 多核 Linux，多生产者 | 约 8 MB（131072 槽） |
| SPSC | 单生产者，更低延迟 | 约 8 MB |
| SPSC + 4096 | 嵌入式 MCU，RAM 受限 | 约 256 KB |
| 单核 + SPSC + 4096 | Cortex-M 裸机 | 约 256 KB |

## 10. 总结

原文的 `mutex + priority_queue + promise/future` 方案是一个功能正确的教学实现，但在多生产者高频场景下存在锁竞争、堆分配、缺乏背压控制等工程问题。

用 MCCC 无锁消息总线重构后：

1. **PostMessage** 直接映射到 `bus.Publish()`，零堆分配，O(1) 入队
2. **SendMessage** 通过预分配 `ResponsePool` 实现同步返回，替代 `shared_ptr<promise>`
3. **优先级** 从手动排序升级为 MCCC 内建的 3 级准入控制，同步消息使用 HIGH 优先级
4. **多生产者** 从 mutex 序列化升级为 CAS 无锁并发

配套代码 [mccc-message-cpp](https://gitee.com/liudegui/mccc-message-cpp) 提供了完整的实现、示例和测试，可直接编译运行验证。

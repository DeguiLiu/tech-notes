---
title: "基于无锁消息总线的观察者模式: 零堆分配、单线程消费"
date: 2026-02-17T14:00:00
draft: false
categories: ["practice"]
tags: ["C++17", "lock-free", "observer-pattern", "embedded", "MPSC", "zero-allocation"]
summary: "基于无锁 MPSC 消息总线，实现嵌入式场景下的数据分发架构。提供两种方案: Component 动态订阅版和 StaticComponent 零开销编译期分发版。单文件 ~100 行，零堆分配，单 worker 线程处理所有订阅者。"
ShowToc: true
TocOpen: true
---

> 数据分发 (一个数据源，多个订阅者) 是嵌入式系统中的常见需求。本文基于无锁 MPSC 消息总线，提供两种实现方案: 支持运行时动态增删订阅者的 Component 版，以及追求零开销的 StaticComponent 编译期分发版。单文件 ~100 行，零堆分配，单 worker 线程。完整代码: [data-visitor-dispatcher](https://gitee.com/liudegui/data-visitor-dispatcher)。

## 1. 数据分发架构

数据分发的核心模型: 数据源 (Receiver) 产生消息，分发器 (Bus) 路由到多个订阅者 (Visitor)，每个订阅者独立处理。

```
Receiver (数据源)
    │
    ▼
AsyncBus (无锁 MPSC Ring Buffer)
    │
    ├──▶ LoggingVisitor   (记录日志)
    ├──▶ ProcessingVisitor (数据处理)
    └──▶ ...更多订阅者
```

核心设计决策:

| 决策 | 方案 | 原因 |
|------|------|------|
| 并发同步 | lock-free CAS (MPSC) | 多生产者无锁并发发布，避免 mutex 串行化 |
| 消息存储 | Ring Buffer 嵌入 | 定长、零堆分配、内置背压 |
| 线程模型 | 单 worker 线程 | `ProcessBatch()` 一次遍历处理所有消息，线程数 O(1) |
| 字符串 | `FixedString<N>` 栈缓冲 | 替代 `std::string`，消除热路径堆分配 |
| 类型路由 | `std::variant` + `Subscribe<T>` | 编译期类型安全，订阅者只收指定类型 |
| 回调存储 | `FixedFunction` SBO | 替代 `std::function`，零堆分配 |
| 生命周期 | `weak_ptr` 自动取消订阅 | `shared_ptr` release 即注销，无需手动管理 |

提供两种实现版本:

| 版本 | 订阅方式 | 分发机制 | 适用场景 |
|------|----------|----------|----------|
| Component 版 | 运行时动态 | `FixedFunction` SBO 回调 | 需要动态增删订阅者 |
| StaticComponent 版 | 编译期固定 | CRTP `Handle()` 内联 | 订阅者集合固定，追求零开销 |

## 2. 消息类型定义

```cpp
struct SensorData {
  int32_t id;
  mccc::FixedString<64> content;  // 64 字节栈上固定缓冲，零堆分配

  SensorData() noexcept : id(0) {}
  SensorData(int32_t id_, const char* msg) noexcept
      : id(id_), content(mccc::TruncateToCapacity, msg) {}
};

using DemoPayload = std::variant<SensorData>;
using DemoBus = mccc::AsyncBus<DemoPayload>;
using DemoComponent = mccc::Component<DemoPayload>;
```

`FixedString<64>` 在栈上预分配 64 字节，超过容量时截断 (`TruncateToCapacity` 策略)，不抛异常，不触发堆分配。

## 3. Component 版: 动态订阅

### 3.1 订阅者定义

```cpp
class LoggingVisitor : public DemoComponent {
 public:
  static std::shared_ptr<LoggingVisitor> Create() noexcept {
    std::shared_ptr<LoggingVisitor> ptr(new LoggingVisitor());
    ptr->Init();
    return ptr;
  }

 private:
  LoggingVisitor() = default;

  void Init() noexcept {
    InitializeComponent();
    SubscribeSimple<SensorData>(
        [](const SensorData& data, const mccc::MessageHeader& hdr) noexcept {
          LOG_INFO("[LoggingVisitor] msg_id=%lu id=%d content=\"%s\"",
                   hdr.msg_id, data.id, data.content.c_str());
        });
  }
};
```

`SubscribeSimple<SensorData>` 在编译期绑定消息类型，只接收 `SensorData`。回调存储在 `FixedFunction` SBO 缓冲中，零堆分配。

### 3.2 数据源与消费

```cpp
class Receiver {
 public:
  explicit Receiver(uint32_t sender_id) noexcept : sender_id_(sender_id) {}

  void ReceiveMessage(int32_t id, const char* content) noexcept {
    SensorData data(id, content);
    DemoBus::Instance().Publish(std::move(data), sender_id_);
  }

 private:
  uint32_t sender_id_;
};
```

单 worker 线程处理所有消息:

```cpp
std::thread worker([&stop_worker]() noexcept {
  while (!stop_worker.load(std::memory_order_acquire)) {
    uint32_t processed = DemoBus::Instance().ProcessBatch();
    if (processed == 0U) {
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
  }
});
```

### 3.3 动态增删订阅者

```cpp
auto logger = LoggingVisitor::Create();
auto processor = ProcessingVisitor::Create();

receiver.ReceiveMessage(1, "Hello");      // 两个 visitor 都收到
receiver.ReceiveMessage(2, "World");      // 两个 visitor 都收到

logger.reset();                           // shared_ptr release → 自动取消订阅

receiver.ReceiveMessage(3, "After");      // 只有 processor 收到
```

`shared_ptr` release 时，Component 内部的 `weak_ptr` 检测到失效，自动跳过该订阅者的回调。

### 3.4 运行输出

```
=== Receiving message #1 ===
[LoggingVisitor] msg_id=1 id=1 content="Hello, CyberRT!"
[ProcessingVisitor] msg_id=1 id=1 length=15
=== Receiving message #2 ===
[LoggingVisitor] msg_id=2 id=2 content="Another data packet."
[ProcessingVisitor] msg_id=2 id=2 length=20

=== Removing LoggingVisitor ===
=== Receiving message #3 ===
[ProcessingVisitor] msg_id=3 id=3 length=27

Statistics:
  Published: 3  Processed: 3  Dropped: 0
```

## 4. StaticComponent 版: 零开销编译期分发

### 4.1 CRTP 订阅者

```cpp
class LoggingVisitor
    : public mccc::StaticComponent<LoggingVisitor, DemoPayload> {
 public:
  void Handle(const SensorData& data) noexcept {
    LOG_INFO("[LoggingVisitor] id=%d content=\"%s\"",
             data.id, data.content.c_str());
  }
};

class ProcessingVisitor
    : public mccc::StaticComponent<ProcessingVisitor, DemoPayload> {
 public:
  void Handle(const SensorData& data) noexcept {
    LOG_INFO("[ProcessingVisitor] id=%d length=%u",
             data.id, data.content.size());
  }
};
```

`Handle()` 方法在编译期被 CRTP 基类检测和绑定，无虚函数、无间接调用。

### 4.2 CombinedVisitor: 单次遍历多路分发

```cpp
template <typename... Visitors>
class CombinedVisitor {
 public:
  explicit CombinedVisitor(Visitors&... visitors) noexcept
      : visitors_(visitors...) {}

  template <typename T>
  void operator()(const T& data) noexcept {
    DispatchAll<T>(data, std::index_sequence_for<Visitors...>{});
  }

 private:
  template <typename T, size_t... Is>
  void DispatchAll(const T& data, std::index_sequence<Is...>) noexcept {
    (std::get<Is>(visitors_).get().Handle(data), ...);  // fold expression 展开
  }

  std::tuple<std::reference_wrapper<Visitors>...> visitors_;
};
```

fold expression `(... , ...)` 在编译期将所有 visitor 的 `Handle()` 调用展开为顺序执行，编译器可以完全内联。

### 4.3 使用

```cpp
// 栈分配，零 shared_ptr，零堆分配
LoggingVisitor logger;
ProcessingVisitor processor;
CombinedVisitor combined(logger, processor);

// 单次 Ring Buffer 遍历，分发到所有 visitor
std::thread worker([&stop_worker, &combined]() noexcept {
  while (!stop_worker.load(std::memory_order_acquire)) {
    uint32_t processed = DemoBus::Instance().ProcessBatchWith(combined);
    if (processed == 0U) {
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
  }
});
```

## 5. 两种方案选型

**Component 版** -- 需要运行时灵活性:
- 订阅者集合在运行期动态变化
- 需要 `shared_ptr` 生命周期管理
- 组件可能被多个模块引用

**StaticComponent 版** -- 追求极致性能:
- 订阅者集合在编译期确定
- 嵌入式实时系统，对延迟敏感
- Handler 调用需要被编译器内联

| 维度 | Component 版 | StaticComponent 版 |
|------|-------------|-------------------|
| 代码量 | ~110 行 / 1 文件 | ~95 行 / 1 文件 |
| 堆分配 (每条消息) | 0 次 | 0 次 |
| 线程数 | 2 (worker + main) | 2 (worker + main) |
| 动态增删订阅者 | 支持 | 不支持 |
| 间接调用 | `FixedFunction` (SBO，非堆) | 无 (可内联) |
| 订阅者存储 | `shared_ptr` 堆分配 | 栈分配 |

对于大多数嵌入式应用，StaticComponent 版是更好的选择。只有在确实需要动态增删订阅者时才使用 Component 版。

## 6. 相关资源

- 完整代码: [data-visitor-dispatcher](https://gitee.com/liudegui/data-visitor-dispatcher) (MIT License)
- 消息总线: [mccc-bus](https://gitee.com/liudegui/mccc-bus) -- C++17 header-only 无锁消息总线
- 基础设施库: [newosp](https://github.com/DeguiLiu/newosp) -- 工业级嵌入式 C++17 库 (基于 mccc-bus)
- [无锁消息总线设计与实现](/posts/practice/mccc_bus_cpp17_practice/)
- [嵌入式系统中的编译期分发](/posts/pattern/compile_time_dispatch_optimization/)

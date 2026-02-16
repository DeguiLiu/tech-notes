---
title: "嵌入式 C++ 智能指针的五个陷阱与零堆分配替代方案"
date: 2026-02-16
draft: false
categories: ["pattern"]
tags: ["C++17", "smart-pointer", "embedded", "memory-management", "RAII", "lock-free", "ARM", "newosp"]
summary: "std::shared_ptr 和 std::weak_ptr 在桌面开发中是安全的默认选择，但在嵌入式实时系统中会引入原子引用计数开销、堆碎片化、不确定延迟和竞态条件等问题。本文从一个 weak_ptr 竞态 bug 出发，系统分析智能指针在嵌入式场景的五个根本陷阱，并展示 newosp C++17 基础设施库如何用 ObjectPool、FixedFunction、ScopeGuard 和 expected 实现零堆分配的确定性内存管理。"
ShowToc: true
TocOpen: true
---

> 基础设施库: [newosp](https://github.com/DeguiLiu/newosp) v0.4.0 (1114 tests, ASan/TSan/UBSan clean)
>
> 目标平台: ARM-Linux (Cortex-A53/A72/A7) | C++17, Header-only
>
> 原始案例: [C++ 智能指针失效分析](https://blog.csdn.net/stallion5632/article/details/140479753)

---

## 1. 引子: 一个 weak_ptr 竞态 Bug

以下是一个典型的生产者-消费者事件队列，队列中存储 `std::weak_ptr` 以避免循环引用:

```cpp
class EventQueue {
  std::queue<std::pair<Event, std::weak_ptr<void>>> events_;
  std::mutex mtx_;

  void consume_events(std::function<void(Event, std::shared_ptr<void>)> callback) {
    std::unique_lock<std::mutex> lck(mtx_);
    while (true) {
      cv_.wait(lck, [this] { return !events_.empty() || stop_; });

      auto event_item = events_.front();
      events_.pop();
      lck.unlock();  // 释放锁以允许其他线程推送事件

      // BUG: weak_ptr::lock() 在无锁保护下执行
      // 此时其他线程可能已销毁最后一个 shared_ptr
      callback(event_item.first, event_item.second.lock());

      lck.lock();
    }
  }
};
```

问题出在 `lck.unlock()` 和 `event_item.second.lock()` 之间: 释放互斥锁后，生产者线程中持有的 `std::shared_ptr` 可能已经析构，`weak_ptr::lock()` 返回空指针，导致回调函数访问无效数据。

原作者给出的修复方案是在持锁期间调用 `lock()`:

```cpp
// 修复: 在持锁时提升 weak_ptr
if (const auto& shared = event_item.second.lock()) {
    callback(event_item.first, shared);
}
```

这个修复是正确的，但它暴露了一个更深层的架构问题: **在嵌入式事件系统中使用 `shared_ptr` / `weak_ptr` 本身就是错误的设计选择**。

---

## 2. 五个根本陷阱

### 陷阱 1: 原子引用计数的隐性开销

`std::shared_ptr` 的引用计数使用 `std::atomic<long>` 实现。每次拷贝、赋值、析构都触发原子操作:

```cpp
// libstdc++ 简化实现
class _Sp_counted_base {
  _Atomic_word _M_use_count;   // 强引用计数
  _Atomic_word _M_weak_count;  // 弱引用计数

  void _M_add_ref_copy() {
    __gnu_cxx::__atomic_add_dispatch(&_M_use_count, 1);  // 原子加
  }

  void _M_release() {
    if (__gnu_cxx::__exchange_and_add_dispatch(&_M_use_count, -1) == 1) {
      _M_dispose();     // 销毁管理对象
      if (__gnu_cxx::__exchange_and_add_dispatch(&_M_weak_count, -1) == 1) {
        _M_destroy();   // 销毁控制块
      }
    }
  }
};
```

在 ARM Cortex-A53 上，每次 `__atomic_add` 编译为 `ldaxr` + `stlxr` + 重试循环 (LL/SC)。在多核竞争下，单次原子操作耗时从 ~5 ns 膨胀到 ~50 ns。

**根因**: `shared_ptr` 的设计目标是通用场景的安全性，它必须支持任意线程在任意时刻拷贝和销毁。这种灵活性的代价是每次操作都要经过原子读-改-写 (RMW) 路径，即使在单线程使用场景下也无法消除。

> 关于 ARM 平台原子操作的硬件实现，参见 Preshing 的 [An Introduction to Lock-Free Programming](https://preshing.com/20120612/an-introduction-to-lock-free-programming/)，其中详细解释了 Load-Link/Store-Conditional (LL/SC) 机制和 CAS 循环。

**实际影响**: 在 100 Hz 帧率的激光雷达 Pipeline 中，假设每帧经过 6 个 stage，每个 stage 拷贝一次 `shared_ptr` (入队) + 析构一次 (出队) = 12 次原子操作/帧。100 Hz x 12 = 1200 次/秒，看似不多。但如果 stage 内部将 `shared_ptr` 传递给子函数或临时存储，拷贝次数会迅速膨胀到数万次/秒。在多核竞争下，LL/SC 重试会产生不可预测的延迟尖峰。

### 陷阱 2: 控制块的堆分配与碎片化

每个 `shared_ptr` 管理的对象都有一个控制块 (control block)，存储引用计数和删除器:

```cpp
// std::make_shared 合并分配 (一次 malloc)
auto p = std::make_shared<Event>();  // sizeof(控制块) + sizeof(Event), 一次 malloc

// std::shared_ptr<T>(new T) 分离分配 (两次 malloc)
auto p = std::shared_ptr<Event>(new Event());  // Event 一次 + 控制块一次
```

即使使用 `make_shared` 合并分配，仍然是一次 `malloc` 调用。在嵌入式系统中，`malloc` 的问题不是速度，而是**碎片化**:

```
初始堆:  [████████████████████████████████] 64 KB free

分配释放 10000 次后:
         [██░░██░██░░░██░██░░██░░░██░██░░]
          ^ 碎片      ^ 碎片      ^ 碎片

此时 malloc(4096) 可能失败，即使总空闲 > 4096
```

**根因**: 通用堆分配器 (glibc malloc / dlmalloc) 为了支持任意大小的分配请求，使用 bin/arena/chunk 结构。频繁的小块分配-释放会产生外部碎片。嵌入式系统的 RAM 通常在 64 KB ~ 512 MB 之间，碎片化会直接导致内存耗尽。

### 陷阱 3: weak_ptr 竞态窗口

引子中的 bug 是 `weak_ptr` 的固有设计问题。`weak_ptr::lock()` 的语义是: "如果管理对象还存在，返回一个 `shared_ptr`; 否则返回空"。这个操作本身是线程安全的 (原子地检查并增加引用计数)，但它的结果与程序逻辑之间存在 TOCTOU (Time-of-Check to Time-of-Use) 窗口:

```
线程 A (消费者)              线程 B (生产者/所有者)
─────────────               ──────────────────
lck.unlock()
                             shared_ptr 离开作用域
                             引用计数 → 0
                             ~Event() 析构
weak.lock() → nullptr!
callback(nullptr)  → UB
```

**根因**: `weak_ptr` 的设计假设是 "观察者不拥有对象"。但在事件队列中，消费者需要在处理期间拥有数据的所有权。用 `weak_ptr` 传递所有权是语义错误 -- 它是观察工具，不是传输机制。

> 关于 TOCTOU 竞态和内存可见性问题，Preshing 在 [Memory Barriers Are Like Source Control Operations](https://preshing.com/20120710/memory-barriers-are-like-source-control-operations/) 中用源码管理系统类比解释了多线程内存交互中的可见性延迟。

### 陷阱 4: std::function 的堆逃逸

上面的事件队列使用 `std::function<void(Event, std::shared_ptr<void>)>` 作为回调类型。`std::function` 内部也有类似 `shared_ptr` 的问题:

```cpp
// libstdc++ 简化实现
class function<R(Args...)> {
  union _Any_data {
    void* _M_access;
    char _M_pod_data[sizeof(void*) * 3];  // SBO: 24 bytes (x86-64)
  };

  _Any_data _M_functor;
  _Manager_type _M_manager;  // 虚函数表指针 (类型擦除)

  // 如果 callable 大于 24 字节 → 堆分配
  template<typename Fn>
  void _M_init_functor(_Any_data& __f, Fn&& __fn) {
    if constexpr (sizeof(Fn) <= sizeof(_Any_data)) {
      ::new (&__f._M_pod_data) Fn(std::forward<Fn>(__fn));  // SBO
    } else {
      __f._M_access = new Fn(std::forward<Fn>(__fn));  // 堆分配!
    }
  }
};
```

关键问题:

1. **SBO 阈值不可控**: libstdc++ 的 SBO 通常为 24 字节 (3 个指针)，捕获 3 个以上变量的 lambda 就会堆逃逸，且没有编译期警告
2. **虚函数调用**: 类型擦除通过 `_M_manager` 虚表指针实现，每次调用多一次间接跳转
3. **拷贝开销**: `std::function` 可拷贝，每次拷贝可能触发堆分配 (复制被擦除的 callable)

**根因**: `std::function` 的设计目标是 "存储任意可调用对象"。这种通用性需要运行时多态 (虚函数/类型擦除) 和动态内存 (大 callable 堆分配)。嵌入式系统需要的是编译期已知大小、不分配内存的回调容器。

### 陷阱 5: 异常路径的不确定性

`shared_ptr` 和 `std::function` 在异常开启时会增加额外的清理路径:

```cpp
// shared_ptr 析构时如果 use_count == 1，调用删除器
// 如果删除器抛出异常 → std::terminate
~shared_ptr() noexcept {
  if (_M_pi && _M_pi->_M_release()) {
    // 调用 deleter
  }
}
```

在 `-fno-exceptions` 编译模式下 (嵌入式常见)，`shared_ptr` 仍然工作，但错误处理变成了 `std::terminate` 或未定义行为。而且 RTTI (运行时类型信息) 通常也被禁用 (`-fno-rtti`)，这使得 `std::function` 的类型擦除机制可能出现问题。

**根因**: C++ 标准库的智能指针和函数对象设计于桌面/服务器环境，假设异常和 RTTI 可用。嵌入式编译选项 (`-fno-exceptions -fno-rtti`) 切断了这些假设，迫使开发者在标准库的 "安全" API 和嵌入式的编译约束之间进行权衡。

---

## 3. newosp 的替代方案

newosp 是为 ARM-Linux 嵌入式平台设计的 C++17 header-only 基础设施库。它遵循一个核心原则:

> **栈优先分配，热路径禁止堆分配。** -- newosp 设计文档

| 原则 | 说明 |
|------|------|
| 栈优先分配 | 固定容量容器，热路径禁止堆分配 |
| 无锁或最小锁 | MPSC 无锁总线，SPSC 无锁队列，SharedMutex 读写分离 |
| 编译期分发 | 模板特化、标签分发、`if constexpr` 替代虚函数 |
| 类型安全 | `expected<V,E>` 错误处理，`NewType<T,Tag>` 强类型，`std::variant` 消息路由 |
| 嵌入式友好 | 兼容 `-fno-exceptions -fno-rtti`，固定宽度整数，缓存行对齐 |

以下是 newosp 对五个陷阱的逐一替代方案。

### 3.1 ObjectPool: 替代 shared_ptr + new

**陷阱 1 + 2 的解决方案**: 用 O(1) 固定块池替代 `shared_ptr` + 堆分配。

```cpp
// newosp: ObjectPool -- 编译期固定大小，O(1) 分配，零碎片
template <typename T, uint32_t MaxObjects>
class ObjectPool {
  FixedPool<sizeof(T), MaxObjects> pool_;   // 内嵌存储，无 malloc
  bool alive_[MaxObjects] = {};             // 存活位图

 public:
  // O(1) 分配: 从 free list 头部取块 + placement new
  template <typename... Args>
  T* Create(Args&&... args) {
    void* mem = pool_.Allocate();           // 跟随 free_head 指针，无搜索
    if (!mem) return nullptr;
    return ::new (mem) T(std::forward<Args>(args)...);
  }

  // O(1) 释放: 析构 + 归还 free list
  void Destroy(T* obj) {
    obj->~T();                              // 显式析构
    pool_.Free(obj);                        // 归还到 free list 头部
  }

  // 安全版本: 返回 expected 而非裸指针
  template <typename... Args>
  expected<T*, MemPoolError> CreateChecked(Args&&... args);
};
```

FixedPool 的内部结构:

```
┌────────────────────────────────────────────┐
│ FixedPool<256, 64>  (内嵌 16 KB 存储)       │
│                                            │
│ free_head_ → [0] → [1] → [2] → ... → [63] │
│              ↑                              │
│              block_size = max(256, align)   │
│                                            │
│ Allocate(): head=0, free_head_=1, return &[0]│
│ Free([0]):  [0].next=free_head_, free_head_=0│
└────────────────────────────────────────────┘
```

**对比**:

| 操作 | shared_ptr + new | ObjectPool |
|------|-----------------|------------|
| 分配 | malloc (不确定延迟) | O(1) free list pop |
| 释放 | atomic decrement + free | O(1) free list push |
| 碎片化 | 随运行时间增长 | **零** (等块大小) |
| 并发开销 | 原子引用计数 (LL/SC 竞争) | mutex (冷路径) 或无锁 (热路径) |
| 内存预算 | 不可预测 | **编译期确定** (sizeof(T) x MaxObjects) |
| 失败模式 | bad_alloc 异常 / OOM kill | `CreateChecked()` 返回 `expected` |

**事件队列重写** -- 替代引子中的 `shared_ptr<void>` 方案:

```cpp
struct Event { uint32_t id; /* ... */ };

// 固定池: 预分配 256 个 Event，零堆分配
osp::ObjectPool<Event, 256> event_pool;

// SPSC 环形缓冲: 传递池索引，不传递指针/智能指针
struct EventHandle {
  uint16_t pool_index;
  uint32_t event_id;
};
osp::SpscRingbuffer<EventHandle, 256> event_queue;

// 生产者: 分配 + 入队
void producer() {
  auto result = event_pool.CreateChecked(42);
  if (result.has_value()) {
    Event* evt = result.value();
    uint16_t idx = /* pool index */;
    event_queue.Push(EventHandle{idx, evt->id});
  }
}

// 消费者: 出队 + 处理 + 释放
void consumer() {
  if (auto* handle = event_queue.Peek()) {
    Event& evt = pool_ref(handle->pool_index);
    process(evt);                      // 直接访问，无 lock() 竞态
    event_queue.Discard();
    event_pool.Destroy(&evt);          // 确定性释放
  }
}
```

**关键区别**: 没有 `weak_ptr::lock()` 竞态窗口。Handle 持有的池索引在 Destroy 之前始终有效，而 Destroy 只由最后一个消费者显式调用。所有权语义清晰: 生产者 Create，消费者 Destroy，SPSC 保证顺序。

### 3.2 FixedFunction: 替代 std::function

**陷阱 4 的解决方案**: 编译期固定大小的可调用对象容器。

```cpp
// newosp: FixedFunction -- SBO 永不逃逸到堆
template <typename Ret, typename... Args, size_t BufferSize>
class FixedFunction<Ret(Args...), BufferSize> final {
  using Storage = typename std::aligned_storage<BufferSize, alignof(void*)>::type;
  using Invoker = Ret (*)(const Storage&, Args...);
  using Destroyer = void (*)(Storage&);

  Storage storage_{};             // 内联存储 (栈上)
  Invoker invoker_ = nullptr;     // 调用器 (函数指针，非虚函数)
  Destroyer destroyer_ = nullptr; // 析构器 (函数指针)

 public:
  template <typename F>
  FixedFunction(F&& f) noexcept {
    using Decay = typename std::decay<F>::type;
    // 编译期断言: 超大 callable 直接报错，不会静默堆分配
    static_assert(sizeof(Decay) <= BufferSize,
                  "Callable too large for FixedFunction buffer");
    ::new (&storage_) Decay(static_cast<F&&>(f));  // placement new
    invoker_ = [](const Storage& s, Args... args) -> Ret {
      return (*reinterpret_cast<const Decay*>(&s))(static_cast<Args&&>(args)...);
    };
  }
};
```

**对比**:

| 特性 | std::function | FixedFunction |
|------|--------------|---------------|
| SBO 大小 | ~24B (实现定义，不可配置) | **模板参数** (默认 16B，Bus 用 32B) |
| 超大 callable | 静默堆分配 | **static_assert 编译报错** |
| 调用方式 | 虚函数表 (_M_manager) | **函数指针** (直接跳转) |
| 拷贝 | 深拷贝 (可能堆分配) | **move-only** |
| -fno-exceptions | 部分支持 | **完全兼容** |
| -fno-rtti | 可能出问题 | **完全兼容** |

**在 AsyncBus 中的使用**:

```cpp
// newosp AsyncBus: 回调使用 FixedFunction<void(const Envelope&), 32>
static constexpr size_t kCallbackBufSize = 4 * sizeof(void*);  // 32B
using CallbackType = FixedFunction<void(const EnvelopeType&), kCallbackBufSize>;

// 订阅时，lambda 直接 placement new 到 32B 栈缓冲中
// 捕获 1-2 个指针 (16B) 完全在 SBO 内
bus.Subscribe<SensorData>([this](const auto& envelope) {
  process(std::get<SensorData>(envelope.payload));
});
```

如果 lambda 捕获超过 32 字节，编译器会在 `Subscribe` 调用处报 `static_assert` 错误，而不是在运行时静默分配堆内存。

### 3.3 ScopeGuard: 替代 unique_ptr 的自定义删除器

**陷阱 5 的解决方案**: 轻量级 RAII 清理器。

使用 `unique_ptr` 管理非指针资源 (文件描述符、锁、硬件寄存器) 需要自定义删除器，语法笨拙且有虚调用开销:

```cpp
// 传统方案: unique_ptr + 自定义删除器
struct FdDeleter { void operator()(int* fd) { close(*fd); delete fd; } };
std::unique_ptr<int, FdDeleter> fd(new int(open("/dev/spi0", O_RDWR)));
// 问题: 1) 必须堆分配 int 2) 删除器类型侵入模板参数
```

newosp 的 `ScopeGuard` 使用 `FixedFunction` 存储清理逻辑:

```cpp
// newosp: ScopeGuard -- 零堆分配 RAII
class ScopeGuard final {
  FixedFunction<void()> cleanup_;   // 16B SBO
  bool active_;

 public:
  explicit ScopeGuard(FixedFunction<void()> cleanup) noexcept
      : cleanup_(static_cast<FixedFunction<void()>&&>(cleanup)),
        active_(true) {}

  ~ScopeGuard() {
    if (active_ && cleanup_) {
      cleanup_();
    }
  }

  void release() noexcept { active_ = false; }  // 取消清理
};

// 便捷宏
#define OSP_SCOPE_EXIT(...)                                              \
  ::osp::ScopeGuard _scope_guard_ {                                     \
    ::osp::FixedFunction<void()> { [&]() { __VA_ARGS__; } }            \
  }
```

使用示例:

```cpp
// 文件描述符管理: 零堆分配
int fd = open("/dev/spi0", O_RDWR);
OSP_SCOPE_EXIT(close(fd));

// 硬件寄存器恢复
uint32_t old_cfg = read_reg(GPIO_CFG);
write_reg(GPIO_CFG, new_cfg);
OSP_SCOPE_EXIT(write_reg(GPIO_CFG, old_cfg));

// 可选释放: 成功时不清理
auto guard = osp::ScopeGuard(FixedFunction<void()>{[&] { rollback(); }});
if (commit_success) {
  guard.release();  // 成功，不回滚
}
```

**对比 unique_ptr**:

| 特性 | unique_ptr + Deleter | ScopeGuard |
|------|---------------------|------------|
| 管理对象 | 指针类型 (T*) | **任意操作** (lambda) |
| 删除器 | 侵入模板参数 | **lambda capture** |
| 堆分配 | Deleter 可能堆分配 | **零** (FixedFunction SBO) |
| 灵活性 | 只能管理指针 | fd / reg / lock / rollback |

### 3.4 expected: 替代异常

**陷阱 5 的解决方案**: 值类型的错误传播。

```cpp
// newosp: expected<V, E> -- 内联存储，零堆分配
template <typename V, typename E>
class expected final {
  typename std::aligned_storage<sizeof(V), alignof(V)>::type storage_;
  E err_;
  bool has_value_;

 public:
  static expected success(V&& val) noexcept;
  static expected error(E err) noexcept;

  bool has_value() const noexcept;
  V& value() noexcept;
  E get_error() const noexcept;
};
```

使用模式:

```cpp
// 传统方案: 异常
try {
  auto* buf = allocate_buffer(4096);
  process(buf);
} catch (const std::bad_alloc& e) {
  handle_oom();
}

// newosp: expected -- 编译期强制错误处理
auto result = pool.CreateChecked(frame_id, data);
if (!result.has_value()) {
  // 编译期可见的错误路径
  log_error(result.get_error());  // MemPoolError::kPoolExhausted
  return;
}
process(result.value());
```

**优势**:

- 兼容 `-fno-exceptions`: 错误处理完全在类型系统中
- 零分配: 成功值和错误码都内联存储
- 调用者无法忽略错误: 必须检查 `has_value()` 才能访问 `value()`

### 3.5 函数指针 + context: 替代虚基类

在事件队列的回调场景中，传统方案通常使用虚基类:

```cpp
// 传统方案: 虚基类 + unique_ptr
class IEventHandler {
 public:
  virtual ~IEventHandler() = default;
  virtual void OnEvent(const Event& e) = 0;
};

class ConcreteHandler : public IEventHandler {
  void OnEvent(const Event& e) override { /* ... */ }
};

queue.SetHandler(std::make_unique<ConcreteHandler>());  // 堆分配
```

newosp 使用函数指针 + context 替代:

```cpp
// newosp: 函数指针 + void* context
using EventCallback = void (*)(const Event& e, void* ctx);

struct EventHandler {
  EventCallback fn = nullptr;
  void* ctx = nullptr;

  void Invoke(const Event& e) const noexcept {
    if (fn) fn(e, ctx);
  }
};

// 使用
void on_sensor_data(const Event& e, void* ctx) {
  auto* pipeline = static_cast<Pipeline*>(ctx);
  pipeline->process(e);
}

handler.fn = on_sensor_data;
handler.ctx = &pipeline;
```

| 特性 | 虚基类 + unique_ptr | 函数指针 + context |
|------|-------------------|-------------------|
| 堆分配 | make_unique 分配 | **零** |
| 调用开销 | vtable 间接跳转 (~5 ns) | **直接调用** (~1 ns) |
| 类型 | 多态 (RTTI 依赖) | **trivial** (可 memcpy) |
| constexpr | 不可能 | **可以** |
| -fno-rtti | vtable 受影响 | **完全兼容** |

---

## 4. AsyncBus: 一个完整的替代案例

将引子中的事件队列用 newosp 组件完全重写:

```cpp
#include "osp/bus.hpp"
#include "osp/node.hpp"
#include "osp/platform.hpp"

// 1. 类型安全的事件定义 (替代 void*)
struct SensorEvent   { uint32_t id; float value; };
struct ControlEvent  { uint32_t id; uint8_t cmd; };

using Payload = std::variant<SensorEvent, ControlEvent>;
using Bus = osp::AsyncBus<Payload>;

// 2. 节点: 编译期绑定 Handler (替代 std::function 回调)
struct SensorHandler {
  void operator()(const SensorEvent& e, const osp::MessageHeader& hdr) {
    // 编译期分发，可内联
    process_sensor(e.id, e.value);
  }
  void operator()(const ControlEvent& e, const osp::MessageHeader& hdr) {
    apply_control(e.cmd);
  }
};

osp::StaticNode<Payload, SensorHandler> node(
    "sensor", 1, SensorHandler{});

// 3. 发布: variant 类型安全 (替代 weak_ptr<void>)
auto& bus = Bus::Instance();
bus.Publish(SensorEvent{42, 3.14f}, /*sender_id=*/1);
// 编译期检查: Publish(UnknownType{}) → 编译错误

// 4. 消费: ProcessBatchWith 直接分发 (替代 function + lock + weak_ptr::lock)
SensorHandler handler;
bus.ProcessBatchWith(handler);
// 无 mutex, 无 weak_ptr 竞态, 无堆分配
```

**对比引子中的方案**:

| 维度 | 原始方案 (CSDN) | newosp 方案 |
|------|----------------|------------|
| 事件传递 | `weak_ptr<void>` + lock() | `std::variant` 值语义 |
| 类型安全 | `void*` 强转 | 编译期 variant 检查 |
| 回调存储 | `std::function` (可能堆分配) | FixedFunction SBO (32B, 编译期断言) |
| 同步 | mutex + condition_variable | lock-free MPSC CAS |
| 竞态风险 | weak_ptr::lock() TOCTOU | **无** (值拷贝，无引用悬挂) |
| 异常依赖 | bad_alloc / terminate | expected + -fno-exceptions |
| 堆分配 | shared_ptr 控制块 + function callable | **零** |

---

## 5. 嵌入式场景的智能指针使用建议

完全避免智能指针不现实，以下是嵌入式 C++ 中的实用决策矩阵:

### 5.1 何时可以用 unique_ptr

- 初始化阶段 (非热路径) 的一次性资源分配
- 所有权语义明确的单一持有者场景
- 对象生命周期与作用域完全一致

```cpp
// 可接受: 启动时分配，生命周期 = 进程
auto config = std::make_unique<SystemConfig>();
config->Load("/etc/sensor.ini");
// config 在整个进程生命周期内有效
```

### 5.2 何时禁止用 shared_ptr

- 实时热路径 (每帧/每消息都经过的代码)
- 多线程高频传递 (原子引用计数竞争)
- 内存受限系统 (控制块碎片化)
- 事件队列 (用值传递或 Handle 替代)

### 5.3 替代方案决策树

```
需要管理资源生命周期?
├── 单一所有者?
│   ├── 热路径? → ObjectPool + Handle 传递
│   └── 冷路径? → unique_ptr 或 ScopeGuard
├── 多消费者共享?
│   ├── 编译期已知消费者数量? → ObjectPool + 引用位图
│   └── 运行时动态? → shared_ptr (仅限冷路径)
└── 临时清理?
    └── ScopeGuard / OSP_SCOPE_EXIT
```

### 5.4 嵌入式内存管理的四个原则

1. **编译期确定内存预算**: 所有容器容量、池大小、缓冲区长度在编译期通过模板参数固定。运行时 `malloc` 失败不是 "异常"，而是设计缺陷。

2. **所有权语义在类型中表达**: 用 `ObjectPool::Create()` / `Destroy()` 显式标注所有权转移，而非隐式的引用计数增减。代码审查时能直接看到 "谁分配，谁释放"。

3. **零堆分配热路径**: 消息传递、回调调用、状态转换等每帧都执行的代码路径中，不允许出现 `malloc` / `new` / `shared_ptr` 拷贝。

4. **失败路径编译期可见**: 用 `expected<V, E>` 替代异常。调用者必须处理 `MemPoolError::kPoolExhausted` 等错误，编译器强制检查。

---

## 6. 总结

| 陷阱 | 根因 | newosp 替代 |
|------|------|------------|
| 原子引用计数 | shared_ptr 为通用多线程设计 | ObjectPool O(1) 固定块 |
| 堆碎片化 | malloc 支持任意大小分配 | 编译期固定容量，内嵌存储 |
| weak_ptr 竞态 | 观察者语义误用为传输 | 值传递 / Handle + SPSC |
| std::function 堆逃逸 | SBO 阈值不可控 | FixedFunction static_assert |
| 异常路径不确定 | 标准库假设异常可用 | expected + -fno-exceptions |

智能指针在桌面 C++ 中是合理的默认选择，但在嵌入式实时系统中，它们引入的不确定性 (原子竞争、堆碎片、TOCTOU) 恰好违反了实时系统最核心的约束: **确定性**。newosp 通过编译期固定内存预算、placement new 管理对象生命周期、函数指针替代虚分发，在保持 C++17 类型安全的同时消除了这些不确定性。

---

## 参考

- [newosp GitHub](https://github.com/DeguiLiu/newosp) -- C++17 header-only 嵌入式基础设施库
- [newosp 设计文档](https://github.com/DeguiLiu/newosp/blob/main/docs/design_zh.md) -- 完整架构设计
- [C++ 智能指针失效分析](https://blog.csdn.net/stallion5632/article/details/140479753) -- weak_ptr 竞态案例
- [An Introduction to Lock-Free Programming](https://preshing.com/20120612/an-introduction-to-lock-free-programming/) -- Lock-free 编程入门 (Jeff Preshing)
- [Memory Barriers Are Like Source Control Operations](https://preshing.com/20120710/memory-barriers-are-like-source-control-operations/) -- 内存屏障与多核可见性 (Jeff Preshing)
- [Double-Checked Locking is Fixed in C++11](https://preshing.com/20130930/double-checked-locking-is-fixed-in-cpp11/) -- C++11 内存模型与 DCLP (Jeff Preshing)
- [Memory Ordering at Compile Time](https://preshing.com/20120625/memory-ordering-at-compile-time/) -- 编译器重排序与屏障 (Jeff Preshing)
- [C++ and the Perils of Double-Checked Locking](http://www.aristeia.com/Papers/DDJ_Jul_Aug_2004_revised.pdf) -- Scott Meyers & Andrei Alexandrescu
- [Is Parallel Programming Hard, And, If So, What Can You Do About It?](https://kernel.org/pub/linux/kernel/people/paulmck/perfbook/perfbook.html) -- Paul McKenney

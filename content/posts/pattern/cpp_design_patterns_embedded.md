---
title: "嵌入式 C++17 设计模式实战: 零虚函数、零堆分配的编译期技术"
date: 2026-02-16T09:00:00
draft: false
categories: ["pattern"]
tags: ["C++17", "CRTP", "MISRA", "RAII", "SBO", "constexpr", "design-pattern", "embedded", "lock-free", "newosp", "template", "type-erasure", "zero-allocation"]
summary: "传统设计模式依赖虚函数和动态分配，在嵌入式系统中代价过高。本文基于 newosp 库的真实代码，展示 8 种编译期设计模式的实现：类型擦除替代 std::function、ScopeGuard 替代虚析构、if constexpr 编译期分发、Tag Dispatch 构造控制、强类型包装、Pub/Sub 零堆回调、Visitor 直接分发、以及 CRTP 编译期 Handler 绑定。所有模式在 -fno-exceptions -fno-rtti 下可用，热路径零堆分配。"
ShowToc: true
TocOpen: true
---

> 原文链接: [MISRA C++设计模式改进：模板编程替代虚函数](https://blog.csdn.net/stallion5632/article/details/143805125)
>
> 参考实现: [newosp](https://github.com/DeguiLiu/newosp) v0.2.0 -- 工业嵌入式 C++17 Header-Only 基础设施库

## 1. 为什么嵌入式需要重新审视设计模式

经典设计模式（GoF）的实现通常依赖三个 C++ 特性：**虚函数**、**动态内存分配**、**异常处理**。这三者在嵌入式系统中都有显著的代价：

| 特性 | 代价 | MISRA C++ 约束 |
|------|------|----------------|
| 虚函数 | vtable 间接跳转，阻止内联优化，需要 RTTI 支持 `dynamic_cast` | Rule 5-0-1: 限制不安全类型转换 |
| `std::function` | 堆分配（大 callable），不可预测的拷贝开销 | 热路径禁止动态分配 |
| `std::string` / `std::vector` | 堆分配，分配失败无法恢复（`-fno-exceptions`） | 内存碎片化风险 |
| 异常 | 栈展开的代码体积和延迟不可控 | 许多嵌入式工具链默认关闭 |

C++17 提供了足够的编译期工具来替代这些运行时机制：`if constexpr`、折叠表达式、`std::variant` + `std::visit`、结构化绑定、`constexpr` 函数等。

本文基于 [newosp](https://github.com/DeguiLiu/newosp) 库中的真实代码，展示在 `-fno-exceptions -fno-rtti` 约束下如何实现零虚函数、零堆分配的设计模式。这些不是教科书示例，而是经过 979 个测试用例和 ASan/UBSan/TSan 验证的产品级实现。

## 2. 类型擦除：FixedFunction 替代 std::function

### 2.1 问题

`std::function` 在 callable 对象超过内部 SBO 缓冲区时会触发堆分配。在嵌入式热路径（消息总线回调、定时器回调）中，这是不可接受的。

### 2.2 newosp 的解决方案

`FixedFunction<Sig, BufferSize>` 通过编译期 `static_assert` 强制所有 callable 必须放入固定大小的栈缓冲区，彻底消除堆分配的可能性：

```cpp
template <typename Ret, typename... Args, size_t BufferSize>
class FixedFunction<Ret(Args...), BufferSize> final {
 public:
  FixedFunction() noexcept = default;

  // 支持 nullptr 赋值清除回调
  FixedFunction(std::nullptr_t) noexcept {}

  template <typename F, typename = typename std::enable_if<
      !std::is_same<typename std::decay<F>::type, FixedFunction>::value &&
      !std::is_same<typename std::decay<F>::type, std::nullptr_t>::value>::type>
  FixedFunction(F&& f) noexcept {
    using Decay = typename std::decay<F>::type;
    // 编译期检查：callable 必须放得下
    static_assert(sizeof(Decay) <= BufferSize,
                  "Callable too large for FixedFunction buffer");
    static_assert(alignof(Decay) <= alignof(Storage),
                  "Callable alignment exceeds buffer alignment");
    // placement-new 就地构造
    ::new (&storage_) Decay(static_cast<F&&>(f));
    // 类型擦除：无状态 lambda 作为函数指针
    invoker_ = [](const Storage& s, Args... args) -> Ret {
      return (*reinterpret_cast<const Decay*>(&s))(static_cast<Args&&>(args)...);
    };
    destroyer_ = [](Storage& s) {
      reinterpret_cast<Decay*>(&s)->~Decay();
    };
  }

  // const-qualified operator() -- 与 std::function 的关键区别
  Ret operator()(Args... args) const {
    OSP_ASSERT(invoker_);
    return invoker_(storage_, static_cast<Args&&>(args)...);
  }

  explicit operator bool() const noexcept { return invoker_ != nullptr; }

 private:
  using Storage = typename std::aligned_storage<BufferSize, alignof(void*)>::type;
  using Invoker = Ret (*)(const Storage&, Args...);
  using Destroyer = void (*)(Storage&);

  Storage storage_{};
  Invoker invoker_ = nullptr;
  Destroyer destroyer_ = nullptr;
};
```

### 2.3 设计要点

**类型擦除的核心**：`invoker_` 和 `destroyer_` 是普通函数指针（非虚函数），由无状态 lambda 在构造时生成。编译器会将这些 lambda 内联为直接的函数指针，没有 vtable 开销。

**const-qualified `operator()`**：`std::function::operator()` 是 non-const 的，这在 const 上下文中无法使用。`FixedFunction` 的 `operator()` 标记为 `const`，使其可以在 const 引用和 const 成员函数中调用。

**`nullptr_t` 支持**：可以通过 `callback = nullptr` 清除回调，语义与原生指针一致。

**编译期安全**：如果 callable 体积超过 `BufferSize`（默认 16 字节），编译直接失败。不存在"静默退化到堆分配"的行为。

### 2.4 对比

| 特性 | `std::function` | `FixedFunction` |
|------|----------------|-----------------|
| 堆分配 | 可能（大 callable） | 不可能（static_assert） |
| `operator()` const | 否 | 是 |
| `nullptr` 赋值 | 是 | 是 |
| 异常安全 | 需要 | 不需要（noexcept） |
| 拷贝 | 可拷贝（可能堆分配） | 仅移动 |
| 大小 | 实现依赖（通常 32-48 B） | 用户控制（默认 16 B + 16 B 元数据） |

## 3. RAII 清理：ScopeGuard 替代虚析构

### 3.1 问题

传统 RAII 清理通常需要定义一个带虚析构函数的基类，或者手动在每个返回点添加清理代码。

### 3.2 newosp 的 ScopeGuard

利用 FixedFunction 实现零虚函数的 RAII 清理守卫：

```cpp
class ScopeGuard final {
 public:
  explicit ScopeGuard(FixedFunction<void()> cleanup) noexcept
      : cleanup_(static_cast<FixedFunction<void()>&&>(cleanup)),
        active_(true) {}

  ~ScopeGuard() {
    if (active_ && cleanup_) {
      cleanup_();
    }
  }

  void release() noexcept { active_ = false; }

  ScopeGuard(const ScopeGuard&) = delete;
  ScopeGuard& operator=(const ScopeGuard&) = delete;

  ScopeGuard(ScopeGuard&& other) noexcept
      : cleanup_(static_cast<FixedFunction<void()>&&>(other.cleanup_)),
        active_(other.active_) {
    other.active_ = false;
  }

 private:
  FixedFunction<void()> cleanup_;
  bool active_;
};

// 便捷宏：作用域退出时自动执行清理
#define OSP_SCOPE_EXIT(...)                                             \
  ::osp::ScopeGuard OSP_CONCAT(_scope_guard_, __LINE__) {              \
    ::osp::FixedFunction<void()> { [&]() { __VA_ARGS__; } }           \
  }
```

使用示例：

```cpp
void ProcessFile() {
  FILE* f = fopen("data.bin", "rb");
  if (!f) return;
  OSP_SCOPE_EXIT(fclose(f));  // 无论如何退出，都会关闭文件

  int fd = open("/dev/ttyS0", O_RDWR);
  if (fd < 0) return;         // fclose(f) 仍会执行
  OSP_SCOPE_EXIT(close(fd));

  // ... 业务逻辑 ...
}  // 析构顺序: close(fd) -> fclose(f) (LIFO)
```

### 3.3 设计要点

- **零虚函数**：清理逻辑通过 FixedFunction 捕获，析构函数直接调用，无 vtable 查表
- **`release()` 机制**：在成功路径上调用 `release()` 取消清理，实现"仅在失败时清理"的语义
- **`__LINE__` 宏**：每行生成唯一变量名，支持同一作用域内多个 ScopeGuard

## 4. 编译期分发：if constexpr 替代运行时分支

### 4.1 问题

在泛型容器（环形缓冲区、容器 push/pop）中，trivially copyable 类型可以用 `memcpy` 高效拷贝，而非 trivially copyable 类型必须逐元素 move。传统做法是运行时 `if` 判断或模板特化，前者有分支代价，后者代码膨胀。

### 4.2 newosp 的 SpscRingbuffer 中的 if constexpr

```cpp
template <typename T, size_t BufferSize = 16, bool FakeTSO = false>
class SpscRingbuffer {
  static constexpr bool kTriviallyCopyable =
      std::is_trivially_copyable<T>::value;

  bool Pop(T& data) noexcept {
    const IndexT cur_tail = tail_.value.load(std::memory_order_relaxed);
    const IndexT cur_head = head_.value.load(AcquireOrder());
    if (cur_tail == cur_head) return false;

    // 编译期选择：POD 用直接赋值，非 POD 用 move
    if constexpr (kTriviallyCopyable) {
      data = data_buff_[cur_tail & kMask];
    } else {
      data = std::move(data_buff_[cur_tail & kMask]);
    }

    tail_.value.store(cur_tail + 1, ReleaseOrder());
    return true;
  }

  size_t PushBatch(const T* buf, size_t count) noexcept {
    // ...
    if constexpr (kTriviallyCopyable) {
      // 批量 memcpy：处理环形缓冲区的回绕
      const size_t first_part = std::min(to_write, BufferSize - head_offset);
      std::memcpy(&data_buff_[head_offset], buf + written,
                   first_part * sizeof(T));
      if (to_write > first_part) {
        std::memcpy(&data_buff_[0], buf + written + first_part,
                     (to_write - first_part) * sizeof(T));
      }
    } else {
      // 逐元素拷贝
      for (size_t i = 0; i < to_write; ++i) {
        data_buff_[(head_offset + i) & kMask] = buf[written + i];
      }
    }
    // ...
  }

  // 内存序也通过编译期选择
  static constexpr std::memory_order AcquireOrder() noexcept {
    return FakeTSO ? std::memory_order_relaxed : std::memory_order_acquire;
  }
};
```

### 4.3 与传统模板特化的对比

```cpp
// 传统做法：需要两个特化版本
template <typename T, bool Trivial>
struct CopyHelper;

template <typename T>
struct CopyHelper<T, true> {
  static void copy(T* dst, const T* src, size_t n) {
    std::memcpy(dst, src, n * sizeof(T));
  }
};

template <typename T>
struct CopyHelper<T, false> {
  static void copy(T* dst, const T* src, size_t n) {
    for (size_t i = 0; i < n; ++i) dst[i] = src[i];
  }
};

// if constexpr 做法：一个函数体，编译器裁剪不可达分支
// 代码更紧凑，逻辑更清晰，无需辅助类
```

`if constexpr` 的优势：未选中的分支在编译期被完全丢弃（不参与编译），即使其中引用了不存在的成员函数也不会报错。这使得一个函数模板可以同时处理多种类型约束。

## 5. Tag Dispatch：构造行为的编译期选择

### 5.1 问题

`FixedString` 需要支持两种构造语义：编译期字面量（必须完整放入缓冲区）和运行时字符串（可以截断）。这两种行为不能用同一个构造函数参数签名区分。

### 5.2 newosp 的 Tag 类型

```cpp
// 空标签类型 -- 仅用于重载决议
struct TruncateToCapacity_t {};
constexpr TruncateToCapacity_t TruncateToCapacity{};

template <uint32_t Capacity>
class FixedString {
 public:
  // 构造方式 1: 编译期字面量 -- 超长直接编译失败
  template <uint32_t N>
  FixedString(const char (&str)[N]) noexcept : size_(N - 1U) {
    static_assert(N - 1U <= Capacity,
                  "String literal exceeds FixedString capacity");
    (void)std::memcpy(buf_, str, N);
  }

  // 构造方式 2: 运行时字符串 -- Tag 标记允许截断
  FixedString(TruncateToCapacity_t, const char* str) noexcept : size_(0U) {
    if (str != nullptr) {
      uint32_t i = 0U;
      while ((i < Capacity) && (str[i] != '\0')) {
        buf_[i] = str[i];
        ++i;
      }
      size_ = i;
    }
    buf_[size_] = '\0';
  }
};

// 使用
FixedString<16> a("hello");                          // 编译期检查
FixedString<8> b(TruncateToCapacity, runtime_str);   // 允许截断
// FixedString<4> c("hello");                        // 编译错误！
```

### 5.3 设计要点

Tag Dispatch 的核心是**用类型区分语义，而非用值区分语义**。`TruncateToCapacity_t` 是一个空结构体，不占运行时空间，仅参与重载决议。这比布尔参数 `bool truncate = false` 更安全：布尔参数容易传错，而 Tag 类型在调用点必须显式写出，意图一目了然。

## 6. 强类型包装：NewType 防止 ID 混用

### 6.1 问题

嵌入式系统中大量使用 `uint32_t` 作为各种 ID（定时器 ID、会话 ID、节点 ID）。裸 `uint32_t` 之间可以随意赋值，编译器无法检查语义错误。

### 6.2 newosp 的 NewType

```cpp
template <typename T, typename Tag>
class NewType final {
 public:
  constexpr explicit NewType(T val) noexcept : val_(val) {}
  constexpr T value() const noexcept { return val_; }

  constexpr bool operator==(NewType rhs) const noexcept {
    return val_ == rhs.val_;
  }
  constexpr bool operator!=(NewType rhs) const noexcept {
    return val_ != rhs.val_;
  }

 private:
  T val_;
};

// 定义语义不同的 ID 类型
struct TimerTaskIdTag {};
struct SessionIdTag {};

using TimerTaskId = NewType<uint32_t, TimerTaskIdTag>;
using SessionId   = NewType<uint32_t, SessionIdTag>;

void CancelTimer(TimerTaskId id);
void CloseSession(SessionId id);

// 使用
TimerTaskId tid(42);
SessionId   sid(42);

CancelTimer(tid);   // OK
// CancelTimer(sid); // 编译错误！SessionId != TimerTaskId
// CancelTimer(42);  // 编译错误！explicit 构造
```

### 6.3 零运行时代价

`NewType<uint32_t, Tag>` 在内存布局和指令生成上与裸 `uint32_t` 完全等价。Tag 类型是空结构体，不占用任何空间。编译器优化后，`NewType` 的所有操作都内联为直接的整数操作。

类型安全的代价完全在编译期支付，运行时零开销。

## 7. 错误处理：expected 替代异常

### 7.1 问题

关闭异常（`-fno-exceptions`）后，传统的错误处理退化为返回错误码。但错误码缺少类型安全：调用者可以忽略返回值，也可以用错误的类型解释返回值。

### 7.2 newosp 的 expected<V, E>

```cpp
template <typename V, typename E>
class expected final {
 public:
  // 工厂方法：明确表达意图
  static expected success(const V& val) noexcept {
    expected e;
    e.has_value_ = true;
    ::new (&e.storage_) V(val);  // placement-new，零堆分配
    return e;
  }

  static expected error(E err) noexcept {
    expected e;
    e.has_value_ = false;
    e.err_ = err;
    return e;
  }

  bool has_value() const noexcept { return has_value_; }

  V& value() & noexcept {
    OSP_ASSERT(has_value_);
    return *reinterpret_cast<V*>(&storage_);
  }

  E get_error() const noexcept {
    OSP_ASSERT(!has_value_);
    return err_;
  }

 private:
  typename std::aligned_storage<sizeof(V), alignof(V)>::type storage_;
  E err_;
  bool has_value_ = false;
};

// void 特化：仅表达成功/失败，无值
template <typename E>
class expected<void, E> final { /* ... */ };
```

函数式链式调用：

```cpp
// and_then: 成功时继续处理，失败时短路
template <typename V, typename E, typename F>
auto and_then(const expected<V, E>& result, F&& fn)
    -> decltype(fn(result.value())) {
  if (result.has_value()) {
    return fn(result.value());
  }
  return decltype(fn(result.value()))::error(result.get_error());
}

// 使用
auto result = ParseConfig(path);
and_then(result, [](const Config& cfg) {
  return ValidateConfig(cfg);
});
```

## 8. 观察者 / Pub-Sub：零堆分配的消息总线

### 8.1 传统观察者的问题

原文中的观察者模式使用 `std::function<void(int)>` + `std::map` + `std::vector`，三个容器都可能触发堆分配。

### 8.2 newosp 的 Bus/Node 实现

newosp 的消息总线用 `FixedFunction` 替代 `std::function`，用固定大小数组替代 `std::map` + `std::vector`，用 CAS 原子操作实现无锁 MPSC：

```cpp
template <typename PayloadVariant,
          uint32_t QueueDepth = 256,
          uint32_t BatchSize = 16>
class AsyncBus {
  // 回调表：固定大小，FixedFunction 替代 std::function
  struct SubscriptionSlot {
    FixedFunction<void(const MessageEnvelope<PayloadVariant>&)> callback;
    std::atomic<bool> active{false};
  };

  std::array<SubscriptionSlot, kMaxSubscriptions> subscriptions_;

  // CAS 无锁发布（MPSC: 多生产者单消费者）
  bool PublishInternal(PayloadVariant&& payload, uint32_t sender_id,
                       uint64_t timestamp_us, MessagePriority priority,
                       uint32_t topic_hash) noexcept {
    uint32_t prod_pos;
    RingBufferNode* target;

    do {
      prod_pos = producer_pos_.load(std::memory_order_relaxed);
      target = &ring_buffer_[prod_pos & kBufferMask];

      uint32_t seq = target->sequence.load(std::memory_order_acquire);
      if (seq != prod_pos) return false;  // 满了
    } while (!producer_pos_.compare_exchange_weak(
        prod_pos, prod_pos + 1,
        std::memory_order_acq_rel, std::memory_order_relaxed));

    // 写入消息并发布
    target->envelope.payload = std::move(payload);
    target->sequence.store(prod_pos + 1, std::memory_order_release);
    return true;
  }
};

// Node 通过 RAII 管理订阅生命周期
template <typename PayloadVariant>
class Node {
  SubscriptionHandle handles_[OSP_MAX_NODE_SUBSCRIPTIONS];
  uint32_t handle_count_ = 0;

  ~Node() noexcept { Stop(); }  // 析构时自动取消所有订阅
};
```

### 8.3 与原文观察者模式的对比

| 维度 | 原文 Events<std::function> | newosp AsyncBus |
|------|---------------------------|-----------------|
| 回调存储 | `std::map<uint32_t, std::vector<std::function>>` | `std::array<FixedFunction, N>` |
| 堆分配 | 3 层（map + vector + function） | 零 |
| 线程安全 | 无 | CAS 无锁 MPSC |
| 订阅管理 | 手动 `removeObserver(key)` | RAII（Node 析构自动取消） |
| Topic 路由 | 无 | FNV-1a 32-bit hash |

## 9. Visitor 模式：std::visit 直接分发

### 9.1 问题

当消息总线使用 `std::variant` 存储多种消息类型时，需要根据实际类型分发到对应的处理函数。传统做法是虚函数 + 双分派，或者 `dynamic_cast` 链。

### 9.2 newosp 的 ProcessBatchWith

```cpp
// 直接分发模式：std::visit 将 Handler 内联到分发点
template <typename Visitor>
uint32_t ProcessBatchWith(Visitor&& visitor) noexcept {
  uint32_t processed = 0;

  while (processed < kBatchSize) {
    auto& node = ring_buffer_[cons_pos & kBufferMask];

    // std::visit 编译期生成跳转表，无虚函数
    std::visit([&visitor, &hdr](const auto& data) {
      visitor(data, hdr);
    }, node.envelope.payload);

    ++processed;
  }

  return processed;
}

// Handler 示例：函数对象，每种类型一个 operator()
struct MyHandler {
  void operator()(const SensorData& data, const MessageHeader& hdr) {
    // 处理传感器数据
  }
  void operator()(const CommandMsg& cmd, const MessageHeader& hdr) {
    // 处理控制命令
  }
};
```

`std::visit` 在编译期为 `std::variant` 的每种 alternative 生成一个跳转表入口。与虚函数调用相比，跳转表的优势是：编译器可以看到所有分支的完整代码，因此可以进行内联优化。

## 10. CRTP + 折叠表达式：StaticNode 编译期 Handler 绑定

### 10.1 问题

动态回调表（`std::vector<std::function>`）需要运行时查找和间接调用。对于性能敏感的消息处理路径，这个间接层是可以消除的。

### 10.2 newosp 的 StaticNode

```cpp
template <typename PayloadVariant, typename Handler>
class StaticNode {
  Handler handler_;  // Handler 作为模板参数，编译器可完全内联

  // 折叠表达式：编译期为 variant 的每个类型注册订阅
  template <size_t... Is>
  bool SubscribeAll(std::index_sequence<Is...>) noexcept {
    return (SubscribeOne<Is>() && ...);  // 短路求值
  }

  template <size_t I>
  bool SubscribeOne() noexcept {
    using T = std::variant_alternative_t<I, PayloadVariant>;

    Handler* handler_ptr = &handler_;
    SubscriptionHandle handle =
        bus_ptr_->template Subscribe<T>(
            [handler_ptr](const EnvelopeType& env) noexcept {
              const T* data = std::get_if<T>(&env.payload);
              if (OSP_LIKELY(data != nullptr)) {
                (*handler_ptr)(*data, env.header);  // 编译器可内联
              }
            });
    return handle.IsValid();
  }

  // 双模式分发
  uint32_t SpinOnce() noexcept {
    if (started_) {
      return bus_ptr_->ProcessBatch();       // 回调表模式
    }
    return bus_ptr_->ProcessBatchWith(handler_);  // 直接分发模式
  }
};
```

### 10.3 设计要点

**Handler 模板参数化**：`Handler` 不是基类指针，而是模板参数。编译器在实例化 `StaticNode<PayloadVariant, MyHandler>` 时，可以看到 `MyHandler::operator()` 的完整定义，因此可以内联到消息处理的热路径中。

**`std::index_sequence` + 折叠表达式**：`SubscribeAll` 在编译期展开为 N 次 `SubscribeOne<0>() && SubscribeOne<1>() && ...`，N 是 `PayloadVariant` 中的类型数量。这是编译期循环的标准技术。

**双模式分发**：`SpinOnce()` 根据是否调用过 `Start()` 选择分发路径。直接分发模式（`ProcessBatchWith`）跳过回调表，让 `std::visit` 直接将事件分发给 Handler，最大化内联机会。

## 11. 策略模式：编译期内存序选择

### 11.1 问题

无锁数据结构在不同硬件平台上需要不同的内存序。x86 的 TSO 模型保证了 store-load 顺序，ARM 则需要显式的 acquire/release 屏障。单核 MCU 甚至可以用 relaxed + signal_fence 替代硬件屏障。

### 11.2 newosp 的编译期策略

```cpp
template <typename T, size_t BufferSize, bool FakeTSO = false>
class SpscRingbuffer {
  // 策略：内存序通过 constexpr 函数在编译期确定
  static constexpr std::memory_order AcquireOrder() noexcept {
    return FakeTSO ? std::memory_order_relaxed : std::memory_order_acquire;
  }

  static constexpr std::memory_order ReleaseOrder() noexcept {
    return FakeTSO ? std::memory_order_relaxed : std::memory_order_release;
  }
};

// x86/ARM Linux: 正常内存序
using NormalQueue = SpscRingbuffer<Msg, 256, false>;

// 单核 MCU: relaxed + signal_fence，省掉硬件 DMB
using McuQueue = SpscRingbuffer<Msg, 256, true>;
```

同样的策略模式也用于平台相关的 CPU 让步指令：

```cpp
static void CpuRelax() noexcept {
#if defined(__x86_64__) || defined(__i386__)
  __builtin_ia32_pause();
#elif defined(__aarch64__) || defined(__arm__)
  asm volatile("yield" ::: "memory");
#else
  std::this_thread::yield();
#endif
}
```

## 12. 总结：模式选择速查表

| 传统模式 | 传统实现 | newosp 替代 | 核心技术 |
|----------|---------|------------|---------|
| 回调/委托 | `std::function` + 堆分配 | `FixedFunction` | 类型擦除 + SBO + placement-new |
| RAII 清理 | 虚析构基类 | `ScopeGuard` | FixedFunction + `__LINE__` 宏 |
| 泛型容器操作 | 模板特化 / 运行时 if | `if constexpr` | 编译期分支裁剪 |
| 构造重载 | 布尔参数 / 枚举 | Tag Dispatch | 空结构体类型参与重载决议 |
| ID 类型安全 | `typedef` / `using`（无保护） | `NewType<T, Tag>` | 空 Tag 类型区分语义 |
| 错误处理 | 异常 / 错误码 | `expected<V, E>` | 判别联合 + 工厂方法 |
| 观察者 | `std::map<std::vector<std::function>>` | `AsyncBus` | CAS 无锁 + FixedFunction |
| 分发 | 虚函数 / `dynamic_cast` | `std::visit` | 编译期跳转表 |
| Handler 绑定 | 回调表 + 间接调用 | `StaticNode<Handler>` | CRTP + 折叠表达式 + 内联 |
| 策略选择 | 虚函数 / 继承 | `constexpr` 模板参数 | 编译期策略确定 |

这些模式的共同特征：

1. **零虚函数**：所有分发在编译期确定或通过函数指针完成
2. **零堆分配**：`FixedFunction`、`FixedString`、`FixedVector`、`expected` 全部栈分配
3. **`-fno-exceptions -fno-rtti` 兼容**：不依赖异常和运行时类型信息
4. **编译期安全**：`static_assert` 在编译时捕获尺寸/对齐/类型错误
5. **可测试**：979 个 Catch2 测试用例 + ASan/UBSan/TSan 全绿

参考实现: [newosp](https://github.com/DeguiLiu/newosp) -- MIT 协议开源，header-only，可直接在嵌入式项目中使用。

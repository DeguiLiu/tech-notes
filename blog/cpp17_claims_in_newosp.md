# C 语言做不到的 C++17 特性在 newosp 中的体现

> 源码仓库: [newosp](https://github.com/DeguiLiu/newosp) | 本文代码引用基于 newosp v0.2.0

> 筛选标准：只保留 C11 在语言层面**无法实现**的能力。
> 边界检查、SBO 回调、ARM 内存序、cache line 对齐、`-fno-exceptions` 等 C 均可做到，已移除。

---

## 1. 编译期类型成员校验

C 的 `void*` 不携带类型信息，编译器无法验证传入类型是否属于合法集合。

`include/osp/bus.hpp:376-385`:

```cpp
template <typename T, typename... Types>
struct VariantIndex<T, std::variant<Types...>> {
  static constexpr size_t value =
      detail::VariantIndexImpl<T, 0, std::variant<Types...>>::value;
  static_assert(value != static_cast<size_t>(-1),
                "Type not found in PayloadVariant");
};
```

`include/osp/bus.hpp:500-505` -- `Subscribe` 调用点验证：

```cpp
template <typename T, typename Func>
SubscriptionHandle Subscribe(Func&& func) noexcept {
  constexpr size_t type_idx = VariantIndex<T, PayloadVariant>::value;
  static_assert(type_idx < OSP_BUS_MAX_MESSAGE_TYPES,
                "Type index exceeds OSP_BUS_MAX_MESSAGE_TYPES");
```

C 等价代码 `subscribe(bus, GPS_TAG, handler)` 中 `GPS_TAG` 写错不会产生编译错误。

---

## 2. 穷举式类型分发

C 的 `switch (msg->tag)` 缺少 `case` 只产生 `-Wswitch` 警告（且对 `void*` 无效）。
C++ 的 `std::visit` + `overloaded` 缺少任何一个 variant 类型的处理，编译直接失败。

`include/osp/bus.hpp:84-90`:

```cpp
template <class... Ts>
struct overloaded : Ts... {
  using Ts::operator()...;
};
template <class... Ts>
overloaded(Ts...) -> overloaded<Ts...>;
```

C 不具备将"遗漏分支"从警告提升为硬错误的语言机制。

---

## 3. 强类型别名

C 的 `typedef uint32_t TimerId` 和 `typedef uint32_t NodeId` 是同一类型，编译器不阻止互相赋值。

`include/osp/vocabulary.hpp:739-763`:

```cpp
template <typename T, typename Tag>
class NewType final {
 public:
  constexpr explicit NewType(T val) noexcept : val_(val) {}
  constexpr T value() const noexcept { return val_; }
  constexpr bool operator==(NewType rhs) const noexcept { return val_ == rhs.val_; }
  constexpr bool operator!=(NewType rhs) const noexcept { return val_ != rhs.val_; }
 private:
  T val_;
};

using TimerTaskId = NewType<uint32_t, TimerTaskIdTag>;
using SessionId   = NewType<uint32_t, SessionIdTag>;
```

`TimerTaskId id; SessionId sid = id;` 编译失败。C 中 `typedef` 做不到。

---

## 4. if constexpr -- 基于类型属性的编译期分支消除

C 的 `#ifdef` 只能基于宏开关，无法检测类型属性（如 `is_trivially_copyable`）。
C 的运行时 `if` 在函数未内联时无法消除死分支，死代码和对应字符串常量留在二进制中。

**(a) `include/osp/spsc_ringbuffer.hpp:144-157`** -- 根据 T 是否 trivially copyable 选择路径：

```cpp
if constexpr (kTriviallyCopyable) {
  std::memcpy(&data_buff_[head_offset], buf + written, first_part * sizeof(T));
} else {
  for (size_t i = 0; i < to_write; ++i) {
    data_buff_[(head_offset + i) & kMask] = buf[written + i];
  }
}
```

同一文件 184 行 (Pop)、210 行 (PopBatch) 使用相同模式。

**(b) `include/osp/config.hpp:534-560`** -- 编译期递归展开多后端分派：

```cpp
template <typename First, typename... Rest>
expected<void, ConfigError> DispatchFile(const char* path, ConfigFormat format) {
  if (First::kFormat == format)
    return ConfigParser<First>::ParseFile(*this, path);
  if constexpr (sizeof...(Rest) > 0)
    return DispatchFile<Rest...>(path, format);
  return expected<void, ConfigError>::error(ConfigError::kFormatNotSupported);
}
```

**(c) `include/osp/fault_collector.hpp:580`** -- 根据回调返回类型选择控制流：

```cpp
if constexpr (std::is_same_v<decltype(fn(recent_ring_[idx])), bool>) {
  if (!fn(recent_ring_[idx])) { break; }
} else {
  fn(recent_ring_[idx]);
}
```

C 没有类型 trait 系统，无法在编译期查询类型属性并据此选择代码路径。

---

## 5. constexpr 函数 -- 编译器保证编译期求值

C 的 `const` 不是编译期常量合同。C 没有"函数必须在编译期求值"的语言机制。

`include/osp/bus.hpp:70-78`:

```cpp
constexpr uint32_t Fnv1a32(const char* str) noexcept {
  if (str == nullptr) return 0;
  uint32_t hash = 2166136261u;
  while (*str) {
    hash ^= static_cast<uint32_t>(*str++);
    hash *= 16777619u;
  }
  return hash;
}
```

`include/osp/app.hpp:80`:

```cpp
constexpr uint32_t MakeIID(uint16_t app_id, uint16_t ins_id) noexcept {
  return (static_cast<uint32_t>(app_id) << 16) | static_cast<uint32_t>(ins_id);
}
```

C 可以用宏做简单常量折叠，但无法在宏中写循环或条件逻辑来实现 FNV-1a 哈希。

---

## 6. 模板实例化 -- 为不同参数生成专用代码

C 的 `void* + size_t` 传参让编译器丢失常量信息，`index % depth` 变成运行时除法。
模板将参数编码为类型的一部分，编译器将 `& (N-1)` 折叠为立即数 AND 指令。

`include/osp/spsc_ringbuffer.hpp:74-86`:

```cpp
template <typename T, size_t BufferSize = 16, bool FakeTSO = false, typename IndexT = size_t>
class SpscRingbuffer {
  static_assert(BufferSize != 0, "Buffer size cannot be zero.");
  static_assert((BufferSize & (BufferSize - 1)) == 0, "Buffer size must be a power of 2.");
  static_assert(sizeof(IndexT) <= sizeof(size_t), "Index type size must not exceed size_t.");
  static_assert(std::is_unsigned<IndexT>::value, "Index type must be unsigned.");
```

`include/osp/bus.hpp:406-420`:

```cpp
static constexpr uint32_t kQueueDepth = static_cast<uint32_t>(OSP_BUS_QUEUE_DEPTH);
static constexpr uint32_t kBufferMask = kQueueDepth - 1;

static_assert((kQueueDepth & (kQueueDepth - 1)) == 0,
              "Queue depth must be power of 2");
```

C11 的 `_Static_assert` 只能断言整型常量表达式，无法断言类型属性（`is_unsigned`、`is_trivially_copyable`）。

---

## 7. RAII -- 编译器自动在每条退出路径插入清理代码

标准 C 没有析构函数。`goto cleanup` 是手动操作，漏一条路径就泄漏。
GCC 的 `__attribute__((cleanup))` 是非标准扩展。

`include/osp/vocabulary.hpp:774-801`:

```cpp
class ScopeGuard final {
 public:
  explicit ScopeGuard(FixedFunction<void()> cleanup) noexcept
      : cleanup_(static_cast<FixedFunction<void()>&&>(cleanup)), active_(true) {}
  ~ScopeGuard() {
    if (active_ && cleanup_) { cleanup_(); }
  }
  void release() noexcept { active_ = false; }
};
```

`include/osp/node.hpp:177` -- Node 析构自动清理全部订阅：

```cpp
~Node() noexcept { Stop(); }
```

`include/osp/shm_transport.hpp:98` -- SharedMemorySegment 析构自动 `munmap` + `shm_unlink`。

编译器保证 `return`、异常、作用域结束等所有退出路径均调用析构函数。C 需要程序员人工保证。

---

## 8. Fold Expression -- 参数包自动展开

C 没有可变参数模板，无法对类型列表做编译期遍历。

`include/osp/worker_pool.hpp:519-522`:

```cpp
template <typename... Types>
void SubscribeAllImpl(std::variant<Types...>* /*tag*/) noexcept {
  (MaybeSubscribe<Types>(), ...);
}
```

一行代码为 `variant` 中每个类型调用 `MaybeSubscribe`。C 需要手动枚举或 X-Macro 生成。

---

## 总结

| # | 能力 | C 的局限 | newosp 实现位置 |
|---|------|---------|----------------|
| 1 | 编译期类型成员校验 | `void*` 无类型信息 | `bus.hpp:376-385` |
| 2 | 穷举式类型分发 | `switch` 缺 `case` 仅警告 | `bus.hpp:84-90` |
| 3 | 强类型别名 | `typedef` 是别名非新类型 | `vocabulary.hpp:739-763` |
| 4 | 基于类型属性的分支消除 | 无 type trait，无 `if constexpr` | `spsc_ringbuffer.hpp:144` |
| 5 | 保证编译期求值的函数 | `const` 非编译期合同 | `bus.hpp:70-78` |
| 6 | 参数化专用代码生成 | `void* + size_t` 丢失常量 | `spsc_ringbuffer.hpp:74-86` |
| 7 | 自动资源清理 | 无析构函数 | `vocabulary.hpp:774-801` |
| 8 | 参数包展开 | 无可变参数模板 | `worker_pool.hpp:519-522` |

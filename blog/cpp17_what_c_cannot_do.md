# C11 做不到的 C++17：工业嵌入式场景的 8 项语言级差异

> 源码仓库: [newosp](https://github.com/DeguiLiu/newosp) | 本文代码引用基于 newosp v0.2.0

> 筛选标准：只保留 C11 在**语言层面无法实现**的能力。
> 条件编译、`_Alignas`、`_Static_assert(常量表达式)`、`atomic_signal_fence`、栈上固定数组等 C11 同样能做到的内容不在讨论范围。

---

## 1. 编译期类型成员校验

C 的 `void*` 不携带类型信息，编译器无法验证传入类型是否属于合法集合。

```cpp
// 编译期递归：将类型 T 映射为 variant 中的索引，不存在则编译失败
template <typename T, typename... Types>
struct VariantIndex<T, std::variant<Types...>> {
  static constexpr size_t value =
      detail::VariantIndexImpl<T, 0, std::variant<Types...>>::value;
  static_assert(value != static_cast<size_t>(-1),
                "Type not found in PayloadVariant");
};

// 订阅不在 variant 中的类型 -> 编译失败
template <typename T, typename Func>
SubscriptionHandle Subscribe(Func&& func) noexcept {
  constexpr size_t idx = VariantIndex<T, PayloadVariant>::value;
```

C 等价代码 `subscribe(bus, GPS_TAG, handler)` 中 tag 写错不会产生编译错误。C11 的 `_Static_assert` 只能检查整型常量表达式，无法检查"某类型是否在类型列表中"。

---

## 2. 穷举式类型分发

C 的 `switch (msg->tag)` 缺少 `case` 只产生 `-Wswitch` 警告，且对 `void*` 载荷无效。`std::visit` 缺少任何一个 variant 类型的处理，编译直接失败。

```cpp
template <class... Ts>
struct overloaded : Ts... { using Ts::operator()...; };
template <class... Ts>
overloaded(Ts...) -> overloaded<Ts...>;

std::visit(overloaded{
    [](const SensorData& d) { process(d); },
    [](const MotorCmd& c)   { execute(c); },
    // 缺 SystemStatus -> 编译错误，不是警告
}, payload);
```

C 没有将"遗漏分支"从警告提升为硬错误的语言机制。

---

## 3. 强类型别名

C 的 `typedef uint32_t TimerId` 和 `typedef uint32_t NodeId` 是同一类型，编译器不阻止互相赋值。

```cpp
template <typename T, typename Tag>
class NewType final {
 public:
  constexpr explicit NewType(T val) noexcept : val_(val) {}
  constexpr T value() const noexcept { return val_; }
 private:
  T val_;
};

using TimerId  = NewType<uint32_t, struct TimerIdTag>;
using SessionId = NewType<uint32_t, struct SessionIdTag>;

TimerId tid{1};
SessionId sid = tid;  // 编译失败: 不同类型
```

同理，`enum class` 阻止枚举值隐式转 `int`，C 的 `enum` 做不到：

```cpp
enum class Priority : uint8_t { kLow, kMedium, kHigh };
int x = Priority::kLow;       // 编译失败
if (Priority::kLow == 0) {}   // 编译失败
```

---

## 4. if constexpr -- 基于类型属性的编译期分支消除

C 的 `#ifdef` 只能基于宏开关，无法检测类型属性。C 的运行时 `if` 在函数未内联时无法消除死分支。

**(a) 按 trivially copyable 选择拷贝策略:**

```cpp
if constexpr (std::is_trivially_copyable_v<T>) {
  std::memcpy(&buffer[offset], src, count * sizeof(T));
} else {
  for (size_t i = 0; i < count; ++i) {
    buffer[(offset + i) & mask] = src[i];
  }
}
// 编译后只保留命中的分支，另一条完全不存在于二进制中
```

**(b) 编译期递归展开多后端分派:**

```cpp
template <typename First, typename... Rest>
auto DispatchFile(const char* path, ConfigFormat format) {
  if (First::kFormat == format)
    return ConfigParser<First>::ParseFile(*this, path);
  if constexpr (sizeof...(Rest) > 0)
    return DispatchFile<Rest...>(path, format);
  return error(ConfigError::kFormatNotSupported);
}
// 未启用的后端不生成任何代码
```

**(c) 按回调返回类型选择控制流:**

```cpp
if constexpr (std::is_same_v<decltype(fn(entry)), bool>) {
  if (!fn(entry)) { break; }  // 返回 bool -> 可提前终止
} else {
  fn(entry);                   // 返回 void -> 无条件执行
}
```

C 没有类型 trait 系统，无法在编译期查询类型属性并据此选择代码路径。

---

## 5. constexpr 函数 -- 保证编译期求值

C 的 `const` 不是编译期常量合同，`#define` 宏无法写循环或条件逻辑。

```cpp
constexpr uint32_t Fnv1a32(const char* str) noexcept {
  uint32_t hash = 2166136261u;
  while (*str) {
    hash ^= static_cast<uint32_t>(*str++);
    hash *= 16777619u;
  }
  return hash;
}

constexpr auto kTopicHash = Fnv1a32("sensor/imu");  // 编译结果: 立即数
```

C 可以用宏做简单常量折叠 (`#define MAKE_IID(a,b) ((a)<<16|(b))`)，但无法在宏中写循环来实现哈希函数。

---

## 6. 模板实例化 -- 参数化专用代码生成

C 的 `void* + size_t` 传参让编译器丢失常量信息。模板将参数编码为类型的一部分，编译器可据此生成专用指令。

```cpp
template <typename T, size_t BufferSize>
class SpscRingbuffer {
  static_assert((BufferSize & (BufferSize - 1)) == 0, "Must be power of 2");
  static constexpr size_t kMask = BufferSize - 1;
  // index & kMask -> 单条 AND 立即数指令
};

// 不同实例化 -> 不同类型 -> 各自生成最优代码
SpscRingbuffer<SensorData, 256> sensor_rb;   // 版本 A
SpscRingbuffer<MotorCmd, 64>    motor_rb;    // 版本 B
```

C 的通用函数接收 `void*`，`index % depth` 变成运行时除法。

同一机制还可做**编译期字面量长度检查**：

```cpp
template <uint32_t Capacity>
class FixedString {
  // 模板参数 N 从 char(&)[N] 自动推导出字面量长度
  template <uint32_t N, typename = std::enable_if_t<(N <= Capacity + 1)>>
  FixedString(const char (&str)[N]) noexcept : size_(N - 1) {
    static_assert(N - 1 <= Capacity, "String literal exceeds capacity");
    std::memcpy(buf_, str, N);
  }
};

FixedString<8> name("too_long_string");  // 编译失败
```

C 无法从 `const char*` 推导字面量长度并做静态断言。`strncpy` 静默截断。

---

## 7. RAII -- 编译器自动在每条退出路径插入清理代码

标准 C 没有析构函数。GCC 的 `__attribute__((cleanup))` 是非标准扩展。

```cpp
class ScopeGuard final {
 public:
  explicit ScopeGuard(FixedFunction<void()> fn) noexcept
      : cleanup_(std::move(fn)), active_(true) {}
  ~ScopeGuard() { if (active_ && cleanup_) { cleanup_(); } }
  void release() noexcept { active_ = false; }
};

auto fd = ::open(path, O_RDONLY);
ScopeGuard guard([fd]{ ::close(fd); });
if (error) return err;  // 自动 close
guard.release();         // 成功路径取消
```

RAII 还保证析构顺序，可用于锁外释放资源避免死锁：

```cpp
bool Unsubscribe(const Handle& handle) noexcept {
    Callback old;                   // 最后析构
    {
      std::unique_lock lock(mtx_);  // 先析构 -> 先释放锁
      old = std::move(slot.callback);
    }
    // old 在锁释放后才析构，避免析构函数内部再次获取锁
    return static_cast<bool>(old);
}
```

C 的 `goto cleanup` 可行但编译器不强制，漏一条路径就泄漏。

---

## 8. Move 语义、拷贝删除、Fold Expression

这三项能力各自独立，但共同点相同：C 缺少对应的语法结构。

**Move 语义** -- C 没有语言级所有权转移：

```cpp
envelope.payload = std::move(payload);  // 零拷贝，源对象进入已知空状态
// C: memcpy + 手动置空源，编译器不追踪
```

**拷贝删除** -- C 无法禁止结构体赋值：

```cpp
Bus(const Bus&) = delete;
Bus& operator=(const Bus&) = delete;
// C: struct Bus b2 = b1; 编译通过，两份指向同一资源
```

**Fold Expression** -- C 没有可变参数模板：

```cpp
template <typename... Types>
void SubscribeAll(std::variant<Types...>*) noexcept {
  (MaybeSubscribe<Types>(), ...);  // 为每个类型展开一次调用
}
// C: 需要手动枚举或 X-Macro 生成
```

---

## 总结

| # | C++17 能力 | C11 的局限 |
|---|-----------|-----------|
| 1 | 编译期类型成员校验 | `void*` 无类型信息，`_Static_assert` 无法检查类型列表 |
| 2 | 穷举式类型分发 | `switch` 缺 `case` 仅警告，`void*` 载荷无法分发 |
| 3 | 强类型别名 / enum class | `typedef` 是别名非新类型，`enum` 隐式转 `int` |
| 4 | 基于类型属性的分支消除 | 无 type trait，无 `if constexpr` |
| 5 | 保证编译期求值的函数 | `const` 非编译期合同，宏无法写循环 |
| 6 | 参数化专用代码生成 | `void* + size_t` 丢失常量和类型信息 |
| 7 | 自动资源清理 | 无析构函数，`goto cleanup` 编译器不强制 |
| 8 | 所有权 / 拷贝控制 / 参数包 | 无 move、无 `= delete`、无可变参数模板 |

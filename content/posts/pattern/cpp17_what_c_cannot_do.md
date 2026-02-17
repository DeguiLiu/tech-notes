---
title: "C11 做不到的事: 10 项 C++17 语言级不可替代能力"
date: 2026-02-17T08:40:00
draft: false
categories: ["pattern"]
tags: ["C++17", "C11", "embedded", "type-safety", "constexpr", "RAII", "lock-free"]
summary: "筛选标准: 只保留 C11 在语言层面无法实现的能力。从类型安全、编译期计算、内存安全、类型分发四个维度，逐项对比 C++17 与 C11 的语言级差异，附完整代码对比。"
ShowToc: true
TocOpen: true
---

> 筛选标准: 只保留 C11 在**语言层面无法实现**的能力。
> 条件编译、`_Alignas`、`_Static_assert(常量表达式)`、`atomic_signal_fence`、栈上固定数组等 C11 同样能做到的内容不在讨论范围。

> 姊妹篇: [newosp 源码中的 C++17 实践]({{< ref "cpp17_claims_in_newosp" >}}) -- 这些语言能力在工业嵌入式库中的具体落地位置与工程决策。

---

## 一、类型安全 -- 编译器拒绝类型混用

### 1. 编译期类型成员校验

C 的 `void*` 不携带类型信息，编译器无法验证传入类型是否属于合法集合。

```cpp
// C++: 编译期递归，将类型 T 映射为 variant 中的索引，不存在则编译失败
template <typename T, typename... Types>
struct VariantIndex<T, std::variant<Types...>> {
  static constexpr size_t value =
      detail::VariantIndexImpl<T, 0, std::variant<Types...>>::value;
  static_assert(value != static_cast<size_t>(-1),
                "Type not found in PayloadVariant");
};

// 订阅不在 variant 中的类型 -> 编译失败
bus.Subscribe<GpsData>(handler);  // GpsData 不在 variant 中 -> 编译错误
```

```c
// C: tag 写错不产生编译错误
subscribe(bus, GPS_TAG, handler);  // GPS_TAG 写错 -> 把 SensorData 按 GpsData 解释
                                    // 编译器无任何警告
```

C11 的 `_Static_assert` 只能检查整型常量表达式，无法检查"某类型是否在类型列表中"。

### 2. 强类型别名

C 的 `typedef uint32_t TimerId` 和 `typedef uint32_t NodeId` 是同一类型，编译器不阻止互相赋值。

```cpp
// C++: NewType 创建真正不同的类型
template <typename T, typename Tag>
class NewType final {
 public:
  constexpr explicit NewType(T val) noexcept : val_(val) {}
  constexpr T value() const noexcept { return val_; }
 private:
  T val_;
};

using TimerId   = NewType<uint32_t, struct TimerIdTag>;
using SessionId = NewType<uint32_t, struct SessionIdTag>;

TimerId tid{1};
SessionId sid = tid;  // 编译失败: 不同类型
```

```c
// C: typedef 不阻止混用
typedef uint32_t TimerId;
typedef uint32_t SessionId;

TimerId tid = 1;
SessionId sid = tid;  // 编译通过，运行时传错 ID
```

### 3. enum class -- 枚举值不泄漏、不隐式转整型

```cpp
// C++: 作用域枚举
enum class Priority : uint8_t { kLow, kMedium, kHigh };
int x = Priority::kLow;       // 编译失败: 不能隐式转 int
if (Priority::kLow == 0) {}   // 编译失败: 不能与 int 比较
```

```c
// C: 枚举值泄漏到全局
enum Priority { LOW, MEDIUM, HIGH };
enum LogLevel { LOW, HIGH };  // 编译错误: LOW/HIGH 重定义
int x = LOW;                  // 编译通过，LOW 就是 int 0
```

### 4. not_null -- 空指针解引用在构造期拦截

```cpp
// C++: 类型系统标注"不可能为空"
void Process(not_null<Sensor*> sensor) {
  sensor->Read();  // 调用者保证非空，函数内无需检查
}
Process(nullptr);  // 编译期或构造期断言失败
```

```c
// C: 指针永远可能为空
void process(Sensor* sensor) {
  if (!sensor) return;  // 每个函数都要防御性检查
  sensor->read();       // 忘了检查 -> SIGSEGV
}
```

---

## 二、编译期计算 -- 将运行时工作移至编译期

### 5. constexpr 函数 -- 保证编译期求值

C 的 `const` 不是编译期常量合同，`#define` 宏无法写循环或条件逻辑。

```cpp
// C++: 编译器保证在编译期求值
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

### 6. if constexpr -- 基于类型属性的编译期分支消除

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

**(b) 按回调返回类型选择控制流:**

```cpp
if constexpr (std::is_same_v<decltype(fn(entry)), bool>) {
  if (!fn(entry)) { break; }  // 返回 bool -> 可提前终止
} else {
  fn(entry);                   // 返回 void -> 无条件执行
}
```

**(c) 编译期递归展开多后端分派:**

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

C 没有类型 trait 系统，无法在编译期查询类型属性并据此选择代码路径。

### 7. 模板实例化 -- 参数化专用代码生成

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

同一机制还可做**编译期字面量长度检查**:

```cpp
template <uint32_t Capacity>
class FixedString {
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

## 三、内存安全 -- 编译器管理资源生命周期

### 8. RAII -- 编译器自动在每条退出路径插入清理代码

标准 C 没有析构函数。GCC 的 `__attribute__((cleanup))` 是非标准扩展。

```cpp
// C++: 编译器在每条退出路径自动插入析构
auto fd = ::socket(AF_INET, SOCK_STREAM, 0);
ScopeGuard guard([fd]{ ::close(fd); });

if (::connect(fd, ...) < 0) return unexpected(kConnectFailed); // 自动 close
if (::setsockopt(...) < 0) return unexpected(kOptionFailed);   // 自动 close
guard.release();
return TcpSocket(fd);  // 成功路径，所有权转移
```

```c
// C: 每条路径手动 close，漏一个就泄漏
int fd = socket(AF_INET, SOCK_STREAM, 0);
if (connect(fd, ...) < 0) { close(fd); return -1; }
if (setsockopt(...) < 0) { return -1; }  // 忘了 close -> fd 泄漏
return fd;                                // 编译器不警告
```

RAII 还保证析构顺序，可用于锁外释放资源避免死锁:

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

### 9. Move 语义与拷贝控制

C 没有语言级所有权转移，也无法禁止结构体赋值。

**Move 语义** -- 零拷贝所有权转移:

```cpp
auto socket = TcpSocket::Connect("host", 8080);
auto socket2 = std::move(socket);  // 所有权转移，源对象进入已知空状态
socket.Send(data);  // 静态分析工具警告 use-after-move
```

```c
int fd = connect_to("host");
int fd2 = fd;           // 复制了 fd，两处都能 close
close(fd);              // 关闭后 fd2 变成悬空句柄
write(fd2, data, len);  // 写入已关闭的 fd -> 未定义行为
```

**拷贝删除** -- 禁止危险的复制:

```cpp
Bus(const Bus&) = delete;
Bus& operator=(const Bus&) = delete;
// C: struct Bus b2 = b1; 编译通过，两份指向同一资源
```

**expected 错误处理** -- 编译器强制检查:

```cpp
auto result = pool.CreateChecked(args...);
auto ptr = result.value();  // 未检查 has_value() -> Debug 断言失败
```

```c
void* ptr = pool_alloc(&pool);     // 返回 NULL 表示失败
memcpy(ptr, data, size);           // ptr == NULL -> SIGSEGV，编译器不警告
```

---

## 四、类型分发 -- 编译器穷举检查

### 10. variant + visit 穷举式分发与 Fold Expression

C 的 `switch (msg->tag)` 缺少 `case` 只产生 `-Wswitch` 警告，且对 `void*` 载荷无效。

```cpp
// C++: 缺少任何一个 variant 类型的处理 -> 编译失败
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

```c
// C: 缺少 case -> 运行时静默丢消息
switch (msg->tag) {
    case SENSOR: handle_sensor(msg->data); break;
    case MOTOR:  handle_motor(msg->data);  break;
    // 忘了 STATUS -> 消息丢失，可能运行数天才发现
}
```

Fold Expression 为 variant 中每个类型自动展开操作:

```cpp
template <typename... Types>
void SubscribeAll(std::variant<Types...>*) noexcept {
  (MaybeSubscribe<Types>(), ...);  // 为每个类型展开一次调用
}
// C: 需要手动枚举或 X-Macro 生成
```

新增消息类型时，C++ 在所有未更新的 `visit` 处报编译错误，强制补全。C 的 `-Wswitch` 只是警告，经常被忽略。

---

## 总结

| # | C++17 能力 | C11 的局限 |
|---|-----------|-----------|
| 1 | 编译期类型成员校验 | `void*` 无类型信息，`_Static_assert` 无法检查类型列表 |
| 2 | 强类型别名 (NewType) | `typedef` 是别名非新类型 |
| 3 | enum class | `enum` 隐式转 `int`，枚举值泄漏到全局 |
| 4 | not_null 空指针拦截 | 指针永远可能为空，每处需防御性检查 |
| 5 | constexpr 保证编译期求值 | `const` 非编译期合同，宏无法写循环 |
| 6 | if constexpr 分支消除 | 无 type trait，无编译期分支选择 |
| 7 | 模板参数化专用代码 | `void* + size_t` 丢失常量和类型信息 |
| 8 | RAII 自动资源清理 | 无析构函数，`goto cleanup` 编译器不强制 |
| 9 | Move / `= delete` / expected | 无所有权转移，无法禁止拷贝，错误码被忽略 |
| 10 | variant + visit 穷举分发 | `switch` 缺 `case` 仅警告，`void*` 无法分发 |

**本质**: C++17 让编译器掌握更多信息 -- 模板给类型和常量，`constexpr` 给求值合同，RAII 给生命周期，`variant` 给完整类型列表，`NewType` 给语义区分。信息越多，编译器能做的检查和优化就越多。C 的 `void*`、宏、手动 cleanup 在隐藏信息，编译器看到的只是指针和整数。

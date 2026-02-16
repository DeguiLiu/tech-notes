---
title: "mccc-bus 中 C 语言做不到的 C++17 实践"
date: 2026-02-15
draft: false
categories: ["mccc"]
tags: ["C++17", "MCCC", "callback", "deadlock", "lock-free", "message-bus", "zero-copy"]
summary: "从 mccc-bus 项目（约 1200 行 header-only）中提炼 C 语言在语言层面无法实现的能力。"
ShowToc: true
TocOpen: true
---

> 源码仓库: [mccc-bus](https://gitee.com/liudegui/mccc-bus) | 本文代码引用基于 mccc-bus v2.0.0

> 从 mccc-bus 项目（约 1200 行 header-only）中提炼 C 语言在语言层面无法实现的能力。
> 条件编译、缓存行对齐、signal fence、固定容量栈数组等 C11 同样能做到的内容已移除。

---

## 一、编译期类型安全

### 1.1 std::variant + VariantIndex：编译期类型路由

C 的 `void*` 让编译器对类型一无所知。C++ 模板在编译期验证类型是否属于 variant 类型列表。

```cpp
// 编译期递归模板，将类型 T 映射为 variant 中的索引
template <typename T, size_t I, typename First, typename... Rest>
struct VariantIndexImpl<T, I, std::variant<First, Rest...>> {
  static constexpr size_t value =
      std::is_same<T, First>::value ? I : VariantIndexImpl<T, I + 1U, std::variant<Rest...>>::value;
};

// 类型不在 variant 中 -> 编译失败
template <typename T, typename... Types>
struct VariantIndex<T, std::variant<Types...>> {
  static constexpr size_t value = detail::VariantIndexImpl<T, 0U, std::variant<Types...>>::value;
  static_assert(value != static_cast<size_t>(-1), "Type not found in PayloadVariant");  // :560
};
```

Subscribe 使用点 (`mccc.hpp:711-713`):

```cpp
template <typename T, typename Func>
SubscriptionHandle Subscribe(Func&& func) {
  constexpr size_t type_idx = VariantIndex<T, PayloadVariant>::value;  // 编译期求值
  static_assert(type_idx < MCCC_MAX_MESSAGE_TYPES, "Type index exceeds MCCC_MAX_MESSAGE_TYPES");
```

订阅一个不在 `PayloadVariant` 中的类型，编译直接报错。C 的 `void* + enum tag` 只能在运行时发现。

**C 为什么做不到**: C 没有模板，无法在编译期将类型映射为索引并做静态断言。`_Static_assert` 只能检查常量表达式，无法检查"某类型是否在类型列表中"。

### 1.2 overloaded + std::visit：分支遗漏编译期报错

```cpp
template <class T, class... Ts>
struct overloaded<T, Ts...> : T, overloaded<Ts...> {
  using T::operator();
  using overloaded<Ts...>::operator();
  explicit overloaded(T t, Ts... ts) : T(std::move(t)), overloaded<Ts...>(std::move(ts)...) {}
};
```

新增消息类型后，所有 `std::visit` 点如果未补全分支，编译器直接拒绝。

**C 为什么做不到**: C 的 `switch` 缺少 `case` 只是 `-Wswitch` 警告，不是错误，经常被忽略。没有语言机制强制穷举所有分支。

### 1.3 enum class：禁止隐式转整型

```cpp
enum class MessagePriority : uint8_t { LOW, MEDIUM, HIGH };      // :408
enum class BusError : uint8_t { QUEUE_FULL, INVALID_MESSAGE };    // :567
enum class BackpressureLevel : uint8_t { NORMAL, WARNING, CRITICAL, FULL };  // :623
```

**C 为什么做不到**: C 的 `enum` 值就是 `int`。`int x = LOW;` 和 `if (LOW == false)` 都能编译通过。无法阻止枚举值与任意整数混用，也无法避免不同枚举间的命名冲突。

---

## 二、RAII 与所有权

### 2.1 RAII：自动析构保证资源释放

```cpp
// component.hpp:62-66
virtual ~Component() {
  for (const auto& handle : handles_) {
    BusType::Instance().Unsubscribe(handle);   // 自动清理所有订阅
  }
}
```

Component 销毁时自动退订所有消息，无论是正常退出、提前 return 还是智能指针释放。

**C 为什么做不到**: C 没有析构函数。每条退出路径都需要手动调用 cleanup，漏一个就是资源泄漏或悬空回调。`goto cleanup` 模式可行但编译器不强制。

### 2.2 锁外析构保证顺序

```cpp
// mccc.hpp:733-755
bool Unsubscribe(const SubscriptionHandle& handle) noexcept {
    CallbackType old_callback;  // destroyed outside lock
    {
      std::unique_lock<std::shared_mutex> lock(callback_mutex_);
      old_callback = std::move(slot.entries[i].callback);
    }
    // old_callback 在锁释放后才析构，避免析构函数内部再次获取锁导致死锁
    return static_cast<bool>(old_callback);
}
```

**C 为什么做不到**: 依赖 RAII 的析构顺序保证。C 需要手动安排释放顺序，编译器不检查。

### 2.3 Move 语义与拷贝删除

```cpp
// mccc.hpp:274-289 -- FixedVector move 构造/赋值
// mccc.hpp:449-453 -- MessageEnvelope defaulted move
// mccc.hpp:987     -- node->envelope.payload = std::move(payload) 零拷贝发布

// component.hpp:73-76 -- 禁止拷贝
Component(const Component&) = delete;
Component& operator=(const Component&) = delete;
Component(Component&&) = delete;
Component& operator=(Component&&) = delete;
```

**C 为什么做不到**: C 没有语言级所有权转移。`int fd2 = fd;` 后编译器不知道谁负责 `close`。也无法禁止结构体赋值拷贝。

---

## 三、编译期代码生成

### 3.1 FixedString 编译期字面量长度检查

```cpp
// mccc.hpp:92-110
template <uint32_t Capacity>
class FixedString {
  template <uint32_t N, typename = typename std::enable_if<(N <= Capacity + 1U)>::type>
  FixedString(const char (&str)[N]) noexcept : size_(N - 1U) {
    static_assert(N > 0U, "String literal must include null terminator");
    static_assert(N - 1U <= Capacity, "String literal exceeds FixedString capacity");
    (void)std::memcpy(buf_, str, N);
  }
```

通过模板参数 `N` 在编译期获取字符串字面量长度，超长直接编译失败。

**C 为什么做不到**: C 无法从 `const char*` 推导出字面量长度并做静态断言。`strncpy(buf, "too_long", sizeof(buf))` 静默截断，不报任何错误。

### 3.2 模板实例化：每个配置生成专用代码

```cpp
template <uint32_t Capacity> class FixedString { ... };          // :92
template <typename T, uint32_t Capacity> class FixedVector { ... };  // :235
template <typename PayloadVariant> class AsyncBus { ... };       // :648
template <typename PayloadVariant> class Component { ... };      // :56
```

`FixedString<32>` 和 `FixedString<64>` 是不同类型，编译器为各自生成最优的 `memcpy`（已知长度时替换为 `mov` 指令序列）。

**C 为什么做不到**: C 的通用函数接收 `void*` + `size_t`，编译器丢失常量信息，`& (QueueDepth - 1)` 无法优化为立即数 AND。

### 3.3 SFINAE：编译期重载选择

```cpp
// mccc.hpp:105
template <uint32_t N, typename = typename std::enable_if<(N <= Capacity + 1U)>::type>
FixedString(const char (&str)[N]) noexcept { ... }
```

字符串字面量长度超过容量时，SFINAE 使该构造函数从重载集合中移除，编译器选择其他版本或报错。

**C 为什么做不到**: C 没有函数重载，无法基于参数属性在编译期选择不同的代码路径。

---

## 总结

mccc-bus 中 C 语言做不到的 C++17 能力集中在三个方面：

1. **编译期类型安全**: variant 类型路由、visit 穷举检查、enum class 隐式转换阻止 -- 将运行时类型错误提前到编译期
2. **RAII 与所有权**: 自动析构保证资源释放、析构顺序保证、move 语义所有权转移、禁止拷贝 -- 编译器管理资源生命周期
3. **编译期代码生成**: 字面量长度推导、模板特化生成专用代码、SFINAE 重载选择 -- 编译器根据类型信息生成更优代码

### 文件索引

| 文件 | 行数 | 核心内容 |
|------|------|---------|
| `include/mccc/mccc.hpp` | 1097 | FixedString, FixedVector, VariantIndex, overloaded, AsyncBus |
| `include/mccc/component.hpp` | 129 | Component RAII, SubscribeSafe/SubscribeSimple |
| `CMakeLists.txt` | 40 | header-only INTERFACE library, C++17 |

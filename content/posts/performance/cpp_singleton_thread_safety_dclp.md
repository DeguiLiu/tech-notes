---
title: "C++ 单例模式的线程安全实现: 从 DCLP 的历史缺陷到 C++11 的修复"
date: 2026-02-16
draft: false
categories: ["performance"]
tags: ["C++11", "C++17", "DCLP", "MISRA", "acquire-release", "atomic", "double-checked-locking", "embedded", "memory-order", "mutex", "singleton", "thread-safety"]
summary: "双重检查锁定 (DCLP) 是 C++ 并发编程中最臭名昭著的模式之一。2004 年 Scott Meyers 和 Andrei Alexandrescu 论证了它在 C++03 中不可移植地安全实现。本文从 DCLP 的历史缺陷出发，解释 C++11 内存模型如何修复它，对比 Magic Statics、acquire/release 原子操作和顺序一致性三种实现，并讨论嵌入式场景下的工程选择。"
ShowToc: true
TocOpen: true
---

> 相关文章:
> - [内存屏障的硬件原理: 从 Store Buffer 到 ARM DMB/DSB/ISB](../memory_barrier_hardware/) -- DCLP 失败的硬件根因 (Store Buffer 导致的写入重排)
> - [无锁编程核心原理](../lockfree_programming_fundamentals/) -- acquire/release 内存序的完整理论
>
> 原文链接: [C++单例的安全实现，double-check(双重检查锁定)的安全实现方法](https://blog.csdn.net/stallion5632/article/details/126218126)
>
> 核心参考: [Double-Checked Locking is Fixed In C++11](https://preshing.com/20130930/double-checked-locking-is-fixed-in-cpp11/) (Jeff Preshing)
>
> 经典论文: [C++ and the Perils of Double-Checked Locking](http://www.aristeia.com/Papers/DDJ_Jul_Aug_2004_revised.pdf) (Scott Meyers & Andrei Alexandrescu, DDJ 2004)

## 1. 单例模式概述

单例模式 (Singleton) 确保一个类在整个进程生命周期中只有一个实例，并提供全局访问点。它是最简单也是最容易实现错误的设计模式之一。

一个正确的单例需要满足：

- **唯一性**: 构造函数私有，禁止拷贝和赋值
- **全局访问**: 通过静态方法获取实例
- **线程安全**: 多线程同时首次访问时，不会创建多个实例
- **初始化安全**: 实例完全构造完成后，其他线程才能使用

前三项容易理解，第四项是 DCLP 问题的根源。

## 2. DCLP 的历史: 一个"正确"了 20 年的错误

### 2.1 朴素加锁方案

最直接的线程安全单例：

```cpp
// 正确，但每次访问都加锁
Singleton* Singleton::getInstance() {
    std::lock_guard<std::mutex> lock(m_mutex);
    if (m_instance == nullptr) {
        m_instance = new Singleton;
    }
    return m_instance;
}
```

这是正确的，但一旦单例创建完成，后续每次访问仍然需要获取锁。在高频访问场景下，锁竞争成为性能瓶颈。

### 2.2 朴素 DCLP: 看起来对，实际上是未定义行为

为了避免每次都加锁，DCLP 在加锁前先检查一次指针：

```cpp
// 错误！C++11 之前无法安全实现
Singleton* Singleton::getInstance() {
    if (m_instance == nullptr) {        // 第一次检查 (无锁)
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_instance == nullptr) {    // 第二次检查 (有锁)
            m_instance = new Singleton;
        }
    }
    return m_instance;
}
```

直觉上这很合理：第一次检查避免了不必要的加锁，第二次检查防止了重复创建。但这段代码在 C++11 之前是**未定义行为**，即使在 C++11 中，如果 `m_instance` 是裸指针 (`Singleton*`)，它仍然是未定义行为。

### 2.3 为什么 DCLP 是错的

2004 年，Scott Meyers 和 Andrei Alexandrescu 在 DDJ 发表了 *"C++ and the Perils of Double-Checked Locking"*，论证了 DCLP 的根本缺陷。问题有两层：

**第一层: 指令重排序**

`m_instance = new Singleton` 在抽象层面是三个操作：

1. 分配内存 (`operator new`)
2. 在分配的内存上构造 `Singleton` 对象
3. 将内存地址赋值给 `m_instance`

编译器和 CPU 可以将步骤 2 和 3 重排序为 3 → 2 (在 ARM 弱序架构上，Store Buffer 的异步刷新机制使这种重排序成为现实，详见 [内存屏障的硬件原理](../memory_barrier_hardware/))。此时另一个线程在第一次检查中看到 `m_instance != nullptr`，直接返回一个**尚未完成构造**的对象。

**第二层: 缺少 synchronizes-with 关系**

即使没有重排序，第一个线程在锁内写入 `m_instance` 和 `Singleton` 的成员变量，第二个线程在**锁外**读取 `m_instance`。锁只保护持有锁的线程之间的可见性。第二个线程跳过了锁，因此无法保证它能看到第一个线程的所有写入。

用 C++ 标准的术语：第一次无锁读取与被保护的写入之间构成**数据竞争** (data race)，属于未定义行为。

> 2000 年，一群 Java 开发者联合发表了声明 *"Double-Checked Locking Is Broken"*。Java 直到 2004 年 (Java 5) 引入新的内存模型和 `volatile` 语义才修复了这个问题。C++ 要到 2011 年才跟上。

## 3. C++11 的三种修复方案

### 3.1 方案一: Magic Statics (推荐)

C++11 标准 [stmt.dcl] p4 保证：

> "If control enters the declaration concurrently while the variable is being initialized, the concurrent execution shall wait for completion of the initialization."

即局部静态变量的初始化是线程安全的，编译器负责生成必要的同步代码。

```cpp
class Singleton {
public:
    static Singleton& getInstance() {
        static Singleton instance;  // C++11 保证线程安全初始化
        return instance;
    }

    Singleton(const Singleton&) = delete;
    Singleton& operator=(const Singleton&) = delete;

private:
    Singleton() = default;
    ~Singleton() = default;
};
```

**编译器实现细节**: GCC 和 Clang 内部使用 guard 变量 + `__cxa_guard_acquire`/`__cxa_guard_release` 实现，本质上就是编译器替你写了正确的 DCLP。在 ARM 上，GCC 甚至利用数据依赖省略了 acquire fence (`dmb` 指令)，生成的代码比手写 DCLP 更高效。

**优点:**
- 代码最简洁
- 编译器保证正确性
- 返回引用而非指针，无需动态分配
- 生成的机器码通常最优

**注意事项:**
- MSVC 2015 之前不支持 Magic Statics (MSVC 2013 和更早版本不符合 C++11 该条款)
- 静态局部变量按照构造的**逆序**销毁，如果另一个静态对象的析构函数访问该单例，可能触发 use-after-destroy

### 3.2 方案二: acquire/release 原子操作

当需要手动控制 (例如动态分配、延迟创建、或避免销毁顺序问题) 时，使用 `std::atomic` + acquire/release 语义：

```cpp
class Singleton {
public:
    static Singleton* getInstance() {
        // acquire load: 保证后续读取能看到 release store 之前的所有写入
        Singleton* tmp = m_instance.load(std::memory_order_acquire);
        if (tmp == nullptr) {
            std::lock_guard<std::mutex> lock(m_mutex);
            // 锁内可以用 relaxed: mutex 本身提供了同步
            tmp = m_instance.load(std::memory_order_relaxed);
            if (tmp == nullptr) {
                tmp = new Singleton;
                // release store: 保证 Singleton 构造完成后才对外可见
                m_instance.store(tmp, std::memory_order_release);
            }
        }
        return tmp;
    }

    Singleton(const Singleton&) = delete;
    Singleton& operator=(const Singleton&) = delete;

private:
    Singleton() = default;

    static std::atomic<Singleton*> m_instance;
    static std::mutex m_mutex;
};

std::atomic<Singleton*> Singleton::m_instance{nullptr};
std::mutex Singleton::m_mutex;
```

**内存序解释:**

| 操作 | 内存序 | 原因 |
|------|--------|------|
| 第一次 load | `memory_order_acquire` | 与创建线程的 release store 构成 synchronizes-with 关系，保证看到完整构造的对象 |
| 锁内 load | `memory_order_relaxed` | `std::mutex` 的 lock/unlock 已提供足够的同步保证，无需额外内存序 |
| store | `memory_order_release` | 保证 `new Singleton` 的所有写操作 (内存分配 + 构造函数) 在 store 之前完成 |

**为什么 `memory_order_relaxed` 不能用于第一次 load:**

```
线程 A (创建):                    线程 B (使用):
  tmp = new Singleton;              tmp = m_instance.load(relaxed);  // 可能看到非空指针
  // Singleton 成员写入              if (tmp != nullptr)
  m_instance.store(tmp, release);     tmp->member;  // 但成员可能还未构造！
```

relaxed load 不提供 acquire 语义，线程 B 看到指针非空时，不保证能看到 Singleton 构造函数中的写入。在 ARM、PowerPC 等弱序架构上，这个 bug 会真实发生。x86 的 TSO 模型碰巧掩盖了这个问题，但依赖特定硬件行为不是可移植的做法。

### 3.3 方案三: 顺序一致性 (默认)

省略 `memory_order` 参数，`std::atomic` 默认使用 `memory_order_seq_cst`：

```cpp
static Singleton* getInstance() {
    Singleton* tmp = m_instance.load();  // 默认 seq_cst
    if (tmp == nullptr) {
        std::lock_guard<std::mutex> lock(m_mutex);
        tmp = m_instance.load();         // 默认 seq_cst
        if (tmp == nullptr) {
            tmp = new Singleton;
            m_instance.store(tmp);       // 默认 seq_cst
        }
    }
    return tmp;
}
```

这是正确的，但 `seq_cst` 的代价比 acquire/release 更高：

| 架构 | acquire/release | seq_cst |
|------|:---------------:|:-------:|
| x86/x64 | load: `mov`; store: `mov` | load: `mov`; store: `xchg` (full barrier) |
| ARMv7 | load: `ldr + dmb`; store: `dmb + str` | load: `dmb + ldr + dmb`; store: `dmb + str + dmb` |
| ARMv8 | load: `ldar`; store: `stlr` | load: `ldar`; store: `stlr` (ARMv8 的 `stlr` 已是 seq_cst) |

> 参考 Herb Sutter 的演讲 *"atomic<> Weapons" Part 2* (00:44:25 - 00:49:16)，详细分析了 seq_cst 在弱序 CPU 上生成的低效代码。

对于单例这个场景，`seq_cst` 的额外开销不重要（store 操作只在首次创建时执行一次），但理解 acquire/release 对于其他 lock-free 编程场景至关重要。

## 4. 常见错误分析

### 4.1 错误一: 裸指针 DCLP

```cpp
// 错误: m_instance 不是 atomic，数据竞争 = 未定义行为
static Singleton* m_instance;

Singleton* getInstance() {
    if (m_instance == nullptr) {        // 无锁读取裸指针 = data race
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_instance == nullptr) {
            m_instance = new Singleton;
        }
    }
    return m_instance;
}
```

即使在 x86 上"看起来"能工作（因为 TSO 模型碰巧保证了对齐指针读写的原子性），这仍然是未定义行为。编译器可以假设不存在数据竞争，并据此进行优化（例如将 `m_instance` 缓存到寄存器中，导致永远看不到其他线程的写入）。

### 4.2 错误二: atomic + relaxed 全用

```cpp
// 错误: relaxed load 不保证看到构造函数的写入
Singleton* tmp = m_instance.load(std::memory_order_relaxed);
if (tmp != nullptr) {
    return tmp;  // tmp 指向的对象可能尚未完成构造！
}
```

`memory_order_relaxed` 只保证原子性（不会读到半写的指针值），但不保证可见性。一个线程可能看到非空指针，却看不到 Singleton 构造函数中对成员变量的赋值。

### 4.3 错误三: volatile 替代 atomic

```cpp
// 错误: C++ 的 volatile 与 Java 的 volatile 语义完全不同
static volatile Singleton* m_instance;
```

C++ 的 `volatile` 只防止编译器优化掉对该变量的读写，不提供任何多线程同步保证。它是为 memory-mapped I/O 设计的，不是线程同步原语。Java 5+ 的 `volatile` 具有 acquire/release 语义，但 C++ 的 `volatile` 没有。

### 4.4 错误四: 忽略销毁顺序

```cpp
// 潜在问题: Logger 析构时 Config 可能已销毁
class Config {
public:
    static Config& getInstance() { static Config c; return c; }
};

class Logger {
public:
    static Logger& getInstance() { static Logger l; return l; }
    ~Logger() {
        Config::getInstance().get("log_level");  // 危险！
    }
};
```

静态局部变量按构造的逆序销毁。如果 `Config` 先于 `Logger` 构造，它会后于 `Logger` 销毁，此时 Logger 析构函数中访问 Config 是安全的。但如果构造顺序相反，就会触发 use-after-destroy。

Nifty Counter 惯用法或 `std::call_once` + 动态分配 (永不删除) 可以规避此问题，但都增加了复杂度。

## 5. 嵌入式系统中的单例

### 5.1 MISRA C++ 视角

MISRA C++ 对单例模式的几个相关规则：

| 规则 | 约束 | 对单例的影响 |
|------|------|-------------|
| Rule 18-4-1 | 不应使用动态堆内存分配 | 禁止 `new Singleton`，只能用 Magic Statics (栈/BSS 分配) |
| Rule 0-1-1 | 所有代码应可达 | 析构函数如果不可达 (单例永不销毁)，需文档化偏差 |
| Rule 3-4-1 | 对象应在最窄作用域声明 | 全局单例与此规则冲突，需文档化 |

在严格 MISRA 合规的嵌入式项目中，Magic Statics 方案是唯一可接受的实现，因为它避免了动态分配。

### 5.2 `-fno-exceptions` 下的行为

嵌入式常用 `-fno-exceptions` 编译。此时 Magic Statics 的行为：

- GCC/Clang: `__cxa_guard_acquire` 失败时调用 `__cxa_guard_abort`，不抛异常。如果构造函数中的代码本身不依赖异常，整个初始化流程是 exception-free 的
- 构造函数中不能使用 `try/catch`，需要用返回值或断言处理错误

### 5.3 单核 MCU 的简化

在没有操作系统的单核 MCU 上，不存在真正的多线程（只有主循环和中断）。此时单例退化为全局变量：

```cpp
// 单核裸机: 不需要任何同步机制
class Peripheral {
public:
    static Peripheral& getInstance() {
        static Peripheral instance;  // 在 main() 之前或首次调用时初始化
        return instance;
    }
private:
    Peripheral() { /* 初始化硬件寄存器 */ }
};
```

如果 ISR 也需要访问单例，只需保证在启用中断之前完成单例初始化即可。

### 5.4 何时不用单例

单例模式在嵌入式系统中被过度使用。以下场景有更好的替代：

| 场景 | 问题 | 替代方案 |
|------|------|---------|
| 多个模块共享配置 | 全局状态隐藏了依赖关系 | 依赖注入: 构造时传入配置引用 |
| 硬件外设抽象 | 如果需要支持多个同类外设？ | 模板参数化: `Uart<1>`, `Uart<2>` |
| 日志系统 | 测试时难以 mock | 接口注入: 构造时传入日志实例 |
| 消息总线 | 不同子系统需要独立的总线 | 实例化多个 Bus 对象 |

## 6. 完整测试代码

```cpp
#include <atomic>
#include <cassert>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>

// ==================== 方案一: Magic Statics ====================
class SingletonA {
public:
    static SingletonA& getInstance() {
        static SingletonA instance;
        return instance;
    }
    int getValue() const { return value_; }

    SingletonA(const SingletonA&) = delete;
    SingletonA& operator=(const SingletonA&) = delete;

private:
    SingletonA() : value_(42) {}
    int value_;
};

// ==================== 方案二: Acquire/Release DCLP ====================
class SingletonB {
public:
    static SingletonB* getInstance() {
        SingletonB* tmp = instance_.load(std::memory_order_acquire);
        if (tmp == nullptr) {
            std::lock_guard<std::mutex> lock(mutex_);
            tmp = instance_.load(std::memory_order_relaxed);
            if (tmp == nullptr) {
                tmp = new SingletonB;
                instance_.store(tmp, std::memory_order_release);
            }
        }
        return tmp;
    }

    int getValue() const { return value_; }

    SingletonB(const SingletonB&) = delete;
    SingletonB& operator=(const SingletonB&) = delete;

private:
    SingletonB() : value_(42) {}
    int value_;

    static std::atomic<SingletonB*> instance_;
    static std::mutex mutex_;
};

std::atomic<SingletonB*> SingletonB::instance_{nullptr};
std::mutex SingletonB::mutex_;

// ==================== 测试 ====================
static std::atomic<int> g_count{0};

template <typename GetInstance>
void concurrencyTest(const char* name, GetInstance getInst) {
    g_count.store(0);
    constexpr int kThreads = 16;
    constexpr int kIterations = 100000;

    std::vector<std::thread> threads;
    threads.reserve(kThreads);

    for (int i = 0; i < kThreads; ++i) {
        threads.emplace_back([&getInst]() {
            for (int j = 0; j < kIterations; ++j) {
                auto* inst = getInst();
                if (inst->getValue() == 42) {
                    g_count.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }

    for (auto& t : threads) {
        t.join();
    }

    int expected = kThreads * kIterations;
    std::cout << name << ": " << g_count.load() << "/" << expected;
    if (g_count.load() == expected) {
        std::cout << " PASS" << std::endl;
    } else {
        std::cout << " FAIL" << std::endl;
    }
}

int main() {
    std::cout << "=== Singleton Thread Safety Test ===" << std::endl;

    concurrencyTest("Magic Statics",
        []() { return &SingletonA::getInstance(); });

    concurrencyTest("Acquire/Release DCLP",
        []() { return SingletonB::getInstance(); });

    // 验证唯一性
    assert(&SingletonA::getInstance() == &SingletonA::getInstance());
    assert(SingletonB::getInstance() == SingletonB::getInstance());
    std::cout << "Uniqueness: PASS" << std::endl;

    return 0;
}
```

## 7. 方案对比与推荐

| 维度 | Magic Statics | acquire/release DCLP | seq_cst DCLP |
|------|:-------------:|:--------------------:|:------------:|
| 正确性 | 编译器保证 | 需要正确使用内存序 | 编译器保证 (默认最强序) |
| 代码量 | 3 行 | 15+ 行 | 12+ 行 |
| 动态分配 | 无 (BSS/栈) | 是 (`new`) | 是 (`new`) |
| 内存泄漏 | 无 | 需手动管理 | 需手动管理 |
| 销毁控制 | 自动 (逆序) | 手动控制 | 手动控制 |
| MISRA 合规 | 合规 | 违反 Rule 18-4-1 | 违反 Rule 18-4-1 |
| ARM 性能 | 最优 (编译器可利用数据依赖) | 接近最优 | 多余的 DMB 指令 |
| 编译器要求 | C++11 完整支持 | C++11 `<atomic>` | C++11 `<atomic>` |

**推荐:**

1. **默认选择 Magic Statics** -- 简洁、正确、零动态分配、MISRA 合规
2. **需要控制销毁顺序时**用 acquire/release DCLP -- 理解内存序是前提
3. **不确定时用 seq_cst** -- 性能代价在单例场景中可忽略，正确性更重要
4. **考虑是否真的需要单例** -- 依赖注入通常是更好的设计

## 参考文献

1. Scott Meyers & Andrei Alexandrescu, [*C++ and the Perils of Double-Checked Locking*](http://www.aristeia.com/Papers/DDJ_Jul_Aug_2004_revised.pdf), DDJ, 2004
2. Jeff Preshing, [*Double-Checked Locking is Fixed In C++11*](https://preshing.com/20130930/double-checked-locking-is-fixed-in-cpp11/), 2013
3. StackOverflow, [*Is implementation of double-checked singleton thread-safe?*](https://stackoverflow.com/questions/43292897/is-implementation-of-double-checked-singleton-thread-safe)
4. Herb Sutter, [*atomic<> Weapons*](https://herbsutter.com/2013/02/11/atomic-weapons-the-c-memory-model-and-modern-hardware/), 2013
5. ISO/IEC 14882:2011 (C++11), [stmt.dcl] p4 -- 静态局部变量并发初始化保证
6. [*Double-Checked Locking Is Broken*](https://www.cs.umd.edu/~pugh/java/memoryModel/DoubleCheckedLocking.html), Bill Pugh et al., 2000

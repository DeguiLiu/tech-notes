---
title: "C++11 线程安全消息总线: 从零实现 Pub/Sub 模型"
date: 2026-02-15T09:00:00
draft: false
categories: ["performance"]
tags: ["C++11", "MCCC", "callback", "lock-free", "message-bus"]
summary: "消息总线（Message Bus）作为一种重要的通信模式，被应用于解耦系统中的组件，实现异步通信和事件驱动架构。本文介绍如何使用 C++11 实现一个基于 mutex 保护的消息总线。"
ShowToc: true
TocOpen: true
---

> 消息总线（Message Bus）作为一种重要的通信模式，被应用于解耦系统中的组件，实现异步通信和事件驱动架构。本文介绍如何使用 C++11 实现一个基于 mutex 保护的消息总线。
>
> 完整代码: [message_bus](https://gitee.com/liudegui/message_bus)

## 1. 设计方案

### 1.1 核心数据结构

```cpp
#include <functional>
#include <cstdint>
#include <map>
#include <vector>
#include <list>
#include <mutex>
#include <string>

typedef std::function<void(const std::string& param1, int param2)> Callback_t;
typedef std::function<void()> TimeOutCallback_t;

enum class CallbackType_t {
    ALWAYS = 0,
    ONCE
};

struct CallbackItem_t {
    Callback_t callback = nullptr;
    TimeOutCallback_t timeOutCallback = nullptr;
    uint32_t timeoutInterval = 1000;    // milliseconds
    uint64_t timeoutStamp = 0;          // milliseconds (与 timeoutInterval 统一单位)
    std::vector<int> msgNumVec;
    CallbackType_t callbackType = CallbackType_t::ALWAYS;
};
```

> 注: 原始版本中 `timeoutInterval` 单位为 ms，`timeoutStamp` 单位为 us，容易引发换算 bug。此处统一为 ms。

### 1.2 MessageBus 类

```cpp
class MessageBus {
public:
    // C++11 保证局部静态变量初始化是线程安全的 (Magic Statics)
    static MessageBus& instance() {
        static MessageBus ins;
        return ins;
    }

    void publish(int msg, const std::string& param1, int param2 = 0);
    void timeOutCheck();
    bool subscribe(const CallbackItem_t& item);
    void reset();
    void stop();
    void start();

private:
    MessageBus() = default;
    MessageBus(const MessageBus&) = delete;
    MessageBus& operator=(const MessageBus&) = delete;

    std::mutex mutex_;
    // 内部使用 mutex_ 保护订阅表的读写
};
```

> 注: `publish` 和 `subscribe` 的参数改为 `const&` 传递，避免不必要的 `std::string` 和 `CallbackItem_t` 拷贝。

### 1.3 核心特性

- 单例模式，全局唯一消息总线实例（C++11 Magic Statics 保证线程安全初始化）
- 支持一次性 (ONCE) 和持久 (ALWAYS) 两种订阅模式
- 内置超时检测机制
- 线程安全（`std::mutex` 保护订阅表读写）

> 注: `publish` 内部持有 `std::mutex`，属于阻塞式互斥。对于需要真正无锁发布的场景，请参考 [mccc-bus](https://gitee.com/liudegui/mccc-bus) 的 Lock-free MPSC 实现。

## 2. 优缺点分析

优点：
- 解耦发布者和订阅者
- 支持超时回调机制
- 接口简洁，易于集成

缺点：
- 使用 `std::function` 和 `std::map`，存在堆分配
- `publish` 持有 mutex，高频发布场景下存在锁竞争
- 全局单例模式，不适合多总线场景
- 缺少优先级和背压控制

（完整实现请参阅代码仓库）

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/125514223)
> 代码仓库: [Gitee](https://gitee.com/liudegui/message_bus)

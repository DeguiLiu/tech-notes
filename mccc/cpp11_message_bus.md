# 使用 C++11 实现的非阻塞消息总线 message_bus

> 消息总线（Message Bus）作为一种重要的通信模式，被应用于解耦系统中的组件，实现异步通信和事件驱动架构。本文介绍如何使用 C++11 实现一个非阻塞消息总线。
>
> 完整代码: [message_bus](https://gitee.com/liudegui/message_bus)

## 1. 设计方案

### 1.1 核心数据结构

```cpp
#include <map>
#include <vector>
#include <list>
#include <mutex>

typedef std::function<void(std::string param1, int param2)> Callback_t;
typedef std::function<void()> TimeOutCallback_t;

enum CallbackType_t {
    ALWAYS = 0,
    ONCE
};

struct CallbackItem_t {
    Callback_t callback = nullptr;
    TimeOutCallback_t timeOutCallback = nullptr;
    uint32_t timeoutInterval = 1000;    // milliseconds
    uint64_t timeoutStamp = 0;          // microseconds
    std::vector<int> msgNumVec;
    CallbackType_t callbackType = ALWAYS;
};
```

### 1.2 MessageBus 类

```cpp
class MessageBus {
public:
    static MessageBus& instance() {
        static MessageBus ins;
        return ins;
    }
    void publish(int msg, std::string param1, int param2 = 0);
    void timeOutCheck();
    bool subscribe(CallbackItem_t);
    void reset();
    void stop();
    void start();

private:
    MessageBus() = default;
    MessageBus(const MessageBus&) = delete;
    MessageBus& operator=(const MessageBus&) = delete;
};
```

### 1.3 核心特性

- 单例模式，全局唯一消息总线实例
- 支持一次性 (ONCE) 和持久 (ALWAYS) 两种订阅模式
- 内置超时检测机制
- 线程安全（std::mutex 保护）
- 非阻塞发布

## 2. 优缺点分析

优点：
- 解耦发布者和订阅者
- 支持超时回调机制
- 接口简洁，易于集成

缺点：
- 使用 std::function 和 std::map，存在堆分配
- 全局单例模式，不适合多总线场景
- 缺少优先级和背压控制

（完整实现请参阅代码仓库）

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/125514223)
> 代码仓库: [Gitee](https://gitee.com/liudegui/message_bus)

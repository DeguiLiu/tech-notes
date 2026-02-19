---
title: "从 C 到 C++17: 软件 FCCU 故障收集器的演进"
date: 2026-02-19T10:00:00
draft: false
categories: ["practice"]
tags: ["C++17", "FCCU", "嵌入式", "故障管理", "HSM", "SPSC", "无锁", "header-only", "裸机", "ringbuffer", "状态机"]
summary: "硬件 FCCU 是汽车/工业 MCU 的标配安全模块。本文从 C 语言 QPC 事件驱动实现出发，介绍 fccu-cpp -- 一个 C++17 header-only 软件 FCCU 组件，复用 newosp 成熟设计模式，基于外部 SPSC ringbuffer 和两层 HSM 构建，零堆分配、裸机友好。"
ShowToc: true
TocOpen: true
---

> **仓库**: [fccu-cpp](https://github.com/DeguiLiu/fccu-cpp) |
> **C 版参考**: [fccu_linux_demo](https://gitee.com/liudegui/fccu_linux_demo) |
> **设计模式来源**: [newosp](https://github.com/DeguiLiu/newosp) fault_collector.hpp
>
> **相关文章**: [QPC 事件驱动与活动对象模式](../qpc_active_object_hsm/) |
> [C 语言 HSM 数据驱动框架](../../pattern/c_hsm_data_driven_framework/) |
> [mccc 无锁 MPSC 设计](../../performance/mccc_lockfree_mpsc_design/) |
> [SPSC 环形缓冲设计](../../performance/spsc_ringbuffer_design/)

## 背景

### 什么是 FCCU

FCCU (Fault Collection and Control Unit) 是汽车/工业 MCU 中常见的硬件模块，典型如 NXP S32K3 和 Infineon AURIX TC3xx 系列中的 FCCU 外设。其核心职责:

- 统一收集全系统故障信号 (软件异常、硬件中断、看门狗超时)
- 按优先级分类缓存、状态维护
- 根据故障属性自动采取后处理措施 (恢复、降级、关停)
- 提供故障快照与诊断查询接口

在没有硬件 FCCU 的平台 (通用 ARM-Linux、RTOS、裸机 MCU) 上，用软件实现同等机制是工业嵌入式系统的常见需求。

### 为什么需要软件 FCCU

工业设备 (激光雷达、机器人控制器、边缘网关) 和汽车 ECU 面对的故障场景高度相似:

| 场景 | 故障源 | 处理策略 |
|------|--------|---------|
| 传感器掉线 | I2C/SPI 通信超时 | 降级运行 |
| 电压异常 | ADC 采样越界 | 紧急关停 |
| 通信丢包 | 序列号跳变 | 重试/升级 |
| 看门狗超时 | 任务死锁 | 系统复位 |
| 温度过高 | 热传感器报警 | 降频/关闭负载 |

这些场景的共性需求: 故障去重、优先级排队、Hook 后处理、状态追踪、统计诊断。

## C 版实现: QPC 事件驱动方案

[CSDN 原文](https://blog.csdn.net/stallion5632/article/details/149144349) 和 [fccu_linux_demo](https://gitee.com/liudegui/fccu_linux_demo) 给出了 C 语言的完整实现。

### 核心架构

C 版 FCCU 基于 QPC 框架构建，采用事件驱动 + 活动对象 (AO) 模式:

```
故障上报 --> 多级环形队列池 --> 事件对象池分配 --> 多态 AO 后处理
                                                    |
                        NVM 日志 / 寄存器控制 / 系统复位 / 用户钩子
```

### 关键数据结构

```c
struct FCCU_FaultTableEntry {
    uint32_t fault_code;
    uint32_t attr;
    uint32_t err_threshold;
    int32_t (*user_hook)(uint32_t, uint64_t, bool);
};

struct FCCU_QueueSet {
    struct SpscQueue *queue[FCCU_QUEUE_LEVELS];
    uint8_t level_count;
    uint32_t item_size;
};

struct FCCU_Event {
    struct QEvt super;       // QPC 事件基类
    uint8_t priority;
    uint8_t fault_index;
    uint64_t fault_detail;
};
```

### C 版的设计亮点与局限

**亮点**:
- 多级 SPSC 环形队列，优先级天然分流
- QPC 活动对象异步后处理，中断安全
- NVM 抽象层，支持 FLASH/EEPROM/文件多种后端
- 用户钩子机制，故障处理可扩展

**局限**:
- 依赖 QPC 框架 (qpc_stub.h)，移植成本高
- 手动管理事件对象池，容易泄漏
- 没有形式化的状态管理 (故障状态用 flag 标记)
- 队列满时仅告警，无准入控制策略
- 故障统计和诊断信息有限

## C++ 版: fccu-cpp 设计

fccu-cpp 是对 C 版的重构，目标: header-only、零堆分配、裸机友好 (无 std::thread)，同时引入 newosp 中久经测试的设计模式。

### 设计决策

| 决策点 | C 版方案 | C++ 版方案 | 理由 |
|--------|---------|-----------|------|
| 队列实现 | 手写 SPSC 队列 | 外部 [ringbuffer](https://github.com/DeguiLiu/ringbuffer) | 复用经过 Sanitizer 验证的库 |
| 状态管理 | bool flag | 两层 [hsm-cpp](https://gitee.com/liudegui/hsm-cpp) HSM | 形式化状态转换，避免非法状态 |
| 事件分发 | QPC AO 模式 | 函数指针 Hook + 外部 ProcessFaults() | 去除框架依赖，裸机友好 |
| 准入控制 | 无 | 4 级阈值 (60%/80%/99%/always) | 防止低优先级故障淹没关键故障 |
| 回调机制 | 裸函数指针 | 函数指针 + void* context | 支持有状态回调，保持零开销 |
| 通知机制 | 无 | mccc AsyncBus (可选) | 解耦故障处理与通知 |
| 调度方式 | QPC AO 异步 | ztask 周期轮询 (可选) | 无线程，适配裸机/RTOS |
| 配置方式 | 宏定义 | 模板参数 | 编译期优化，类型安全 |

### 架构全景

```
                      +---------------------------------------+
                      |       FaultCollector<Config>           |
                      |                                       |
  ReportFault() --->  |  FaultTable    GlobalHsm              |
                      |  (array)       Idle/Active/            |
                      |                Degraded/Shutdown       |
                      |                                       |
                      |  FaultQueueSet                         |
                      |  spsc::Ringbuffer per level            |
                      |  + priority admission control          |
                      |                                       |
  ProcessFaults() --> |  HookAction dispatch                   |
                      |  Handled/Escalate/Defer/Shutdown       |
                      |                                       |
                      |  Per-Fault HSM (optional, <=8)         |
                      |  Dormant->Detected->Active->Cleared    |
                      |                                       |
                      |  Atomic bitmap + Stats + Recent ring   |
                      +-------+---------------+---------------+
                              |               |
                       mccc AsyncBus    ztask Scheduler
                       (optional)       (optional)
```

### 组件复用关系

fccu-cpp 不重复造轮子，而是组合已有组件:

| 组件 | 仓库 | 在 fccu-cpp 中的角色 |
|------|------|---------------------|
| ringbuffer | [DeguiLiu/ringbuffer](https://github.com/DeguiLiu/ringbuffer) | SPSC 队列基础设施 |
| hsm-cpp | [liudegui/hsm-cpp](https://gitee.com/liudegui/hsm-cpp) | 全局 + Per-Fault 状态机 |
| mccc | [DeguiLiu/mccc](https://github.com/DeguiLiu/mccc) | 可选故障通知总线 |
| ztask-cpp | [DeguiLiu/ztask-cpp](https://github.com/DeguiLiu/ztask-cpp) | 可选周期调度 |
| newosp | [DeguiLiu/newosp](https://github.com/DeguiLiu/newosp) | 设计模式来源 (不作为依赖) |

## 关键设计模式 (来自 newosp)

fccu-cpp 从 newosp `fault_collector.hpp` 复用了多个经过 979 条测试验证的模式，但做了裸机适配。

### 1. 优先级准入控制

newosp 的 `AdmitByPriority()` 模式: 队列越满，对低优先级越严格。

```cpp
// fault_queue_set.hpp
template <typename T, uint32_t Levels = 4U, uint32_t LevelSize = 32U>
class FaultQueueSet {
  static constexpr uint32_t kLowThreshold = (LevelSize * 60U) / 100U;
  static constexpr uint32_t kMediumThreshold = (LevelSize * 80U) / 100U;
  static constexpr uint32_t kHighThreshold = (LevelSize * 99U) / 100U;

  bool PushWithAdmission(uint8_t level, const T& item) noexcept {
    auto current_size = queues_[level].size();
    // Critical (level 0): always admit
    // High (level 1): reject when >= 99%
    // Medium (level 2): reject when >= 80%
    // Low (level 3): reject when >= 60%
    uint32_t threshold = LevelSize;
    switch (level) {
      case 1U: threshold = kHighThreshold; break;
      case 2U: threshold = kMediumThreshold; break;
      case 3U: threshold = kLowThreshold; break;
      default: break;  // Critical: no limit
    }
    if (current_size >= threshold) { return false; }
    return queues_[level].push(item);
  }
};
```

直觉解释: 队列满 60% 时先丢 Low，满 80% 时再丢 Medium，满 99% 时丢 High，Critical 只在物理满时才丢弃。

### 2. 原子位图

newosp 的 `SetFaultActive()` / `ClearFaultActive()` + `PopCount64()` 模式:

```cpp
// fccu.hpp
static constexpr uint32_t kBitmapWords = (MaxFaults + 63U) / 64U;
std::array<std::atomic<uint64_t>, kBitmapWords> active_bitmap_{};

void SetFaultActive(uint16_t fault_index) noexcept {
  uint32_t word_idx = fault_index / 64U;
  uint32_t bit_idx = fault_index % 64U;
  active_bitmap_[word_idx].fetch_or(1ULL << bit_idx, std::memory_order_relaxed);
}

uint32_t ActiveFaultCount() const noexcept {
  uint32_t count = 0U;
  for (uint32_t i = 0U; i < kBitmapWords; ++i) {
    count += PopCount64(active_bitmap_[i].load(std::memory_order_relaxed));
  }
  return count;
}
```

256 个故障点只需 4 个 `uint64_t` 字 (32 字节)，`PopCount64` 在 ARMv8 上编译为单条 `cnt` 指令。

### 3. HookAction 四路分发

```cpp
enum class HookAction : uint8_t {
  kHandled = 0U,   // 已处理，清除故障活跃位
  kEscalate = 1U,  // 升级到更高优先级，重新入队
  kDefer = 2U,     // 保持活跃，稍后处理
  kShutdown = 3U   // 请求系统关停
};
```

对比 C 版的 `int32_t` 返回值，枚举类型保证编译期穷举检查。

### 4. FaultReporter 注入点

newosp 的 `FaultReporter` POD 模式，用于将故障上报能力注入到子模块:

```cpp
struct FaultReporter {
  FaultReportFn fn = nullptr;
  void* ctx = nullptr;

  void Report(uint16_t fault_index, uint32_t detail = 0U,
              FaultPriority priority = FaultPriority::kMedium) const noexcept {
    if (fn != nullptr) { fn(fault_index, detail, priority, ctx); }
  }
};

// 使用: 子模块只持有 FaultReporter，不依赖 FaultCollector 头文件
auto reporter = collector.GetReporter();
reporter.Report(0U, 0xBEEF, fccu::FaultPriority::kMedium);
```

16 字节 POD，零间接调用开销，编译防火墙。

## 两层 HSM 设计

C 版用 bool flag 管理故障状态，容易出现非法状态组合。fccu-cpp 引入形式化的层次状态机。

### 全局 FCCU 状态机

管理整个 FCCU 子系统的运行态:

```
       FaultReported        CriticalDetected
Idle ──────────────> Active ───────────────> Degraded
  ^                    |                        |
  |    AllCleared      |      DegradeRecovered  |
  +<───────────────────+<───────────────────────+
                       |
                       | ShutdownReq
                       v
                    Shutdown
```

状态语义:
- **Idle**: 无活跃故障，系统正常
- **Active**: 有非关键故障在处理
- **Degraded**: 检测到 Critical 级故障，限制功能
- **Shutdown**: 收到关停请求，停止故障处理

### Per-Fault 状态机

管理单个关键故障的生命周期 (最多 8 个):

```
          Detected        Confirmed         RecoveryStart
Dormant ─────────> Detected ──────> Active ─────────────> Recovering
   ^                                                          |
   |                          ClearFault                      | RecoveryDone
   +<──────────────────────── Cleared <───────────────────────+
```

Per-Fault HSM 的关键设计:
- `Confirmed` 转换有 guard 条件: `occurrence_count >= threshold`
- 阈值可配: 抖动频繁的信号设置高阈值 (如温度传感器 threshold=5)
- 可选绑定: 只对关键故障启用，节省内存

```cpp
// 绑定 Per-Fault HSM (可选)
collector.BindFaultHsm(0U, 3U);  // fault_index=0, threshold=3
// 连续检测到 3 次后才从 Detected -> Active
```

## 代码示例

### 基本使用

```cpp
#include "fccu/fccu.hpp"

fccu::FaultCollector<16, 8, 4> collector;

// 注册故障点
collector.RegisterFault(0, 0x1001);  // 温度传感器
collector.RegisterFault(1, 0x1002);  // 电压监控

// 注册 Hook
collector.RegisterHook(0, [](const fccu::FaultEvent& e, void*) -> fccu::HookAction {
    printf("Fault 0x%04x: detail=0x%x, count=%u\n",
           e.fault_code, e.detail, e.occurrence_count);
    return fccu::HookAction::kHandled;
});

// 设置关停回调
collector.SetShutdownCallback([](void*) {
    printf("EMERGENCY SHUTDOWN!\n");
});

// 上报故障
collector.ReportFault(0, 0xDEAD, fccu::FaultPriority::kCritical);

// 处理故障 (在主循环或 ztask 回调中)
collector.ProcessFaults();

// 查询状态
printf("Active faults: %u\n", collector.ActiveFaultCount());
printf("HSM state: %s\n", collector.GetGlobalHsm().IsIdle() ? "Idle" : "Active");
```

### mccc 总线集成

故障处理时自动通过消息总线广播通知:

```cpp
#include "fccu/fccu.hpp"
#include "mccc/message_bus.hpp"

struct FaultNotification {
  uint16_t fault_index;
  uint32_t fault_code;
  uint32_t detail;
  uint8_t priority;
};

using BusPayload = std::variant<FaultNotification>;
using Bus = mccc::AsyncBus<BusPayload>;

// 订阅故障通知
Bus& bus = Bus::Instance();
bus.Subscribe<FaultNotification>([](const Bus::EnvelopeType& env) {
    if (auto* msg = std::get_if<FaultNotification>(&env.payload)) {
        printf("Bus: fault 0x%04x pri=%u\n", msg->fault_code, msg->priority);
    }
});

// 设置 FCCU 的总线通知回调
collector.SetBusNotifier([](const fccu::FaultEvent& event, void* ctx) {
    auto* bus = static_cast<Bus*>(ctx);
    FaultNotification msg{event.fault_index, event.fault_code,
                          event.detail, static_cast<uint8_t>(event.priority)};
    bus->Publish(BusPayload{msg}, 0U);
}, &bus);
```

### ztask 周期调度

无需手动调用 ProcessFaults()，交给协作式调度器:

```cpp
#include "fccu/fccu.hpp"
#include "ztask/task_scheduler.hpp"

fccu::FaultCollector<16, 8, 4> collector;
ztask::TaskScheduler<8> scheduler;

// 注册周期任务: 每 10ms 处理一次故障队列
scheduler.Bind("fccu_proc", 10, [](void* ctx) {
    static_cast<decltype(&collector)>(ctx)->ProcessFaults();
}, &collector);

// 主循环
while (!collector.IsShutdownRequested()) {
    scheduler.Tick();
}
```

## 测试覆盖

fccu-cpp 包含 38 个 Catch2 测试用例，覆盖:

| 测试类别 | 数量 | 覆盖内容 |
|---------|------|---------|
| 注册 | 4 | 正常/重复/越界/Hook 前置检查 |
| 上报与处理 | 4 | 基本流程/未注册/越界/多优先级 |
| HookAction | 5 | Handled/Defer/Escalate/Shutdown/Default |
| 准入控制 | 2 | 低优先级丢弃/Critical 始终准入 |
| 统计 | 2 | 计数准确性/重置 |
| 全局 HSM | 4 | 初始状态/转换/恢复/关停 |
| Per-Fault HSM | 3 | 绑定/槽位限制/完整生命周期 |
| 清除 | 2 | 单个/全部 |
| 溢出 | 1 | 回调触发 |
| 背压 | 1 | 初始等级 |
| FaultReporter | 2 | 注入点/空指针安全 |
| 近期故障环 | 1 | 遍历顺序 |
| 队列独立 | 4 | Push/Pop/优先级序/准入/越界 |

所有测试在 ASan + UBSan 下通过。

## 与 newosp FaultCollector 的关系

fccu-cpp 与 newosp `fault_collector.hpp` 共享设计模式，但定位不同:

| 维度 | newosp FaultCollector | fccu-cpp |
|------|----------------------|----------|
| 定位 | 内置模块，服务 newosp 生态 | 独立库，可单独引用 |
| 队列 | 内置 MPSC CAS 队列 | 外部 ringbuffer (SPSC) |
| 消费者 | std::thread + condition_variable | 外部调用 ProcessFaults() |
| 状态管理 | atomic bool | hsm-cpp 两层 HSM |
| 通知 | 无 | mccc AsyncBus (可选) |
| 平台 | Linux (依赖 std::thread) | 裸机友好 (无 OS 依赖) |
| 测试 | newosp 内部测试 | 独立 38 test cases |

选择建议:
- **已使用 newosp 生态**: 直接使用 newosp 内置的 FaultCollector
- **裸机/RTOS 项目**: 使用 fccu-cpp
- **需要形式化状态管理**: 使用 fccu-cpp (HSM 保证状态合法性)
- **多生产者场景**: 使用 newosp (MPSC 队列) 或 fccu-cpp + mccc 前端

## 构建与验证

```bash
# 基本构建
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j

# 运行测试
cd build && ctest --output-on-failure

# ASan + UBSan 验证
cmake -B build -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_CXX_FLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer"
cmake --build build -j && ctest --output-on-failure

# 中国大陆加速
cmake -B build -DFCCU_GITHUB_MIRROR="https://ghfast.top/"
```

## 总结

| 演进方向 | C 版 (fccu_linux_demo) | C++ 版 (fccu-cpp) |
|---------|----------------------|-------------------|
| 框架依赖 | QPC 事件驱动 | 无框架依赖 |
| 队列安全 | 手写 SPSC | 复用 ringbuffer 库 |
| 状态管理 | bool flag | 两层 HSM |
| 内存模型 | 静态分配 | 零堆分配 (模板参数化) |
| 准入控制 | 无 | 4 级阈值 |
| 故障追踪 | 有限 | 原子位图 + PopCount64 |
| 统计诊断 | 基础 | per-priority 原子计数 + 近期环 |
| 集成能力 | 绑定 QPC | mccc/ztask 可选集成 |
| 测试 | demo 验证 | 38 Catch2 tests + Sanitizer |

核心原则不变: 统一收集、优先级分流、Hook 后处理、零堆分配。C++ 版在此基础上引入形式化状态管理和准入控制，将手写基础设施替换为经过验证的外部组件。

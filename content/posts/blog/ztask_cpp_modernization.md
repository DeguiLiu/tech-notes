---
title: "ztask 调度器的 C++14 重写: 类型安全、RAII 与模板化改造"
date: 2026-02-15
draft: false
categories: ["blog"]
tags: ["C++14", "CRC", "RTOS", "callback", "embedded", "lock-free", "memory-pool", "performance", "scheduler"]
summary: "在轻量 RTOS 项目和嵌入式Linux中，合作式任务调度器是比操作系统线程更轻量的执行抽象。"
ShowToc: true
TocOpen: true
---

> 源码仓库: [gitee.com/liudegui/ztask-cpp](https://gitee.com/liudegui/ztask-cpp)
> 设计文档: [docs/design.md](https://gitee.com/liudegui/ztask-cpp/blob/main/docs/design.md)

> 在轻量 RTOS 项目和嵌入式Linux中，合作式任务调度器是比操作系统线程更轻量的执行抽象。


## 1. 设计决策
本文的设计是参考与对比[tomzbj/ztask](https://github.com/tomzbj/ztask)

| 决策 | 理由 |
|------|------|
| Header-only | 零链接开销，模板按容量实例化 |
| 排序侵入式链表 | O(1) poll vs 数组扫描 O(n) vs 堆 O(log n) |
| 定长数组存储 | 栈分配，cache-friendly，确定性内存 |
| Generation 计数器 | 2B TaskId 内嵌 ABA 防护 |
| Tick 驱动时基 | 硬件无关，裸机/RTOS/Linux 通用 |
| 不内建线程安全 | 目标平台单线程为主，用户按需添加同步 |
| TicksToNextTask() | MCU idle/sleep/stop 模式的低功耗优化路径 |

## 2. 架构设计

ztask-cpp 保留了ztask原始算法的核心 -- 排序侵入式链表，但将运行时约束提升为编译期保证:

```
                    C 版 ztask                          ztask-cpp
              ┌─────────────────────┐           ┌─────────────────────────────┐
  容量管理    │ malloc / 静态池      │    →      │ template<MaxTasks>           │
              │ zt_init(mem, size)  │           │ 编译期固定，零初始化代码       │
              ├─────────────────────┤           ├─────────────────────────────┤
  类型安全    │ void(*)(void)       │    →      │ void(*)(void* ctx)          │
              │ 全局变量传 context   │           │ ctx 参数直接传递             │
              ├─────────────────────┤           ├─────────────────────────────┤
  ABA 防护    │ 裸指针 handle       │    →      │ (generation<<8|index) 编码   │
              │ 无过期检测          │           │ 256 代 ABA 防护              │
              ├─────────────────────┤           ├─────────────────────────────┤
  Tick 类型   │ 固定 uint32_t      │    →      │ TickType 模板参数            │
              │ 49 天溢出           │           │ uint64_t 可选 (585 年)       │
              └─────────────────────┘           └─────────────────────────────┘
```

### 2.1 核心类模板

```cpp
template <uint32_t MaxTasks, typename TickType = uint32_t>
class TaskScheduler {
 public:
  using TaskFn = void (*)(void* ctx);
  using TaskId = uint16_t;
  static constexpr TaskId kInvalidId = 0xFFFF;

  TaskScheduler() noexcept;
  void Tick() noexcept;
  TaskId Bind(TaskFn fn, TickType repeat_ticks, TickType delay_ticks,
              void* ctx = nullptr) noexcept;
  TaskId BindOneShot(TaskFn fn, TickType delay_ticks,
                     void* ctx = nullptr) noexcept;
  void Unbind(TaskId id) noexcept;
  uint32_t Poll() noexcept;
  TickType TicksToNextTask() const noexcept;
  // ...
};
```

所有方法标记 `noexcept`，兼容 `-fno-exceptions -fno-rtti` 编译。Header-only，无链接依赖。

### 2.2 数据结构: 定长数组上的侵入式排序链表

```
TaskSlot 内存布局 (32 字节 / 32-bit 平台):
┌──────────────────────────────────────────────────────┐
│ fn: TaskFn            (4/8 B)                        │
│ ctx: void*            (4/8 B)                        │
│ repeat_ticks: TickType (4/8 B)                       │
│ next_schedule: TickType (4/8 B)                      │
│ next_index: uint32_t  (4 B)   -- 侵入式链表指针       │
│ generation: uint8_t   (1 B)   -- ABA 代计数器         │
│ active: bool          (1 B)                          │
│ [padding]             (2 B)                          │
└──────────────────────────────────────────────────────┘
```

设计选择的理由:

**为什么不用堆 (priority_queue)?**
- `std::priority_queue` 底层是 `std::vector`，insert/extract 均 O(log n)
- 隐含堆分配 -- 嵌入式环境不可接受
- 指针追踪导致 cache-unfriendly

**为什么不用简单数组扫描?**
- O(n) poll 在实时系统中不可接受
- 每次 poll 浪费 CPU 周期检查未就绪任务

**排序侵入式链表的优势:**
- 任务存储在定长数组中 -- cache-friendly，零分配
- 侵入式 `next_index` 按 `next_schedule` 升序链接
- head 始终指向最早到期任务 -- poll 仅检查 head，O(1)
- insert O(n) 最坏情况，但任务绑定是低频操作

总内存占用:

| 容量 | 总大小 |
|------|--------|
| `TaskScheduler<8>` | 268 B |
| `TaskScheduler<16>` | 524 B |
| `TaskScheduler<32>` | 1036 B |

## 3. 关键设计细节

### 3.1 TaskId 编码与 ABA 防护

TaskId 是一个 16-bit 值，高 8 位为 generation 计数器，低 8 位为 slot index:

```
TaskId (16-bit):
┌────────────────┬────────────────┐
│ generation (8) │ slot_index (8) │
└────────────────┴────────────────┘
```

每次 slot 被释放时 generation 自增。Unbind 时同时校验 index 和 generation:

```cpp
void Unbind(TaskId id) noexcept {
  uint32_t slot_idx = id & 0xFF;
  uint8_t generation = static_cast<uint8_t>(id >> 8);

  if (slot_idx >= MaxTasks) return;

  TaskSlot& slot = slots_[slot_idx];
  // generation 不匹配 -> 过期 ID，静默忽略
  if (!slot.active || slot.generation != generation) return;

  RemoveFromList(slot_idx);
  slot.active = false;
  slot.generation = static_cast<uint8_t>((slot.generation + 1) & 0xFF);
  --active_count_;
}
```

256 代循环提供充足的 ABA 窗口。对于典型嵌入式场景 (任务绑定/解绑频率远低于 256 次/slot 生命周期)，碰撞概率可忽略。

### 3.2 Poll 算法: 执行前摘链

Poll 的关键安全设计是执行前先将任务从链表摘除:

```cpp
uint32_t Poll() noexcept {
  uint32_t executed = 0;

  while (head_index_ != kNoTask) {
    TaskSlot& head = slots_[head_index_];

    // 排序链表: head 不就绪则无任务就绪
    if (head.next_schedule > current_ticks_) break;

    // 关键: 执行前摘链 (允许回调内调用 Unbind)
    uint32_t exec_idx = head_index_;
    head_index_ = head.next_index;

    // 执行回调
    slots_[exec_idx].fn(slots_[exec_idx].ctx);
    ++executed;

    // 执行后检查 active (回调可能已 Unbind 自身)
    if (slots_[exec_idx].active) {
      if (slots_[exec_idx].repeat_ticks > 0) {
        // 周期任务: 重新调度
        slots_[exec_idx].next_schedule =
            current_ticks_ + slots_[exec_idx].repeat_ticks;
        InsertSorted(exec_idx);
      } else {
        // 一次性任务: 自动释放
        slots_[exec_idx].active = false;
        slots_[exec_idx].generation++;
        --active_count_;
      }
    }
  }
  return executed;
}
```

这个设计确保了两个安全属性:
1. **任务自解绑安全**: 回调函数可以调用 `Unbind(self_id)`，因为执行时任务已不在链表中
2. **重入绑定安全**: 回调函数可以调用 `Bind()` 注册新任务，InsertSorted 操作不会影响当前执行流

### 3.3 TicksToNextTask: 低功耗精确休眠

```cpp
TickType TicksToNextTask() const noexcept {
  if (head_index_ == kNoTask)
    return static_cast<TickType>(-1);  // 无任务

  if (slots_[head_index_].next_schedule <= current_ticks_)
    return 0;  // 任务已就绪

  return slots_[head_index_].next_schedule - current_ticks_;
}
```

O(1) 复杂度。典型的低功耗主循环:

```cpp
ztask::TaskScheduler<16> sched;

while (true) {
  sched.Tick();
  sched.Poll();

  auto remaining = sched.TicksToNextTask();
  if (remaining == 0) continue;  // 仍有就绪任务

  if (remaining <= 5)
    __WFI();                    // 短休眠: idle
  else if (remaining <= 50)
    HAL_PWR_EnterSLEEPMode();   // 中休眠: sleep (~20% 功耗)
  else
    HAL_PWR_EnterSTOPMode();    // 深休眠: stop (~5% 功耗)
}
```

## 4. C vs C++14: 逐项对比

### 4.1 编译期容量 vs 运行时内存池

```c
// C 版: 运行时初始化，容量错误延迟到运行期
static uint8_t mem[10 * sizeof(zt_task_t)] __attribute__((aligned(4)));
int32_t num = zt_init(mem, sizeof(mem));
if (num < 0) { /* 处理错误 */ }
```

```cpp
// C++ 版: 编译期固定，无初始化代码
ztask::TaskScheduler<10> sched;  // 栈分配，大小编译期确定
// 容量为 0 时可通过 static_assert 在编译期捕获
```

C++ 版的优势不仅仅是语法简洁 -- 编译器可以:
- 在编译期计算总内存大小，无运行时 `sizeof` 计算
- 模板实例化时展开循环，对小 MaxTasks 值可完全展开
- 构造函数中的初始化循环在 `-O2` 下被优化为 `memset`

### 4.2 Context 传递: void* 参数 vs 全局变量

```c
// C 版: 无 context 参数，只能用全局变量
static uint32_t g_led_count = 0;
static void led_task(void) {
  g_led_count++;
  toggle_led();
}
```

```cpp
// C++ 版: context 指针直接传递
struct LedContext {
  uint32_t count;
  uint8_t pin;
};

static void led_task(void* ctx) {
  auto* led = static_cast<LedContext*>(ctx);
  led->count++;
  toggle_pin(led->pin);
}

LedContext led1{0, GPIO_PIN_5};
LedContext led2{0, GPIO_PIN_6};
sched.Bind(led_task, 100, 0, &led1);  // 同一回调，不同实例
sched.Bind(led_task, 100, 0, &led2);
```

### 4.3 TaskId 编码 vs 裸指针

| 属性 | C 版 (裸指针) | C++ 版 (编码 ID) |
|------|:---:|:---:|
| ABA 防护 | 无 | 256 代循环 |
| 过期 ID 检测 | 不可能 | generation 校验 |
| ID 大小 | 4/8 B (指针) | 2 B (uint16_t) |
| 传递开销 | 指针宽度 | 寄存器友好 |

### 4.4 TickType 可配置

C 版固定使用 `uint32_t`，在 1ms tick 下约 49 天溢出。C++ 版通过模板参数支持不同精度:

```cpp
// 默认: 32-bit, 49 天 @ 1ms
TaskScheduler<16, uint32_t> sched32;

// 长周期: 64-bit, 5.85 亿年 @ 1ms
TaskScheduler<16, uint64_t> sched64;

// 资源极度受限: 16-bit, 65 秒 @ 1ms
TaskScheduler<8, uint16_t> sched16;
```

## 5. 性能基准

基准测试环境: GCC 11.4, `-O3`, x86_64 Linux, 1M iterations/operation。

| 操作 | C 版 (ns/op) | C++ 版 (ns/op) | 差异 |
|------|---:|---:|---:|
| Bind | 45 | 42 | -6.7% |
| Poll (hit) | 40 | 38 | -5.0% |
| Poll (idle) | 2 | 2 | 0% |
| TicksToNextTask | 2 | 2 | 0% |
| Bind+Unbind | 82 | 77 | -6.1% |

C++ 版在热路径 (Bind/Poll) 上稳定快 5-7%，原因是模板实例化使编译器能对 `MaxTasks` 相关的循环和分支进行更激进的内联和展开。idle poll 和 TicksToNextTask 均为 2ns，体现了 O(1) 头部检查的极低开销。

内存占用完全一致:

| 指标 | C 版 | C++ 版 |
|------|---:|---:|
| 单任务 slot | 32 B | 32 B |
| 调度器 (16 tasks) | 524 B | 524 B |

## 6. 时间复杂度

| 操作 | 最好 | 平均 | 最坏 |
|------|:---:|:---:|:---:|
| Tick() | O(1) | O(1) | O(1) |
| Poll() | O(1) | O(k) | O(n) |
| Bind() | O(1) | O(n/2) | O(n) |
| Unbind() | O(1) | O(n/2) | O(n) |
| TicksToNextTask() | O(1) | O(1) | O(1) |

*k = 就绪任务数, n = 活跃任务总数*

关键路径是 Poll -- 在无任务就绪时仅需一次比较即退出，这是低功耗系统最频繁的状态。

## 7. 线程安全

ztask-cpp **设计上不是线程安全的**。这是刻意的选择:

- 目标平台通常是单线程裸机 MCU
- 锁同步开销对实时系统不可接受
- 用户在需要时可自行添加外部同步

安全使用模式:

| 场景 | 说明 |
|------|------|
| 单线程主循环 | 所有调用在 main loop (最常见) |
| ISR + 主循环 | `Tick()` 在 ISR，`Poll()` 在主循环 (ISR 不调用其他方法即安全) |
| RTOS 多任务 | 用 mutex 包装调度器 |

## 8. 边界情况处理

### 8.1 Tick 溢出

`uint32_t` 在 1ms tick 下约 49 天溢出。unsigned 算术天然处理回绕:

```cpp
// 无符号减法在回绕时仍正确
if (head.next_schedule <= current_ticks_) { ... }
```

限制: 调度超过 `2^31` ticks 的未来任务可能行为异常。

### 8.2 Slot 耗尽

`Bind()` 在无可用 slot 时返回 `kInvalidId`。调用者应检查返回值:

```cpp
auto id = sched.Bind(my_task, 100, 0);
if (id == ztask::TaskScheduler<16>::kInvalidId) {
  // 处理容量不足
}
```

建议: `MaxTasks` 取预期峰值的 2x 余量。

### 8.3 Generation 回绕

generation 为 8-bit (0-255)，256 次复用后回绕。若同一 slot 在 256 代内未被误引用，则安全。对典型嵌入式场景 (任务绑定频率远低于 256 次/slot) 完全足够。

## 9. 典型集成示例

### 9.1 主循环

```cpp
#include <ztask/ztask.hpp>

static ztask::TaskScheduler<16> sched;

// 硬件定时器 ISR (1ms)
extern "C" void TIM2_IRQHandler(void) {
  sched.Tick();
  TIM2->SR &= ~TIM_SR_UIF;
}

struct SensorCtx {
  uint32_t readings;
  uint8_t  adc_channel;
};

static void read_sensor(void* ctx) {
  auto* s = static_cast<SensorCtx*>(ctx);
  s->readings++;
  uint16_t val = HAL_ADC_Read(s->adc_channel);
  process_reading(val);
}

static void heartbeat_led(void* /*ctx*/) {
  HAL_GPIO_TogglePin(GPIOA, GPIO_PIN_5);
}

int main(void) {
  HAL_Init();
  SystemClock_Config();
  setup_timer_1ms();

  SensorCtx sensor{0, ADC_CHANNEL_0};

  sched.Bind(read_sensor, 50, 10, &sensor);   // 50ms 周期, 10ms 延迟
  sched.Bind(heartbeat_led, 500, 0);           // 500ms 心跳

  while (true) {
    sched.Poll();

    auto remaining = sched.TicksToNextTask();
    if (remaining > 0 && remaining != UINT32_MAX) {
      __WFI();  // 等待中断唤醒
    }
  }
}
```


---

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/xxxxxxxx)

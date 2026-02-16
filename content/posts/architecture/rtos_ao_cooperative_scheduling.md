---
title: "RTOS + Active Object 协同调度优化: 从浅层适配到深度融合"
date: 2026-02-16
draft: false
categories: ["architecture"]
tags: ["RTOS", "Active-Object", "embedded", "real-time", "QP/C", "RT-Thread", "scheduling", "lock-free"]
summary: "主动对象 (AO) 框架移植到 RTOS 的主流做法是浅层 API 映射，存在调度开销大、双重内存拷贝、缺乏差异化处理等七项缺陷。本文介绍一种协同调度优化层设计，在 AO 框架与 RTOS 内核之间构建快速路径直接派发、零拷贝传递、分级暂存批处理、可插拔策略引擎四大机制。RISC-V 37MHz 平台实测: 中断响应延迟下降 80.5%，吞吐量提升 314%，上下文切换减少 99%。"
ShowToc: true
TocOpen: true
---

## 1. 问题: 浅层适配的七个缺陷

### 1.1 主动对象模式回顾

主动对象 (Active Object, AO) 是一种并发设计模式，每个 AO 拥有三个基本要素:

1. **独立的执行线程**: 每个 AO 在独立的 RTOS 线程中运行
2. **私有的事件队列**: 外部通过向该队列发送事件来与 AO 通信
3. **内部状态机**: AO 内部使用层次状态机 (HSM) 处理事件

这种模式通过异步事件通信避免了共享数据和互斥锁带来的并发问题。典型实现包括 QP/C 和 QP/C++ 等框架。**1 个 AO = 1 个 RTOS 线程 = 1 个可调度实体**。

### 1.2 当前主流移植方式

将 AO 框架移植到 RTOS 的主流做法是**浅层适配**: 为每个 AO 创建独立的 RTOS 线程和消息队列，通过 API 映射实现功能集成。每个 AO 对应一个 RTOS 线程; 事件队列直接使用 RTOS 消息队列或邮箱; 事件投递时将数据拷贝入队，接收时再拷贝出队。

这种方式实现简单、移植快速，但仅停留在 API 映射层面，未对事件传递和调度流程进行深度优化。

### 1.3 七个具体缺陷

**缺陷一: 破坏 run-to-complete 语义。** AO 的核心约束是事件处理函数必须不间断执行完毕，不可在中途阻塞。浅层适配未建立语义隔离层，使得 RTOS 的阻塞语义泄漏到了事件驱动框架内部。

**缺陷二: 调度开销大。** 一个事件从产生到处理需经历"入队 -> memcpy -> 调度器介入 -> 上下文保存 -> 线程切换 -> 上下文恢复 -> 出队 -> memcpy -> 处理"的完整链路。在 RISC-V 37MHz 处理器上，一次完整上下文切换消耗约 4000+ CPU 周期，响应延迟达 118us。

**缺陷三: 双重内存拷贝。** RTOS 消息队列采用值拷贝语义，发送时拷贝入队，接收时拷贝出队。32 字节事件每次传递需 64 字节拷贝，高频场景下内存拷贝可占传递总耗时 30% 以上。

**缺陷四: 高优先级事件无差异化处理。** 所有事件无论紧急程度都必须走完相同的全流程。

**缺陷五: 缺乏突发批量处理能力。** 短时间大量事件每个独立触发一次上下文切换，造成"调度风暴"，同一事件处理延迟的变异性高达 200%。

**缺陷六: 策略固化。** 没有机制根据负载动态合并冗余事件或在过载时智能丢弃非关键事件。

**缺陷七: 资源消耗大。** 每个 AO 需要独立的 RTOS 线程和消息队列，在资源受限的 MCU 上限制了可创建的 AO 数量。

## 2. 方案: 协同调度优化层

在 AO 框架与 RTOS 内核之间构建**协同调度优化层**，作为统一的事件派发决策中枢。该层包含六个功能模块: 直接派发模块、零拷贝传递模块、分级暂存批处理模块、策略引擎模块、运行时监控模块、熔断/恢复管理模块。

```mermaid
flowchart LR
    APP["应用层\nAO 主动对象"] --> |事件发布| QF

    QF["事件驱动框架\n事件池/HSM引擎\n时间服务/发布订阅"] --> |事件投递| OPT

    OPT["协同调度优化层\n路径决策器\n快速路径\n分级暂存缓冲区\n安全卫士"]

    OPT --> |快速派发| APP
    OPT --> |批量派发| APP
    OPT --> |系统调用| RTOS["RTOS内核"]

    RTOS --> |中断通知| OPT
    RTOS --> |线程调度| QF

    HW["硬件平台\n中断/定时器"] --> |硬件中断| RTOS
```

**初始化顺序:**

| 阶段 | 关键操作 | 依赖 |
|------|---------|------|
| 第一阶段: RTOS 初始化 | 创建内存堆、空闲线程、滴答定时器 | 无 |
| 第二阶段: 优化层初始化 | 分配三级缓冲区; 创建派发器线程; 初始化信号量; 重置熔断状态 | 依赖第一阶段 |
| 第三阶段: 框架与业务初始化 | 创建事件池; 启动各 AO 线程; 注册快速派发属性 | 依赖第二阶段 |

### 2.1 核心数据结构

优化层的全部状态集中在一个静态分配的结构体中，不依赖动态内存:

```c
/* 暂存缓冲区槽位: 仅存储指针，体现零拷贝原则 */
typedef struct {
    QEvt const *evt;       /* 事件指针 (不拷贝事件本体) */
    QActive    *target;    /* 目标 AO 指针 */
    uint32_t    timestamp; /* 入队时间戳 (tick) */
} StagingSlot;

/* 分级环形缓冲区 */
typedef struct {
    StagingSlot  slots[QF_STAGING_CAPACITY];
    uint16_t     head;     /* 消费索引 */
    uint16_t     tail;     /* 生产索引 */
    uint16_t     count;    /* 当前元素数 */
    uint16_t     capacity; /* 最大容量 */
} StagingRing;

/* 每个 AO 的快速派发运行时属性 */
typedef struct {
    uint8_t  directDispatchEnabled;  /* C1: 资格预声明 */
    uint8_t  violationCount;         /* 连续违规计数 */
    uint16_t cooldownRemain;         /* 熔断恢复倒计时 */
    uint32_t lastExecCycles;         /* 上次执行耗时 (CPU cycle) */
} AO_FastPathAttr;

/* 策略引擎: 四个函数指针 */
typedef struct {
    bool (*shouldMerge)(QEvt const *prev, QEvt const *next);
    bool (*shouldDrop)(QEvt const *evt, QActive const *target);
    uint8_t (*getPrioLevel)(QEvt const *evt);
    int8_t  (*comparePriority)(QEvt const *a, QEvt const *b);
} DispatchPolicy;

/* 运行时统计 */
typedef struct {
    uint32_t fastPathCount;     /* 快速路径派发次数 */
    uint32_t batchPathCount;    /* 批处理路径派发次数 */
    uint32_t mergedCount;       /* 合并事件数 */
    uint32_t droppedCount;      /* 丢弃事件数 */
    uint32_t retryCount;        /* 背压重试次数 */
    uint32_t circuitBreakCount; /* 熔断触发次数 */
} DispatchStats;

/* 协同调度优化层: 全局单例，静态分配 */
typedef struct {
    StagingRing     rings[3];    /* HIGH=0, NORMAL=1, LOW=2 */
    AO_FastPathAttr aoAttrs[QF_MAX_ACTIVE]; /* 每个 AO 的属性 */
    DispatchPolicy  policy;      /* 当前策略 (函数指针表) */
    DispatchStats   stats;       /* 运行时统计 */
    rt_sem_t        dispatchSem; /* 派发器信号量 */
    volatile uint8_t nestingDepth; /* 当前直接派发嵌套深度 */
} CoopScheduler;
```

**内存布局说明:** `StagingSlot` 每槽 12 字节 (32 位平台) 或 16 字节 (64 位平台)。三级缓冲区共 224 槽 (32+64+128)，32 位平台总计 2688 字节。`AO_FastPathAttr` 每个 AO 占 8 字节，16 个 AO 共 128 字节。整个优化层静态内存开销约 3KB，不触发任何堆分配。

## 3. 快速路径: 直接派发机制

### 3.1 核心思路: 解耦事件派发与目标线程

传统方案中，事件派发**必须**在目标 AO 的专属线程中执行。核心创新在于**解耦事件派发与目标 AO 线程**，使 dispatch 可以发生在不同的执行上下文中:

| 路径 | 谁执行 dispatch | 线程切换次数 |
|------|----------------|-------------|
| 快速路径 (ISR 上下文) | 当前中断处理函数直接调用目标 AO 的 dispatch | 0 次 |
| 快速路径 (线程上下文) | 发送者线程直接调用目标 AO 的 dispatch | 0 次 |
| 批处理路径 | 派发器线程投递到目标 AO 队列，目标 AO 线程自己 dispatch | 1 次 |

dispatch 本质上是一次函数调用。只要满足安全准入条件，这个函数调用不必非在目标 AO 的线程中发生。

### 3.2 四项安全准入检查

快速路径仅在以下四项检查**全部通过**时启用，任一不通过则自动降级到批处理路径 (事件不丢失):

**C1: 资格预声明。** 目标 AO 须在初始化阶段显式声明"直接派发"属性。"默认关闭，显式启用"，防止误用。

**C2: 优先级匹配。** 发送者线程优先级须高于目标 AO 线程优先级。若目标 AO 优先级更高，RTOS 抢占式调度本身已能保证快速响应; 反过来在低优先级上下文执行高优先级 AO 的处理函数会导致优先级反转。ISR 上下文自动通过此检查。

**C3: 嵌套深度。** 当前直接派发递归嵌套深度须小于阈值 (默认 3 层)。限制嵌套深度防止栈溢出 (嵌入式系统栈通常仅 2-4KB)，保证实时确定性，防止中断风暴。

```
// 嵌套场景示例
ISR_1() {
    // nestingDepth = 0 -> 1
    ao1->dispatch(evt1);  // 直接派发
        ISR_2() {         // 中断抢占
            // nestingDepth = 1 -> 2
            ao2->dispatch(evt2);
                ISR_3() { // 再次抢占
                    // nestingDepth = 2 -> 3 (达到阈值)
                    // 拒绝直接派发，放入暂存缓冲区
                }
        }
}
```

嵌套深度达到阈值时，事件指针被放入分级暂存缓冲区，释放信号量通知派发器线程，当前 ISR 正常返回。

**C4: 队列水位。** 目标 AO 的事件队列须有足够空闲空间。当队列剩余容量低于安全阈值 (默认 25%) 时拒绝快速路径，避免在 ISR 上下文中因队列满而阻塞。

```c
/* 四项安全准入检查的完整实现 */
static bool fastpath_check(CoopScheduler *cs,
                           QActive *target,
                           uint8_t senderPrio)
{
    AO_FastPathAttr *attr = &cs->aoAttrs[target->prio];

    /* C1: 资格预声明 */
    if (!attr->directDispatchEnabled) {
        return false;
    }

    /* C1.5: 熔断状态检查 */
    if (attr->cooldownRemain > 0U) {
        return false;  /* 熔断中，强制走批处理 */
    }

    /* C2: 优先级匹配 (ISR 上下文视为最高优先级) */
    if ((senderPrio != ISR_CONTEXT_PRIO) &&
        (senderPrio <= target->prio)) {
        return false;
    }

    /* C3: 嵌套深度 */
    if (cs->nestingDepth >= QF_MAX_NEST_DEPTH) {
        return false;
    }

    /* C4: 队列水位 (剩余 > 25%) */
    uint16_t remain = target->eQueue.maxLen - target->eQueue.nUsed;
    if (remain < (target->eQueue.maxLen >> 2)) {
        return false;
    }

    return true;
}
```

### 3.3 事件路径决策流程

```mermaid
flowchart TD
    Start(["事件产生"]) --> Query["查询目标AO属性"]
    Query --> C1{"C1: AO声明快速资格?"}

    C1 -->|否| B1
    C1 -->|是| C2{"C2: 发送者优先级 > 目标AO?"}

    C2 -->|否| B1
    C2 -->|是| C3{"C3: 嵌套深度 < 阈值?"}

    C3 -->|否| B1
    C3 -->|是| C4{"C4: 队列水位安全?"}

    C4 -->|否| B1
    C4 -->|是| F1

    subgraph FastPath ["快速路径: 直接在当前上下文处理"]
        F1["1. 记录入口时间戳"]
        F2["2. 嵌套深度+1"]
        F3["3. 直接调用AO的dispatch"]
        F4["4. 嵌套深度-1"]
        F5["5. 记录出口时间戳"]
        F1 --> F2 --> F3 --> F4 --> F5
    end

    subgraph BatchPath ["批处理路径: 暂存后统一处理"]
        B1["1. 策略引擎判定优先级"]
        B2["2. 事件指针存入暂存缓冲区"]
        B3["3. 释放信号量"]
        B1 --> B2 --> B3
    end

    F5 --> TimeCheck{"执行时间 <= 时限?"}
    TimeCheck -->|是| Normal["维持快速路径资格"]
    TimeCheck -->|否| Violation["违规计数+1"]
    Violation --> BreakCheck{"连续违规 >= 3?"}
    BreakCheck -->|否| Done1(["完成"])
    BreakCheck -->|是| CircuitBreak["触发熔断"]
    Normal --> Done3(["完成"])
    B3 --> Done4(["派发器线程批量处理"])
```

### 3.4 熔断保护机制

三层安全保护:

**第一层: 执行时间监控。** 每次快速派发记录进出时间戳，超过预设时限 (默认 50us) 即记录违规。

**第二层: 违规计数与熔断。** 每个 AO 维护连续违规计数器。连续违规达阈值 (默认 3 次) 触发熔断，该 AO 后续所有事件自动降级到批处理路径，实现故障隔离。

**第三层: 自动恢复。** 熔断后进入冷却期 (默认 100 个派发周期)，期满自动恢复资格并重置计数。

```mermaid
stateDiagram-v2
    [*] --> Normal
    Normal --> Normal: 执行时间正常
    Normal --> Warning: 执行超时, 违规+1
    Warning --> Normal: 连续正常, 违规重置
    Warning --> Warning: 再次超时, 违规+1
    Warning --> CircuitBreak: 违规 >= 3, 触发熔断
    CircuitBreak --> CircuitBreak: 事件强制走批处理
    CircuitBreak --> Recovery: 冷却100个周期后
    Recovery --> Normal: 恢复成功
    Recovery --> CircuitBreak: 再次超时
```

| 参数 | 配置宏 | 默认值 |
|------|--------|--------|
| 最大嵌套深度 | QF_MAX_NEST_DEPTH | 3 |
| 最大执行时间 | QF_MAX_EXEC_TIME_US | 50us |
| 熔断阈值 | QF_VIOLATION_THRESHOLD | 3 |
| 恢复周期 | QF_RECOVERY_CYCLES | 100 |

### 3.5 事件投递入口实现

以下为优化层统一事件投递函数的核心逻辑，替换 QP/C 原生的 `QACTIVE_POST`:

```c
void CoopSched_post(CoopScheduler *cs,
                    QActive       *target,
                    QEvt const    *evt,
                    uint8_t        senderPrio)
{
    /* --- 快速路径尝试 --- */
    if (fastpath_check(cs, target, senderPrio)) {
        uint32_t t0 = bsp_timestamp_cycles();

        cs->nestingDepth++;
        target->super.vptr->dispatch(&target->super, evt);
        cs->nestingDepth--;

        uint32_t elapsed = bsp_timestamp_cycles() - t0;
        cs->stats.fastPathCount++;

        /* 执行时间监控 */
        AO_FastPathAttr *attr = &cs->aoAttrs[target->prio];
        attr->lastExecCycles = elapsed;

        if (elapsed > QF_MAX_EXEC_CYCLES) {
            attr->violationCount++;
            if (attr->violationCount >= QF_VIOLATION_THRESHOLD) {
                attr->cooldownRemain = QF_RECOVERY_CYCLES;
                attr->violationCount = 0U;
                cs->stats.circuitBreakCount++;
            }
        } else {
            attr->violationCount = 0U;  /* 连续正常，重置 */
        }
        return;
    }

    /* --- 批处理路径: 暂存到分级缓冲区 --- */
    uint8_t level = cs->policy.getPrioLevel(evt);
    StagingRing *ring = &cs->rings[level];

    if (ring->count < ring->capacity) {
        StagingSlot *slot = &ring->slots[ring->tail];
        slot->evt       = evt;
        slot->target    = target;
        slot->timestamp = rt_tick_get();

        QF_MEM_WRITE_BARRIER();  /* 写屏障: 确保内容对派发器可见 */

        ring->tail = (ring->tail + 1U) % ring->capacity;
        ring->count++;

        rt_sem_release(cs->dispatchSem);  /* 唤醒派发器线程 */
        cs->stats.batchPathCount++;
    } else {
        /* 缓冲区满: 背压处理 */
        cs->stats.droppedCount++;
    }
}
```

这段代码展示了快速路径与批处理路径的完整决策链: 四项检查通过则在当前上下文直接 dispatch，否则将事件指针存入分级缓冲区并唤醒派发器线程。注意所有权转移点的写屏障调用。

## 4. 零拷贝传递机制

### 4.1 指针传递 + 唯一所有权

事件从分配到回收始终驻留在事件内存池同一地址，只有指针在各模块间传递。

| 指标 | 传统方案 | 优化方案 |
|------|---------|---------|
| 内存拷贝次数 | 2 次 (入队+出队) | **0 次** |
| 每次传输数据量 | 事件全量 (如 32 字节) | **仅一个指针** |
| CPU 开销 | 高 | **极低** |

```mermaid
sequenceDiagram
    participant Pool as 事件内存池
    participant P as 事件发布者
    participant OptLayer as 协同调度优化层
    participant A as 目标AO

    Pool-->>P: 1.分配事件内存
    P->>P: 2.原地填充内容
    P-->>OptLayer: 3.传递指针
    OptLayer-->>A: 4.传递指针
    A->>A: 5.直接访问原内存
    A-->>Pool: 6.归还内存池
```

### 4.2 唯一所有权语义

事件在任一时刻有且仅有一个所有者，所有权按固定顺序转移: **内存池 -> 发布者 -> 优化层 -> 目标 AO -> 内存池 (回收)**。

| 阶段 | 持有者 | 允许操作 | 禁止操作 |
|------|--------|---------|---------|
| 已分配 | 发布者 | 填充事件内容 | 其他模块访问 |
| 已发布 | 优化层 | 暂存、路由 | 发布者修改 |
| 已派发 | 目标 AO | 读取和处理 | 优化层访问 |
| 已处理 | 内存池 | 回收、重新分配 | AO 继续访问 |

多播场景使用引用计数: 事件进入暂存缓冲区时自动递增引用计数，防止提前回收导致悬空指针。

### 4.3 内存屏障保障

在零拷贝机制中，所有权转移点必须插入内存屏障，解决 CPU/编译器重排序和缓存可见性问题:

- **所有权转移点 1 (发布者 -> 优化层)**: 事件指针写入暂存缓冲区后、更新尾索引之前，插入写屏障
- **所有权转移点 2 (优化层 -> 目标 AO)**: 派发器线程从缓冲区提取事件指针后、目标 AO 访问内容之前，插入读屏障

| 架构 | 写屏障 | 读屏障 |
|------|--------|--------|
| ARM Cortex-M | DMB | DMB |
| RISC-V | FENCE W,W | FENCE R,R |
| GCC 通用 | __sync_synchronize() | __sync_synchronize() |

单核 MCU 上 ISR 与线程之间也存在可见性问题，此时内存屏障的主要作用是禁止编译器重排序 (通过 `"memory"` clobber 约束)。

## 5. 分级暂存与批处理

### 5.1 三级暂存缓冲区

三个独立的环形缓冲区按优先级分区:

| 优先级 | 默认容量 | 典型内容 | 处理顺序 |
|--------|---------|---------|---------|
| HIGH | 32 槽 | 紧急控制指令、安全报警 | 最先处理 |
| NORMAL | 64 槽 | 常规业务事件 | HIGH 清空后 |
| LOW | 128 槽 | 日志、统计上报、重试事件 | 最后处理 |

每个槽位存储事件指针 + 目标 AO 指针 + 时间戳，体现零拷贝原则。

### 5.2 派发器线程工作流程

派发器线程以**最高优先级**运行:

1. **等待信号量**: 无事件时阻塞，不消耗 CPU
2. **处理熔断恢复**: 递减被熔断 AO 的恢复倒计时
3. **按优先级提取**: 严格 HIGH -> NORMAL -> LOW 顺序
4. **策略引擎处理**: 对每个事件调用合并/丢弃判定
5. **批量投递**: 连续将多个事件投递到各目标 AO 的邮箱，直到本轮缓冲区清空。所有 AO 线程在派发器线程工作期间不会被调度，效果等同于"一次性投递、一次唤醒、批量处理"
6. **背压处理**: 投递失败的事件，若带有 NO_DROP 标志且重试次数未超上限 (默认 3 次)，放回 LOW 队列重试; 否则安全丢弃并记录统计

```c
/* 派发器线程入口 */
static void dispatcher_thread_entry(void *param)
{
    CoopScheduler *cs = (CoopScheduler *)param;

    for (;;) {
        rt_sem_take(cs->dispatchSem, RT_WAITING_FOREVER);

        /* 处理熔断恢复 */
        for (uint8_t i = 0U; i < QF_MAX_ACTIVE; i++) {
            if (cs->aoAttrs[i].cooldownRemain > 0U) {
                cs->aoAttrs[i].cooldownRemain--;
            }
        }

        /* 严格按 HIGH -> NORMAL -> LOW 顺序处理 */
        for (uint8_t level = 0U; level < 3U; level++) {
            StagingRing *ring = &cs->rings[level];

            while (ring->count > 0U) {
                StagingSlot *slot = &ring->slots[ring->head];

                QF_MEM_READ_BARRIER();  /* 读屏障 */

                QEvt const *evt    = slot->evt;
                QActive    *target = slot->target;

                ring->head = (ring->head + 1U) % ring->capacity;
                ring->count--;

                /* 策略引擎: 合并判定 */
                if (cs->policy.shouldMerge != NULL &&
                    ring->count > 0U) {
                    StagingSlot *next = &ring->slots[ring->head];
                    if (next->target == target &&
                        cs->policy.shouldMerge(evt, next->evt)) {
                        QF_gc(evt);  /* 回收被合并的旧事件 */
                        cs->stats.mergedCount++;
                        continue;
                    }
                }

                /* 策略引擎: 丢弃判定 */
                if (cs->policy.shouldDrop != NULL &&
                    cs->policy.shouldDrop(evt, target)) {
                    QF_gc(evt);
                    cs->stats.droppedCount++;
                    continue;
                }

                /* 投递到目标 AO 邮箱 */
                if (!QACTIVE_POST_X(target, evt, 0U, NULL)) {
                    /* 投递失败: 背压重试 */
                    if ((evt->dynamic_ & EVT_FLAG_NO_DROP) &&
                        slot->timestamp < MAX_RETRY_COUNT) {
                        slot->evt = evt;
                        slot->target = target;
                        slot->timestamp++;
                        staging_push(&cs->rings[2], slot); /* LOW */
                        cs->stats.retryCount++;
                    } else {
                        QF_gc(evt);
                        cs->stats.droppedCount++;
                    }
                }
            }
        }
    }
}
```

```mermaid
flowchart TD
    subgraph EventSource ["事件产生"]
        E1([evt1]) & E2([evt2]) & EN([evtN])
    end

    E1 & E2 & EN --> Strategy

    Strategy["策略引擎"] --> HIGH & NORMAL & LOW

    subgraph Buffer ["分级暂存缓冲区"]
        HIGH["HIGH (32槽)"]
        NORMAL["NORMAL (64槽)"]
        LOW["LOW (128槽)"]
    end

    HIGH & NORMAL & LOW -->|信号量通知| Dispatcher

    subgraph Dispatcher ["派发器线程"]
        D1["按优先级批量提取"]
        D2["事件合并判定"]
        D3["丢弃判定"]
        D4["批量投递到目标AO"]
        D1 --> D2 --> D3 --> D4
    end

    D4 --> AO1 & AO2

    subgraph ActiveObjects ["主动对象"]
        AO1["AO_1: 邮箱中有N个事件"]
        AO2["AO_2: 邮箱中有M个事件"]
    end
```

### 5.3 实时性保证

三重机制确保关键事件不被延误:

- **优先级严格保序**: HIGH 队列完全清空后才处理 NORMAL，高优先级事件最大延迟 < 5ms
- **关键事件立即唤醒**: 每次有事件进入暂存缓冲区时立即释放信号量唤醒派发器线程
- **防饿死机制**: 低优先级队列等待时间超过阈值时，强制提取至少 1 个事件

**批处理收益对比:**

| 场景 | 传统方案 | 优化方案 |
|------|---------|---------|
| 100 个中断事件 | 100 次上下文切换 | **1 次上下文切换** |
| 调度器调用次数 | 100 次 | **1 次** |
| 系统抖动 | 高 | **低** |

### 5.4 空闲钩子补偿

利用 RTOS 空闲钩子 (idle hook) 作为安全补偿机制: 每次调度器进入空闲状态时遍历三级缓冲区，检测到非空则立即释放信号量唤醒派发器线程。防止极端情况下信号量通知丢失导致事件滞留。

## 6. 可插拔策略引擎

### 6.1 策略接口

四个函数指针接口:

| 决策点 | 接口 | 功能 |
|--------|------|------|
| 合并判定 | shouldMerge(prev, next) | 判断两事件是否可合并 |
| 丢弃判定 | shouldDrop(evt, targetAO) | 判断事件是否可安全丢弃 |
| 优先级映射 | getPrioLevel(evt) | 映射到 HIGH/NORMAL/LOW |
| 优先级比较 | comparePriority(a, b) | 同级队列排序 |

### 6.2 预置策略

**默认策略 (安全优先):** 信号值相等即合并，从不丢弃，所有事件统一放入 NORMAL 队列。适用于安全关键场景。

```c
/* 默认策略: 安全优先 */
static bool default_shouldMerge(QEvt const *prev, QEvt const *next) {
    return (prev->sig == next->sig);  /* 同信号即合并 */
}
static bool default_shouldDrop(QEvt const *evt, QActive const *target) {
    (void)evt; (void)target;
    return false;  /* 从不丢弃 */
}
static uint8_t default_getPrioLevel(QEvt const *evt) {
    (void)evt;
    return PRIO_NORMAL;  /* 统一 NORMAL */
}

static const DispatchPolicy POLICY_DEFAULT = {
    .shouldMerge    = default_shouldMerge,
    .shouldDrop     = default_shouldDrop,
    .getPrioLevel   = default_getPrioLevel,
    .comparePriority = NULL,
};
```

**高性能策略 (过载优雅降级):** 引入扩展事件标志 (MERGEABLE、CRITICAL) 和显式优先级字段。队列水位超 80% 时主动丢弃非 CRITICAL 事件; 仅合并同时标记 MERGEABLE 的同信号事件。在高频传感器数据采集 + 安全报警并存的场景中，ADC 采样事件标记 MERGEABLE (只关心最新值)，温度超限报警标记 CRITICAL (每条都重要)。

```c
/* 高性能策略: 过载优雅降级 */
#define EVT_FLAG_MERGEABLE  (1U << 0)
#define EVT_FLAG_CRITICAL   (1U << 1)
#define EVT_FLAG_NO_DROP    (1U << 2)

static bool perf_shouldMerge(QEvt const *prev, QEvt const *next) {
    /* 仅合并同时标记 MERGEABLE 的同信号事件 */
    return (prev->sig == next->sig) &&
           (prev->dynamic_ & EVT_FLAG_MERGEABLE) &&
           (next->dynamic_ & EVT_FLAG_MERGEABLE);
}

static bool perf_shouldDrop(QEvt const *evt, QActive const *target) {
    if (evt->dynamic_ & EVT_FLAG_CRITICAL) {
        return false;  /* CRITICAL 事件永不丢弃 */
    }
    /* 队列水位超 80% 时丢弃非关键事件 */
    uint16_t used  = target->eQueue.nUsed;
    uint16_t total = target->eQueue.maxLen;
    return (used * 5U > total * 4U);
}

static uint8_t perf_getPrioLevel(QEvt const *evt) {
    if (evt->dynamic_ & EVT_FLAG_CRITICAL) return PRIO_HIGH;
    if (evt->dynamic_ & EVT_FLAG_MERGEABLE) return PRIO_LOW;
    return PRIO_NORMAL;
}

static const DispatchPolicy POLICY_HIGH_PERF = {
    .shouldMerge    = perf_shouldMerge,
    .shouldDrop     = perf_shouldDrop,
    .getPrioLevel   = perf_getPrioLevel,
    .comparePriority = NULL,
};
```

策略切换只需一条原子指针赋值:

```c
/* 运行时切换策略 (下一个派发周期生效) */
void CoopSched_setPolicy(CoopScheduler *cs, const DispatchPolicy *p) {
    cs->policy = *p;  /* 结构体拷贝, ISR 安全 (单核 MCU) */
}
```

### 6.3 两层策略配置

系统采用"**全局策略选择 + 单事件标记**"两层配合:

```mermaid
flowchart TD
    A["全局策略选择"] --> B{当前策略?}
    B -- 默认策略 --> C["同信号即合并"]
    B -- 高性能策略 --> D{"检查事件 flags"}
    D -- "flags 含 MERGEABLE" --> E["同信号合并"]
    D -- "flags 无 MERGEABLE" --> F["不合并, 逐一投递"]
```

策略通过单条指针赋值原子切换，新策略在下一个派发周期生效，无需重启。

## 7. 典型应用场景

### 7.1 场景 A: 高优先级突发响应

传感器报警中断触发，需要在最短时间内完成事件处理。

```
时间线 (RISC-V 37MHz):

t=0us     GPIO 中断触发, 进入 ISR
t=2us     ISR 从事件池分配事件, 填充报警数据
t=3us     调用 CoopSched_post(), 四项检查全部通过
t=3us     快速路径: 直接调用 AO_Alarm->dispatch()
t=20us    dispatch 完成 (状态机转换 + 执行 entry action)
t=23us    CoopSched_post() 返回, ISR 继续
          总延迟: 23us (传统方案: 118us)
```

传统方案的时间线对比:

```
t=0us     GPIO 中断触发, 进入 ISR
t=2us     ISR 填充事件, memcpy 入队 (32B)       -- 缺陷三
t=10us    ISR 返回, RTOS 调度器介入               -- 缺陷二
t=15us    保存当前线程上下文 (通用寄存器+FPU)
t=55us    切换到 AO_Alarm 线程
t=60us    恢复 AO_Alarm 线程上下文
t=68us    memcpy 出队 (32B)                       -- 缺陷三
t=75us    调用 dispatch()
t=95us    dispatch 完成
t=118us   调度器再次介入, 恢复原线程
          总延迟: 118us
```

关键差异在于: 快速路径消除了入队拷贝 (t=2~10us)、调度器介入 (t=10~15us)、上下文切换 (t=15~60us)、出队拷贝 (t=68us) 四个环节。

### 7.2 场景 B: 中断风暴下的批处理

连续 7 个中断事件在短时间内到达:

```
ISR_1 (t=0):   evt_1 -> 快速路径直接处理 (nesting=1)
  ISR_2 (t=5):   evt_2 -> 快速路径直接处理 (nesting=2)
    ISR_3 (t=8):   evt_3 -> nesting=3, 达到阈值 -> 暂存 HIGH
    ISR_4 (t=10):  evt_4 -> nesting=3, 达到阈值 -> 暂存 NORMAL
  ISR_2 返回 (t=12): evt_2 dispatch 完成, nesting=1
ISR_1 返回 (t=18): evt_1 dispatch 完成, nesting=0

线程上下文恢复后:
t=20:  evt_5 到达 -> 暂存 NORMAL
t=21:  evt_6 到达 -> 暂存 NORMAL (与 evt_5 同信号, 被合并)
t=22:  evt_7 到达 -> 暂存 LOW

派发器线程被唤醒 (1 次上下文切换):
t=25:  处理 HIGH[evt_3] -> 投递到目标 AO
t=26:  处理 NORMAL[evt_4, evt_5] -> 投递 (evt_6 已合并)
t=27:  处理 LOW[evt_7] -> 投递
```

结果: 7 个事件仅需 2 次直接派发 + 1 次批处理 (含 1 次合并)。传统方案需要 7 次完整的上下文切换。

### 7.3 场景 C: 传感器过采样与策略降级

工业场景中 ADC 以 10KHz 采样，但控制循环仅需 1KHz 处理。使用高性能策略:

```c
/* ADC ISR: 每 100us 触发一次 */
void ADC_IRQHandler(void) {
    QEvt *evt = Q_NEW(AdcSampleEvt, ADC_SAMPLE_SIG);
    evt->dynamic_ |= EVT_FLAG_MERGEABLE;  /* 标记可合并 */
    ((AdcSampleEvt *)evt)->value = ADC->DR;
    CoopSched_post(&g_sched, AO_Controller, evt, ISR_CONTEXT_PRIO);
}

/* 温度超限 ISR: 偶发触发 */
void TEMP_ALARM_IRQHandler(void) {
    QEvt *evt = Q_NEW(TempAlarmEvt, TEMP_ALARM_SIG);
    evt->dynamic_ |= EVT_FLAG_CRITICAL;  /* 标记关键事件 */
    CoopSched_post(&g_sched, AO_Safety, evt, ISR_CONTEXT_PRIO);
}
```

在 10KHz 采样率下，派发器线程每 1ms 唤醒一次，此时 NORMAL 队列中已积累约 10 个 ADC 事件。策略引擎将同信号且标记 MERGEABLE 的事件合并为 1 个 (保留最新值)，实际投递到 AO_Controller 的仅 1 个事件。而 TEMP_ALARM 事件标记为 CRITICAL，进入 HIGH 队列优先处理，不参与合并。

效果: 10KHz 中断源仅产生 1KHz 的实际事件处理负载，CPU 占用下降约 90%。

## 8. 性能实测

### 8.1 测试环境

**平台一 (RISC-V 低频):** CV1800B SoC (C906 核心)，64 位，37MHz，64MB DDR2，RT-Thread v5.1.0，QP/C v7.3.0，gcc-riscv64 10.3.0，-O2 优化。

**平台二 (ARM 高频):** Cortex-M4，180MHz，192KB RAM，RT-Thread v4.0.5，QP/C v6.9.3，-O2 优化。

**方法:** 同一代码库、同一硬件，通过运行时接口启用/禁用优化层进行 A/B 对照。每轮 1000 个事件，重复 10 轮取平均。

### 8.2 实测结果

**平台一 (RISC-V 37MHz):**

| 指标 | 传统方案 | 优化方案 | 改进 | 归因 |
|------|---------|---------|------|------|
| 中断至响应延迟 | 118 us | 23 us | **-80.5%** | 快速路径绕过调度器 |
| 持续吞吐量 | 8.5 KHz | 35.2 KHz | **+314%** | 快速路径 + 零拷贝 |
| 峰值突发吞吐量 | 8.5 KHz | 43.5 KHz | **+412%** | 批处理合并调度 |
| 上下文切换 (1000 事件) | ~1010 次 | ~10 次 | **-99%** | 快速路径 + 批处理 |
| 内存拷贝 | 2 次/事件 | 0 次 | **消除 100%** | 零拷贝 |

**平台二 (ARM Cortex-M4 180MHz):**

| 指标 | 传统方案 | 优化方案 | 改进 |
|------|---------|---------|------|
| 中断至响应延迟 (us) | 125.6 | 24.3 | **-80.7%** |
| 吞吐量 (KHz) | 8.2 | 32.6 | **+297.6%** |
| 延迟抖动 (标准差 us) | 52.3 | 18.5 | **-64.6%** |
| 空闲 CPU 占用 (%) | 12.4 | 8.6 | **-30.6%** |

两个平台结果高度一致，证明优化效果具有跨架构普适性。在 37MHz 低频平台上，传统方案响应链路消耗约 4366 CPU 周期 (118us x 37MHz)，优化后压缩至约 851 周期 (23us x 37MHz)，相当于通过软件优化实现了 5 倍响应速度提升。

## 9. 资源开销分析

### 9.1 RAM 开销

| 组件 | 计算 (32 位平台) | 大小 |
|------|------------------|------|
| HIGH 缓冲区 (32 槽) | 32 x 12B | 384 B |
| NORMAL 缓冲区 (64 槽) | 64 x 12B | 768 B |
| LOW 缓冲区 (128 槽) | 128 x 12B | 1536 B |
| AO 属性表 (16 个 AO) | 16 x 8B | 128 B |
| 策略引擎 (4 指针) | 4 x 4B | 16 B |
| 运行时统计 | 6 x 4B | 24 B |
| 派发器线程栈 | 配置决定 | 512~1024 B |
| 信号量 + 管理字段 | - | ~32 B |
| **合计** | | **约 3~4 KB** |

作为参照，浅层适配方案中每个 AO 的 RTOS 线程栈通常为 1~2KB，16 个 AO 共需 16~32KB 仅用于线程栈。优化层新增的 3~4KB 开销换取了调度效率的数量级提升。

### 9.2 ROM 开销

优化层核心代码 (快速路径检查、派发器线程、策略引擎) 编译后约 800~1200 字节 (.text 段)，对于典型 512KB~2MB Flash 容量的 MCU 而言可忽略。

### 9.3 CPU 开销

| 操作 | 典型耗时 (Cortex-M4 180MHz) |
|------|----------------------------|
| 四项安全准入检查 | ~0.3 us (约 54 cycles) |
| 快速路径 dispatch 包装 (不含业务逻辑) | ~0.5 us |
| 暂存入队 (含写屏障) | ~0.4 us |
| 派发器批量提取 + 投递 (每事件) | ~0.8 us |

快速路径的检查开销约为一次完整上下文切换 (约 22 us) 的 1.4%，即使检查失败降级到批处理路径，增加的开销也仅为原方案的 ~2%。

## 10. RTOS 集成要点

### 10.1 RT-Thread 集成

```c
/* 初始化: 在 RTOS 启动后、AO 线程创建前调用 */
void CoopSched_init(CoopScheduler *cs)
{
    rt_memset(cs, 0, sizeof(CoopScheduler));

    /* 三级缓冲区容量配置 */
    cs->rings[PRIO_HIGH].capacity   = 32U;
    cs->rings[PRIO_NORMAL].capacity = 64U;
    cs->rings[PRIO_LOW].capacity    = 128U;

    /* 创建派发器信号量 (初始值 0) */
    cs->dispatchSem = rt_sem_create("disp", 0, RT_IPC_FLAG_FIFO);

    /* 设置默认策略 */
    cs->policy = POLICY_DEFAULT;

    /* 创建派发器线程 (最高优先级) */
    rt_thread_t tid = rt_thread_create(
        "dispatcher",
        dispatcher_thread_entry,
        cs,
        DISPATCHER_STACK_SIZE,
        0,  /* 最高优先级 */
        10  /* 时间片 */
    );
    rt_thread_startup(tid);
}
```

**关键集成点:**

- **信号量选型**: 使用 `rt_sem` 而非 `rt_event`，因为信号量的 take/release 路径更短 (约 15 cycles vs 40 cycles)，且派发器只需"有事件"这一个信号
- **优先级配置**: 派发器线程设为最高优先级 (0)，确保被唤醒后立即执行。各 AO 线程优先级从 1 开始递增
- **空闲钩子注册**: 通过 `rt_thread_idle_sethook()` 注册补偿函数

### 10.2 FreeRTOS 集成

FreeRTOS 的主要差异在于 API 命名和 ISR 安全函数:

| 操作 | RT-Thread | FreeRTOS |
|------|-----------|----------|
| 信号量释放 (线程) | `rt_sem_release()` | `xSemaphoreGive()` |
| 信号量释放 (ISR) | `rt_sem_release()` | `xSemaphoreGiveFromISR()` |
| 信号量等待 | `rt_sem_take(, RT_WAITING_FOREVER)` | `xSemaphoreTake(, portMAX_DELAY)` |
| 空闲钩子 | `rt_thread_idle_sethook()` | `vApplicationIdleHook()` |
| 临界区进入 | `rt_enter_critical()` | `taskENTER_CRITICAL()` |

FreeRTOS 需要特别注意 ISR 中必须使用 `FromISR` 后缀的 API，并在 ISR 退出时检查是否需要上下文切换 (`portYIELD_FROM_ISR`)。优化层在 ISR 上下文中释放信号量时应调用:

```c
BaseType_t xHigherPriorityTaskWoken = pdFALSE;
xSemaphoreGiveFromISR(cs->dispatchSem, &xHigherPriorityTaskWoken);
portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
```

### 10.3 QP/C 框架集成

优化层作为 QP/C 移植层的扩展插入，替换原有的 `QActive_post_()` 实现:

```c
/* qf_port.h: 重定向事件投递宏 */
#define QACTIVE_POST(me_, e_, sender_) \
    CoopSched_post(&g_sched, (me_), (e_), get_sender_prio(sender_))

/* AO 初始化时声明快速派发资格 */
#define AO_ENABLE_FAST_DISPATCH(ao_) \
    g_sched.aoAttrs[(ao_)->prio].directDispatchEnabled = 1U
```

这种宏替换方式对业务代码完全透明: 应用层仍然使用 `QACTIVE_POST` 发送事件，无需感知底层是走了快速路径还是批处理路径。

## 11. 各技术特征的协同关系

各技术特征并非独立模块，而是深度耦合的有机整体:

| 依赖关系 | 具体说明 |
|---------|---------|
| 快速路径 -> 零拷贝传递 | 快速路径依赖零拷贝提供的指针传递基础设施 |
| 快速路径 -> 熔断机制 | 熔断机制为快速路径提供安全兜底 |
| 快速路径 -> 批处理路径 | 批处理路径是快速路径不满足条件时的自动降级目标 |
| 批处理路径 -> 策略引擎 | 事件合并、丢弃、分级依赖策略引擎 |
| 所有路径 -> 共享基础设施 | 共享事件内存池、引用计数和同步原语 |

## 12. 局限性与适用边界

在采用本方案前需评估以下约束:

**快速路径的前提条件。** 快速路径要求事件处理函数严格遵守 run-to-completion 语义且执行时间短 (< 50us)。如果系统中大多数 AO 的事件处理函数执行时间长或包含阻塞调用 (如 Flash 写入、I2C 通信)，快速路径的适用比例将很低，优化效果有限。

**单核假设。** 当前设计假设单核 MCU 环境，`nestingDepth` 使用普通变量而非原子变量，分级缓冲区的 head/tail 更新依赖 ISR 禁用保护而非无锁算法。移植到多核 SMP 平台需要重新设计同步机制。

**策略引擎的合并语义。** 事件合并仅保留最新事件，丢弃历史事件。对于需要累积处理的场景 (如编码器脉冲计数)，MERGEABLE 标志不可使用，需走完整的逐事件处理流程。

**派发器线程的单点瓶颈。** 所有批处理事件都经由单个派发器线程分发。在极端高负载下 (> 50KHz 事件率)，派发器线程可能成为瓶颈。实测中 RISC-V 37MHz 平台的持续吞吐量上限为 35.2KHz，Cortex-M4 180MHz 平台为 32.6KHz。

## 13. 总结

协同调度优化层的设计思想可以归纳为三个原则:

1. **差异化处理**: 不是所有事件都需要走完整调度流程。满足安全条件的高优先级事件可以在当前上下文直接处理; 不满足条件的事件暂存后批量处理
2. **渐进式降级**: 快速路径 -> 批处理路径 -> 背压重试 -> 安全丢弃，每一级都有明确的降级条件和安全保障
3. **可观测可调**: 内建完整的度量指标体系 (派发周期数、合并/丢弃/重试事件数、熔断触发次数等)，通过 RTOS Shell 命令实时暴露，支持不停机监控和策略调优

这种设计适用于基于 AO 模式的事件驱动框架 (如 QP/C、QP/C++) 与各类 RTOS (RT-Thread、FreeRTOS、Zephyr) 的组合应用场景，尤其适用于资源受限的 MCU 平台 (ARM Cortex-M、RISC-V)。核心代价是约 3~4KB RAM 和 1KB ROM 的额外开销，换取中断响应延迟降低 80%、吞吐量提升 3 倍以上、上下文切换减少 99% 的收益。对于事件处理函数执行时间短且符合 run-to-completion 语义的系统，该方案的投入产出比最高。

---
title: "QPC 框架深度解析: Active Object 模型、层次状态机与零拷贝事件通信"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["QPC", "Active-Object", "HSM", "RT-Thread", "RTOS", "embedded", "lock-free", "zero-copy", "state-machine", "SPSC", "CAS", "Run-to-Completion", "event-driven"]
summary: "QP/C (Quantum Platform in C) 是一个面向嵌入式实时系统的事件驱动框架，其核心是 Active Object (主动对象) 并发模型与层次状态机 (HSM)。本文从架构设计出发，深入剖析 QPC 的三大支柱: HSM 的冒泡-继承-覆盖机制与 QHsm/QMsm 双实现策略、QActive 零拷贝无锁事件队列的 SPSC 设计、以及 QActive 在 RT-Thread 上的完整移植方案。通过 1kHz 高频采样案例展示框架的工程优势。"
ShowToc: true
TocOpen: true
---

> 相关文章:
> - [newosp 深度解析: C++17 事件驱动架构](../newosp_event_driven_architecture/) -- C++17 对 QPC Active Object 理念的重新实现
> - [C 语言层次状态机框架: 从过程驱动到数据驱动](../c_hsm_data_driven_framework/) -- C 语言 HSM 的另一种设计方法
> - [无锁编程核心原理](../lockfree_programming_fundamentals/) -- QActive 零拷贝队列的无锁理论基础
> - [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- QActive 事件队列的 SPSC 设计详解
>
> 参考:
> - [QPC 层次状态机（HSM）设计与优势分析](https://blog.csdn.net/stallion5632/article/details/149359525)
> - [QPC 框架中状态机的设计优势和特殊之处](https://blog.csdn.net/stallion5632/article/details/149260812)
> - [QPC QActive 零拷贝 & 无锁数据传输解析](https://blog.csdn.net/stallion5632/article/details/149374727)
> - [QPC QActive 在 RT-Thread 上的实现原理详述](https://blog.csdn.net/stallion5632/article/details/149604623)
>
> QP/C 官方: [state-machine.com](https://www.state-machine.com/qpc/)

## 1. QP/C 框架概述

[QP/C](https://www.state-machine.com/qpc/) (Quantum Platform in C) 是由 Quantum Leaps 开发的轻量级实时嵌入式框架，其核心理念是将 **Active Object (主动对象/Actor)** 并发模型与 **层次状态机 (Hierarchical State Machine, HSM)** 融合，构建事件驱动的嵌入式系统。

QP/C 的三大支柱:

```
┌─────────────────────────────────────────────────────────┐
│                    QP/C Framework                        │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Active       │  │  Hierarchical│  │  Zero-Copy   │  │
│  │  Object       │  │  State       │  │  Event       │  │
│  │  (QActive)    │  │  Machine     │  │  Queue       │  │
│  │              │  │  (QHsm/QMsm)│  │  (QEQueue)   │  │
│  │  线程隔离    │  │  行为建模    │  │  无锁通信    │  │
│  │  RTC 语义    │  │  层次复用    │  │  指针传递    │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  OS Abstraction: 裸机 / FreeRTOS / RT-Thread / QXK │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 1.1 Active Object 模型

Active Object (AO) 是 QP/C 对 Actor 模式的具体实现。每个 AO 是一个自包含的并发构件，封装了:

- **一个层次状态机 (QHsm)**: 作为行为引擎，处理事件并执行状态转换
- **一个事件队列 (QEQueue)**: 作为"信箱"，接收所有发送给它的事件
- **一个专属的执行上下文**: 映射为 RTOS 线程或裸机的前后台循环

AO 的核心设计原则:

| 原则 | 含义 | 工程价值 |
|------|------|---------|
| **线程隔离** | 每个 AO 独占私有数据，不与其他 AO 共享 | 从设计上消除数据竞争 |
| **Run-to-Completion (RTC)** | 事件处理从开始到结束不被中断 | AO 内部无需锁保护 |
| **异步通信** | AO 之间仅通过事件队列通信 | 生产者/消费者完全解耦 |
| **Shared-Nothing** | 无共享状态，仅传递不可变事件 | 天然适合多核扩展 |

### 1.2 与传统多线程编程的对比

传统多线程模型的典型问题:

```c
// 传统方式: 多个线程共享数据，需要锁保护
static SensorData g_sensorData;  // 全局共享
static pthread_mutex_t g_mutex;

void sensor_thread(void *arg) {
    while (1) {
        pthread_mutex_lock(&g_mutex);    // 获取锁
        g_sensorData = read_sensor();     // 写共享数据
        pthread_mutex_unlock(&g_mutex);   // 释放锁
        usleep(1000);
    }
}

void display_thread(void *arg) {
    while (1) {
        pthread_mutex_lock(&g_mutex);    // 获取锁
        show(g_sensorData);               // 读共享数据
        pthread_mutex_unlock(&g_mutex);   // 释放锁
        usleep(100000);
    }
}
```

QP/C Active Object 方式:

```c
// AO 方式: 通过事件传递数据，无共享状态
typedef struct {
    QEvt    super;
    int32_t temperature;
} SensorEvt;

// 传感器 AO: 只负责采集，通过事件发布数据
static QState Sensor_active(SensorAO *me, QEvt const *e) {
    switch (e->sig) {
    case SAMPLE_SIG: {
        SensorEvt *evt = Q_NEW(SensorEvt, SENSOR_DATA_SIG);
        evt->temperature = read_sensor();
        QF_publish(&evt->super, me);  // 发布，不关心谁消费
        return Q_HANDLED();
    }
    }
    return Q_SUPER(&QHsm_top);
}

// 显示 AO: 只负责显示，订阅感兴趣的事件
static QState Display_active(DisplayAO *me, QEvt const *e) {
    switch (e->sig) {
    case SENSOR_DATA_SIG: {
        SensorEvt const *se = (SensorEvt const *)e;
        show(se->temperature);  // 直接使用，无需加锁
        return Q_HANDLED();
    }
    }
    return Q_SUPER(&QHsm_top);
}
```

两种方式的本质区别: 传统多线程是"共享内存 + 锁保护"，AO 是"消息传递 + 线程隔离"。后者从架构层面消除了数据竞争，代价是所有通信必须通过事件队列。

## 2. 层次状态机 (HSM) 设计

### 2.1 从平面 FSM 到层次 HSM

传统平面状态机的核心问题是**状态爆炸**:

```c
// 平面 FSM: 状态组合呈指数增长
enum {
    STATE_IDLE_NORMAL,
    STATE_IDLE_ERROR,
    STATE_WORKING_NORMAL,
    STATE_WORKING_ERROR,
    STATE_SLEEP_NORMAL,
    STATE_SLEEP_ERROR,
    // ... 每增加一个维度，状态数翻倍
};
```

HSM 通过**状态层次**解决此问题。子状态自动继承父状态的行为，只需处理差异化逻辑:

```
Top (根状态)
├── Normal_mode (处理通用正常逻辑)
│   ├── Idle
│   ├── Working
│   └── Sleep
└── Error_mode (处理通用错误逻辑)
    ├── Idle      ← 继承 Normal_mode::Idle 的大部分行为
    ├── Working   ← 继承 Normal_mode::Working 的大部分行为
    └── Sleep     ← 继承 Normal_mode::Sleep 的大部分行为
```

### 2.2 QMState: 编译期静态状态结构

QP/C 的 HSM 实现基于 `QMState` 结构体，所有状态关系在编译期确定:

```c
typedef struct QMState {
    struct QMState const *superstate;    // 父状态指针 (编译期绑定)
    QStateHandler const   stateHandler;  // 状态处理函数
    QActionHandler const  entryAction;   // ENTRY 动作
    QActionHandler const  exitAction;    // EXIT  动作
    QActionHandler const  initAction;    // INIT  动作 (初始转移)
} QMState;
```

设计要点:

- **所有指针在编译期生成**: `superstate` 链、处理函数、Entry/Exit/Init 动作全部是静态绑定，零运行时开销
- **superstate 链深度远小于平面状态数**: 典型嵌入式应用的状态树深度为 3-5 层，而平面状态可能有数十甚至上百个
- **内存布局紧凑**: `QMState` 数组在链接时连续排列，对 I-Cache 友好

### 2.3 事件派发: 冒泡-继承-覆盖

HSM 的事件派发核心是**冒泡 (Bubble)** 机制:

```c
void QMsm_dispatch_(QHsm * const me, QEvt const * const e) {
    QMState const *t = me->state.obj;  // 当前状态
    QState r;

    // 从当前状态开始，沿 superstate 链向上冒泡
    do {
        r = (*t->stateHandler)(me, e);   // 调用状态处理函数
        if (r == Q_RET_UNHANDLED) {
            t = t->superstate;           // 未处理 → 委托给父状态
        }
    } while (r == Q_RET_UNHANDLED && t != NULL);

    // 如果需要状态转换，执行预生成的 Entry/Exit/Init 序列
    if (r >= Q_RET_TRAN) {
        QMsm_execTatbl_(me, me->temp.tatbl);
    }
}
```

这一机制实现了面向对象中的**继承与覆盖**:

```c
// 父状态: 处理通用事件
static QState Parent_state(MyAO *me, QEvt const *e) {
    switch (e->sig) {
    case COMMON_SIG:
        handle_common();     // 所有子状态共享的默认行为
        return Q_HANDLED();
    }
    return Q_SUPER(&QHsm_top);
}

// 子状态: 覆盖特定事件，其余自动委托给父状态
static QState Child_state(MyAO *me, QEvt const *e) {
    switch (e->sig) {
    case SPECIFIC_SIG:
        handle_specific();   // 子状态专属处理
        return Q_HANDLED();
    case COMMON_SIG:
        handle_override();   // 覆盖父状态的默认行为
        return Q_HANDLED();
    }
    return Q_SUPER(&Parent_state);  // 其余事件冒泡到父状态
}
```

### 2.4 自动化 Entry/Exit/Init

状态转换时，HSM 自动计算 **LCA (Lowest Common Ancestor, 最低公共祖先)**，按正确顺序执行 Exit 和 Entry 动作:

```
转换: S1.1 → S2.1

自动执行序列:
  S1.1-EXIT → S1-EXIT → S2-ENTRY → S2.1-ENTRY → S2.1-INIT
              ↑ LCA ↑
```

这种自动化保证了:

- 资源的正确获取和释放 (Entry 获取，Exit 释放)
- 不会遗漏中间层的清理逻辑
- 整个转换过程满足 Run-to-Completion 语义

### 2.5 QHsm vs QMsm: 两种实现策略

QP/C 提供两种 HSM 实现，适用于不同开发模式:

| 维度 | QHsm (手工编码) | QMsm (QM 工具生成) |
|------|:---------------:|:------------------:|
| 编码方式 | 手写状态函数 + `Q_SUPER()` | QM 图形化工具自动生成 |
| 派发性能 | 运行时沿 superstate 链查找 | 编译期已确定路径，直接表驱动 |
| 栈使用 | 中等 (递归查找深度) | **70% 更少** (预计算路径) |
| 转换开销 | 运行时计算 LCA | **编译期预计算 LCA** |
| 维护性 | 高 (代码可读性好) | 中 (依赖 QM 工具) |
| 适用场景 | 原型开发、小型项目 | 生产系统、性能敏感场景 |

**QMsm 的编译期优化原理:**

```c
// QMsm 的转换动作表在编译期由 QM 工具生成
// 不需要运行时查找 LCA，直接执行预定义的 Exit/Entry 序列
static QMTranActTable const tatbl_s11_to_s21 = {
    &QMsm_s2_s21,  // 目标状态
    {
        Q_ACTION_CAST(&s11_exit),   // Exit S1.1
        Q_ACTION_CAST(&s1_exit),    // Exit S1
        Q_ACTION_CAST(&s2_entry),   // Entry S2
        Q_ACTION_CAST(&s21_entry),  // Entry S2.1
        Q_ACTION_NULL               // 终止标记
    }
};
```

### 2.6 HSM 的关键优势总结

| 特性 | 说明 |
|------|------|
| **冒泡-继承-覆盖** | 未处理事件自动冒泡；子状态覆盖、父状态提供默认；符合开闭原则 |
| **编译期静态绑定** | 状态关系、转移表、Entry/Exit 序列在编译期生成，无动态查找 |
| **零动态分配** | 状态派发与转移不使用堆内存，只读访问静态表 |
| **缓存友好** | 状态表及处理函数连续排列，减少 I-Cache 失效 |
| **插拔式拦截** | 在状态树任意层插入横切逻辑 (日志、度量、限流)，不修改子状态 |
| **与 RTOS 调度解耦** | HSM 派发独立于线程上下文，可在 PendSV 或轻量任务中完成 |

## 3. 零拷贝事件队列 (QEQueue)

### 3.1 事件传递: 只传指针，不拷贝数据

QP/C 在 AO 之间传递事件时，遵循**零拷贝**原则:

| 操作 | 成本 | 机制 |
|------|------|------|
| 事件创建 | 对完整事件对象一次 `memset` | 对象通常几十字节 |
| 队列入/出队 | **O(1)** 指针写读，无 memcpy | `frontEvt` + `ring[]` |
| 大数据载荷 | 只传指针和长度，不移动数据 | 外部缓冲区 |
| 唤醒开销 | 一次 RTOS 调度 | 队列空→非空触发 |

### 3.2 SPSC 环形缓冲: frontEvt 优化

QEQueue 的设计精妙之处在于 `frontEvt` 快速路径:

```c
// QEQueue 内部结构 (简化)
typedef struct {
    QEvt const * volatile frontEvt;  // 最新事件的快速缓存
    QEvt const **ring;               // 环形缓冲数组
    QEQueueCtr   end;                // 数组长度
    QEQueueCtr   head;               // 写入位置
    QEQueueCtr   tail;               // 读取位置
    QEQueueCtr   nFree;              // 空闲槽数
} QEQueue;
```

入队操作:

```c
// 事件投递 (简化)
bool QActive_post_(QActive *me, QEvt const *e, uint_fast16_t margin) {
    QF_CRIT_ENTRY();  // 进入临界区 (关中断)

    if (me->eQueue.frontEvt == NULL) {
        // 快速路径: 队列为空，直接放入 frontEvt
        me->eQueue.frontEvt = e;
        // 唤醒 AO 线程
        rt_thread_resume(me->thread);
    } else {
        // 普通路径: 放入环形缓冲
        me->eQueue.ring[me->eQueue.head] = e;
        if (me->eQueue.head == 0U) {
            me->eQueue.head = me->eQueue.end;
        }
        --me->eQueue.head;
        --me->eQueue.nFree;
    }

    QF_CRIT_EXIT();   // 退出临界区
    return true;
}
```

出队操作:

```c
QEvt const *QActive_get_(QActive *me) {
    QF_CRIT_ENTRY();

    if (me->eQueue.frontEvt == NULL) {
        // 队列为空，挂起线程等待事件
        rt_thread_suspend(rt_thread_self());
        QF_CRIT_EXIT();
        rt_schedule();        // 让出 CPU
        QF_CRIT_ENTRY();     // 被唤醒后重新进入临界区
    }

    QEvt const *e = me->eQueue.frontEvt;

    if (me->eQueue.nFree < me->eQueue.end) {
        // 环形缓冲中还有事件，提升到 frontEvt
        me->eQueue.frontEvt = me->eQueue.ring[me->eQueue.tail];
        if (me->eQueue.tail == 0U) {
            me->eQueue.tail = me->eQueue.end;
        }
        --me->eQueue.tail;
        ++me->eQueue.nFree;
    } else {
        me->eQueue.frontEvt = NULL;  // 队列已空
    }

    QF_CRIT_EXIT();
    return e;
}
```

`frontEvt` 优化的意义: 在低负载场景 (大多数嵌入式系统的常态)，事件到达时队列通常为空。此时事件直接存入 `frontEvt`，出队时直接读取 `frontEvt`，完全避免了环形缓冲的头尾指针操作。

### 3.3 携带数据的事件模式

**小数据: 内嵌在事件结构体中**

```c
typedef struct {
    QEvt    super;     // 基类
    int32_t value;     // 直接内嵌 payload
} DataEvt;

// 生产者
DataEvt *e = Q_NEW(DataEvt, DATA_SIG);
e->value = 42;
QACTIVE_POST(&receiver->super, &e->super, 0);

// 消费者
DataEvt const *de = (DataEvt const *)e;
process(de->value);  // 直接使用，零拷贝
```

**大数据: 外部缓冲 + 指针**

当 payload 较大 (几 KB 以上) 时，避免在事件对象内部持有大数组:

```c
static uint8_t bigBuf[10240];  // 外部大缓冲

typedef struct {
    QEvt     super;
    uint8_t *dataPtr;     // 指向外部缓冲
    uint32_t length;
} BigEvt;

BigEvt *e = Q_NEW(BigEvt, BIG_SIG);
e->dataPtr = bigBuf;
e->length  = sizeof(bigBuf);
// 入队仅写入 BigEvt 指针 (几字节)，不拷贝 10KB 数据
QACTIVE_POST(&consumer->super, &e->super, 0);
```

### 3.4 动态事件池

QP/C 通过**固定大小事件池**替代 `malloc/free`，实现 O(1) 分配和回收:

```c
// 初始化: 预分配事件池
static uint8_t poolSto[20 * sizeof(DataEvt)];
QF_poolInit(poolSto, sizeof(poolSto), sizeof(DataEvt));

// 分配: O(1) 从池中弹出
DataEvt *e = Q_NEW(DataEvt, SENSOR_SIG);

// 回收: O(1) 归还到池中
// QF_gc(e) 在事件循环中自动调用
```

事件池的引用计数机制:

- `Q_NEW()` 分配时引用计数为 1
- `QF_publish()` 广播时，每个订阅者增加引用计数
- 每个 AO 处理完后 `QF_gc()` 减少引用计数
- 引用计数归零时自动回收到池中

## 4. QActive 在 RT-Thread 上的实现

### 4.1 架构映射

`qpc-rtthread` 将 QP/C 的抽象模型与 RT-Thread 的内核原语精确对接:

| QP/C 层 | RT-Thread 层 | 说明 |
|---------|-------------|------|
| QActive 对象 | `rt_thread_t` 线程 | 一一对应，每个 AO 创建一个专属线程 |
| QEQueue 事件队列 | 普通 C 数组 + 指针 | 纯 C 实现，不依赖 `rt_mailbox` |
| `QActive_ctor()` | -- | 纯 C 构造函数，绑定初始状态处理函数 |
| `QACTIVE_START()` | `rt_thread_create()` + `rt_thread_startup()` | 创建并启动 AO 线程 |
| `QActive_get_()` | `rt_thread_suspend()` | 队列空时挂起线程，让出 CPU |
| `QHSM_DISPATCH()` | 同一线程内函数调用 | 纯函数调用，不涉及 RTOS API |
| `QACTIVE_POST_FROM_ISR()` | `rt_thread_resume()` | ISR 中仅恢复线程，调度延迟到中断退出 |

### 4.2 QActive 生命周期

```
QActive_ctor()          QACTIVE_START()           qf_thread_function()
   │                        │                          │
   ▼                        ▼                          ▼
绑定初始状态  ──→  QEQueue_init()           ┌──→ QActive_get_() ─── 阻塞等待
                   QActive_register_()      │       │
                   QHSM_INIT()             │       ▼ 有事件
                   rt_thread_create()       │   QHSM_DISPATCH()
                   rt_thread_startup()      │       │
                          │                 │       ▼
                          └──────────────→  │   QF_gc() 回收事件
                                            │       │
                                            └───────┘ 永不退出
```

完整的启动代码:

```c
// 1. AO 实例和资源 (全部静态分配)
static BlinkyAO  l_blinky;
static QEvt const *blinkyQSto[8];     // 事件队列缓冲
static uint8_t    blinkyStk[256];      // 线程栈

// 2. 构造
void BlinkyAO_ctor(void) {
    QActive_ctor(&l_blinky.super, Q_STATE_CAST(&Blinky_initial));
}

// 3. 启动
void BlinkyAO_start(void) {
    BlinkyAO_ctor();
    QACTIVE_START(&l_blinky.super,
                  BLINKY_PRIO,                     // 优先级
                  blinkyQSto, Q_DIM(blinkyQSto),   // 事件队列
                  blinkyStk, sizeof(blinkyStk),     // 线程栈
                  (void *)0);                       // 初始事件参数
}
```

### 4.3 事件循环: 一次性事件驱动

所有 AO 线程运行同一个事件循环:

```c
static void qf_thread_function(void *arg) {
    QActive *me = (QActive *)arg;
    for (;;) {
        QEvt const *e = QActive_get_(me);     // 阻塞等待事件
        QHSM_DISPATCH(&me->super, e);         // 派发给状态机
        QF_gc(e);                              // 回收动态事件
    }
}
```

与传统"多线程 + 多次唤醒"的对比:

| 场景 | 传统多线程/回调 | QActive + HSM |
|------|:-------------:|:------------:|
| 同一事件引发多级状态转换 | 多次唤醒/阻塞，多次上下文切换 | 单次唤醒 → `QHSM_DISPATCH` 连续执行 → 单次阻塞 |
| 调度抖动 | 数十至数百微秒 | < 20 us (单次唤醒 + 函数调用) |
| 逻辑耦合 | 硬编码函数跳转，难以插桩 | 全事件驱动，自动委托，易加横切日志 |

关键优势在于: `QHSM_DISPATCH()` 在同一线程上下文中一口气完成所有冒泡、Exit/Entry/Init 动作，不触发额外调度。整个过程是纯粹的函数调用链，开销等同于几次函数指针跳转。

### 4.4 线程与中断的投递路径

`qpc-rtthread` 为线程和中断提供两条优化的投递路径:

| 上下文 | 投递宏 | 唤醒与调度 |
|-------|-------|-----------|
| **线程** | `QACTIVE_POST()` | 队列从空变非空时，`rt_thread_resume()` + **立即** `rt_schedule()` |
| **中断** | `QACTIVE_POST_FROM_ISR()` | 队列从空变非空时，仅 `rt_thread_resume()`，调度**延迟**到 `rt_interrupt_leave()` |

ISR 投递的实现:

```c
// 在 ISR 中发布事件
static QEvt const sigX_evt = { SIG_X, 0U };

// ISR 版本: 最短路径，几十条指令
QACTIVE_POST_FROM_ISR(&myAO->super, &sigX_evt);
// 内部: 写队列指针 + rt_thread_resume()
// 调度器在 rt_interrupt_leave() 中统一触发
```

这种设计保证了 ISR 的简短和确定性: ISR 只做入队和恢复线程，不在中断上下文中调用调度器。

### 4.5 时间事件 (QTimeEvt)

QTimeEvt 是 QP/C 内置的高效定时器，完全避免了 `rt_thread_mdelay()` 或创建大量 `rt_timer`:

```c
// 在 AO 构造函数中创建周期性定时事件
QTimeEvt_ctorX(&me->sampleEvt, &me->super, SAMPLE_SIG, 0U);

// 在状态机初始化时启动: 首次 1 tick 延迟，周期 1 tick
QTimeEvt_armX(&me->sampleEvt, 1U, 1U);

// 停止定时器
QTimeEvt_disarm(&me->sampleEvt);
```

与 RT-Thread SysTick 的集成:

```c
void SysTick_Handler(void) {
    rt_interrupt_enter();
    rt_tick_increase();       // RT-Thread 系统节拍
    QF_TICK_X(0U, (void *)0); // QP/C 时间事件驱动
    rt_interrupt_leave();
}
```

每次 SysTick 中断，`QF_TICK_X()` 遍历定时器链表，将到期的定时事件通过 `QACTIVE_POST_FROM_ISR()` 发送给对应的 AO。

## 5. 方案对比: QActive on RT-Thread vs. QXK

QP/C 自带一个名为 QXK 的小型抢占式内核。在已有 RT-Thread 的项目中，推荐使用 QActive on RT-Thread 模式:

| 特性 | QXK (QP/C 自带内核) | QActive on RT-Thread |
|------|:------------------:|:-------------------:|
| **阻塞调用** | 严格禁止 | 完全允许 (在普通线程中) |
| **栈模型** | 主栈 + 私有栈，动态切换 | 标准线程栈，简单明了 |
| **调度器** | QXK + RT-Thread 双调度器 | **仅 RT-Thread 调度器** |
| **生态集成** | 难以使用 RT-Thread 驱动和组件 | **无缝集成** |
| **学习成本** | 高 (需理解 QXK 限制) | 低 (标准 RT-Thread API) |

QActive on RT-Thread 的已知权衡:

| 权衡 | 风险 | 缓解措施 |
|------|------|---------|
| RTC 被同级抢占 | 同优先级 AO 可能互相抢占 | 为关键 AO 分配独一无二的优先级 |
| 无抢占阈值 | 低优先级 AO 唤醒也触发调度 | 合理规划优先级，批量处理事件 |
| RAM 占用 | 每个 AO 对应一个线程栈 | 控制 AO 粒度，简单工作用普通函数 |

## 6. 实战: 1kHz 高频采样

### 6.1 传统实现的问题

```c
void sensor_thread_entry(void *p) {
    while (1) {
        rt_thread_mdelay(1);   // 每次循环: 阻塞→调度→唤醒→调度
        data = read_sensor();
        process_data(data);
    }
}
```

每次循环都经历"阻塞 → 上下文切换 → 唤醒 → 上下文切换"，调度开销大且 `rt_thread_mdelay()` 精度受限于软定时器。

### 6.2 QActive + QTimeEvt 实现

```c
typedef struct {
    QActive   super;
    QTimeEvt  sampleEvt;     // 内置定时器
} SensorAO;

static SensorAO   l_sensor;
static QEvt const *sensorQSto[8];
static uint8_t     sensorStk[256];

enum { SAMPLE_SIG = Q_USER_SIG };

// 构造
void SensorAO_ctor(void) {
    QActive_ctor(&l_sensor.super, Q_STATE_CAST(&Sensor_initial));
    QTimeEvt_ctorX(&l_sensor.sampleEvt, &l_sensor.super, SAMPLE_SIG, 0U);
}

// 状态机
static QState Sensor_initial(SensorAO *me, QEvt const *e) {
    (void)e;
    QTimeEvt_armX(&me->sampleEvt, 1U, 1U);  // 周期 1ms (假设 1kHz tick)
    return Q_TRAN(&Sensor_active);
}

static QState Sensor_active(SensorAO *me, QEvt const *e) {
    switch (e->sig) {
    case SAMPLE_SIG: {
        uint16_t data = BSP_readADC();
        BSP_processData(data);
        return Q_HANDLED();
    }
    }
    return Q_SUPER(&QHsm_top);
}
```

### 6.3 调度序列

```
SysTick → QF_TICK_X() → sampleEvt.ctr-- → ctr==0
  → QACTIVE_POST_FROM_ISR(SAMPLE_SIG) → rt_thread_resume()
    → rt_interrupt_leave() → 调度 → SensorAO 运行
      → QActive_get_() 取到 SAMPLE_SIG
        → QHSM_DISPATCH → Sensor_active 执行采样
          → 处理完毕 → QActive_get_() → 队列空 → 挂起
```

### 6.4 性能对比

| 优化点 | QActive + QTimeEvt | 传统软定时器 + 线程 |
|-------|:------------------:|:------------------:|
| ISR 工作量 | 极小: 更新计数器、写指针、恢复线程 | 较大: 遍历链表、调用回调、发送 IPC |
| 切换次数 | 最少: 每周期 1 次中断 + 1 次调度 | 多次: 中断 → 软定时器线程 → 业务线程 |
| 数据拷贝 | **零拷贝**: 仅传递事件指针 | 有拷贝: MailBox 通常复制消息内容 |
| 定时精度 | 直接由 SysTick 驱动，无中间层 | 受限于软定时器实现 |

## 7. 最佳实践

### 7.1 AO 设计原则

- **单一职责**: 每个 AO 只负责一类紧密相关的业务 (传感器、网络、UI)
- **粒度适中**: AO 数量建议几十个以内，平衡架构清晰度与资源开销
- **AO 与普通线程配合**: AO 处理非阻塞事件驱动逻辑；普通线程处理阻塞操作 (文件 I/O、Shell)

### 7.2 API 使用要点

| 规则 | 说明 |
|------|------|
| 禁止在状态机内阻塞 | 不调用 `rt_thread_mdelay()`、`rt_sem_take()` 等阻塞 API |
| 使用 QTimeEvt | 所有定时需求通过 `QTimeEvt` 实现，不用 `rt_timer` |
| ISR 用专用宏 | ISR 中必须使用 `QACTIVE_POST_FROM_ISR()` |
| 监控资源水位 | 调试期用 `QF_getPoolMin()` 和 `QActive_getQueueMin()` 监控 |

### 7.3 性能优化

- **批量处理**: 高频事件在 AO 事件循环中一次性处理多个，减少循环开销
- **长逻辑拆分**: 耗时操作拆分为多步，通过向自身投递后续事件分步完成
- **队列深度规划**: 根据峰值事件突发预估，`QACTIVE_POST()` 返回 `false` 时需有降级策略

## 8. 总结

| 维度 | 传统多线程 | QP/C Active Object |
|------|:--------:|:------------------:|
| 并发模型 | 共享内存 + 锁 | 消息传递 + 线程隔离 |
| 状态管理 | if-else / switch-case | 层次状态机 (HSM) |
| 数据传递 | memcpy / IPC | **零拷贝指针传递** |
| 同步机制 | mutex / semaphore | **SPSC 队列 + 关中断临界区** |
| 动态分配 | malloc / free | **固定事件池 O(1) 分配** |
| 定时器 | 软定时器 → IPC → 线程 | **QTimeEvt 直接投递** |
| 调度开销 | 多次上下文切换 | 单次唤醒 + RTC 连续执行 |
| 扩展性 | 锁竞争随核心数增长 | AO 天然适合多核分布 |

QP/C 的 Active Object 模型通过**线程隔离 + 零拷贝事件队列 + 层次状态机**三位一体的设计，从架构层面消除了传统多线程编程中的数据竞争、锁死锁、优先级反转等问题。HSM 的冒泡-继承-覆盖机制提供了比平面 FSM 更强的行为复用能力，QMsm 的编译期优化使其在资源受限的嵌入式平台上保持极高的运行效率。

对于 RT-Thread 用户，QActive on RT-Thread 方案在保持 QP/C 全部架构优势的同时，无缝集成 RT-Thread 的驱动和生态，是构建事件驱动嵌入式系统的推荐选择。

## 参考资料

1. [QP/C 官方文档](https://www.state-machine.com/qpc/) -- Quantum Leaps
2. [QPC 层次状态机（HSM）设计与优势分析](https://blog.csdn.net/stallion5632/article/details/149359525)
3. [QPC 框架中状态机的设计优势和特殊之处](https://blog.csdn.net/stallion5632/article/details/149260812)
4. [QPC QActive 零拷贝 & 无锁数据传输解析](https://blog.csdn.net/stallion5632/article/details/149374727)
5. [QPC QActive 在 RT-Thread 上的实现原理详述](https://blog.csdn.net/stallion5632/article/details/149604623)
6. Miro Samek, *Practical UML Statecharts in C/C++*, 2nd Edition -- QP/C 作者的经典著作
7. [RT-Thread 官方文档](https://www.rt-thread.org/document/site/)

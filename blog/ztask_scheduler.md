# ztask 轻量级合作式任务调度器

> 参考: [tomzbj/ztask](https://github.com/tomzbj/ztask)

## 1. 简介

`ztask` 是一个为资源受限的嵌入式系统设计的、轻量级的合作式任务调度器。它的核心设计理念是简洁、高效、确定性和低功耗。它不依赖于任何操作系统，也不使用动态内存分配（如`malloc`），使其非常适合用于裸机应用中。

## 2. 核心设计理念

### 2.1 合作式调度 (Cooperative Scheduling)

调度器本身不会强制中断正在执行的任务。每个任务函数一旦开始执行，就会一直运行直到它自己返回。这种非抢占式的模型避免了多线程应用中常见的复杂性，如竞态条件、死锁和资源同步问题。

### 2.2 基于Tick的时基 (Tick-based Timing)

系统的所有时间度量都基于一个单调递增的"Tick"计数器。这个计数器通常由一个硬件定时器以固定的时间间隔（如1毫秒）来驱动。

### 2.3 静态内存管理 (Static Memory Management)

调度器所需的所有内存都从一个在初始化时由用户提供的静态内存池中分配。杜绝了因内存碎片或分配失败而导致系统崩溃的风险。

## 3. 架构与实现方案

### 3.1 核心数据结构

- 任务控制块 (`zt_task_t`): `func` 任务函数指针, `repeat_ticks` 重复周期, `next_schedule` 下次执行时间戳, `next` 链表指针
- 活动任务链表 (`active_tasks`): 按 `next_schedule` 升序排列的单向链表
- 空闲任务链表 (`free_tasks`): 管理所有当前未被使用的任务块

### 3.2 内存管理机制

`zt_init` 将用户传入的静态内存分割成多个 `zt_task_t` 单元，串联成 `free_tasks` 链表。`zt_bind` 从空闲链表头部取节点，任务完成或 `zt_unbind` 时归还。

### 3.3 核心调度算法

1. `zt_bind`: 从空闲链表获取节点，计算 `next_schedule`，插入活动链表正确位置
2. `zt_poll`: 仅检查活动链表头部任务。时间到则取出执行，周期性任务重新插入，一次性任务归还空闲链表。时间复杂度 O(1)

## 4. 关键API函数

- `zt_init()`: 初始化调度器，构建内存池
- `zt_tick()`: 由硬件定时器中断调用，驱动内部时钟
- `zt_bind()`: 注册并调度一个新任务
- `zt_unbind()`: 从调度器中移除一个已注册的任务
- `zt_poll()`: 在主循环中调用，检查并执行到期的任务
- `zt_ticks_to_next_task()`: 低功耗核心API，返回距离下一个任务执行还需等待的tick数

## 5. 完整代码

### ztask.h

```c
#ifndef ZTASK_H
#define ZTASK_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

struct zt_task_s;
typedef struct zt_task_s* zt_task_handle_t;
typedef void (*zt_func_t)(void);

int32_t zt_init(void *zt_mem, uint32_t size);
zt_task_handle_t zt_bind(zt_func_t func, uint32_t repeat_ticks, uint32_t delay_ticks);
void zt_unbind(zt_task_handle_t task);
void zt_poll(void);
void zt_tick(void);
uint32_t zt_ticks_to_next_task(void);
uint32_t zt_get_ticks(void);

#ifdef __cplusplus
}
#endif

#endif /* ZTASK_H */
```

### ztask.c

```c
#include "ztask.h"

typedef struct zt_task_s {
    zt_func_t func;
    uint32_t repeat_ticks;
    uint32_t next_schedule;
    struct zt_task_s *next;
} zt_task_t;

static struct {
    volatile uint32_t ticks;
    zt_task_t *active_tasks;
    zt_task_t *free_tasks;
} g_ztask_ctx;

static void zt_insert_task(zt_task_t * const task)
{
    if ((g_ztask_ctx.active_tasks == NULL) ||
        (task->next_schedule < g_ztask_ctx.active_tasks->next_schedule)) {
        task->next = g_ztask_ctx.active_tasks;
        g_ztask_ctx.active_tasks = task;
    } else {
        zt_task_t *current = g_ztask_ctx.active_tasks;
        while ((current->next != NULL) &&
               (current->next->next_schedule < task->next_schedule)) {
            current = current->next;
        }
        task->next = current->next;
        current->next = task;
    }
}

int32_t zt_init(void * const zt_mem, const uint32_t size)
{
    if ((zt_mem == NULL) || (size < sizeof(zt_task_t))) { return -1; }

    uint8_t* mem_ptr = (uint8_t*)zt_mem;
    const uint32_t num_tasks = size / sizeof(zt_task_t);

    g_ztask_ctx.ticks = 0U;
    g_ztask_ctx.active_tasks = NULL;
    g_ztask_ctx.free_tasks = (zt_task_t*)mem_ptr;

    zt_task_t *current = g_ztask_ctx.free_tasks;
    for (uint32_t i = 0U; i < (num_tasks - 1U); ++i) {
        mem_ptr += sizeof(zt_task_t);
        current->next = (zt_task_t*)mem_ptr;
        current = current->next;
    }
    current->next = NULL;
    return (int32_t)num_tasks;
}

void zt_tick(void) { g_ztask_ctx.ticks++; }
uint32_t zt_get_ticks(void) { return g_ztask_ctx.ticks; }

zt_task_handle_t zt_bind(zt_func_t const func, const uint32_t repeat_ticks,
                         const uint32_t delay_ticks)
{
    if (func == NULL) { return NULL; }
    zt_task_t *new_task = g_ztask_ctx.free_tasks;
    if (new_task != NULL) {
        g_ztask_ctx.free_tasks = new_task->next;
        new_task->func = func;
        new_task->repeat_ticks = repeat_ticks;
        new_task->next_schedule = g_ztask_ctx.ticks + delay_ticks;
        zt_insert_task(new_task);
    }
    return new_task;
}

void zt_unbind(zt_task_handle_t const task)
{
    if (task == NULL) { return; }
    zt_task_t* const to_remove = (zt_task_t*)task;

    if (g_ztask_ctx.active_tasks == to_remove) {
        g_ztask_ctx.active_tasks = to_remove->next;
    } else {
        zt_task_t *current = g_ztask_ctx.active_tasks;
        while ((current != NULL) && (current->next != to_remove)) {
            current = current->next;
        }
        if (current != NULL) { current->next = to_remove->next; }
    }
    to_remove->next = g_ztask_ctx.free_tasks;
    g_ztask_ctx.free_tasks = to_remove;
}

void zt_poll(void)
{
    for (;;) {
        zt_task_t *task_to_run = NULL;
        if (g_ztask_ctx.active_tasks != NULL) {
            if ((g_ztask_ctx.ticks - g_ztask_ctx.active_tasks->next_schedule)
                < (UINT32_MAX / 2U)) {
                task_to_run = g_ztask_ctx.active_tasks;
                g_ztask_ctx.active_tasks = task_to_run->next;
            }
        }
        if (task_to_run != NULL) {
            task_to_run->func();
            if (task_to_run->repeat_ticks > 0U) {
                task_to_run->next_schedule += task_to_run->repeat_ticks;
                zt_insert_task(task_to_run);
            } else {
                task_to_run->next = g_ztask_ctx.free_tasks;
                g_ztask_ctx.free_tasks = task_to_run;
            }
        } else {
            break;
        }
    }
}

uint32_t zt_ticks_to_next_task(void)
{
    if (g_ztask_ctx.active_tasks != NULL) {
        const uint32_t now = g_ztask_ctx.ticks;
        if (g_ztask_ctx.active_tasks->next_schedule > now) {
            return g_ztask_ctx.active_tasks->next_schedule - now;
        }
        return 0U;
    }
    return UINT32_MAX;
}
```

## 6. 典型应用

```c
int main(void) {
    zt_init(zt_mem, sizeof(zt_mem));
    setup_timer_for_ztick(); // 配置1ms定时器中断调用 zt_tick()

    zt_bind(task_a, 100, 50);
    zt_bind(task_b, 1000, 1000);

    while(1) {
        zt_poll();
        uint32_t sleep_ticks = zt_ticks_to_next_task();
        if (sleep_ticks > 0) {
            enter_sleep_mode(sleep_ticks);
        }
    }
}
```

核心优势: O(1) 任务检查、精确休眠计算、无动态内存分配、代码简洁易移植。

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/153326149)

---

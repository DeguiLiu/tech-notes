# 全局锁策略：通过有序获取与超时保护构建无死锁系统

> 在多任务并发任务中，不当的锁管理是导致系统死锁或永久阻塞的罪魁祸首。本文聚焦于"全局锁获取顺序"与"锁超时与回退"两大技术手段，破坏死锁必要条件，从设计层面规避多锁竞争引发的稳定性问题。

## 1. 死锁原理与应对策略

### 1.1 死锁的四个必要条件

只有当以下四个条件同时满足时，死锁才会发生：

1. 互斥使用 (Mutual Exclusion)：资源（如硬件外设）一次只能被一个任务占用。
2. 持有并等待 (Hold and Wait)：一个任务已经持有了至少一个资源，并且正在请求另一个被其他任务占用的资源。
3. 不可抢占 (No Preemption)：资源只能由持有它的任务主动释放，不能被强制剥夺。
4. 循环等待 (Circular Wait)：存在一个任务等待链，任务T1等待T2的资源，T2等待T3的资源，...，Tn等待T1的资源，形成闭环。

> 场景模拟：死锁是如何发生的？
>
> - 任务A： `lock(I2C)` 成功 -> 尝试 `lock(SPI)` (等待任务B释放)
> - 任务B： `lock(SPI)` 成功 -> 尝试 `lock(I2C)` (等待任务A释放)
>
> 此时，A和B互相持有对方需要的资源，并等待对方释放，形成了循环等待，系统死锁。

### 1.2 核心破坏策略

- 建立全局锁顺序：强制所有任务按同一升序规则获取锁，从根本上破坏"循环等待"。
- 引入超时与回退：在获取锁时设置时限，若超时则释放已持有的锁，破坏"持有并等待"。

## 2. 核心策略1：建立全局锁获取顺序

### 2.1 锁优先级设计与编号

```c
typedef enum {
    LOCK_ID_I2C   = 10,
    LOCK_ID_SPI   = 20,
    LOCK_ID_UART  = 30,
    LOCK_ID_NVM   = 40,
    // 新增锁时继续按升序编号
} LockID_t;
```

- ID 唯一且全局可见。
- 按升序获取，打破循环等待。

### 2.2 带优先级ID的锁结构

```c
typedef struct {
    const LockID_t id;  // 锁的全局唯一ID
    Mutex_t        mtx; // 底层RTOS互斥量句柄
} OrderedLock_t;
```

将 ID 与互斥量句柄绑定，便于统一管理。

### 2.3 按序获取与逆序释放的实现

提供统一的函数来处理多个锁的获取与释放，函数内部封装排序逻辑。

```c
/**
 * @brief 对锁指针数组按其ID进行升序排序 (示例: 简单的冒泡排序)
 * @note 对于锁数量较少（如<10）的场景，性能足够。
 */
static void sort_locks_by_id(OrderedLock_t *arr[], int n) {
    for (int i = 0; i < n - 1; i++) {
        for (int j = i + 1; j < n; j++) {
            if (arr[i]->id > arr[j]->id) {
                OrderedLock_t *tmp = arr[i];
                arr[i] = arr[j];
                arr[j] = tmp;
            }
        }
    }
}

/**
 * @brief 按ID升序获取多个锁 (阻塞式)
 */
void lock_multiple(OrderedLock_t *locks[], int count) {
    OrderedLock_t *local_locks[count];
    memcpy(local_locks, locks, sizeof(OrderedLock_t*) * count);
    sort_locks_by_id(local_locks, count);
    for (int i = 0; i < count; i++) {
        mutex_lock(&local_locks[i]->mtx);
    }
}

/**
 * @brief 按ID降序释放多个锁
 * @note 逆序释放是良好实践（LIFO），与获取顺序对应。
 */
void unlock_multiple(OrderedLock_t *locks[], int count) {
    OrderedLock_t *local_locks[count];
    memcpy(local_locks, locks, sizeof(OrderedLock_t*) * count);
    sort_locks_by_id(local_locks, count);
    for (int i = count - 1; i >= 0; i--) {
        mutex_unlock(&local_locks[i]->mtx);
    }
}
```

## 3. 核心策略2：引入超时与回退

### 3.1 带超时的尝试锁函数

```c
#define DEFAULT_LOCK_TIMEOUT_MS 100

/**
 * @brief 尝试获取单个锁，带超时
 * @return true: 成功, false: 超时失败
 */
bool try_lock_with_timeout(OrderedLock_t *lock, uint32_t timeout_ms) {
    if (mutex_timed_lock(&lock->mtx, timeout_ms) == true) {
        return true;
    }
    log_warning("Locking timeout for lock ID: %d", lock->id);
    return false;
}
```

### 3.2 批量获取与原子回退

在批量获取过程中，一旦有任何一个锁超时失败，必须立即释放所有已经成功获取的锁。

```c
/**
 * @brief 尝试按ID升序获取多个锁，任何失败则回退并返回false
 */
bool lock_multiple_with_timeout(OrderedLock_t *locks[], int count, uint32_t timeout_ms) {
    OrderedLock_t *local_locks[count];
    memcpy(local_locks, locks, sizeof(OrderedLock_t*) * count);
    sort_locks_by_id(local_locks, count);

    for (int i = 0; i < count; i++) {
        if (!try_lock_with_timeout(local_locks[i], timeout_ms)) {
            // 获取失败，执行回退
            for (int j = i - 1; j >= 0; j--) {
                mutex_unlock(&local_locks[j]->mtx);
            }
            return false;
        }
    }
    return true;
}
```

## 4. 实践中的标准使用流程

```c
void complex_task(void) {
    OrderedLock_t *req[] = { &g_spi_lock, &g_nvm_lock, &g_i2c_lock };
    int cnt = sizeof(req)/sizeof(req[0]);
    int retry_count = 0;
    const int MAX_RETRIES = 3;

    while(retry_count < MAX_RETRIES) {
        if (lock_multiple_with_timeout(req, cnt, DEFAULT_LOCK_TIMEOUT_MS)) {
            /* 临界区 */
            access_spi();
            access_nvm();
            access_i2c();

            unlock_multiple(req, cnt);
            return;
        } else {
            retry_count++;
            log_warning("Failed to lock resources, retry %d/%d...", retry_count, MAX_RETRIES);

            /* 指数退避 + 随机抖动，避免活锁 */
            uint32_t backoff_delay = (1 << retry_count) * 10 + (rand() % 10);
            task_delay_ms(backoff_delay);
        }
    }

    log_error("Failed to lock resources after %d retries.", MAX_RETRIES);
    /* 降级或报警逻辑 */
}
```

## 5. 嵌入式系统集成要点与最佳实践

- 初始化：在系统启动的单线程阶段，完成所有 `OrderedLock_t` 对象的初始化。
- 锁的作用域最小化：仅在必要时持有锁，临界区代码应尽可能简短高效。
- 超时参数调优：应根据该锁保护的临界区代码的最大正常执行时间来评估。一个好的起点是：`Timeout > (最大执行时间 * 1.5) + 系统抖动`。
- 与Watchdog联动：超时失败是系统异常的明确信号。累计超时次数，达到阈值后主动进入安全模式或计划性复位。
- 代码审查与静态分析：将"遵守全局锁顺序"作为代码审查的必检项。
- 活锁规避：采用带有随机抖动的指数退避（Exponential Backoff with Jitter）策略，有效错开不同任务的重试高峰。

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/156591921)

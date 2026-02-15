# eventpp 性能优化技术报告

> 仓库: [gitee.com/liudegui/eventpp](https://gitee.com/liudegui/eventpp) v0.2.0
> 基准版本: eventpp v0.1.3 (wqking/eventpp)
> 平台: 跨平台 (ARM + x86) | C++14

---

## 一、根因分析

通过逐行阅读 eventpp v0.1.3 核心头文件，定位到 6 个性能瓶颈：

| # | 根因 | 位置 | 严重程度 |
|---|------|------|:--------:|
| 1 | CallbackList 遍历时**每个节点都加锁** | `callbacklist.h` doForEachIf | 致命 |
| 2 | EventQueue enqueue **双锁** (freeListMutex + queueListMutex) | `eventqueue.h` doEnqueue | 高 |
| 3 | EventDispatcher dispatch 时**加排他锁查 map** | `eventdispatcher.h` | 高 |
| 4 | SpinLock 无 YIELD 指令，纯烧 CPU | `eventpolicies.h` | 中 |
| 5 | std::list 每节点堆分配，cache 不友好 | `eventpolicies_i.h` | 中 |
| 6 | 无 cache-line 对齐，多核 false sharing | 全局 | 中 |

---

## 二、优化方案 (OPT-1 ~ OPT-8)

### OPT-1: SpinLock CPU Hint [Batch 1]

添加平台特定的 CPU hint，降低自旋功耗：

```cpp
void lock() {
    while(locked.test_and_set(std::memory_order_acquire)) {
#if defined(__aarch64__) || defined(__arm__)
        __asm__ __volatile__("yield");
#elif defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
        __builtin_ia32_pause();
#endif
    }
}
```

### OPT-6: Cache-Line 对齐 [Batch 1]

对 EventQueue 热成员进行 cache-line 隔离，消除 false sharing：

```cpp
#define EVENTPP_ALIGN_CACHELINE alignas(64)

EVENTPP_ALIGN_CACHELINE mutable ConditionVariable queueListConditionVariable;
EVENTPP_ALIGN_CACHELINE mutable Mutex queueListMutex;
EVENTPP_ALIGN_CACHELINE Mutex freeListMutex;
```

### OPT-7: 内存序降级 [Batch 1]

`CounterGuard` 从 `seq_cst` 降级为 `acq_rel`/`release`。ARM 上避免额外的 `dmb ish` 全屏障：

```cpp
struct CounterGuard {
    explicit CounterGuard(T & v) : value(v) {
        value.fetch_add(1, std::memory_order_acq_rel);
    }
    ~CounterGuard() {
        value.fetch_sub(1, std::memory_order_release);
    }
};
```

### OPT-2: CallbackList 批量预取 [Batch 2] ★ 核心改动

原始代码每访问一个 `node->next` 都加锁（N 个回调 = N 次 mutex），这是 128μs 最大延迟的根因。

改为批量预取：一次加锁读取 8 个节点，无锁遍历批次，再加锁取下一批。锁操作减少约 8 倍。

```cpp
static constexpr size_t kBatchSize = 8;
NodePtr batch[kBatchSize];
while(node) {
    size_t count = 0;
    {
        std::lock_guard<Mutex> lockGuard(mutex);  // 每 8 个节点锁一次
        NodePtr cur = node;
        while(cur && count < kBatchSize) { batch[count++] = cur; cur = cur->next; }
    }
    for(size_t i = 0; i < count; ++i) { /* 无锁执行回调 */ }
    { std::lock_guard<Mutex> lockGuard(mutex); node = batch[count - 1]->next; }
}
```

> 最初尝试"一次快照全部节点"，但破坏了重入 append 语义（counter overflow 测试失败）。批量预取保留了重入语义。

### OPT-3: EventDispatcher 读写锁分离 [Batch 3]

dispatch（高频）用读锁，appendListener（低频）用写锁，多线程 dispatch 不再互斥：

```cpp
using SharedMutex = std::shared_timed_mutex;  // C++14
// dispatch: std::shared_lock<SharedMutex>   (读锁)
// append:   std::unique_lock<SharedMutex>   (写锁)
```

### OPT-4: doEnqueue try_lock [Batch 4]

`freeListMutex` 改为 `try_lock`，竞争时跳过回收直接分配新节点，不阻塞热路径：

```cpp
std::unique_lock<Mutex> lock(freeListMutex, std::try_to_lock);
if(lock.owns_lock() && !freeList.empty()) {
    tempList.splice(tempList.end(), freeList, freeList.begin());
}
```

### OPT-8: waitFor 自适应 Spin [Batch 4]

三阶段等待：快速检查 → 短暂 spin (128 次) → 回退到 futex。减少系统调用开销：

```cpp
if(doCanProcess()) return true;           // Phase 1: 快速检查
for(int i = 0; i < 128; ++i) {           // Phase 2: spin
    if(doCanProcess()) return true;
    /* yield / pause */
}
return cv.wait_for(lock, duration, ...);  // Phase 3: futex
```

### OPT-5: PoolAllocator 池化分配器 [Batch 5]

静态 per-type 池化分配器，通过 Policy 机制 opt-in。保留 `splice()` 兼容性（14 处调用）：

```cpp
struct MyPolicies {
    template <typename T>
    using QueueList = eventpp::PoolQueueList<T, 4096>;
};
eventpp::EventQueue<int, void(const Payload&), MyPolicies> queue;
```

关键设计：静态单例池 → `operator==` 恒 true → `splice()` 安全；池耗尽时透明回退到堆分配。

---

## 三、性能数据

测试环境：Ubuntu 24.04, GCC 13.3, `-O3 -march=native`

### Raw EventQueue (1M 消息)

| 指标 | 优化前 (v0.1.3) | 优化后 | 变化 |
|------|:---------------:|:------:|:----:|
| 吞吐量 | 22.2 M/s | 24.8 M/s | +12% |
| 入队延迟 | 46 ns | 42 ns | -9% |

### Active Object 模式（多线程）

| 指标 | 优化前 | 优化后 | 提升 |
|------|:------:|:------:|:----:|
| 吞吐量 (10K) | ~1.6 M/s | 8.5 M/s | 5.3x |
| 持续吞吐 (5s) | ~1.25 M/s | 3.1 M/s | 2.5x |
| E2E P50 | ~1,200 ns | 11,588 ns | 吞吐-延迟权衡 |
| E2E P99 | ~8,953 ns | 24,289 ns | 吞吐-延迟权衡 |

### PoolQueueList (OPT-5, 10K 消息)

| 方案 | 吞吐量 | 入队延迟 |
|------|:------:|:-------:|
| std::list (默认) | 22.2 M/s | 46 ns |
| PoolQueueList | 28.5 M/s | 36 ns |

### 资源消耗

| 指标 | 优化前 | 优化后 | 变化 |
|------|:------:|:------:|:----:|
| 测试套件时间 | ~23 s | ~18 s | -22% |
| 峰值内存 | 113 MB | 113 MB | 不变 |
| 上下文切换 | ~90 | 84 | -7% |

---

## 四、设计决策

| 问题 | 选择 | 原因 |
|------|------|------|
| OPT-2: 快照 vs 批量预取 | 批量预取 (8 节点) | 快照破坏重入 append 语义 |
| OPT-3: shared_mutex vs 无锁 map | shared_mutex | 改动小，C++14 兼容 |
| OPT-5: Ring Buffer vs Pool Allocator | Pool Allocator | Ring Buffer 不支持 splice()（14 处调用） |

---

## 五、修改文件

| 文件 | 涉及 OPT |
|------|----------|
| `include/eventpp/eventpolicies.h` | OPT-1, OPT-3, OPT-6 |
| `include/eventpp/callbacklist.h` | OPT-2 |
| `include/eventpp/eventdispatcher.h` | OPT-3 |
| `include/eventpp/hetereventdispatcher.h` | OPT-3 |
| `include/eventpp/eventqueue.h` | OPT-4, OPT-6, OPT-8 |
| `include/eventpp/internal/eventqueue_i.h` | OPT-7 |
| `include/eventpp/internal/poolallocator_i.h` | OPT-5 (新增) |

---

## 六、验证体系

| 验证项 | 方法 | 通过标准 |
|--------|------|----------|
| 编译 | `cmake --build . --target unittest` | 零错误 |
| 功能 | `ctest` (209 个测试用例) | 209/209 PASS |
| 线程安全 | `-fsanitize=thread` | 无新增 data race |
| 内存安全 | `-fsanitize=address` + `detect_leaks=1` | 零错误零泄漏 |
| 性能 | `eventpp_raw_benchmark` | 无回退 >5% |

```bash
cd refs/eventpp/tests && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build . --target unittest -j$(nproc)
ctest --output-on-failure
```

---

## 七、致谢

- [wqking/eventpp](https://github.com/wqking/eventpp) — 原始库
- [iceoryx](https://github.com/eclipse-iceoryx/iceoryx) — PoolAllocator 设计灵感

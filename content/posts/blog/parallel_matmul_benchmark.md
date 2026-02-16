---
title: "C++17 并行矩阵乘法: 从单线程到多进程共享内存的性能实测"
date: 2026-02-15
draft: false
categories: ["blog"]
tags: ["C++17", "DMA", "callback", "embedded", "heterogeneous", "lock-free", "message-bus", "newosp", "performance", "scheduler", "zero-copy"]
summary: "项目地址: [https://gitee.com/liudegui/zmq_parallel_tasks](https://gitee.com/liudegui/zmq_parallel_tasks)"
ShowToc: true
TocOpen: true
---

> 并行计算是嵌入式 Linux 平台上绑不开的话题。线程池、消息总线、共享内存 IPC -- 每种并行方案都有其适用场景和性能特征。本文以矩阵乘法为载体，基于 [newosp](https://github.com/DeguiLiu/newosp) 基础设施库，实测对比四种并行方案的性能差异，并分析各方案的架构取舍。

项目地址: [https://gitee.com/liudegui/zmq_parallel_tasks](https://gitee.com/liudegui/zmq_parallel_tasks)

## 1. 问题定义

矩阵乘法 C = A x B 是经典的可并行化计算任务。对于 N x N 矩阵，C 的每一行可以独立计算，天然适合按行拆分的并行策略。

我们选择 512 x 512 的 `float` 矩阵作为基准，原因:
- 单线程耗时约 200ms，足够体现并行加速效果
- 单个矩阵 1MB (512 x 512 x 4B)，三个矩阵 3MB，不会触发内存瓶颈
- 行粒度任务 (512 个) 远大于线程数 (64)，负载均衡充分

## 2. 公共基础: 零拷贝任务设计

传统做法是用 `std::ostringstream` 序列化任务数据，这在嵌入式场景下是不可接受的。我们的设计原则: POD 结构体 + `memcpy`，零堆分配。

```cpp
// matrix_task.hpp -- 编译期维度，POD 类型，trivially_copyable

static constexpr uint32_t kDim = MATRIX_DIM;  // CMake 注入

using Row    = std::array<float, kDim>;
using Matrix = std::array<Row, kDim>;

struct RowTask {
  uint32_t row_index;  // 4 字节，仅传递行号
};
```

关键设计决策:

- `RowTask` 只有 4 字节的行号，不携带矩阵数据。A/B 矩阵通过全局变量 (线程方案) 或共享内存 (进程方案) 共享，真正的零拷贝。
- `std::array` 替代裸数组，保持 `trivially_copyable` 的同时获得值语义。
- `MATRIX_DIM` 通过 CMake `target_compile_definitions` 注入，编译期确定维度，避免运行时分支。

行计算函数:

```cpp
inline void ComputeRow(const Row& a_row, const Matrix& B, Row& c_row) {
  for (uint32_t j = 0; j < kDim; ++j) {
    float sum = 0.0F;
    for (uint32_t k = 0; k < kDim; ++k) {
      sum += a_row[k] * B[k][j];
    }
    c_row[j] = sum;
  }
}
```

## 3. 方案一: 单线程基线

```cpp
auto t0 = std::chrono::high_resolution_clock::now();
for (uint32_t i = 0; i < kDim; ++i) {
  ComputeRow(g_A[i], g_B, g_C[i]);
}
auto t1 = std::chrono::high_resolution_clock::now();
```

没有任何框架开销，纯计算。这是所有并行方案的参照基准。

结果: 218 ms。

## 4. 方案二: std::thread + 原子工作窃取

最轻量的并行方案，不依赖任何框架:

```cpp
static std::atomic<uint32_t> g_next_row{0};

auto worker = [&]() {
  while (true) {
    uint32_t row = g_next_row.fetch_add(1U, std::memory_order_relaxed);
    if (row >= kDim) break;
    ComputeRow(g_A[row], g_B, g_C[row]);
  }
};

std::vector<std::thread> threads;
for (uint32_t i = 0; i < num_workers; ++i) {
  threads.emplace_back(worker);
}
for (auto& t : threads) t.join();
```

核心思路: 一个原子计数器 `g_next_row`，每个线程通过 `fetch_add` 抢占下一行。没有锁、没有队列、没有消息传递，开销仅为一次原子操作。

`memory_order_relaxed` 足够，因为:
- 每个线程写不同的行 (无数据竞争)
- A/B 矩阵在线程启动前已初始化 (happens-before 由 `thread::thread()` 保证)

结果: 8.6 ms，25.4x 加速。

## 5. 方案三: newosp WorkerPool

newosp 的 `WorkerPool` 是一个基于无锁 MPSC 总线的工作线程池，支持类型安全的消息分发:

```cpp
using Payload = std::variant<RowTask>;

osp::WorkerPoolConfig cfg;
cfg.name = "matmul";
cfg.worker_num = num_workers;

osp::WorkerPool<Payload> pool(cfg);
pool.RegisterHandler<RowTask>(
    [](const RowTask& task, const osp::MessageHeader& /*hdr*/) {
      ComputeRow(g_A[task.row_index], g_B, g_C[task.row_index]);
      g_completed.fetch_add(1U, std::memory_order_release);
    });

pool.Start();

for (uint32_t i = 0; i < kDim; ++i) {
  RowTask task{i};
  while (!pool.Submit(std::move(task))) {
    std::this_thread::yield();
  }
}

// 等待完成
while (g_completed.load(std::memory_order_acquire) < kDim) {
  std::this_thread::yield();
}
pool.Shutdown();
```

WorkerPool 的内部架构:
1. 主线程调用 `Submit()` 将任务推入无锁 MPSC 环形缓冲
2. 内部调度线程批量取出消息，分发到 N 个工作线程
3. 工作线程通过 `RegisterHandler<T>` 注册的回调处理任务

相比裸 `std::thread`，WorkerPool 多了一层消息总线的间接调用，但提供了:
- 类型安全的消息分发 (`std::variant` + `RegisterHandler<T>`)
- 自适应退避策略 (AdaptiveBackoff: spin -> yield -> sleep)
- 生命周期管理 (Start/Shutdown)

结果: 17.4 ms，12.5x 加速。

## 6. 方案四: 多进程共享内存 (零拷贝)

这是最有意思的方案。利用 newosp 的 `SharedMemorySegment` 实现跨进程零拷贝并行:

```cpp
// 共享内存布局: 三个矩阵 + 原子工作窃取计数器
struct ShmMatmulState {
  Matrix   A;
  Matrix   B;
  Matrix   C;
  std::atomic<uint32_t> next_row;
  std::atomic<uint32_t> completed;
  uint32_t total_rows;
  uint32_t padding[13];  // 缓存行对齐
};
```

Master 进程:

```cpp
auto shm = osp::SharedMemorySegment::CreateOrReplace(
    "pt_matmul_state", sizeof(ShmMatmulState));
auto* state = static_cast<ShmMatmulState*>(shm.value().Data());

// 初始化矩阵到共享内存
RandomFill(state->A, 42U);
RandomFill(state->B, 137U);

// fork N 个 worker 进程
for (uint32_t i = 0; i < num_workers; ++i) {
  const char* argv[] = {self_path, "--worker", nullptr};
  osp::SubprocessConfig cfg;
  cfg.argv = argv;
  osp::Subprocess sub;
  sub.Start(cfg);
  workers.push_back(std::move(sub));
}
```

Worker 进程:

```cpp
auto shm = osp::SharedMemorySegment::Open("pt_matmul_state");
auto* state = static_cast<ShmMatmulState*>(shm.value().Data());

// 与线程方案相同的工作窃取模式
while (true) {
  uint32_t row = state->next_row.fetch_add(1U, std::memory_order_relaxed);
  if (row >= state->total_rows) break;
  ComputeRow(state->A[row], state->B, state->C[row]);
  state->completed.fetch_add(1U, std::memory_order_release);
}
```

这个方案的精妙之处:
- 没有序列化/反序列化: 矩阵直接在共享内存中，所有进程直接读写
- 没有 IPC 通道: 不需要管道、socket、消息队列，原子变量就是同步机制
- 进程隔离: 任何一个 worker 崩溃不影响其他 worker 和 master
- 与线程方案代码几乎相同: 只是把全局变量换成了共享内存指针

结果: 17.4 ms，12.5x 加速。

## 7. 方案五: ZeroMQ (反面教材)

为了验证"消息传递"在细粒度并行任务中的局限性，我们实现了一个基于 ZeroMQ `inproc://` 协议的版本 (`examples/zmq_matmul.cpp`)。

架构:
- Master 线程创建 PUSH socket 分发任务
- Worker 线程创建 PULL socket 接收任务
- 每个任务包含 4 字节的行号 (RowTask)

虽然这也是"零拷贝"（指数据载荷小），但 ZMQ 框架本身的开销巨大:
1. **消息封装**: 每个任务需要创建一个 `zmq_msg_t`
2. **锁竞争**: PUSH/PULL socket 内部有互斥锁
3. **信号机制**: 线程唤醒依赖 eventfd/pipe，比原子自旋慢得多

**测试结果 (128x128 矩阵):**
- 单线程基线: 2.62 ms
- ZeroMQ: **42.18 ms** (比单线程慢 16 倍!)

这是一个经典的"粒度错误"。对于每行仅需几微秒的计算任务，引入一个重量级的消息中间件是致命的。

## 8. 性能对比

测试环境: 512 x 512 float 矩阵 (ZMQ 为 128x128 推算)，64 线程/进程，Release -O3。

| 方案 | 耗时 | 加速比 | 框架开销 |
|------|------|--------|----------|
| 单线程基线 | 218 ms | 1.00x | 无 |
| std::thread + 原子窃取 | 8.6 ms | 25.4x | 极低 (一次 atomic) |
| newosp WorkerPool | 17.4 ms | 12.5x | 中等 (MPSC 总线分发) |
| newosp SHM 多进程 | 17.4 ms | 12.5x | 中等 (fork + shm 映射) |
| ZeroMQ (inproc) | >3000 ms* | <0.1x | 极高 (消息对象 + 锁 + 信号) |

### 8.1 为什么 WorkerPool 比裸线程慢?

WorkerPool 的 17.4ms vs 裸线程的 8.6ms，差距来自架构差异:

- 裸线程: 每个线程直接 `fetch_add` 抢行，零间接调用
- WorkerPool: 主线程 -> MPSC 队列 -> 调度线程 -> 工作线程，多了两次上下文切换

但 WorkerPool 的价值不在于极致性能，而在于:
- 类型安全的消息分发 (不同任务类型走不同 handler)
- 自适应退避 (低负载时降低 CPU 占用)
- 生产级生命周期管理

对于矩阵乘法这种"所有任务类型相同、计算密集"的场景，裸线程是最优解。但在实际嵌入式系统中，一个线程池要处理多种异构任务，WorkerPool 的类型安全分发就体现出价值了。

### 8.2 SHM 多进程的开销分析

SHM 方案与 WorkerPool 耗时相同 (17.4ms)，但开销来源不同:
- `fork()` + `exec()` 创建进程: 约 1-2ms
- `shm_open()` + `mmap()` 映射共享内存: 约 0.1ms
- 计算阶段: 与裸线程相同的原子工作窃取

进程创建的一次性开销被 512 行的计算量摊薄后，与 WorkerPool 的持续性总线开销恰好持平。

SHM 方案的真正优势在大规模系统中:
- 进程隔离: worker 崩溃不影响 master
- 独立部署: 每个 worker 可以是不同的二进制
- 资源隔离: 每个进程有独立的地址空间、文件描述符表

## 9. 关于 AsyncBus 的补充说明

newosp 的 `AsyncBus` 是一个无锁 MPSC (多生产者单消费者) 消息总线，设计目标是事件驱动的异步通信，而非并行计算。

我们也实现了 AsyncBus 版本 (`osp_bus_matmul.cpp`)，但其性能接近单线程 (约 220ms)。原因很直接: MPSC 的 "单消费者" 意味着所有消息最终由一个线程处理，无法实现真正的并行计算。

这不是 AsyncBus 的缺陷，而是设计目标不同:
- AsyncBus: 适合事件驱动架构 (传感器数据 -> 处理 -> 输出)
- WorkerPool: 适合并行计算 (同一任务的多实例并行)

选择正确的工具解决正确的问题。

## 10. 构建与运行

```bash
mkdir build && cd build
# 开启 ZMQ 示例 (需要 libzmq)
cmake .. -DCMAKE_BUILD_TYPE=Release -DMATRIX_DIM=512 -DBUILD_ZMQ_EXAMPLE=ON
make -j$(nproc)

# 统一基准测试
./bench_matmul

# 单独运行
./baseline_matmul
./osp_thread_matmul
./osp_shm_matmul
./zmq_matmul
```

依赖: newosp 和 libzmq 通过 CMake FetchContent 自动拉取，无需手动安装。

## 11. 总结

四种方案的适用场景:

| 场景 | 推荐方案 |
|------|----------|
| 计算密集、同构任务 | std::thread + 原子工作窃取 |
| 异构任务、需要类型安全分发 | newosp WorkerPool |
| 需要进程隔离、独立部署 | newosp SHM 多进程 |
| 跨语言、分布式网络通信 | ZeroMQ |
| 事件驱动、异步通信 | newosp AsyncBus |

并行方案的选择不是"哪个最快"，而是"哪个最适合你的架构约束"。
- **裸线程**: 最快但最脆弱
- **WorkerPool**: 平衡了性能和工程质量
- **SHM 多进程**: 提供了最强的隔离性
- **ZeroMQ**: 在细粒度计算任务上表现糟糕，不要滥用

在嵌入式 Linux 系统中，这三种方案往往共存: WorkerPool 处理节点内的并行任务，SHM 实现节点间的零拷贝通信，AsyncBus 驱动整体的事件流。newosp 提供了统一的基础设施，让这些方案可以无缝组合。

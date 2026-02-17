---
title: "跨语言共享内存 IPC: C++ 与 Python 的零拷贝数据通道"
date: 2026-02-17T10:00:00+08:00
categories: ["ipc"]
tags: ["shared-memory", "ring-buffer", "cross-language", "cpp", "python", "lock-free"]
draft: false
---

C++ 采集、Python 处理是工业视觉的常见架构。两个进程之间传 1080p 帧，序列化/反序列化的开销比传输本身还大。共享内存是唯一能做到零拷贝的 IPC 方式，但跨语言使用有几个容易踩的坑。

本文基于 [cpp_py_shmbuf](https://github.com/DeguiLiu/cpp_py_shmbuf) 项目，记录从 v1 (Boost + `__sync_synchronize`) 到 v2 (POSIX shm_open + atomic_thread_fence) 的重构过程和设计决策。

## 1. 问题: 为什么不用 socket/pipe/protobuf?

1080p BGR 帧 = 1920 x 1080 x 3 = 6,220,800 字节。30 FPS = 180 MB/s。

| 方案 | 拷贝次数 | 序列化开销 | 30 FPS 可行性 |
|------|---------|-----------|--------------|
| TCP socket | 4 (user→kernel→kernel→user) | 无 | 勉强 (CPU 占用高) |
| Unix domain socket | 2 | 无 | 可行 |
| protobuf/msgpack | 2 + 编解码 | 高 (6MB 编码 ~10ms) | 不可行 |
| 共享内存 | 0 (同一物理页) | 无 | 轻松 (< 1% CPU) |

共享内存的本质: 两个进程的虚拟地址映射到同一块物理内存。写入方的 `memcpy` 直接对消费方可见，没有内核参与，没有数据拷贝。

## 2. v1 的三个问题

### 2.1 Boost.Interprocess 依赖

v1 用 `boost::interprocess::shared_memory_object` 管理共享内存。Boost 是 C++ 生态的瑞士军刀，但对于 "创建一块共享内存" 这个需求来说太重了:

- 编译时间增加 3-5 秒
- 部署需要 Boost 头文件 (或安装 libboost-dev)
- Python 端用的是 `multiprocessing.shared_memory`，两边 API 完全不同

实际上 POSIX `shm_open` + `mmap` 只需要 20 行代码。Windows 用 `CreateFileMappingA` + `MapViewOfFile` 也差不多。

### 2.2 `__sync_synchronize` 全屏障

v1 在每次读写索引后调用 `__sync_synchronize()`。这是 GCC 内置的全内存屏障，等价于 x86 的 `mfence` 或 ARM 的 `dmb ish`。

问题: SPSC 场景不需要全屏障。生产者只需要 release 语义 (写完数据后再更新 head)，消费者只需要 acquire 语义 (读到 head 后再读数据)。全屏障在 ARM 上的代价是 acquire/release 的 2-3 倍。

### 2.3 buf_full flag 竞态

v1 用一个 `uint32_t full` flag 区分 "空" 和 "满" (当 head == tail 时)。这个 flag 被生产者和消费者同时读写，没有原子保护:

```cpp
// Producer: append() 末尾
set_buf_full(w == r ? 1 : 0);  // 写 flag
set_windex(w);                  // 写 head

// Consumer: retrieve() 末尾
set_rindex(r);                  // 写 tail
set_buf_full(0);                // 写 flag  <-- 竞态!
```

两端同时写 `buf[8..11]`，在 ARM 弱内存序下可能丢失更新。

## 3. v2 设计: 三个关键决策

### 3.1 单调递增索引 (消除 buf_full flag)

借鉴 [ringbuffer](https://gitee.com/liudegui/ringbuffer) 的设计:

```
head, tail: uint32_t, 单调递增, 永不回绕到 0
capacity: 必须是 2 的幂
实际偏移 = index & (capacity - 1)

可读 = head - tail
可写 = capacity - (head - tail)
空:  head == tail
满:  head - tail == capacity
```

uint32_t 自然溢出在 4GB 处。对于 MB 级缓冲区，索引差值始终正确。不需要 buf_full flag，不需要额外的共享变量。

### 3.2 atomic_thread_fence (而非 std::atomic)

为什么不直接用 `std::atomic<uint32_t>`? 因为 Python 端无法操作 C++ 的 atomic 对象。共享内存中的数据必须是 POD (Plain Old Data)。

解决方案: 原始 `uint32_t` + `atomic_thread_fence`:

```cpp
// Producer: Write()
WriteRaw(head, &len, 4);           // 写长度前缀
WriteRaw(head + 4, data, len);     // 写载荷
atomic_thread_fence(release);       // 确保数据可见
header_->head = head + total;       // 更新 head

// Consumer: Read()
uint32_t tail = header_->tail;
atomic_thread_fence(acquire);       // 确保读到最新 head
uint32_t head = header_->head;
```

Python 端依赖两个保证:
1. 对齐的 uint32 读写在 x86 和 ARMv6+ 上天然原子
2. CPython GIL 提供额外的序列化

### 3.3 跨平台共享内存 (from libsharedmemory)

从 [libsharedmemory](https://github.com/kyr0/libsharedmemory) 提取 `Memory` 类，简化为:

```cpp
class SharedMemory {
  // POSIX: shm_open + mmap
  // Windows: CreateFileMappingA + MapViewOfFile
  // RAII: 析构时 munmap/UnmapViewOfFile
  // 错误: enum Error (无异常)
};
```

Consumer 打开时用 `fstat` 自动获取 size，不需要调用方传入。

## 4. 共享内存布局

```
Offset  Size  Description
------  ----  -----------
0       4B    head (uint32 LE, producer writes)
4       4B    tail (uint32 LE, consumer writes)
8       4B    capacity (uint32 LE, producer init, read-only)
12      4B    reserved (alignment)
16      NB    data area (circular buffer, N = capacity)
```

消息格式: `[4B length (LE)][payload]`

head 和 tail 分别由不同端独占写入，天然避免 false sharing (虽然在同一 cache line 内，但 SPSC 场景下不会同时写同一变量)。

## 5. 性能

测试环境: x86-64, GCC 13, -O2

| 消息大小 | 单线程吞吐 | 跨线程吞吐 | 延迟 (写+读) |
|---------|-----------|-----------|-------------|
| 64 B | 2.1 GB/s, 35M msg/s | 0.5 GB/s, 9M msg/s | 11 ns |
| 1 KB | 3.2 GB/s, 3.4M msg/s | 3.9 GB/s, 4.1M msg/s | 52 ns |
| 4 KB | 3.2 GB/s, 830K msg/s | 5.7 GB/s, 1.5M msg/s | 169 ns |
| 6 MB (1080p) | 2.5 GB/s, 423 FPS | 4.4 GB/s, 763 FPS | - |

30 FPS 1080p 仅需 180 MB/s，占跨线程吞吐能力的 4%。CPU 占用 < 1%。

对比 v1 (Boost + `__sync_synchronize`): 纯传输 FPS 从 ~133 提升到 ~423 (单线程) / ~763 (跨线程)，主要收益来自消除 buf_full flag 竞态和 Boost 层间接调用。

## 6. 跨语言协议约束

C++ 和 Python 共享同一块内存，必须遵守以下约定:

| 约束 | 原因 |
|------|------|
| 同架构 (不跨字节序) | uint32 LE 直接读写 |
| CPython (有 GIL) | 保证 struct.pack_into 的原子性 |
| ARMv6+ 或 x86 | 对齐 uint32 读写天然原子 |
| SPSC (单生产者单消费者) | 无锁设计的前提 |

如果需要支持 PyPy 或多消费者，需要引入 `multiprocessing.Lock` 或改用 [cpp-ipc](https://github.com/mutouyun/cpp-ipc) 的 MPMC 方案。

## 7. 项目结构

```
include/shm/
  shared_memory.hpp      -- 跨平台共享内存 (from libsharedmemory)
  byte_ring_buffer.hpp   -- SPSC 字节环形缓冲 (inspired by ringbuffer)
  shm_channel.hpp        -- 高层 API: ShmProducer / ShmConsumer
py/
  byte_ring_buffer.py    -- Python 环形缓冲 (兼容 C++ 布局)
  shm_channel.py         -- Python ShmProducer / ShmConsumer
```

C++ 端 header-only，零依赖，C++14。Python 端仅依赖标准库 `multiprocessing.shared_memory` (Python 3.8+)。

源码: [GitHub](https://github.com/DeguiLiu/cpp_py_shmbuf) | [Gitee](https://gitee.com/liudegui/cpp_py_shmbuf)

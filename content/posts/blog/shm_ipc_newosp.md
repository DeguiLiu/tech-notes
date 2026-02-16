---
title: "共享内存进程间通信的工程实践: 从 POSIX shm 原理到 newosp 无锁 Ring Buffer"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["ARM", "C++17", "CAS", "IPC", "POSIX", "RAII", "cpp-ipc", "embedded", "lock-free", "mmap", "newosp", "ring-buffer", "shared-memory", "zero-copy"]
summary: "共享内存是 Linux 进程间通信中延迟最低的机制，但原始的 POSIX shm_open/mmap 接口缺少同步、生命周期管理和崩溃恢复。本文从 POSIX 共享内存原理出发，剖析 newosp 框架中 ShmRingBuffer 的 CAS 无锁设计、ARM 内存序加固、缓存行对齐等工程决策，并与 cpp-ipc 库进行架构对比，展示嵌入式场景下共享内存 IPC 的完整工程方案。"
ShowToc: true
TocOpen: true
---

> 相关文章:
> - [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- ShmRingBuffer 底层的 SPSC 设计详解
> - [无锁编程核心原理](../lockfree_programming_fundamentals/) -- CAS 无锁设计的理论基础
> - [newosp 深度解析: C++17 事件驱动架构](../newosp_event_driven_architecture/) -- newosp 框架全景
> - [newosp ospgen: YAML 驱动的零堆消息代码生成](../newosp_ospgen_codegen/) -- ShmRingBuffer 传输的消息类型生成
>
> 参考: [C++编程：使用 cpp-ipc 实现基于共享内存的进程间发布订阅](https://blog.csdn.net/stallion5632/article/details/140881982)
>
> newosp 实现: [shm_transport.hpp](https://github.com/DeguiLiu/newosp) -- POSIX 共享内存 + 无锁 MPSC Ring Buffer
>
> 对比库: [cpp-ipc](https://github.com/mutouyun/cpp-ipc) -- 跨平台共享内存 IPC 库

## 1. 为什么选择共享内存

Linux 提供多种 IPC 机制，每种有不同的延迟和吞吐量特征：

| 机制 | 内核拷贝次数 | 典型延迟 | 适用场景 |
|------|:----------:|:-------:|---------|
| **共享内存** (shm) | 0 | **< 1 us** | 大数据量、低延迟 |
| Unix Domain Socket | 2 (用户→内核→用户) | 2-10 us | 通用 IPC |
| 管道 (pipe) | 2 | 2-10 us | 父子进程 |
| 消息队列 (mqueue) | 2 | 3-15 us | 小消息 |
| TCP loopback | 2+ | 10-50 us | 网络兼容 |

共享内存的核心优势：**零内核拷贝**。两个进程通过 `mmap` 将同一块物理内存映射到各自的虚拟地址空间，写入方的数据立即对读取方可见（受 CPU 缓存一致性协议保护），不经过 `read`/`write` 系统调用。

但共享内存只解决了"数据传输"问题，以下问题需要应用层自行处理：

- **同步**: 写入方何时完成？读取方何时可以读？
- **并发控制**: 多个生产者同时写入如何避免竞争？
- **生命周期**: 进程崩溃后共享内存如何清理？
- **命名与发现**: 如何让两个独立进程找到同一块共享内存？

## 2. POSIX 共享内存原理

### 2.1 核心 API

```c
#include <sys/mman.h>
#include <fcntl.h>

// 创建/打开共享内存对象 (返回文件描述符)
int fd = shm_open("/my_channel", O_CREAT | O_RDWR, 0600);

// 设置大小
ftruncate(fd, size);

// 映射到进程虚拟地址空间
void* addr = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);

// ... 通过 addr 直接读写 ...

// 解除映射
munmap(addr, size);

// 关闭文件描述符
close(fd);

// 删除共享内存对象 (所有进程关闭后释放)
shm_unlink("/my_channel");
```

### 2.2 在文件系统中的体现

```bash
# 共享内存对象出现在 tmpfs 文件系统中
$ ls -la /dev/shm/
-rw------- 1 root root 1048576 Feb 16 10:00 osp_shm_video_ch0
```

`shm_open` 本质上是在 `/dev/shm/` (tmpfs) 中创建一个文件。tmpfs 完全在内存中，不涉及磁盘 I/O。`mmap` 将该文件的页面映射到进程地址空间，多个进程映射同一文件即共享同一物理页。

### 2.3 生命周期管理难题

POSIX 共享内存的最大工程难题是**生命周期**：

```
进程 A: shm_open(CREATE) → mmap → 写入数据 → [SIGKILL/断电]
         ↑ 此时 shm_unlink 未调用，/dev/shm/ 中残留文件

进程 B: shm_open(OPEN) → 打开了上次的残留数据 → 数据不一致
```

`shm_unlink` 只是标记删除，实际释放发生在**所有**进程 `munmap + close` 之后。如果进程被 SIGKILL 或断电终止，`shm_unlink` 未执行，共享内存对象会残留在 `/dev/shm/` 中。

## 3. newosp 的共享内存架构

newosp 的 `shm_transport.hpp` 提供三层抽象：

```
┌─────────────────────────────────────────┐
│  ShmChannel                             │  命名通道 (Writer/Reader 端点)
│  ┌───────────────────────────────────┐  │
│  │  ShmRingBuffer<SlotSize, Count>   │  │  无锁 MPSC 环缓冲
│  │  ┌─────────────────────────────┐  │  │
│  │  │  SharedMemorySegment        │  │  │  POSIX shm RAII 封装
│  │  └─────────────────────────────┘  │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### 3.1 SharedMemorySegment: RAII 封装

```cpp
class SharedMemorySegment final {
public:
  // 创建: shm_open(O_CREAT | O_RDWR | O_EXCL) + ftruncate + mmap
  static expected<SharedMemorySegment, ShmError> Create(
      const char* name, uint32_t size) noexcept;

  // 容错创建: 先 shm_unlink 清除残留，再 Create
  static expected<SharedMemorySegment, ShmError> CreateOrReplace(
      const char* name, uint32_t size) noexcept;

  // 打开: shm_open(O_RDWR) + fstat + mmap
  static expected<SharedMemorySegment, ShmError> Open(
      const char* name) noexcept;

  // RAII 析构: munmap + close
  ~SharedMemorySegment();

  // 标记删除
  void Unlink() noexcept;

  void* Data() noexcept;
  uint32_t Size() const noexcept;

private:
  int32_t fd_;
  void* addr_;
  uint32_t size_;
  FixedString<64> name_;  // 零堆分配的名称存储
};
```

**设计要点:**

1. **`O_EXCL` 排他创建**: `Create` 使用 `O_EXCL` 标志，如果同名共享内存已存在则失败。这防止了两个进程同时创建导致的数据不一致。

2. **`CreateOrReplace` 崩溃恢复**: 先 `shm_unlink` 清除可能的残留，再执行标准创建。嵌入式系统中进程被 SIGKILL 或断电是常见场景。

3. **`expected<T, ShmError>` 错误处理**: 不使用异常（`-fno-exceptions` 兼容），通过 `expected` 返回值区分成功/失败。

4. **`FixedString<64>` 名称存储**: 共享内存名称 `/osp_shm_<user_name>` 存储在栈上，零堆分配。

5. **Move-only 语义**: 禁止拷贝，支持移动，确保文件描述符和 mmap 地址的唯一所有权。

### 3.2 ShmRingBuffer: 无锁 MPSC 环缓冲

这是整个 shm_transport 的核心。Ring Buffer 的全部状态 (原子变量 + 数据 slot) 都是 POD 类型，可以直接放在共享内存中，跨进程安全。

```cpp
template <uint32_t SlotSize = 4096, uint32_t SlotCount = 256>
class ShmRingBuffer final {
  static_assert((SlotCount & (SlotCount - 1)) == 0,
                "SlotCount must be power of 2");

  struct Slot {
    std::atomic<uint32_t> sequence;  // CAS 同步序号
    uint32_t size;                   // 实际数据大小
    char data[SlotSize];             // 数据负载
  };

  // 缓存行对齐: 防止 producer_pos_ 和 consumer_pos_ 的 false sharing
  alignas(64) std::atomic<uint32_t> producer_pos_;
  char pad_[64 - sizeof(std::atomic<uint32_t>)];
  alignas(64) std::atomic<uint32_t> consumer_pos_;
  Slot slots_[SlotCount];
};
```

#### 3.2.1 CAS 无锁 Push (生产者)

```cpp
bool TryPush(const void* data, uint32_t size) noexcept {
    if (size > SlotSize) return false;

    uint32_t prod_pos;
    Slot* target;

    // CAS 循环: 竞争一个 slot
    do {
        prod_pos = producer_pos_.load(std::memory_order_relaxed);
        target = &slots_[prod_pos & kBufferMask];

        uint32_t seq = target->sequence.load(std::memory_order_acquire);
        if (seq != prod_pos) {
            return false;  // 满了
        }
    } while (!producer_pos_.compare_exchange_weak(
        prod_pos, prod_pos + 1,
        std::memory_order_acq_rel, std::memory_order_relaxed));

    // 写入数据
    target->size = size;
    std::memcpy(target->data, data, size);

    // ARM 内存序: 确保 memcpy 完成后再发布序号
    std::atomic_thread_fence(std::memory_order_release);
    target->sequence.store(prod_pos + 1, std::memory_order_release);

    return true;
}
```

**CAS 协议详解:**

```
初始状态: slot[0].seq = 0, slot[1].seq = 1, ..., slot[N-1].seq = N-1
          producer_pos_ = 0, consumer_pos_ = 0

生产者 push 第 k 个消息:
  1. load producer_pos_ → prod_pos = k
  2. load slot[k % N].seq → 如果 seq == k，slot 可用
  3. CAS(producer_pos_, k, k+1) → 原子递增，竞争该 slot
  4. 写入数据到 slot
  5. store slot[k % N].seq = k + 1 → 发布 (对消费者可见)

消费者 pop 第 k 个消息:
  1. load consumer_pos_ → cons_pos = k
  2. load slot[k % N].seq → 如果 seq == k + 1，数据已就绪
  3. 读取数据
  4. store slot[k % N].seq = k + N → 释放 slot (对生产者可见)
  5. store consumer_pos_ = k + 1
```

序号 (sequence) 同时承担了**可用性标记**和**发布/释放语义**：

- `seq == prod_pos`: slot 空闲，可被生产者占用
- `seq == cons_pos + 1`: slot 有数据，可被消费者读取
- `seq == cons_pos + N`: slot 释放回池中

#### 3.2.2 ARM 内存序加固

x86 是 TSO (Total Store Order) 架构，store-store 顺序天然保证。ARM 是弱序架构，必须显式插入内存屏障：

```cpp
// 生产者端:
std::memcpy(target->data, data, size);          // 写入数据
std::atomic_thread_fence(std::memory_order_release);  // DMB ST
target->sequence.store(prod_pos + 1, release);  // 发布序号

// 消费者端:
uint32_t seq = slot.sequence.load(acquire);     // 读取序号
std::atomic_thread_fence(std::memory_order_acquire);  // DMB LD
std::memcpy(data, slot.data, size);             // 读取数据
```

如果没有 release fence，ARM 可能将 `sequence.store` 重排到 `memcpy` 之前，消费者看到序号更新但数据尚未写入。

#### 3.2.3 缓存行对齐

```cpp
alignas(64) std::atomic<uint32_t> producer_pos_;
char pad_[64 - sizeof(std::atomic<uint32_t>)];
alignas(64) std::atomic<uint32_t> consumer_pos_;
```

`producer_pos_` 和 `consumer_pos_` 分别由不同的 CPU 核心高频更新。如果它们在同一缓存行 (64 字节) 内，一个核心的写入会使另一个核心的缓存行无效 (false sharing)，导致性能降低。`alignas(64)` + padding 确保它们在不同缓存行。

### 3.3 ShmChannel: 命名通道

ShmChannel 组合了 SharedMemorySegment 和 ShmRingBuffer，提供面向用户的 API：

```cpp
template <uint32_t SlotSize = 4096, uint32_t SlotCount = 256>
class ShmChannel final {
public:
  // Writer 端: 创建共享内存 + 初始化 Ring Buffer
  static expected<ShmChannel, ShmError> CreateWriter(const char* name);
  static expected<ShmChannel, ShmError> CreateOrReplaceWriter(const char* name);

  // Reader 端: 打开共享内存 + 附加到 Ring Buffer
  static expected<ShmChannel, ShmError> OpenReader(const char* name);

  // 读写操作
  expected<void, ShmError> Write(const void* data, uint32_t size);
  expected<void, ShmError> Read(void* data, uint32_t& size);

  // 轮询等待 (指数退避: 50us → 100us → ... → 1ms)
  expected<void, ShmError> WaitReadable(uint32_t timeout_ms);

  uint32_t Depth() const;
  void Unlink();
};
```

**WaitReadable 的指数退避策略:**

```
初始 sleep: 50 us
每次翻倍: 50 → 100 → 200 → 400 → 800 → 1000 us (上限)
```

这是一个权衡：纯忙等 (spin) 延迟最低但浪费 CPU；固定 sleep 延迟高但省 CPU。指数退避在低负载时快速响应，在高负载时收敛到 1ms 轮询，适合嵌入式的 CPU 预算约束。

## 4. 与 cpp-ipc 的架构对比

[cpp-ipc](https://github.com/mutouyun/cpp-ipc) 是一个成熟的跨平台共享内存 IPC 库，支持 Linux/Windows/FreeBSD。以下从多个维度对比两者的设计取舍。

### 4.1 通信模型

| 维度 | newosp ShmChannel | cpp-ipc |
|------|:-----------------:|:-------:|
| 写模式 | MPSC (多写单读) | `ipc::route` SPMC, `ipc::channel` MPMC |
| 读模式 | 单消费者 | 多消费者广播 (最多 32) |
| 消息大小 | 固定 SlotSize (编译期) | 动态大小 (运行时) |
| 消费者最大数 | 1 | 32 (route/channel) |
| 数据分发 | 点对点 | 广播 / 可选单播 |

newosp 选择 MPSC 模型是因为嵌入式场景中，一个通道通常对应一个数据流 (如一路视频帧)，由一个消费者处理。如果需要多消费者，每个消费者开一个独立通道，避免广播的复杂性。

cpp-ipc 的广播模型更适合桌面/服务器场景，一份数据需要被多个订阅者消费。

### 4.2 内存管理

| 维度 | newosp ShmChannel | cpp-ipc |
|------|:-----------------:|:-------:|
| Slot 大小 | 编译期固定 (`static_assert`) | 运行时动态 |
| 堆分配 | 零 (热路径) | `std::vector` 用于大数据 |
| 消息序列化 | memcpy raw bytes | `ipc::buff_t` (支持 vector) |
| 最大消息 | SlotSize (如 4096 或 81920) | 理论无限 (分段传输) |

newosp 的固定 Slot 设计牺牲了灵活性 (消息不能超过 SlotSize)，但换来了**编译期确定性**：Ring Buffer 的总内存占用在编译时已知，不存在运行时分配失败的可能。

### 4.3 同步机制

| 维度 | newosp ShmChannel | cpp-ipc |
|------|:-----------------:|:-------:|
| 生产者同步 | CAS (`compare_exchange_weak`) | CAS + spin-lock |
| 消费者等待 | 指数退避轮询 (50us-1ms) | spin 重试 → 信号量等待 |
| 超时支持 | `WaitReadable(timeout_ms)` | `recv(timeout)` |
| 背压 | 返回 `kFull` 错误 | 策略可选 |

cpp-ipc 的"spin 重试 → 信号量等待"策略更智能：短时间 spin 捕获高频场景，超时后切换到信号量避免 CPU 空转。newosp 使用指数退避轮询达到类似效果，但不依赖信号量 (信号量在跨进程场景中需要 `sem_open` 额外管理)。

### 4.4 平台兼容性

| 维度 | newosp ShmChannel | cpp-ipc |
|------|:-----------------:|:-------:|
| 平台 | Linux only (POSIX) | Linux / Windows / FreeBSD |
| 共享内存 API | `shm_open` / `mmap` | `shm_open` (Linux), `CreateFileMapping` (Win) |
| 通知机制 | 指数退避轮询 | 信号量 (跨平台) |
| ARM 加固 | 显式 `atomic_thread_fence` | 依赖编译器内建 |
| 编译约束 | `-fno-exceptions -fno-rtti` 兼容 | 需要异常支持 |
| 依赖 | 仅 STL (header-only) | 仅 STL (需编译) |

newosp 仅面向 Linux 嵌入式，因此可以直接使用 POSIX API 而不需要跨平台抽象层。cpp-ipc 的跨平台支持增加了一层间接 (platform abstraction)，但覆盖面更广。

### 4.5 崩溃恢复

| 维度 | newosp ShmChannel | cpp-ipc |
|------|:-----------------:|:-------:|
| 残留清理 | `CreateOrReplace` (shm_unlink + Create) | 需手动清理或重启 |
| 状态一致性 | Ring Buffer 全 POD，序号协议自恢复 | 依赖原子操作一致性 |
| 进程监控 | `ThreadWatchdog` + `FaultCollector` 集成 | 无内建 |

newosp 的 `CreateOrReplace` 模式专为嵌入式设计：SIGKILL、断电后重启时自动清除 `/dev/shm/` 中的残留文件，无需运维手动干预。

## 5. 实战: newosp 视频帧传输

newosp 的 `examples/shm_ipc/` 演示了一个完整的视频帧跨进程传输系统：

```
┌─────────────────┐     /dev/shm/osp_shm_video_ch0     ┌─────────────────┐
│  shm_producer    │  ──────────────────────────────→  │  shm_consumer    │
│  (HSM 8 状态)    │     ShmChannel<81920, 16>          │  (HSM 8 状态)    │
│                  │     320x240 帧 = 76,816 B          │                  │
│  生成帧 → Write  │                                    │  Read → 校验帧   │
└─────────────────┘                                    └─────────────────┘
        ↓                                                       ↓
┌─────────────────┐                                    SpscRingbuffer<ShmStats>
│  shm_monitor     │  ←───── 统计快照 (48B) ──────────
│  Shell 调试      │
│  telnet :9527   │
└─────────────────┘
```

### 5.1 帧格式定义

```cpp
struct FrameHeader {
    uint32_t magic;    // 0x4652414D ('FRAM')
    uint32_t seq_num;  // 递增序号
    uint32_t width;    // 320
    uint32_t height;   // 240
};

static constexpr uint32_t kFrameSize = sizeof(FrameHeader) + 320 * 240;  // 76,816 B
static constexpr uint32_t kSlotSize = 81920;   // > kFrameSize
static constexpr uint32_t kSlotCount = 16;     // 16 个 slot 环缓冲
// 共享内存总大小 ≈ 16 x 82 KB ≈ 1.3 MB
```

### 5.2 Producer 状态机

```
Operational (root)
├── Init       → 创建通道 + 分配帧池 (FixedPool<80KB, 4>)
├── Running
│   ├── Streaming  → 正常生产帧 (Write)
│   ├── Paused     → 环缓冲满，等待 200us
│   └── Throttled  → 连续 3 次满，降速 5ms
├── Error      → 可恢复错误，重试初始化
└── Done       → Unlink 通道 + 输出统计
```

HSM 驱动的状态管理比简单的 `while(true)` + `if-else` 更健壮：

- **背压处理**: Ring Buffer 满时不是无限重试，而是进入 Paused 状态，避免 CPU 空转
- **降速机制**: 连续满标志着消费者跟不上，进入 Throttled 主动降速
- **崩溃恢复**: Error 状态可以重试初始化，不需要人工重启

### 5.3 Consumer 帧校验

```cpp
// 消费者逐字节校验帧完整性
bool ValidateFrame(const uint8_t* frame, uint32_t size) {
    auto* hdr = reinterpret_cast<const FrameHeader*>(frame);

    if (hdr->magic != 0x4652414Du) return false;     // magic 校验
    if (hdr->width != 320 || hdr->height != 240) return false;  // 尺寸一致性

    // 逐字节验证像素数据
    const uint8_t* pixels = frame + sizeof(FrameHeader);
    for (uint32_t i = 0; i < hdr->width * hdr->height; ++i) {
        if (pixels[i] != ((hdr->seq_num + i) & 0xFF)) {
            return false;  // 数据不一致
        }
    }
    return true;
}
```

这种逐字节校验可以检测到：
- 内存序错误 (数据未完整写入即被消费)
- 缓存一致性问题 (ARM 平台)
- Ring Buffer 索引溢出 (读到了另一帧的数据)

## 6. 与 cpp-ipc 使用方式对比

### 6.1 cpp-ipc: 发布订阅

```cpp
// 生产者
ipc::route channel{"my_channel", ipc::sender};
channel.wait_for_recv(1);
channel.send(data.data(), data.size());
channel.send(ipc::buff_t('\0'));  // 终止信号

// 消费者
ipc::route channel{"my_channel", ipc::receiver};
while (true) {
    auto buf = channel.recv();
    if (buf.empty() || static_cast<char*>(buf.data())[0] == '\0') break;
    // 处理 buf
}
```

**特点**: API 简洁，`send`/`recv` 隐藏了共享内存和同步细节。支持动态大小消息 (`ipc::buff_t` 可变长)。

### 6.2 newosp: 显式控制

```cpp
// 生产者
auto result = ShmChannel<81920, 16>::CreateOrReplaceWriter("video_ch0");
if (!result.has_value()) { /* 错误处理 */ }
auto channel = std::move(result.value());

uint8_t frame[81920];
// ... 填充帧数据 ...
auto write_result = channel.Write(frame, sizeof(FrameHeader) + 320 * 240);
if (!write_result.has_value()) {
    // ShmError::kFull -- 环缓冲满，需要背压处理
}
channel.Unlink();

// 消费者
auto result = ShmChannel<81920, 16>::OpenReader("video_ch0");
auto channel = std::move(result.value());

uint8_t buffer[81920];
uint32_t size = 0;
auto wait = channel.WaitReadable(1000);  // 等待最多 1 秒
if (wait.has_value()) {
    auto read_result = channel.Read(buffer, size);
    // 处理 buffer[0..size]
}
```

**特点**: 所有参数编译期确定 (SlotSize, SlotCount)，错误通过 `expected` 返回，零异常零堆分配。API 更底层，但每一步的行为完全可预测。

### 6.3 关键差异总结

| 维度 | cpp-ipc | newosp ShmChannel |
|------|:-------:|:-----------------:|
| API 风格 | 高层封装 (`send`/`recv`) | 底层显式控制 (`Write`/`Read` + `expected`) |
| 消息大小 | 运行时动态 | 编译期固定 (`static_assert`) |
| 多消费者 | 内建广播 (最多 32) | 每通道单消费者 |
| 等待机制 | spin → 信号量 | 指数退避轮询 |
| 异常依赖 | 需要 | `-fno-exceptions` 兼容 |
| 崩溃恢复 | 手动清理 | `CreateOrReplace` 自动 |
| 嵌入式适配 | 通用 | ARM 内存序、缓存行对齐、零堆分配 |

## 7. 共享内存 IPC 的工程注意事项

### 7.1 权限与安全

```cpp
static constexpr mode_t kShmPermissions = 0600;  // 仅 owner 读写
```

共享内存对象在 `/dev/shm/` 中是文件，权限管理与普通文件相同。嵌入式系统中通常所有进程以 root 运行，但在多用户系统中需要注意权限设置。

### 7.2 NUMA 感知

在多 NUMA 节点的服务器上，共享内存的物理页可能分配在创建进程所在的 NUMA 节点上。如果消费者在另一个 NUMA 节点的 CPU 上运行，每次访问需要跨节点，延迟增加 50-100 ns。

嵌入式 ARM Linux 通常是单 NUMA 节点 (UMA)，不存在此问题。

### 7.3 大页 (Huge Pages)

默认页大小 4 KB，76 KB 的视频帧需要 19 次 TLB miss (首次访问)。使用 2 MB 大页可以将 TLB miss 降到 1 次：

```c
mmap(NULL, size, PROT_READ | PROT_WRITE,
     MAP_SHARED | MAP_HUGETLB, fd, 0);
```

大页在高吞吐量场景 (如 4K 视频帧) 中可以带来 5-15% 的性能提升。

### 7.4 SELinux / seccomp 限制

容器化环境 (Docker) 中，`shm_open` 可能被 seccomp 策略拦截。需要在容器启动时添加 `--ipc=host` 或显式允许 `shm_open` 系统调用。嵌入式系统通常不运行在容器中，但如果使用 Yocto 构建的 SELinux 加固镜像，需要配置相应的 policy。

## 8. 总结

| 维度 | 裸 POSIX shm | cpp-ipc | newosp ShmChannel |
|------|:----------:|:-------:|:-----------------:|
| 同步机制 | 无 (需自行实现) | CAS + spin-lock + 信号量 | CAS + 指数退避 |
| 生命周期 | 手动 unlink | 手动管理 | RAII + CreateOrReplace |
| 堆分配 | 用户决定 | 有 (buff_t) | 零 (编译期固定) |
| ARM 支持 | 用户负责 | 编译器内建 | 显式 fence + alignas |
| 多消费者 | 用户实现 | 内建广播 | 每通道单消费者 |
| 错误处理 | errno | 异常/返回值 | `expected<T, ShmError>` |
| 适用场景 | 原型验证 | 桌面/服务器跨平台 IPC | **嵌入式实时系统** |

newosp 的共享内存 IPC 遵循嵌入式系统的核心原则：**编译期确定性、零堆分配、显式内存序、RAII 生命周期**。它牺牲了 cpp-ipc 的灵活性 (动态消息大小、多消费者广播、跨平台)，换来了确定性延迟和可审计的资源占用——这正是安全关键嵌入式系统所需要的。

> 参考实现: [newosp](https://github.com/DeguiLiu/newosp) -- MIT 协议开源，header-only
>
> 对比库: [cpp-ipc](https://github.com/mutouyun/cpp-ipc) -- MIT 协议开源，跨平台
>
> 相关文章: [C++编程：使用 cpp-ipc 实现基于共享内存的进程间发布订阅](https://blog.csdn.net/stallion5632/article/details/140881982)

---
title: "TCP 非阻塞发送的 Short Write 问题: 环形缓冲区 + epoll 事件驱动方案"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["C++17", "TCP", "epoll", "ring-buffer", "SPSC", "short-write", "non-blocking", "ARM", "embedded", "zero-copy"]
summary: "非阻塞 TCP 发送的 short write 问题在高吞吐嵌入式场景下不可回避。本文从一个 CSDN 环形缓冲方案出发，逐项分析其 5 个工程缺陷 (非 2 幂、无界索引、内存泄漏、部分发送丢失、EAGAIN 误判)，给出工程级改进方案: 2 的幂位掩码、精确 acquire-release 内存序、EPOLLOUT 驱动异步刷写，并对比 newosp SpscRingbuffer 的设计取舍。"
ShowToc: true
TocOpen: true
---

> 原文链接: [C++编程：利用环形缓冲区优化 TCP 发送流程，避免 Short Write 问题](https://blog.csdn.net/stallion5632/article/details/143668586)
>
> 相关: [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) | [ARM-Linux 网络性能优化](../arm_linux_network_optimization/)

---

## 1. 问题域: 什么是 TCP Short Write

非阻塞模式下调用 `send()` / `write()`，内核 TCP 发送缓冲区空间不足时，系统调用只写入**部分字节**并返回实际写入数，`errno` 置为 `EAGAIN` / `EWOULDBLOCK`。这就是 short write。

```
应用层: send(fd, buf, 4096)
        ↓ 内核 TCP 发送缓冲区只剩 1500 字节
        返回 1500 (而非 4096)
        剩余 2596 字节需要应用层自行处理
```

阻塞模式下 `send()` 会等待直到全部写完，但阻塞会导致线程挂起，在 epoll 事件循环中不可接受。

正确的做法: **用户态维护发送缓冲区**，配合 `EPOLLOUT` 事件在内核缓冲区可写时继续刷出。

---

## 2. 原始方案分析

原文实现了一个 `LockFreeBytesBuffer` (SPSC 字节环形缓冲) + `SocketContext` (epoll 事件驱动)。核心思路正确，但代码存在 5 个工程问题。

### 2.1 原始代码 (关键部分)

```cpp
class LockFreeBytesBuffer {
 public:
  static const std::size_t kBufferSize = 10240U;

  bool append(const char* data, std::size_t length) noexcept {
    const std::size_t current_write = writer_index_.load(std::memory_order_relaxed);
    const std::size_t current_read = reader_index_.load(std::memory_order_acquire);
    const std::size_t free_space =
        (current_read + kBufferSize - current_write - 1U) % kBufferSize;
    if (length > free_space) return false;

    const std::size_t pos = current_write % kBufferSize;
    const std::size_t first_part = std::min(length, kBufferSize - pos);
    std::memcpy(&buffer_[pos], data, first_part);
    std::memcpy(&buffer_[0], data + first_part, length - first_part);
    writer_index_.store(current_write + length, std::memory_order_release);
    return true;
  }

  std::size_t beginRead(const char** target) noexcept {
    const std::size_t current_read = reader_index_.load(std::memory_order_relaxed);
    const std::size_t current_write = writer_index_.load(std::memory_order_acquire);
    const std::size_t available = (current_write - current_read) % kBufferSize;
    if (available == 0U) return 0U;

    const std::size_t pos = current_read % kBufferSize;
    *target = &buffer_[pos];
    return std::min(available, kBufferSize - pos);
  }

  void endRead(std::size_t length) noexcept {
    const std::size_t current_read = reader_index_.load(std::memory_order_relaxed);
    reader_index_.store(current_read + length, std::memory_order_release);
  }

 private:
  char buffer_[kBufferSize];
  std::atomic<std::size_t> reader_index_{0};
  std::atomic<std::size_t> writer_index_{0};
};
```

### 2.2 问题 1: 缓冲区大小不是 2 的幂

```cpp
static const std::size_t kBufferSize = 10240U;  // 不是 2 的幂

const std::size_t pos = current_write % kBufferSize;  // 除法取模
```

10240 不是 2 的幂，`% 10240` 编译器无法优化为位掩码 `& (N-1)`，在 ARM Cortex-A53 上一次除法需要 ~20 个时钟周期，位与只需 1 个。

每次 `append()` 和 `beginRead()` 各有 2 次取模，单次 I/O 操作多出 ~60 ns 的无谓开销。

**修正**: 缓冲区大小用 2 的幂，取模改为位与:

```cpp
static constexpr std::size_t kBufferSize = 8192U;  // 2^13
static constexpr std::size_t kMask = kBufferSize - 1U;

const std::size_t pos = current_write & kMask;  // 1 条 AND 指令
```

### 2.3 问题 2: 无界索引 + 非 2 幂 = 溢出隐患

`writer_index_` 和 `reader_index_` 是无界递增的 `size_t`。如果 `kBufferSize` 是 2 的幂，无符号溢出后 `(write - read)` 的差值仍然正确 (利用无符号算术的回绕性质)。但 `kBufferSize = 10240` 下:

```cpp
std::size_t available = (current_write - current_read) % kBufferSize;
```

当 `write - read` 接近 `SIZE_MAX` 时，`% 10240` 的结果不等于实际有效数据量。虽然在实践中 `size_t` 的回绕周期极长 (64-bit 下约 1.8 x 10^19)，但设计上不应依赖此假设。

**修正**: 使用 2 的幂后，索引差值天然正确:

```cpp
// 2 的幂下，无符号差值 & kMask 始终正确
std::size_t available = (current_write - current_read);  // 无需 % 或 &
// available 直接表示有效数据量，因为 write 永远 >= read
```

### 2.4 问题 3: 测试程序内存泄漏

```cpp
// 原始代码: unique_ptr 创建后未存储
std::unique_ptr<SocketContext> client =
    std::make_unique<SocketContext>(epoll_fd, client_fd);

ev.data.ptr = client.get();
// client 在此作用域结束后析构，data.ptr 变成悬空指针
```

`unique_ptr` 在栈上创建，离开 `if` 块后立即析构，`epoll_event.data.ptr` 指向已释放内存。后续 `EPOLLOUT` 事件触发时解引用这个悬空指针，行为未定义。

此外，`addFd()` 在构造函数中已经 `EPOLL_CTL_ADD` 了一次，`main()` 中又加了一次，造成重复注册。

### 2.5 问题 4: `doSend` 不处理部分发送

```cpp
int doSend() {
  const char* pdata = nullptr;
  std::size_t data_size = buffer_.beginRead(&pdata);
  if (data_size == 0) return 0;

  int send_size = send(sock_fd_, pdata, static_cast<int>(data_size), MSG_DONTWAIT);
  if (send_size > 0) {
    buffer_.endRead(static_cast<std::size_t>(send_size));
  }
  return send_size;
}
```

`send()` 可能只发送了 `data_size` 的一部分 (short write)。此时 `endRead(send_size)` 正确推进了读指针，但**没有重新注册 `EPOLLOUT`** 来触发下一次刷写。在 EPOLLET (边缘触发) 模式下，如果不重新 MOD 事件，剩余数据将永远不会被发送。

LT (水平触发) 模式下问题较轻，因为只要发送缓冲区可写，`EPOLLOUT` 会持续触发。但原文使用 `EPOLLONESHOT`，每次事件后必须重新注册。

**修正**: `doSend()` 返回后检查缓冲区是否还有数据，有则重新注册 `EPOLLOUT`:

```cpp
int doSend() {
  // ... send logic ...
  if (send_size > 0) {
    buffer_.endRead(static_cast<std::size_t>(send_size));
  }
  // 缓冲区非空，继续注册 EPOLLOUT
  if (buffer_.available() > 0) {
    modifyEvent(true, true);  // EPOLLIN + EPOLLOUT
  } else {
    modifyEvent(true, false);  // 只保留 EPOLLIN
  }
  return send_size;
}
```

### 2.6 问题 5: EAGAIN 处理不完整

```cpp
if (send_size == -1 && errno != EAGAIN) {
  fprintf(stderr, "send failed, error: %s\n", strerror(errno));
}
```

两个问题:
- 缺少 `EWOULDBLOCK` 检查 (POSIX 允许 `EAGAIN != EWOULDBLOCK`，虽然 Linux 上相等)
- `EINTR` (被信号中断) 也应当重试，而非静默忽略

---

## 3. 工程级改进方案

### 3.1 SendBuffer: 2 的幂字节环形缓冲

```cpp
/// @brief SPSC 字节环形缓冲区，用于 TCP 非阻塞发送缓冲
/// @tparam SizeLog2 缓冲区大小的 log2 值 (默认 13 = 8KB)
template <uint32_t SizeLog2 = 13>
class SendBuffer {
 public:
  static constexpr uint32_t kSize = 1U << SizeLog2;
  static constexpr uint32_t kMask = kSize - 1U;

  /// @brief 写入数据到缓冲区 (生产者线程调用)
  /// @return 实际写入的字节数 (可能小于 len，表示缓冲区满)
  uint32_t Write(const uint8_t* data, uint32_t len) noexcept {
    const uint32_t w = write_idx_.load(std::memory_order_relaxed);
    const uint32_t r = read_idx_.load(std::memory_order_acquire);
    const uint32_t free = kSize - (w - r);  // 无符号差值在 2 的幂下天然正确
    const uint32_t to_write = (len < free) ? len : free;
    if (to_write == 0) return 0;

    const uint32_t pos = w & kMask;
    const uint32_t first = (kSize - pos < to_write) ? (kSize - pos) : to_write;
    std::memcpy(&buf_[pos], data, first);
    if (first < to_write) {
      std::memcpy(&buf_[0], data + first, to_write - first);
    }
    write_idx_.store(w + to_write, std::memory_order_release);
    return to_write;
  }

  /// @brief 获取可读数据的连续区间指针 (消费者线程调用)
  /// @param[out] ptr 指向缓冲区内数据起始位置 (零拷贝)
  /// @return 连续可读字节数 (不跨环形边界)
  uint32_t Peek(const uint8_t** ptr) noexcept {
    const uint32_t r = read_idx_.load(std::memory_order_relaxed);
    const uint32_t w = write_idx_.load(std::memory_order_acquire);
    const uint32_t avail = w - r;
    if (avail == 0) return 0;

    const uint32_t pos = r & kMask;
    *ptr = &buf_[pos];
    const uint32_t contig = kSize - pos;
    return (avail < contig) ? avail : contig;
  }

  /// @brief 消费者确认已读取 len 字节
  void Consume(uint32_t len) noexcept {
    read_idx_.fetch_add(len, std::memory_order_release);
  }

  /// @brief 查询缓冲区内待发送数据量
  uint32_t Pending() const noexcept {
    return write_idx_.load(std::memory_order_acquire)
         - read_idx_.load(std::memory_order_relaxed);
  }

  bool IsEmpty() const noexcept { return Pending() == 0; }

 private:
  alignas(64) std::atomic<uint32_t> write_idx_{0};
  alignas(64) std::atomic<uint32_t> read_idx_{0};
  alignas(64) uint8_t buf_[kSize]{};
};
```

与原始 `LockFreeBytesBuffer` 的设计差异:

| 设计点 | 原始方案 | 改进方案 |
|--------|---------|---------|
| 缓冲区大小 | 10240 (非 2 幂) | `1 << SizeLog2` (编译期保证) |
| 索引取模 | `% kBufferSize` (除法) | `& kMask` (1 条指令) |
| 可用空间计算 | `(r + N - w - 1) % N` | `N - (w - r)` (无符号差值) |
| 缓存行对齐 | 无 | `alignas(64)` 消除伪共享 |
| API 设计 | `beginRead`/`endRead` 分离 | `Peek`/`Consume` (更明确语义) |
| 索引类型 | `size_t` (8 字节) | `uint32_t` (4 字节, 嵌入式友好) |

### 3.2 AsyncSocket: 事件驱动异步发送

```cpp
/// @brief 非阻塞 TCP socket，内置发送缓冲区
class AsyncSocket {
 public:
  AsyncSocket(int epoll_fd, int sock_fd) noexcept
      : epoll_fd_(epoll_fd), fd_(sock_fd) {
    // 设置非阻塞
    int flags = ::fcntl(fd_, F_GETFL, 0);
    ::fcntl(fd_, F_SETFL, flags | O_NONBLOCK);
  }

  ~AsyncSocket() {
    if (fd_ >= 0) {
      ::epoll_ctl(epoll_fd_, EPOLL_CTL_DEL, fd_, nullptr);
      ::close(fd_);
    }
  }

  /// @brief 异步发送: 数据先入缓冲区，由 EPOLLOUT 驱动实际发送
  /// @return 实际入队的字节数 (< len 表示缓冲区满，应用层需处理背压)
  uint32_t AsyncSend(const uint8_t* data, uint32_t len) noexcept {
    uint32_t written = send_buf_.Write(data, len);
    if (written > 0 && !epollout_armed_) {
      ArmEpollout();
    }
    return written;
  }

  /// @brief EPOLLOUT 事件回调: 将缓冲区数据刷入内核
  /// @return >0 实际发送字节数, 0 缓冲区空, <0 连接错误
  int FlushSendBuffer() noexcept {
    int total_sent = 0;
    for (;;) {
      const uint8_t* ptr = nullptr;
      uint32_t avail = send_buf_.Peek(&ptr);
      if (avail == 0) break;

      ssize_t n = ::send(fd_, ptr, avail, MSG_DONTWAIT | MSG_NOSIGNAL);
      if (n > 0) {
        send_buf_.Consume(static_cast<uint32_t>(n));
        total_sent += static_cast<int>(n);
        continue;  // 尝试继续发送 (边界跨回环可能还有数据)
      }
      if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
          break;  // 内核缓冲区满，等下一次 EPOLLOUT
        }
        if (errno == EINTR) continue;  // 被信号中断，重试
        return -1;  // 真正的错误 (EPIPE, ECONNRESET 等)
      }
      // n == 0: 对端关闭
      return -1;
    }

    // 更新 epoll 注册状态
    if (send_buf_.IsEmpty()) {
      DisarmEpollout();
    }
    return total_sent;
  }

 private:
  void ArmEpollout() noexcept {
    struct epoll_event ev{};
    ev.data.ptr = this;
    ev.events = EPOLLIN | EPOLLOUT | EPOLLET;
    ::epoll_ctl(epoll_fd_, EPOLL_CTL_MOD, fd_, &ev);
    epollout_armed_ = true;
  }

  void DisarmEpollout() noexcept {
    struct epoll_event ev{};
    ev.data.ptr = this;
    ev.events = EPOLLIN | EPOLLET;
    ::epoll_ctl(epoll_fd_, EPOLL_CTL_MOD, fd_, &ev);
    epollout_armed_ = false;
  }

  int epoll_fd_;
  int fd_;
  SendBuffer<13> send_buf_;       // 8 KB 发送缓冲
  bool epollout_armed_ = false;
};
```

关键设计:

1. **EPOLLOUT 按需注册**: 只在缓冲区有数据时注册 `EPOLLOUT`，避免空转唤醒
2. **FlushSendBuffer 循环刷写**: 一次 `EPOLLOUT` 事件尽量多地发送数据 (EPOLLET 要求)
3. **EAGAIN/EINTR 正确处理**: `EAGAIN` 等待下次事件，`EINTR` 立即重试，其他错误断开
4. **背压感知**: `AsyncSend()` 返回实际入队字节数，应用层可据此控制生产速率

### 3.3 事件循环集成

```cpp
// epoll 事件循环
struct epoll_event events[64];
int n = ::epoll_wait(epoll_fd, events, 64, -1);

for (int i = 0; i < n; ++i) {
  auto* sock = static_cast<AsyncSocket*>(events[i].data.ptr);

  if (events[i].events & (EPOLLERR | EPOLLHUP)) {
    // 连接错误或对端挂断
    delete sock;
    continue;
  }
  if (events[i].events & EPOLLIN) {
    // 读取数据...
    sock->OnReadable();
  }
  if (events[i].events & EPOLLOUT) {
    int ret = sock->FlushSendBuffer();
    if (ret < 0) {
      // 发送失败，关闭连接
      delete sock;
      continue;
    }
  }
}
```

对比原始方案缺失的错误处理:
- `EPOLLERR` / `EPOLLHUP` 事件现在被正确检测
- 连接关闭时清理资源 (原始方案的 `unique_ptr` 生命周期管理有缺陷)

### 3.4 数据流时序

```
生产者线程                      I/O 线程 (epoll)
    |                               |
    | AsyncSend(data, 4096)         |
    |   → Write 4096B to SendBuffer |
    |   → ArmEpollout()            |
    |                               |
    |                          EPOLLOUT 触发
    |                               |
    |                          FlushSendBuffer()
    |                            → Peek() 获取连续区间
    |                            → send(fd, ptr, avail)
    |                            → 内核接受 1500B (short write)
    |                            → Consume(1500)
    |                            → send(fd, ptr, avail) 再次尝试
    |                            → EAGAIN (内核缓冲满)
    |                            → 等待下一次 EPOLLOUT
    |                               |
    |                          EPOLLOUT 再次触发
    |                            → 发送剩余 2596B
    |                            → 缓冲区空
    |                            → DisarmEpollout()
```

---

## 4. 与 newosp 基础设施的对比

newosp 的 `SpscRingbuffer<T, N>` 和本文的 `SendBuffer` 解决不同层面的问题:

### 4.1 SpscRingbuffer: 类型化元素队列

```cpp
// newosp: 传递结构化帧 (类型安全)
using RecvRing = osp::SpscRingbuffer<RecvFrameSlot, 32>;

RecvFrameSlot slot;
slot.header = ...;
std::memcpy(slot.payload, data, len);
ring.Push(slot);  // 整帧入队
```

- **用途**: 接收线程 → 处理线程的帧传递
- **元素**: 固定大小结构体 (`RecvFrameSlot` ~4KB)
- **操作粒度**: 整帧 Push/Pop

### 4.2 SendBuffer: 字节流缓冲

```cpp
// 本文: 字节流发送缓冲 (面向 TCP)
SendBuffer<13> buf;

buf.Write(header_bytes, 14);   // 帧头
buf.Write(payload, 4096);      // 载荷
// 由 EPOLLOUT 驱动 Peek() + send() + Consume()
```

- **用途**: 应用层 → TCP 发送的字节流暂存
- **元素**: 原始字节 (uint8_t)
- **操作粒度**: 可变长度字节块

### 4.3 核心共性

两者共享相同的底层设计原则:

| 设计原则 | SpscRingbuffer | SendBuffer |
|---------|----------------|------------|
| 2 的幂大小 + 位掩码 | `static_assert(IsPowerOf2)` | `1 << SizeLog2` |
| 无界递增索引 | `head_`/`tail_` 无符号递增 | `write_idx_`/`read_idx_` |
| acquire-release 配对 | `AcquireOrder()`/`ReleaseOrder()` | `acquire`/`release` |
| 缓存行对齐消除伪共享 | `alignas(kCacheLineSize)` | `alignas(64)` |
| SPSC 约束 (不可多线程写) | 文档约定 + API 分离 | 同上 |
| 零堆分配 | 栈上 `std::array` | 栈上 `uint8_t[]` |

### 4.4 newosp transport 的 short write 处理

newosp `transport.hpp` 中的 `SendAll()` 已经处理了 TCP short write:

```cpp
// newosp transport.hpp SendAll():
while (remaining > 0) {
  auto r = socket_.Send(ptr, remaining);
  // ... 循环直到全部发送
  ptr += sent;
  remaining -= sent;
}
```

这是**同步阻塞式**的 short write 处理 -- 循环重试直到全部写完。优点是实现简单，缺点是 `send()` 返回 EAGAIN 时直接判定为失败，不支持异步缓冲。

对于 newosp 的目标场景 (同机 shm_transport 优先，TCP 仅作远程备选)，同步方案是合理的选择。如果未来需要高吞吐 TCP 传输，可引入本文的 `SendBuffer` + EPOLLOUT 异步方案。

---

## 5. 内存序细节

### 5.1 为什么 Write 侧 load 自己的 write_idx 用 relaxed

```cpp
const uint32_t w = write_idx_.load(std::memory_order_relaxed);  // 只有自己写
const uint32_t r = read_idx_.load(std::memory_order_acquire);   // 对方写，需 acquire
```

SPSC 模型中，`write_idx_` 只由生产者线程修改，`read_idx_` 只由消费者线程修改。加载自己拥有的索引不需要同步 (值一定是上次 store 的值)，加载对方的索引需要 acquire 来保证看到对方的最新值以及相关的数据写入。

### 5.2 store 用 release 的含义

```cpp
write_idx_.store(w + to_write, std::memory_order_release);
```

release 保证: 在 store 之前的所有 `memcpy`(数据写入) 对另一个线程的 acquire load 可见。这是 SPSC 无锁正确性的核心 -- 消费者 acquire load 到新的 write_idx 后，数据一定已经就位。

### 5.3 ARM 上的实际代价

ARM (非 TSO 架构) 上:
- `relaxed` load/store: 普通 `ldr`/`str` 指令
- `acquire` load: `ldar` 指令 (ARM v8) 或 `ldr` + `dmb ishld` (ARM v7)
- `release` store: `stlr` 指令 (ARM v8) 或 `dmb ish` + `str` (ARM v7)

每次 `Write()` 只有 1 次 acquire + 1 次 release，开销可控。这也是 newosp SpscRingbuffer 提供 `FakeTSO` 模式的原因 -- 单核 MCU 上所有 acquire/release 可降级为 relaxed + compiler fence，进一步消除 barrier 开销。

---

## 6. 总结

1. **TCP short write 在非阻塞 + EPOLLET 模式下必须处理**。正确方案是用户态发送缓冲 + EPOLLOUT 事件驱动刷写，而非阻塞重试。

2. **字节环形缓冲的工程要求**: 2 的幂位掩码 (非除法取模)、无界递增无符号索引、精确 acquire-release 内存序、缓存行对齐消除伪共享。这些要求与结构化 SPSC 队列完全一致。

3. **EPOLLOUT 管理关键**: 按需注册 (有数据时 arm，空时 disarm)，EPOLLET 模式下一次事件循环内尽量多发送，区分 EAGAIN (等待) / EINTR (重试) / 其他 (断开)。

4. **同步 vs 异步 short write 处理**: 同步方案 (循环 `send()` 直到写完) 适合低吞吐场景，实现简单但阻塞调用线程; 异步方案 (缓冲 + EPOLLOUT) 适合高吞吐场景，不阻塞但需要管理缓冲区生命周期和背压。

---

## 参考

- [newosp SpscRingbuffer](https://github.com/DeguiLiu/newosp) -- C++17 header-only SPSC 无锁环形缓冲
- [SPSC 无锁环形缓冲区设计剖析](../spsc_ringbuffer_design/) -- 12 项设计决策详解
- [ARM-Linux 网络性能优化](../arm_linux_network_optimization/) -- 内核协议栈调优
- Linux man page: epoll(7), send(2)

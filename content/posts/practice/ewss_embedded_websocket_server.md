---
title: "EWSS: 面向嵌入式 Linux 的轻量级 WebSocket 服务器"
date: 2026-02-19T10:00:00
draft: false
categories: ["practice"]
tags: ["ARM", "C++17", "WebSocket", "embedded", "zero-copy", "state-machine", "poll", "reactor", "ringbuffer", "benchmark"]
summary: "从 Simple-WebSocket-Server 重构而来，去掉 ASIO 依赖，用 poll Reactor + 固定 RingBuffer + 状态机实现一个 67KB 二进制、12KB/连接、热路径零堆分配的嵌入式 WebSocket 服务器。"
ShowToc: true
TocOpen: true
---

## 背景

嵌入式 Linux 设备（激光雷达、机器人控制器、边缘网关）经常需要一个 WebSocket 接口，用于调试面板、远程配置、实时数据推送。现有方案要么太重（Boost.ASIO 体系，二进制 2MB+），要么太简陋（裸 socket 手写帧解析，缺乏状态管理）。

[Simple-WebSocket-Server](https://gitlab.com/eidheim/Simple-WebSocket-Server) 是一个广泛使用的 C++ WebSocket 库，功能完整、接口简洁。但它依赖 ASIO（或 Boost.ASIO），使用 `std::shared_ptr`、`std::ostream`、动态 `std::string` 做帧编码，每条消息都有堆分配。对桌面/服务器场景这不是问题，但在内存受限、要求确定性延迟的嵌入式平台上，这些开销不可接受。

[EWSS](https://github.com/DeguiLiu/ewss)（Embedded WebSocket Server）是对 Simple-WebSocket-Server 的嵌入式重构：去掉 ASIO 依赖，用 `poll()` 单线程 Reactor 替代多线程模型，用固定大小 RingBuffer 替代动态缓冲区，用状态机替代隐式的 ASIO handler 链。目标是在 67KB 二进制、12KB/连接的资源预算内，提供完整的 RFC 6455 WebSocket 协议支持。

项目地址: [https://github.com/DeguiLiu/ewss](https://github.com/DeguiLiu/ewss)

## 架构概览

```
Server (poll Reactor)
  |
  +-- Connection #1 ─┐
  +-- Connection #2  ├─ 每个连接:
  +-- Connection #N ─┘
        RxBuffer (RingBuffer<4096>)
            | readv 零拷贝接收
        ProtocolHandler (状态机)
            | on_message 回调
        Application
            | send()
        TxBuffer (RingBuffer<8192>)
            | writev 零拷贝发送
        TCP Socket (sockpp)
```

核心设计决策：

- 单线程 Reactor: `poll()` 事件循环，无锁、无上下文切换、Cache 友好
- 固定内存: 编译期确定的 RingBuffer 大小，运行时零堆分配
- 状态机驱动: 4 状态协议处理器（Handshaking/Open/Closing/Closed），静态实例零分配
- 零拷贝 I/O: `readv` 直接读入 RingBuffer，`writev` 直接从 RingBuffer 发送

## 为什么去掉 ASIO

不是 ASIO 不好，而是嵌入式场景的约束不同。

| 维度 | ASIO 方案 | EWSS 方案 |
|------|-----------|-----------|
| 二进制体积 | ~2 MB (含 ASIO 模板实例化) | 67 KB (stripped) |
| 每连接内存 | 动态，取决于消息大小 | 固定 12 KB (4KB RX + 8KB TX) |
| 热路径堆分配 | 每消息 `make_shared<SendStream>` | 零 |
| 线程模型 | 多线程 + strand 序列化 | 单线程，无锁 |
| 依赖 | Boost.ASIO 或 standalone ASIO | sockpp (仅 TCP 封装) |
| 异常处理 | 必须开启 | 可选 (`-fno-exceptions`) |

在 ARM Cortex-A 平台上，2MB 二进制意味着更多的 I-Cache miss；动态内存分配意味着不确定的延迟毛刺；多线程意味着锁竞争和上下文切换开销。对于 64 连接以内的嵌入式场景，单线程 `poll()` Reactor 是更合适的选择。

## 核心模块详解

### RingBuffer: 固定内存的循环缓冲

RingBuffer 是整个系统的数据通道。每个连接有两个：RxBuffer (4KB) 接收数据，TxBuffer (8KB) 发送数据。

```cpp
template <typename T, size_t Size>
class alignas(64) RingBuffer {
 public:
  static constexpr size_t kCapacity = Size;

  bool push(const T* data, size_t len);     // 写入数据
  size_t peek(T* data, size_t max_len) const; // 读取不移除
  void advance(size_t len);                  // 消费数据

  // 零拷贝 I/O 接口
  size_t fill_iovec(struct iovec* iov, size_t max_iov) const;       // writev 发送
  size_t fill_iovec_write(struct iovec* iov, size_t max_iov) const; // readv 接收
  void commit_write(size_t len);                                     // readv 后提交

 private:
  alignas(64) std::array<T, kCapacity> buffer_{};
  size_t read_idx_ = 0;
  size_t write_idx_ = 0;
  size_t count_ = 0;
};
```

关键设计点：

- `alignas(64)` 缓存行对齐，避免 false sharing
- `fill_iovec_write` + `commit_write` 配合 `readv`，内核直接写入 RingBuffer 的可写区域，省去一次 `memcpy`
- `fill_iovec` 配合 `writev`，从 RingBuffer 的读侧直接发送，同样零拷贝
- 环形缓冲区可能跨越数组边界，`fill_iovec` 返回 1 或 2 个 iovec 段处理 wrap-around

为什么不用 `std::vector` 或 `std::string`？因为它们会在数据增长时 `realloc`，产生不确定延迟和内存碎片。RingBuffer 的所有操作都是 O(1)，内存占用在编译期确定。

### 零拷贝接收路径

传统做法是先 `recv` 到临时缓冲区，再 `memcpy` 到应用缓冲区。EWSS 用 `readv` 直接读入 RingBuffer：

```cpp
expected<void, ErrorCode> Connection::handle_read() {
  struct iovec iov[2];
  size_t iov_count = rx_buffer_.fill_iovec_write(iov, 2);
  if (iov_count == 0) {
    return expected<void, ErrorCode>::error(ErrorCode::kBufferFull);
  }

  ssize_t n = ::readv(socket_.handle(), iov, static_cast<int>(iov_count));
  if (n > 0) {
    rx_buffer_.commit_write(static_cast<size_t>(n));
    protocol_handler_->handle_data_received(*this);
    return expected<void, ErrorCode>::success();
  }
  // ... 错误处理
}
```

`fill_iovec_write` 返回 RingBuffer 写侧的 1-2 个连续内存段（处理 wrap-around），`readv` 一次系统调用直接填充，`commit_write` 更新写指针。整个路径零 `memcpy`。

发送路径同理，`fill_iovec` 返回读侧的连续段，`writev` 一次系统调用发送：

```cpp
expected<void, ErrorCode> Connection::handle_write_vectored() {
  struct iovec iov[2];
  size_t iov_count = tx_buffer_.fill_iovec(iov, 2);
  if (iov_count == 0) return expected<void, ErrorCode>::success();

  ssize_t n = ::writev(socket_.handle(), iov, static_cast<int>(iov_count));
  if (n > 0) {
    tx_buffer_.advance(static_cast<size_t>(n));
  }
  // ...
}
```

### 协议状态机

WebSocket 连接有 4 个状态，每个状态是一个独立的 `ProtocolHandler` 实现：

```
Handshaking ──(握手成功)──> Open ──(Close 帧)──> Closing ──> Closed
     |                       |                                  ^
     +──(超时/错误)──────────+──────(错误)──────────────────────+
```

```cpp
// 静态实例，零堆分配
static HandshakeState g_handshake_state;
static OpenState      g_open_state;
static ClosingState   g_closing_state;
static ClosedState    g_closed_state;
```

状态转换通过指针切换实现，不需要 `new`/`delete`：

```cpp
void Connection::transition_to_state(ConnectionState state) {
  switch (state) {
    case ConnectionState::kOpen:
      protocol_handler_ = &g_open_state;
      if (on_open) on_open(shared_from_this());
      break;
    case ConnectionState::kClosed:
      protocol_handler_ = &g_closed_state;
      if (on_close) on_close(shared_from_this(), true);
      break;
    // ...
  }
}
```

每个状态只处理自己关心的事件。`HandshakeState` 解析 HTTP Upgrade 请求，`OpenState` 解析 WebSocket 帧，`ClosingState` 等待对端 Close 帧。职责清晰，不会出现 if-else 嵌套的状态混乱。

### 帧编码: 栈上完成

WebSocket 帧头最大 14 字节（2 字节基础 + 8 字节扩展长度 + 4 字节掩码）。EWSS 在栈上编码，直接写入 TxBuffer：

```cpp
void Connection::write_frame(std::string_view payload, ws::OpCode opcode) {
  uint8_t header_buf[14];  // 栈上分配
  size_t header_len = ws::encode_frame_header(
      header_buf, opcode, payload.size(), false);

  tx_buffer_.push(header_buf, header_len);
  if (!payload.empty()) {
    tx_buffer_.push(
        reinterpret_cast<const uint8_t*>(payload.data()), payload.size());
  }
}
```

对比 Simple-WebSocket-Server 的做法：

```cpp
// Simple-WebSocket-Server: 每次发送都堆分配
auto send_stream = make_shared<SendStream>();
*send_stream << message_str;  // std::ostream 格式化
connection->send(send_stream, callback);
```

一个是 14 字节栈缓冲 + RingBuffer push，一个是 `shared_ptr` + `ostream` + 堆分配。在嵌入式热路径上，差距是数量级的。

### Server: poll Reactor

Server 的主循环是经典的 Reactor 模式：

```cpp
void Server::run() {
  while (is_running_) {
    // 1. 构建 pollfd 数组（预分配，零堆分配）
    poll_fds_[0] = {server_sock_, POLLIN, 0};
    for (uint32_t i = 0; i < connections_.size(); ++i) {
      short events = POLLIN;
      if (connections_[i]->has_data_to_send()) events |= POLLOUT;
      poll_fds_[i + 1] = {connections_[i]->get_fd(), events, 0};
    }

    // 2. poll 等待事件
    int ret = ::poll(poll_fds_.data(), nfds, poll_timeout_ms_);

    // 3. 处理新连接（含过载保护）
    if (poll_fds_[0].revents & POLLIN) {
      if (stats_.is_overloaded(max_connections_)) {
        // Accept and immediately close to drain kernel backlog
        int reject_sock = accept(server_sock_, ...);
        if (reject_sock >= 0) ::close(reject_sock);
      } else {
        accept_connection();
      }
    }

    // 4. 处理客户端 I/O
    for (size_t i = 1; i < nfds; ++i) {
      handle_connection_io(connections_[i - 1], poll_fds_[i]);
    }

    // 5. 清理已关闭连接（swap-and-pop）
    remove_closed_connections();
  }
}
```

几个细节：

- `poll_fds_` 是 `std::array<pollfd, 65>`，编译期固定，不需要每轮 `new`
- `connections_` 是 `FixedVector<ConnPtr, 64>`，栈上分配，swap-and-pop 移除
- 过载保护：活跃连接超过 90% 容量时，accept 后立即 close，避免资源耗尽
- 性能监控：原子计数器跟踪 poll 延迟、连接数、错误数

### 词汇类型: 从 newosp 移植

EWSS 的基础类型（`expected`、`optional`、`FixedVector`、`FixedString`、`FixedFunction`、`ScopeGuard`）来自 [newosp](https://github.com/DeguiLiu/newosp) 库，全部栈分配、零堆开销：

| 类型 | 替代 | 用途 |
|------|------|------|
| `expected<V, E>` | 异常 / errno | 类型安全错误处理 |
| `FixedVector<T, N>` | `std::vector` | 连接列表 (N=64) |
| `FixedFunction<Sig, Cap>` | `std::function` | SBO 回调 |
| `ScopeGuard` | 手动 cleanup | RAII 资源释放 |

这些类型兼容 `-fno-exceptions -fno-rtti`，适合嵌入式编译配置。

## 性能实测

测试环境：x86-64 Linux (虚拟化)，GCC 13.3.0 -O2 Release，loopback TCP。EWSS 目标平台是 ARM-Linux 嵌入式，x86-64 结果作为基线参考。

### 单客户端吞吐量 (10,000 消息)

| 载荷大小 | 吞吐量 (msg/s) | P50 (us) | P99 (us) |
|----------|----------------|----------|----------|
| 8 B      | 27,344         | 35.5     | 55.9     |
| 64 B     | 27,446         | 35.5     | 54.6     |
| 128 B    | 26,830         | 36.1     | 58.9     |
| 512 B    | 25,462         | 37.7     | 61.0     |
| 1024 B   | 22,084         | 42.5     | 73.8     |

小载荷（8-128B）吞吐量稳定在 ~27K msg/s，说明瓶颈在系统调用开销而非数据拷贝。1KB 载荷下降到 22K msg/s，符合预期。

### 多客户端吞吐量 (64B 载荷)

| 客户端数 | 总吞吐量 (msg/s) | P50 (us) | P99 (us) |
|----------|------------------|----------|----------|
| 1        | 27,446           | 35.5     | 54.6     |
| 4        | 66,731           | 57.8     | 84.9     |
| 8        | 67,856           | 102.6    | 167.2    |

4 客户端时总吞吐量达到 ~67K msg/s，接近单线程 poll Reactor 的上限。8 客户端时吞吐量不再增长，P99 延迟上升到 167us，这是单线程模型的固有限制——所有连接共享一个事件循环。

### 资源占用

| 指标 | 值 |
|------|-----|
| 二进制大小 (stripped) | 67 KB |
| 静态库 (libewss.a) | 94 KB |
| 每连接内存 | ~12 KB (4KB RX + 8KB TX RingBuffer) |
| 热路径堆分配 | 0 |
| 最大连接数 (编译期) | 64 |

67KB 二进制 vs Simple-WebSocket-Server 的 ~2MB，差 30 倍。这个差距主要来自 ASIO 的模板实例化和异常处理代码。

## 设计权衡

EWSS 为嵌入式约束做了明确的取舍：

| 取舍 | EWSS 选择 | 代价 |
|------|-----------|------|
| 最大连接数 | 64 (编译期固定) | 不能动态扩展 |
| 线程模型 | 单线程 | CPU 密集型任务会阻塞所有连接 |
| 缓冲区大小 | 固定 4KB RX / 8KB TX | 大消息需要分片 |
| poll vs epoll | poll() | POSIX 可移植，但 O(n) 扫描 |
| 内存模型 | 全部预分配 | 固定容量，不能按需增长 |

这些取舍在嵌入式场景下是合理的：64 连接足够覆盖调试面板、配置接口、数据推送等典型用途；单线程避免了锁竞争；固��内存消除了碎片化风险。

对于需要数千并发连接和多核扩展的桌面/服务器场景，Simple-WebSocket-Server（或类似 ASIO 方案）仍然是更好的选择。

## Simple-WebSocket-Server 的优势

公平起见，列出 EWSS 做不到而 Simple-WebSocket-Server 能做的：

- 多线程扩展：ASIO 线程池可利用多核
- 动态缓冲区：处理任意大小的消息
- 成熟生态：ASIO 集成、OpenSSL TLS
- URL 路由：正则表达式端点路由
- 客户端库：内置 WebSocket 客户端

## 测试覆盖

EWSS 目前有 7 个测试套件，119 个测试用例，307 个断言：

- 单元测试：Base64、SHA1、帧解析、RingBuffer、连接状态机、对象池
- 集成测试：13 个端到端测试（握手、echo、批量消息、二进制、Ping/Pong、关闭、统计、回调）
- Sanitizer：ASan + UBSan 全部通过

集成测试使用原始 POSIX socket 实现的 WebSocket 客户端，覆盖了从 TCP 连接到 WebSocket 帧收发的完整链路，对标 Simple-WebSocket-Server 的 `io_test.cpp`。

## 快速上手

```bash
git clone https://github.com/DeguiLiu/ewss.git
cd ewss
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

最小 echo 服务器：

```cpp
#include "ewss/server.hpp"

int main() {
  ewss::Server server(8080);

  ewss::TcpTuning tuning;
  tuning.tcp_nodelay = true;
  server.set_tcp_tuning(tuning);

  server.on_message = [](const auto& conn, std::string_view msg) {
    conn->send(msg);  // Echo back
  };

  server.run();
}
```

## 适用场景

EWSS 适合这些场景：

- 嵌入式 Linux 设备的 WebSocket 调试/配置接口
- 资源受限环境（ARM Cortex-A，内存 < 64MB）
- 对延迟确定性有要求，不能容忍堆分配毛刺
- 连接数少（< 64），不需要多核扩展
- 需要最小二进制体积（67KB vs 2MB）

如果你的场景是高并发服务器、需要 TLS、需要 URL 路由，Simple-WebSocket-Server 或其他 ASIO 方案更合适。

项目地址: [https://github.com/DeguiLiu/ewss](https://github.com/DeguiLiu/ewss)

---

> 本文介绍的 EWSS 库基于 MIT 协议开源。

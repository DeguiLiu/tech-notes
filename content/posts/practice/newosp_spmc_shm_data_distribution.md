---
title: "跨进程数据分发: newosp SPMC 共享内存实战"
date: 2026-02-17T16:00:00
draft: false
categories: ["practice"]
tags: ["C++17", "shared-memory", "SPMC", "lock-free", "embedded", "IPC", "zero-copy"]
summary: "从进程内 MPSC 总线到跨进程 SPMC 共享内存，newosp 同时支持 1:1 (SPSC) 和 1:N (SPMC) 两种共享内存数据分发模式。本文以 LiDAR 点云分发为例，展示 SPMC 的设计、实现和工业应用场景。"
ShowToc: true
TocOpen: true
---

> 在[上一篇文章](/posts/practice/cyberrt_datavisitor_mccc_rewrite/)中，我们用无锁 MPSC 消息总线实现了进程内的观察者模式数据分发。但工业嵌入式系统中，数据源和消费者往往运行在不同进程甚至不同容器中。本文介绍 newosp 的 SPMC 共享内存通道，将数据分发从进程内扩展到跨进程，支持一写多读的零拷贝传输。

## 1. 问题: 进程内分发不够用

[基于无锁消息总线的观察者模式](/posts/practice/cyberrt_datavisitor_mccc_rewrite/)解决了进程内的数据分发:

```
进程内 (MPSC Bus)
┌─────────────────────────────────┐
│  Receiver → AsyncBus → Visitor  │
│                     → Visitor   │
│                     → Visitor   │
└─────────────────────────────────┘
```

这在单进程架构中工作良好。但当系统规模增长，出现以下需求时，进程内方案遇到瓶颈:

- **故障隔离**: 感知算法崩溃不应影响数据采集进程
- **独立升级**: 融合模块更新不需要重启整个系统
- **资源隔离**: 不同消费者需要独立的 CPU/内存配额
- **多语言**: 数据源是 C++ 驱动，消费者可能是 Python 算法

这些场景需要跨进程的数据分发。

## 2. newosp 的两层数据分发架构

newosp 提供完整的两层数据分发方案:

```
层级 1: 进程内 (AsyncBus, MPSC)
  多个生产者 → 无锁 Ring Buffer → 单 Worker 线程 → 多个订阅者
  适用: 同进程内的模块间通信

层级 2: 跨进程 (ShmSpmcByteChannel, SPMC)
  单个生产者 → POSIX 共享内存 → 多个消费者进程
  适用: 跨进程/跨容器的数据分发
```

两层可以组合: 数据采集进程通过 SPMC 共享内存分发原始数据，每个消费者进程内部再用 AsyncBus 做二次路由。

### 共享内存通道对比

| 维度 | ShmByteChannel (SPSC) | ShmSpmcByteChannel (SPMC) |
|------|----------------------|--------------------------|
| 消费者数 | 1 | 1..N (编译期可配，默认 8) |
| 写入检查 | `head - tail` | `head - slowest_tail` |
| 通知机制 | `futex wake 1` | `futex wake ALL` |
| 消费者注册 | 无 | CAS 原子注册 |
| 适用场景 | 点对点传输 | 数据分发 (1:N) |
| 内存开销 | header 64B + ring | header 64B + N×8B tails + ring |

SPSC 通道 (`ShmByteChannel`) 适合确定性的点对点传输，如传感器驱动到预处理模块。SPMC 通道 (`ShmSpmcByteChannel`) 适合一对多分发，如 LiDAR 点云同时送给感知、融合、日志三个模块。

## 3. SPMC 共享内存设计

### 3.1 内存布局

```
POSIX 共享内存 (/osp_lidar_spmc)
┌──────────────────────────────────────────────┐
│ ShmSpmcByteRingHeader (cache-line aligned)   │
│   head          : atomic<uint32_t>           │
│   capacity      : uint32_t                   │
│   max_consumers : uint32_t                   │
│   consumer_count: atomic<uint32_t>           │
│   tails[N]      : atomic<uint32_t> × N       │  ← 每个消费者独立 tail
│   active[N]     : atomic<uint8_t>  × N       │  ← 注册/注销标志
│   futex_word    : atomic<uint32_t>           │  ← futex 通知
│   writer_pid    : atomic<uint32_t>           │
├──────────────────────────────────────────────┤
│ Ring Buffer Data (capacity bytes)            │
│   [4B len][payload][4B len][payload]...      │  ← 长度前缀消息
└──────────────────────────────────────────────┘
```

关键设计:

- **Per-consumer tail**: 每个消费者维护独立的读位置，互不干扰。写入时检查最慢消费者的 tail，防止覆盖未读数据。
- **CAS 原子注册**: 消费者通过 `compare_exchange_strong` 原子操作注册到 `active[]` 数组，无锁、无竞态。
- **futex 广播**: 写入后 `FUTEX_WAKE` 唤醒所有等待的消费者，而非逐个通知。
- **长度前缀**: 每条消息前 4 字节 LE 编码长度，支持变长消息。

### 3.2 写入流程

```
Producer::Write(data, len)
  │
  ├─ 计算 needed = 4 + len (长度前缀 + 数据)
  │
  ├─ 检查可写空间:
  │    slowest_tail = min(tails[i] for active[i])
  │    writeable = capacity - (head - slowest_tail)
  │    if writeable < needed → 返回 kFull
  │
  ├─ 写入 ring buffer (可能跨尾部回绕)
  │    store length (4B LE)
  │    memcpy data
  │
  ├─ head.store(new_head, release)  ← 发布写入
  │
  └─ futex_wake(futex_word, INT_MAX)  ← 唤醒所有消费者
```

### 3.3 读取流程

```
Consumer[i]::Read(buf, max_len)
  │
  ├─ readable = head.load(acquire) - tails[i]
  │    if readable == 0 → 返回 kEmpty
  │
  ├─ 读取长度前缀 (4B LE)
  │    if msg_len > max_len → 返回 kTooLarge
  │
  ├─ memcpy data from ring buffer
  │
  └─ tails[i].store(new_tail, release)  ← 推进本消费者 tail
```

每个消费者独立推进自己的 tail，不影响其他消费者。这意味着:
- 快消费者不会被慢消费者阻塞
- 慢消费者会限制写入者的可用空间 (背压)
- 消费者崩溃后注销，其 tail 不再参与 slowest_tail 计算

### 3.4 消费者生命周期

```
OpenReader(channel_name)
  │
  ├─ shm_open + mmap (只读映射)
  ├─ RegisterConsumer():
  │    遍历 active[] 找空位
  │    CAS: active[i] = 0 → 1
  │    tails[i] = head (从当前位置开始读)
  │    consumer_count.fetch_add(1)
  └─ 返回 consumer_id = i

~ShmSpmcByteChannel() (析构)
  │
  ├─ UnregisterConsumer():
  │    active[consumer_id] = 0
  │    consumer_count.fetch_sub(1)
  └─ munmap + close
```

RAII 保证: reader 对象析构时自动注销消费者，即使进程异常退出 (通过 destructor)。

## 4. API 使用

### 4.1 生产者

```cpp
#include "osp/shm_transport.hpp"

// 创建 SPMC 通道 (256KB ring, 最多 4 消费者)
auto result = osp::ShmSpmcByteChannel::CreateOrReplaceWriter(
    "lidar_spmc", 256 * 1024, 4);
if (!result.has_value()) { /* 错误处理 */ }
auto channel = std::move(result.value());

// 写入数据
uint8_t frame[16016];
FillLidarFrame(frame, seq, timestamp);
auto wr = channel.Write(frame, sizeof(frame));
if (!wr.has_value()) {
  // ring full, 背压处理
}
```

### 4.2 消费者

```cpp
// 打开已有通道 (自动注册为消费者)
auto result = osp::ShmSpmcByteChannel::OpenReader("lidar_spmc");
if (!result.has_value()) { /* 通道不存在或消费者已满 */ }
auto reader = std::move(result.value());

// 等待数据 (futex, 超时 100ms)
reader.WaitReadable(100);

// 读取
uint8_t buf[16016];
auto rd = reader.Read(buf, sizeof(buf));
if (rd.has_value()) {
  uint32_t len = rd.value();
  ProcessFrame(buf, len);
}
```

### 4.3 编译期配置

```cpp
// 在包含头文件前定义，覆盖默认值
#define OSP_SHM_SPMC_MAX_CONSUMERS 16  // 默认 8
#include "osp/shm_transport.hpp"
```

## 5. 完整示例: LiDAR 点云分发

[data_visitor_dispatcher](https://github.com/DeguiLiu/newosp/tree/main/examples/data_visitor_dispatcher) 示例模拟工业场景中的 LiDAR 点云一对多分发:

```
                    POSIX 共享内存
                 /osp_lidar_spmc (256KB)
                 ShmSpmcByteRing (SPMC)
                        │
    ┌───────────────────┼───────────────────┐
    │                   │                   │
Producer           Visitor-Logging    Visitor-Fusion
(10 Hz LiDAR)     (帧统计/日志)      (障碍物检测)
    │
    ├── Monitor (telnet Shell)
    │
Launcher (进程管理器)
```

### 5.1 数据格式

```cpp
struct LidarPoint {
  float x, y, z;
  uint8_t intensity, ring, pad[2];
};  // 16 bytes

struct LidarFrame {
  uint32_t magic;        // 0x4C494441 ('LIDA')
  uint32_t seq_num;
  uint32_t point_count;  // 1000
  uint32_t timestamp_ms;
  LidarPoint points[1000];
};  // 16016 bytes
```

每帧 16016 字节，10 Hz 产生，256KB ring buffer 可缓存约 16 帧。

### 5.2 HSM 驱动的生产者

生产者使用层次状态机管理生命周期:

```
Operational (root)
├── Init       → 创建 ShmSpmcByteChannel
├── Running    → 父状态 (处理 SHUTDOWN/LIMIT)
│   ├── Streaming → 10 Hz 帧生产
│   └── Paused    → 背压 (ring full)
├── Error      → 可恢复错误, 1s 后重试
└── Done       → 清理退出
```

```cpp
// 状态转换由事件驱动
auto wr = ctx.channel.Write(ctx.frame_buf, kFrameDataSize);
if (wr.has_value()) {
  ++ctx.frames_sent;
  if (ctx.frames_sent >= ctx.max_frames)
    sm.Dispatch({kEvtLimitReached});  // → Done
} else {
  sm.Dispatch({kEvtRingFull});        // → Paused
}
```

当 ring buffer 满时，生产者从 Streaming 转入 Paused 状态，周期性检查可写空间，恢复后自动回到 Streaming。这比简单的 sleep-retry 更清晰，状态转换可追踪、可调试。

### 5.3 消费者: 日志 vs 融合

两个消费者读取相同的数据，做不同的处理:

**Visitor-Logging**: 每 10 帧输出统计，检测序号间隙，3 秒无数据报 stall。

**Visitor-Fusion**: 计算点云包围盒 (bounding box)，模拟 1-3ms 处理延迟，处理过慢时进入 Overloaded 状态跳帧。

两者完全独立，互不影响。融合模块崩溃不会影响日志模块，反之亦然。

### 5.4 Shell 调试

Monitor 进程提供 telnet 调试接口:

```bash
$ telnet localhost 9600
osp> dvd_status
Channel: osp_lidar_spmc
  Capacity: 262144 bytes
  Consumers: 2
  Readable: 48048 bytes

osp> dvd_stats
  Frames observed: 150
  Avg FPS: 9.98
  Avg frame size: 16016 bytes

osp> dvd_peek
  Frame #150: magic=LIDA seq=149 points=1000 ts=15000ms
```

### 5.5 运行

```bash
# 一键启动所有进程
./build/examples/data_visitor_dispatcher/osp_dvd_launcher --frames 200

# 或手动启动
./osp_dvd_producer osp_lidar_spmc 500    # 终端 1
./osp_dvd_visitor_logging osp_lidar_spmc  # 终端 2
./osp_dvd_visitor_fusion osp_lidar_spmc   # 终端 3
./osp_dvd_monitor osp_lidar_spmc 9600     # 终端 4
telnet localhost 9600                      # 终端 5
```

## 6. 工业应用场景

### 6.1 激光雷达点云分发

最典型的 SPMC 场景。一个 LiDAR 驱动进程采集点云，同时分发给:
- 感知模块 (障碍物检测)
- 定位模块 (SLAM)
- 日志模块 (数据录制)
- 可视化模块 (调试显示)

使用 SPMC 共享内存，4 个消费者读取同一份数据，零拷贝，无序列化开销。

### 6.2 视觉传感器多路消费

工业相机采集图像 (640×480, 30 FPS, ~900KB/帧):
- 质检算法 (缺陷检测)
- 定位算法 (视觉里程计)
- 录像模块 (存储回放)

SPMC 通道容量设为 4MB，可缓存约 4 帧，足够应对消费者的短暂延迟。

### 6.3 CAN 总线数据广播

车载/工业控制场景，CAN 网关进程接收总线数据:
- 仪表盘显示进程
- 数据记录进程
- 远程诊断进程
- OTA 升级监控进程

CAN 帧很小 (8-64 字节)，但频率高 (1000+ msg/s)。SPMC 的 futex 广播通知比逐个 pipe 通知更高效。

### 6.4 边缘计算数据流

边缘网关接收传感器数据流:
- 本地推理模块 (TensorRT/ONNX)
- 云端上传模块 (MQTT/gRPC)
- 本地存储模块 (时序数据库)
- 告警模块 (阈值检测)

不同模块可能用不同语言实现 (C++ 推理、Python 上传)，POSIX 共享内存是天然的跨语言 IPC。

### 6.5 SPSC vs SPMC 选型指南

| 场景 | 推荐 | 原因 |
|------|------|------|
| 传感器 → 预处理 (1:1) | ShmByteChannel (SPSC) | 确定性延迟，无 slowest_tail 开销 |
| 传感器 → 多算法 (1:N) | ShmSpmcByteChannel (SPMC) | 一份数据多路消费，零拷贝 |
| 高频小消息 (>1000 msg/s) | SPMC + 批量读取 | futex 广播比多路 pipe 高效 |
| 大帧低频 (<30 FPS) | SPMC | ring buffer 缓存足够 |
| 消费者数动态变化 | SPMC | CAS 注册/注销，运行时增减 |
| 消费者数固定为 1 | SPSC | 更简单，无注册开销 |

## 7. 与进程内分发的对比

回顾[基于无锁消息总线的观察者模式](/posts/practice/cyberrt_datavisitor_mccc_rewrite/)，两种方案的定位:

| 维度 | AsyncBus (进程内) | ShmSpmcByteChannel (跨进程) |
|------|-------------------|---------------------------|
| 通信范围 | 同进程线程间 | 跨进程 (POSIX shm) |
| 并发模型 | MPSC (多写单读) | SPMC (单写多读) |
| 数据格式 | `std::variant` 类型安全 | 原始字节流 (应用层定义格式) |
| 序列化 | 无 (内存直传) | 无 (共享内存零拷贝) |
| 故障隔离 | 无 (同进程) | 有 (进程级隔离) |
| 动态订阅 | `shared_ptr` + `weak_ptr` | CAS 原子注册 |
| 通知机制 | Ring Buffer 轮询 | futex 唤醒 |
| 延迟 | ~100ns (L1 cache) | ~1us (共享内存 + futex) |
| 适用场景 | 模块间解耦 | 进程间数据分发 |

两者可以组合使用: SPMC 负责跨进程传输，AsyncBus 负责进程内路由。

```
进程 A (数据采集)          进程 B (感知)           进程 C (融合)
┌──────────────┐     ┌──────────────────┐   ┌──────────────────┐
│ LiDAR Driver │     │ SPMC Reader      │   │ SPMC Reader      │
│      │       │     │      │           │   │      │           │
│      ▼       │     │      ▼           │   │      ▼           │
│ SPMC Writer ─┼─shm─┤→ AsyncBus       │   │→ AsyncBus       │
│              │     │   ├→ Detector    │   │   ├→ Fuser      │
│              │     │   └→ Tracker     │   │   └→ Planner    │
└──────────────┘     └──────────────────┘   └──────────────────┘
```

## 8. 资源预算

以 LiDAR 点云分发场景为例 (16KB/帧, 10 Hz, 4 消费者):

| 资源 | 用量 | 说明 |
|------|------|------|
| 共享内存 | 256KB + 128B header | ring buffer + SPMC header |
| 每消费者开销 | 8B tail + 1B active | 原子变量 |
| 写入带宽 | 160 KB/s | 16KB × 10 Hz |
| futex 系统调用 | 10 次/s (wake) | 每帧一次广播 |
| CPU (生产者) | <1% | memcpy + atomic store |
| CPU (消费者) | <1% (读取) + 算法 | memcpy + atomic load |

总内存开销约 256KB，对于嵌入式 ARM-Linux 平台 (通常 512MB+ RAM) 完全可接受。

## 9. 相关资源

- newosp 项目: [github.com/DeguiLiu/newosp](https://github.com/DeguiLiu/newosp) (Apache-2.0)
- SPMC 示例: [data_visitor_dispatcher](https://github.com/DeguiLiu/newosp/tree/main/examples/data_visitor_dispatcher)
- 进程内分发: [基于无锁消息总线的观察者模式](/posts/practice/cyberrt_datavisitor_mccc_rewrite/)
- 参考项目: [data-visitor-dispatcher](https://gitee.com/liudegui/data-visitor-dispatcher) (mccc-bus 版)
- 消息总线: [mccc-bus](https://gitee.com/liudegui/mccc-bus) -- C++17 header-only 无锁消息总线

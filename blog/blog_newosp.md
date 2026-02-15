# newosp: 一个面向工业嵌入式的 C++17 Header-Only 基础设施库

## 为什么写这个库

做嵌入式 Linux 开发这些年，一直有个痛点：项目之间的基础设施代码反复造轮子。消息总线、状态机、串口协议、共享内存 IPC、线程池......每个项目都要重新搭一遍，质量参差不齐，测试覆盖更是随缘。

ROS2 太重，对资源受限的嵌入式 Linux 平台不友好；自研框架又容易陷入"只有作者能维护"的困境。于是我决定把这些年积累的基础设施抽象出来，做成一个轻量、可组合、经过充分测试的 C++17 库。

这就是 newosp -- 一个面向工业嵌入式系统（激光雷达、机器人、边缘计算）的纯头文件基础设施库。

项目地址: [https://github.com/DeguiLiu/newosp](https://github.com/DeguiLiu/newosp)

## 核心设计原则

newosp 的设计围绕几个在嵌入式场景下至关重要的原则：

**栈优先，热路径零堆分配。** 库内提供了 `FixedVector<T, N>`、`FixedString<N>`、`FixedFunction<Sig, Cap>` 等固定容量容器，所有数据都在栈上或静态分配。在实时性要求高的路径上，不会出现 `malloc` 导致的不确定延迟。

**兼容 `-fno-exceptions -fno-rtti`。** 很多嵌入式项为了减小二进制体积和避免异常处理的运行时开销，会关闭异常和 RTTI。newosp 用 `expected<V, E>` 和 `optional<T>` 做类型安全的错误处理，完全不依赖异常机制。

**零全局状态。** 所有状态封装在对象中，通过 RAII 管理生命周期。支持多实例并行，不会出现全局单例导致的测试困难和耦合问题。

**编译期分发替代虚函数。** 标签分发、`if constexpr`、CRTP、变参模板......用现代 C++ 的编译期技术替代传统的虚函数 OOP，在保持灵活性的同时实现零开销抽象。

## 七层架构一览

newosp 按职责分为七层，共 38 个头文件：

```
应用层          app.hpp / post.hpp / qos.hpp / lifecycle_node.hpp
服务与发现层    service.hpp / discovery.hpp / node_manager.hpp / *_hsm.hpp
传输层          transport.hpp / shm_transport.hpp / serial_transport.hpp / transport_factory.hpp
网络层          socket.hpp / connection.hpp / io_poller.hpp / net.hpp
核心通信层      bus.hpp / node.hpp / worker_pool.hpp / spsc_ringbuffer.hpp / executor.hpp
状态机与行为树  hsm.hpp / bt.hpp
基础层          platform.hpp / vocabulary.hpp / config.hpp / log.hpp / timer.hpp / shell.hpp ...
```

每一层只依赖下层，不存在循环依赖。上层模块统一复用基础层的 `FixedString`、`FixedVector`、`SteadyNowUs` 等组件，零重复实现。

## 核心模块详解

### 无锁消息总线 (bus.hpp + node.hpp)

这是整个库的通信骨架。`AsyncBus<PayloadVariant>` 是一个基于 CAS 的无锁 MPSC 消息总线，支持优先级准入控制和 topic 路由。

```cpp
// 定义消息类型
struct SensorData { float temperature; float humidity; };
struct MotorCmd   { uint32_t mode; float target; };
using Payload = std::variant<SensorData, MotorCmd>;

// 创建节点，订阅消息
osp::Node<Payload> sensor("sensor", 1);
sensor.Subscribe<SensorData>([](const SensorData& d, const auto&) {
    OSP_LOG_INFO("sensor", "temp=%.1f humidity=%.1f", d.temperature, d.humidity);
});

// 发布并处理
sensor.Publish(SensorData{25.0f, 60.0f});
sensor.SpinOnce();
```

几个关键设计点：
- 基于 `std::variant` + `VariantIndex<T>` 的编译期类型路由，不需要字符串匹配
- FNV-1a 32-bit hash 做 topic 路由，O(1) 查找
- Bus 通过依赖注入传入 Node，而非全局单例
- 模板参数化 `QueueDepth` 和 `BatchSize`，适配不同硬件

### SPSC 环形缓冲 (spsc_ringbuffer.hpp)

无锁 wait-free 的单生产者单消费者环形缓冲区，是共享内存传输和工作线程池的基础组件。支持 `trivially_copyable` 类型约束、批量读写操作，以及针对 ARM 弱内存序的 `FakeTSO` 模式（用显式 acquire/release fence 替代 x86 的隐式 TSO 保证）。

### 层次状态机 (hsm.hpp)

零堆分配的层次状态机实现，支持 LCA（Least Common Ancestor）转换算法和 guard 条件。在 newosp 中被广泛使用：串口 OTA 的帧解析、共享内存 IPC 的生产者/消费者状态管理、节点心跳监控、服务生命周期管理......几乎所有需要状态管理的场景都用 HSM 来驱动。

### 行为树 (bt.hpp)

扁平数组存储、索引引用（非指针）的缓存友好行为树。支持 Sequence、Fallback、Parallel、Decorator 等标准节点类型。和 HSM 配合使用，HSM 管理底层状态转换，BT 编排高层任务流程。

### 实时调度 (executor.hpp)

提供 Single、Static、Pinned 三种通用调度器，以及一个 `RealtimeExecutor`，支持 `SCHED_FIFO` 实时调度策略、`mlockall` 内存锁定、CPU 亲和性绑定。适合对延迟敏感的工业控制场景。

### 多传输后端 (transport_factory.hpp)

`transport_factory` 根据通信双方的位置自动选择最优传输方式：
- 同进程内: inproc（直接函数调用）
- 同机器不同进程: 共享内存 IPC（`shm_transport.hpp`，无锁 SPSC）
- 跨机器: TCP/UDP（`transport.hpp`，v0/v1 帧协议）
- 工业串口: `serial_transport.hpp`（CRC-CCITT 校验，符合 IEC 61508）

### 可靠性基础设施

- `watchdog.hpp`: 软件看门狗，截止时间监控 + 超时回调
- `fault_collector.hpp`: 故障收集与上报，FaultReporter POD 注入，环形缓冲存储
- `lifecycle_node.hpp`: 生命周期节点（Unconfigured → Inactive → Active → Finalized），HSM 驱动
- `qos.hpp`: QoS 配置（Reliability、History、Deadline、Lifespan）
- `shell_commands.hpp`: 15 个内置诊断 Shell 命令，零侵入桥接

## 四个工业级示例

光看 API 文档容易觉得抽象，所以 newosp 提供了多个多文件示例，展示这些模块如何在真实场景中组合使用。这里重点介绍 4 个。

### 1. 串口 OTA 固件升级 (serial_ota/)

这是最复杂的一个示例，集成了 12 个 newosp 组件，模拟了一个完整的工业串口 OTA 固件升级流程。

**场景**: 主机通过串口向设备推送固件，支持分块传输、CRC 校验、NAK 重传、超时恢复。

**架构**:
- `protocol.hpp`: 定义帧格式（0xAA 帧头 / 0x55 帧尾）、CRC-CCITT constexpr 查表、OTA 命令集
- `parser.hpp`: HSM 驱动的 9 状态帧解析器（Idle → LenLo → LenHi → CmdClass → Cmd → Data → CrcLo → CrcHi → Tail），逐字节状态转移
- `host.hpp`: 主机端升级流程，用行为树编排 4 个阶段（SendStart → SendChunks → SendEnd → SendVerify）
- `device.hpp`: 设备端 6 状态 OTA 状态机（Idle → Erasing → Receiving → Verifying → Complete/Error），内含 Flash 模拟器
- `main.cpp`: 用 `SpscRingbuffer` 模拟双向 UART FIFO，注入约 5% 的信道噪声

这个示例的亮点在于 HSM 和 BT 的配合：BT 负责编排"发送启动 → 分块传输 → 发送结束 → 校验"的高层流程，HSM 负责底层的帧解析状态转换和设备端 OTA 状态管理。两者各司其职，代码结构清晰。

同时集成了 `ThreadWatchdog`（5s 超时检测）、`FaultCollector`（故障上报）、`AsyncBus`（进度/状态变更/完成事件广播）、`DebugShell`（9 条调试命令），展示了一个工业级应用该有的可观测性。

### 2. 共享内存 IPC (shm_ipc/)

三进程架构的共享内存通信示例，模拟视频帧的跨进程传输。

**三个进程**:
- `shm_producer`: HSM 驱动的帧生产者（8 状态），支持背压检测和自动降速
- `shm_consumer`: HSM 驱动的帧消费者（8 状态），支持重连重试和帧完整性校验
- `shm_monitor`: DebugShell 调试监控，提供 5 个 telnet 命令实时查看通道状态

**设计要点**:
- 生产者在 ring buffer 满时不是简单丢弃，而是进入 Paused 状态；连续 3 次满则进入 Throttled 降速状态
- 消费者对每一帧做逐字节校验（`(seq_num + offset) & 0xFF`），确保数据完整性
- 通过 `SpscRingbuffer` 在进程间传递统计快照（`ShmStats`，48 字节 trivially_copyable），监控进程可以实时获取生产者/消费者的运行状态
- 集成 `ThreadWatchdog`（线程存活监控）和 `FaultCollector`（ring-full、thread-death、frame-invalid 等故障码）

这个示例很好地展示了 newosp 在多进程场景下的能力：共享内存做数据面的零拷贝传输，`SpscRingbuffer` 做控制面的统计信息同步，`DebugShell` 提供运行时可观测性。

### 3. 流式协议 (streaming_protocol/)

模拟 GB28181/RTSP 风格的视频监控协议，展示 Bus + Node + Timer 的多节点协作。

**4 个节点共享一条 AsyncBus**:
- `Registrar`: 处理设备注册请求，分配 session_id
- `HeartbeatMonitor`: 监控心跳延迟，>500ms 告警
- `StreamController`: 处理流控命令（START/STOP）和流数据
- `Client`: 发起注册、发送心跳、控制流

**三阶段流程**:
1. 设备注册（HIGH 优先级消息）
2. 心跳保活（TimerScheduler 每 50ms 触发）
3. 流控制（START → 5 帧数据 → STOP，数据用 LOW 优先级）

这个示例的价值在于展示了 newosp 的消息优先级机制：注册请求用 HIGH 优先级确保及时处理，流数据用 LOW 优先级避免阻塞控制消息。所有消息类型都是 POD 结构，通过 `std::variant` 统一路由。

### 4. 多节点客户端网关 (client_gateway/)

IoT 网关场景，展示 WorkerPool + Node 的分工协作。

**组件分工**:
- `Node "gateway"`: 订阅连接/断连事件，管理客户端生命周期
- `Node "monitor"`: 订阅心跳事件，监控客户端健康状态
- `WorkerPool`（2 个 worker 线程）: 并行处理 `ClientData` 数据消息

**5 阶段主流程**:
1. 连接 4 个客户端
2. 提交 32 条数据消息（4 clients x 8 msgs），WorkerPool 并行处理
3. 发送心跳到监控节点
4. `FlushAndPause` 排空在途工作，汇总处理结果
5. 有序断连所有客户端

这个示例展示了一个关键模式：用 Node 处理控制面（连接管理、心跳监控），用 WorkerPool 处理数据面（业务数据并行处理）。`FlushAndPause` 确保所有在途工作完成后再进入下一阶段，避免数据丢失。统计信息全部用 `std::atomic` 在栈上分配，零堆开销。

## 测试与质量保证

newosp 目前有 788 个测试用例，覆盖所有模块：

- 正常模式: 788 tests
- `-fno-exceptions` 模式: 393 tests（排除 sockpp 依赖的测试）
- Sanitizer: AddressSanitizer + UBSanitizer + ThreadSanitizer 全部通过
- CI: GitHub Actions，Debug + Release 双配置，每次提交自动验证

测试框架用的 Catch2 v3.5.2，每个模块对应独立的测试文件 `tests/test_<module>.cpp`，另有集成测试 `tests/test_integration.cpp`。

## 快速上手

newosp 是纯头文件库，集成非常简单：

```bash
git clone https://github.com/DeguiLiu/newosp.git
cd newosp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DOSP_BUILD_EXAMPLES=ON
cmake --build build -j$(nproc)
```

在你的项目中使用：

```cmake
add_subdirectory(newosp)
target_link_libraries(your_app PRIVATE osp)
```

然后就可以 `#include "osp/bus.hpp"` 开始用了。所有外部依赖（Catch2、sockpp、inicpp 等）通过 CMake FetchContent 自动获取，不需要手动安装。

## 适用场景

newosp 适合这些场景：
- 工业嵌入式 Linux 设备（激光雷达、机器人控制器、边缘网关）
- 对实时性有要求，不能容忍 GC 或动态内存分配的不确定延迟
- 需要多种通信方式（进程内消息、共享内存 IPC、TCP/UDP、串口）统一管理
- 项目规模不大，不想引入 ROS2 这样的重量级框架
- 需要在资源受限环境下运行（支持 `-fno-exceptions -fno-rtti`）

如果你也在做类似的嵌入式 Linux 开发，欢迎试用和反馈。项目还在持续迭代中，Issue 和 PR 都欢迎。

项目地址: [https://github.com/DeguiLiu/newosp](https://github.com/DeguiLiu/newosp)

---

> 本文介绍的 newosp 库基于 MIT 协议开源，当前版本 v0.1.0。

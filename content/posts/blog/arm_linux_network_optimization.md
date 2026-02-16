---
title: "ARM-Linux 网络性能优化实战: 从中断到零拷贝的全链路调优"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["ARM", "Linux", "network", "performance", "UDP", "IRQ", "NAPI", "DMA", "zero-copy", "XDP", "ethtool", "sysctl", "PREEMPT_RT", "embedded", "real-time", "GRO", "RSS", "RPS"]
summary: "面向 ARM-Linux 嵌入式系统的网络性能优化系统指南。从数据包接收全链路出发，覆盖 CPU 频率管理、中断亲和性与分流（RSS/RPS/RFS）、NAPI 轮询、Ring Buffer 调优、协议栈 sysctl 参数、硬件卸载（GRO/TSO/Checksum）、DMA 与零拷贝、Busy Polling 低延迟技术、XDP 快速路径、实时调度（SCHED_FIFO/PREEMPT_RT）等十余个维度的工程实践。每项优化均标注适用场景、ARM 特有注意事项和副作用。"
ShowToc: true
TocOpen: true
---

> 原文链接: [嵌入式Linux的网络吞吐量优化](https://blog.csdn.net/stallion5632/article/details/143636884)
>
> 参考:
> - [Linux Network Performance Ultimate Guide](https://ntk148v.github.io/posts/linux-network-performance-ultimate-guide/)
> - [Monitoring and Tuning the Linux Networking Stack](https://blog.packagecloud.io/monitoring-tuning-linux-networking-stack-receiving-data/)
> - [Linux Kernel: Scaling in the Networking Stack](https://docs.kernel.org/networking/scaling.html)
> - [ARM: Optimize Network Interrupt Handling on Arm Servers](https://learn.arm.com/learning-paths/servers-and-cloud-computing/irq-tuning-guide/patterns/)
> - [Red Hat: Network Performance Tuning Guide](https://access.redhat.com/sites/default/files/attachments/20150325_network_performance_tuning.pdf)

## 1. 数据包接收全链路概览

优化网络性能的前提是理解数据包从网卡到应用层的完整路径。以下是 Linux 接收侧（RX）的关键阶段：

```
NIC Hardware
    |
    | DMA write to Ring Buffer (pre-allocated sk_buff)
    |
    v
HardIRQ (driver ISR)
    |
    | napi_schedule() -- 触发 SoftIRQ
    |
    v
SoftIRQ (NET_RX_SOFTIRQ)
    |
    | NAPI poll: driver->poll() 批量收包
    | GRO 聚合
    |
    v
netif_receive_skb()
    |
    | RPS/RFS 分发到目标 CPU
    |
    v
Protocol Stack (IP -> UDP/TCP)
    |
    | Socket Buffer (sk->sk_receive_queue)
    |
    v
Application (recvmsg / read)
```

每个阶段都可能成为瓶颈。优化的核心原则是：**减少每个阶段的 CPU 开销，减少数据拷贝次数，减少跨 CPU 缓存失效**。

## 2. CPU 频率管理

ARM 处理器普遍支持动态电压频率调节（DVFS）。默认的 `ondemand` 或 `schedutil` 调速器在网络 I/O 密集场景下存在问题：**网络中断和协议处理属于 I/O 密集型负载，但 CPU 利用率指标可能不高，导致调速器不升频**。

### 2.1 问题表现

`ondemand` 调速器仅观察 CPU 负载（`%busy`），不考虑 I/O 等待。网络收包主要在 SoftIRQ 上下文执行，CPU 利用率统计可能不准确。结果：CPU 运行在低频率下处理网络包，吞吐量受限。

在 ARM Cortex-A 系列上，这个问题尤为突出，因为 ARM SoC 的频率范围通常很宽（如 408 MHz ~ 1.8 GHz），低频与高频之间的性能差距达 4x 以上。

### 2.2 优化方案

```bash
# 查看当前调速器
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# 设置为 performance 模式（锁定最高频率）
echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# 永久生效（systemd 系统）
# 在 /etc/rc.local 或 udev 规则中设置
```

### 2.3 ARM 特有说明

现代 ARM 处理器（Cortex-A7/A53/A72）具备良好的时钟门控（clock gating）机制。实测表明：

- **空闲功耗差异极小**：Cortex-A7 在 408 MHz 和 1008 MHz 空闲时功耗几乎相同
- **"Race to idle" 策略有效**：高频快速完成任务后进入 WFI（Wait For Interrupt）低功耗状态，总能耗可能更低

因此，**对于非电池供电的嵌入式设备，`performance` 调速器是网络密集场景的推荐选项**。

**副作用**：电池供电设备需要评估功耗影响。可折中使用 `ondemand` 并设置 `io_is_busy=1`：

```bash
echo 1 > /sys/devices/system/cpu/cpufreq/ondemand/io_is_busy
```

## 3. 中断优化

网络中断是整个接收路径的起点。默认配置下，中断处理效率低下是 ARM-Linux 网络性能的首要瓶颈。

### 3.1 中断合并（Interrupt Coalescing）

默认情况下，每个数据包触发一次硬中断。在高流量场景下，每秒数十万次中断会耗尽 CPU 资源。

```bash
# 查看当前合并参数
ethtool -c eth0

# 设置合并参数：每 50us 或每 64 个帧触发一次中断
ethtool -C eth0 rx-usecs 50 rx-frames 64

# 启用自适应合并（驱动自动调整）
ethtool -C eth0 adaptive-rx on
```

**吞吐量 vs 延迟权衡**：

| 参数 | 高吞吐量场景 | 低延迟场景 |
|------|------------|-----------|
| `rx-usecs` | 50-100 | 0-10 |
| `rx-frames` | 64-256 | 1-16 |
| `adaptive-rx` | on | off |

**ARM 注意事项**：ARM SoC 上的以太网控制器（如 Marvell mvneta、Allwinner EMAC）通常只有一个 RX 队列，中断合并对这类设备尤为重要。关闭自适应模式（AIC）在某些场景下反而能提供更稳定的性能。

### 3.2 中断亲和性（IRQ Affinity）

将网络中断绑定到特定 CPU 核心，避免在多核之间随机迁移导致的缓存失效。

```bash
# 查看网卡中断号
grep eth0 /proc/interrupts

# 假设中断号为 42，绑定到 CPU 1（bitmask: 0x2）
echo 2 > /proc/irq/42/smp_affinity

# 或使用 CPU 列表格式
echo 1 > /proc/irq/42/smp_affinity_list
```

**原则**：

1. 网络中断和处理该网络数据的应用线程应绑定到同一个 CPU 或同一 NUMA 节点
2. 不同网卡的中断应分散到不同 CPU
3. 禁用 `irqbalance` 守护进程（它会动态迁移中断，在网络密集场景下反而有害）

```bash
systemctl stop irqbalance
systemctl disable irqbalance
```

### 3.3 RSS、RPS 与 RFS

当 NIC 只有单队列（ARM 嵌入式常见），所有包的中断由同一个 CPU 处理，协议栈处理也在同一个 CPU，其他核心空闲。这时需要 **RPS**（Receive Packet Steering）将协议处理分散到多核。

```
              硬件单队列
                 |
           HardIRQ (CPU 0)
                 |
           NAPI poll (CPU 0)
                 |
          ┌──────┼──────┐
          v      v      v
        CPU 1  CPU 2  CPU 3     <-- RPS 按流哈希分发
          |      |      |
       Protocol Stack 并行处理
```

```bash
# 启用 RPS：将 RX 队列 0 的处理分散到 CPU 0-3（bitmask: 0xf）
echo f > /sys/class/net/eth0/queues/rx-0/rps_cpus

# 设置 RPS 流表大小（建议 32768）
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries
echo 2048 > /sys/class/net/eth0/queues/rx-0/rps_flow_cnt
```

**RFS**（Receive Flow Steering）在 RPS 基础上更进一步：将包导向**正在消费该流数据的应用所在 CPU**，提升数据缓存命中率。RPS 和 RFS 通常配合使用。

**RSS**（Receive Side Scaling）是硬件多队列方案，如果 NIC 支持，优先使用 RSS：

```bash
# 查看队列数
ethtool -l eth0

# 设置 RX 队列数（匹配 CPU 核心数）
ethtool -L eth0 combined 4
```

| 方案 | 实现层 | 前提条件 | ARM 可用性 |
|------|--------|----------|-----------|
| RSS | 硬件 | NIC 支持多队列 + MSI-X | 高端 SoC（如 i.MX8） |
| RPS | 软件 | 内核 CONFIG_RPS | 所有 ARM Linux |
| RFS | 软件 | RPS + 应用使用 connect() | 所有 ARM Linux |

## 4. NAPI 轮询与 SoftIRQ 调优

NAPI（New API）是 Linux 网络栈的核心优化机制：收到第一个包时触发硬中断，随后切换到轮询模式批量收包，避免高频中断风暴。

### 4.1 NAPI Budget 调优

```bash
# 每次 SoftIRQ 处理的最大包数（默认 300）
sysctl -w net.core.netdev_budget=600

# SoftIRQ 处理超时（微秒，默认 2000）
sysctl -w net.core.netdev_budget_usecs=4000

# 单个 NAPI poll 的 weight（驱动层参数，通常默认 64）
# 需要在驱动代码中修改，或通过驱动模块参数设置
```

**ARM 注意事项**：ARM 处理器的单核性能通常低于 x86，SoftIRQ 处理速度较慢。适当增大 `netdev_budget` 可以让每次轮询处理更多包，但不能过大，否则会饿死其他 SoftIRQ（如定时器）。建议从 300 开始逐步增加到 600-1200 进行测试。

### 4.2 Busy Polling（低延迟模式）

Busy Polling 让应用线程在 `recvmsg()` 等待数据时直接轮询 NIC 的 NAPI 队列，绕过 SoftIRQ 调度延迟。

```bash
# 全局启用（单位：微秒）
sysctl -w net.core.busy_read=50
sysctl -w net.core.busy_poll=50
```

或在代码中按 socket 启用：

```c
int val = 50;  /* 微秒 */
setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &val, sizeof(val));
```

**性能数据**（参考 Cloudflare 和 FIX Protocol 基准）：

| 指标 | 无 Busy Poll | 有 Busy Poll |
|------|------------|-------------|
| 平均延迟 | 47.5 us | 16.4 us |
| 最大延迟 | 166 us | 131 us |

约 **3x 平均延迟改善**。

**副作用**：CPU 核心在等待数据期间持续占用（忙等待），功耗显著增加。适用于延迟敏感型应用（如工业控制 UDP 响应），不适用于电池供电场景。

**ARM 嵌入式实测**：在 DE0-Nano-SoC（ARM Cortex-A9）上，基础 Linux UDP 延迟为 0.5 ms，通过 Busy Polling + CPU 绑定可降至 0.1 ms 以内。

## 5. Ring Buffer 与 Backlog 调优

### 5.1 NIC Ring Buffer

Ring Buffer 是 NIC 与内核之间的 DMA 缓冲区，存储待处理的数据包描述符。Ring Buffer 满时，新到达的包会被丢弃。

```bash
# 查看当前 Ring Buffer 大小和最大值
ethtool -g eth0

# 增大 Ring Buffer（如果硬件允许）
ethtool -G eth0 rx 4096 tx 4096
```

**诊断丢包**：

```bash
# 查看 NIC 统计中的丢包
ethtool -S eth0 | grep -i drop
ethtool -S eth0 | grep -i discard

# 查看系统级统计
cat /proc/net/softnet_stat
# 第一列: 处理包数 第二列: 丢包数 第三列: time_squeeze（budget 用尽次数）
```

**权衡**：大 Ring Buffer 增加吞吐量但也增加最大延迟（更多包在队列中等待）。低延迟场景应使用较小的 Ring Buffer 配合更频繁的中断。

### 5.2 Backlog 队列

Backlog 队列位于 Ring Buffer 之后、协议栈之前，是 RPS 分发的目标缓冲区。

```bash
# 增大 backlog 队列（默认 1000）
sysctl -w net.core.netdev_max_backlog=25000
```

**何时增大**：当 `/proc/net/softnet_stat` 第二列非零时，说明 backlog 满导致丢包，应增大。

## 6. 协议栈 sysctl 参数调优

### 6.1 Socket Buffer

```bash
# UDP/TCP 接收缓冲区最大值
sysctl -w net.core.rmem_max=16777216    # 16 MB
sysctl -w net.core.wmem_max=16777216    # 16 MB

# UDP 接收缓冲区默认值
sysctl -w net.core.rmem_default=1048576  # 1 MB

# TCP 缓冲区自动调优（min, default, max）
sysctl -w net.ipv4.tcp_rmem="4096 1048576 16777216"
sysctl -w net.ipv4.tcp_wmem="4096 1048576 16777216"
```

**ARM 注意事项**：嵌入式设备 RAM 有限（通常 256 MB - 2 GB），不应盲目套用服务器级参数。建议：

| 设备 RAM | rmem_max 建议值 | 说明 |
|----------|----------------|------|
| 256 MB | 2 MB | 保守设置 |
| 512 MB | 4-8 MB | 平衡 |
| 1 GB+ | 16 MB | 高吞吐量 |

### 6.2 TCP 拥塞控制

```bash
# 查看可用算法
sysctl net.ipv4.tcp_available_congestion_control

# BBR 拥塞控制（Linux 4.9+，部分场景提升 2-25x）
sysctl -w net.ipv4.tcp_congestion_control=bbr
sysctl -w net.core.default_qdisc=fq
```

BBR 由 Google 开发，在高延迟和有丢包的链路上表现优异。但在 ARM 嵌入式的局域网场景（低延迟、低丢包），默认的 `cubic` 通常已经足够。

### 6.3 其他关键参数

```bash
# TCP 窗口缩放（高带宽必须开启）
sysctl -w net.ipv4.tcp_window_scaling=1

# TCP 连接复用
sysctl -w net.ipv4.tcp_tw_reuse=1

# 禁用不需要的协议（减少协议栈开销）
sysctl -w net.ipv6.conf.all.disable_ipv6=1

# 增大连接跟踪表（如果使用 conntrack/NAT）
sysctl -w net.netfilter.nf_conntrack_max=131072
# 或者完全禁用 conntrack（不需要 NAT/状态防火墙时）
# modprobe -r nf_conntrack
```

## 7. 硬件卸载功能

现代 NIC 可以将部分协议处理卸载到硬件，释放 CPU 资源。

### 7.1 查看和管理卸载

```bash
# 查看所有卸载特性
ethtool -k eth0

# 常用卸载开关
ethtool -K eth0 rx-checksumming on     # 接收校验和卸载
ethtool -K eth0 tx-checksumming on     # 发送校验和卸载
ethtool -K eth0 gro on                 # Generic Receive Offload
ethtool -K eth0 tso on                 # TCP Segmentation Offload
ethtool -K eth0 sg on                  # Scatter-Gather
```

### 7.2 各卸载特性说明

| 特性 | 作用 | ARM 可用性 |
|------|------|-----------|
| **Checksum Offload** | NIC 硬件计算 IP/TCP/UDP 校验和 | 多数 ARM NIC 支持 |
| **GRO** (Generic Receive Offload) | 将多个小包在软件层合并为大包再上送协议栈 | 所有 Linux（软件实现） |
| **LRO** (Large Receive Offload) | 硬件层合并，但可能违反 RFC | 部分高端 NIC |
| **TSO** (TCP Segmentation Offload) | NIC 硬件做 TCP 分段 | 部分 ARM NIC |
| **GSO** (Generic Segmentation Offload) | 软件层推迟分段到最后一刻 | 所有 Linux |
| **Scatter-Gather** | 非连续内存直接发送，减少拷贝 | 多数 ARM NIC |

**GRO 是 ARM 嵌入式最有价值的卸载**：它是纯软件实现，不依赖硬件支持，通过合并 TCP/UDP 数据包减少协议栈处理次数。

**排错提示**：如果遇到网络性能异常，尝试关闭所有卸载作为基线测试，然后逐个开启定位问题：

```bash
ethtool -K eth0 gro off tso off sg off rx off tx off
```

## 8. DMA 与零拷贝

### 8.1 DMA 基础

NIC 通过 DMA 直接将数据写入内核内存，绕过 CPU。DMA 性能取决于：

- **缓冲区分配策略**：`dma_alloc_coherent`（一致性映射，无需手动同步）vs `dma_map_single`（流式映射，需显式同步）
- **缓冲区对齐**：ARM 要求 DMA 缓冲区按 `ARCH_DMA_MINALIGN`（通常为 cache line 大小）对齐
- **Cache 一致性**：ARM 非一致性 DMA 需要显式 `dma_sync_single_for_cpu/device` 调用

### 8.2 ARM 特有的 DMA 注意事项

```
CPU Cache          Main Memory         NIC DMA
    |                  |                   |
    |<-- cache line -->|                   |
    |                  |<-- DMA write -----|
    |                  |                   |
    |--- 需要 invalidate cache 才能看到 DMA 数据 ---|
```

ARM 的 DMA 一致性问题：

1. NIC DMA 写入主存后，CPU Cache 可能仍持有旧数据
2. 需要在 DMA 完成后调用 `dma_sync_single_for_cpu()` 使 Cache 失效
3. `dma_alloc_coherent` 分配的内存默认映射为 uncacheable，避免一致性问题但降低 CPU 访问速度

**优化建议**：对于高频访问的 DMA 缓冲区（如网络接收），使用流式 DMA 映射 + 显式同步，比 coherent mapping 性能更好。

### 8.3 零拷贝技术

传统数据路径需要多次拷贝：

```
NIC -> DMA -> Kernel Buffer -> copy_to_user -> App Buffer
         (1)                      (2)
```

零拷贝技术减少或消除第 (2) 次拷贝：

| 技术 | 原理 | 适用场景 |
|------|------|----------|
| `mmap` | 内核缓冲区直接映射到用户空间 | 自定义驱动 |
| `sendfile` | 内核内直接从文件到 socket，不经过用户空间 | 文件传输 |
| `MSG_ZEROCOPY` | 发送时不拷贝用户数据 | 大包发送 |
| AF_XDP | 用户空间直接访问 NIC ring buffer | 高性能收发 |

**ARM `mmap` 注意事项**：Linux 在 ARM/ARM64 上默认将 DMA mmap 映射为 non-cacheable（避免 Cache 别名问题）。如需 cacheable 映射（性能更好但需要手动同步），可使用 `u-dma-buf` 驱动的 `quirk-mmap` 选项。

## 9. XDP 快速数据路径

XDP（eXpress Data Path）在数据包到达协议栈**之前**，在驱动 NAPI poll 循环内执行 eBPF 程序处理数据包。

```
传统路径:  NIC -> DMA -> Ring Buffer -> sk_buff 分配 -> 协议栈
XDP 路径:  NIC -> DMA -> Ring Buffer -> XDP 程序 -> [DROP/TX/REDIRECT/PASS]
                                          ^
                                     无 sk_buff 分配
```

### 9.1 XDP 模式

| 模式 | 执行位置 | 性能 | 要求 |
|------|----------|------|------|
| Native XDP | 驱动 NAPI poll 内 | 最高 | 驱动支持 |
| Generic XDP | `napi_gro_receive` 之后 | 较低 | 所有 NIC |

### 9.2 ARM 上的 Native XDP

多个 ARM SoC 的以太网驱动已支持 Native XDP：

- **Marvell mvneta**（Armada 38x/37x）：`mvneta_run_xdp()` 在 sk_buff 分配前执行
- **FreeScale/NXP DPAA2**：i.MX8 系列
- **TI CPSW**：AM335x/AM57x

```bash
# 加载 XDP 程序（示例：丢弃所有 UDP 端口 9999 的包）
ip link set dev eth0 xdp obj xdp_filter.o sec xdp

# 查看 XDP 状态
ip link show eth0
```

XDP 适合的 ARM 嵌入式场景：

- **DDoS 过滤**：在驱动层丢弃攻击流量，不消耗协议栈资源
- **数据包转发**：XDP_TX 直接从 NIC 重新发出，绕过整个协议栈
- **流量采样**：XDP_REDIRECT 到 AF_XDP socket 的零拷贝接收

**副作用**：需要内核编译支持 eBPF 和 XDP；Generic XDP 的性能优势有限。

## 10. 实时调度与内存锁定

对于延迟敏感的网络应用（工业控制、机器人、激光雷达），还需要从调度器层面保证确定性。

### 10.1 SCHED_FIFO + CPU 绑定

```bash
# 将网络处理进程设为 FIFO 实时调度，优先级 80
chrt -f 80 ./my_udp_server

# 绑定到 CPU 2
taskset -c 2 chrt -f 80 ./my_udp_server
```

在代码中：

```c
#include <sched.h>
#include <sys/mman.h>

/* 锁定所有内存，防止缺页中断 */
mlockall(MCL_CURRENT | MCL_FUTURE);

/* 设置 SCHED_FIFO 优先级 80 */
struct sched_param param;
param.sched_priority = 80;
sched_setscheduler(0, SCHED_FIFO, &param);

/* 绑定到 CPU 2 */
cpu_set_t cpuset;
CPU_ZERO(&cpuset);
CPU_SET(2, &cpuset);
sched_setaffinity(0, sizeof(cpuset), &cpuset);
```

### 10.2 PREEMPT_RT 内核

Linux 6.12 起，PREEMPT_RT 已合入主线内核，支持 ARM64 和 RISC-V。

PREEMPT_RT 的关键改进：

- 将 spinlock 替换为可抢占的 mutex
- 所有中断处理线程化（可被 SCHED_FIFO 任务抢占）
- 最坏调度延迟从标准内核的数毫秒降至 **100 us 以下**

```bash
# 检查内核是否支持 PREEMPT_RT
uname -a  # 应包含 PREEMPT_RT 或 PREEMPT RT
cat /sys/kernel/realtime  # 输出 1 表示 RT 内核
```

**ARM PREEMPT_RT 延迟实测**（cyclictest，SCHED_FIFO 优先级 80）：

| 内核 | 无负载 (max) | 重负载 (max) |
|------|-------------|-------------|
| 标准内核 | 50 us | 717 us |
| PREEMPT_RT | 32 us | 279 us |

### 10.3 RT 内核调优要点

```bash
# 禁用调试选项（严重影响延迟）
# 内核编译时确保关闭：
# CONFIG_DEBUG_LOCKDEP=n
# CONFIG_DEBUG_PREEMPT=n
# CONFIG_DEBUG_OBJECTS=n
# CONFIG_SLUB_DEBUG=n

# 隔离 CPU 核心（不运行普通任务）
# 内核启动参数：
# isolcpus=2,3 nohz_full=2,3 rcu_nocbs=2,3
```

## 11. 内存管理优化

### 11.1 禁用 Swap

Swap 导致的缺页中断会引入不可预测的延迟（毫秒级），在实时网络系统中不可接受。

```bash
# 立即禁用
swapoff -a

# 永久禁用：编辑 /etc/fstab 注释 swap 行

# 或者设置 swappiness=0（尽量不用但不完全禁止）
sysctl -w vm.swappiness=0
echo "vm.swappiness = 0" >> /etc/sysctl.conf
```

| 方案 | 行为 | 适用场景 |
|------|------|----------|
| `swapoff -a` | 完全禁用 swap | RAM 充足的嵌入式系统 |
| `swappiness=0` | 极力避免但不禁止 | RAM 紧张但需要兜底 |

### 11.2 内存锁定

使用 `mlockall()` 防止实时进程的内存被换出或触发缺页：

```c
/* 在 main() 起始处调用 */
if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0) {
    perror("mlockall failed");
}
```

## 12. 应用层优化

### 12.1 Socket 选项

```c
/* 增大接收缓冲区 */
int rcvbuf = 4 * 1024 * 1024;  /* 4 MB */
setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

/* 启用时间戳（减少系统调用获取时间的开销） */
int ts = 1;
setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPNS, &ts, sizeof(ts));

/* UDP: 使用 connect() 建立绑定（启用 RFS，减少路由查找） */
connect(fd, (struct sockaddr*)&dest, sizeof(dest));
```

### 12.2 批量收发

```c
/* recvmmsg: 一次系统调用接收多个包 */
struct mmsghdr msgs[BATCH_SIZE];
/* ... 初始化 msgs ... */
int count = recvmmsg(fd, msgs, BATCH_SIZE, MSG_WAITFORONE, NULL);

/* sendmmsg: 一次系统调用发送多个包 */
int sent = sendmmsg(fd, msgs, count, 0);
```

`recvmmsg`/`sendmmsg` 减少系统调用次数，在 ARM 上效果明显（ARM 的系统调用开销高于 x86）。

### 12.3 GRO 与 UDP

Linux 内核支持 UDP GRO（Generic Receive Offload for UDP），将多个相同流的 UDP 包合并为一个大包上送应用层：

```c
/* 启用 UDP GRO */
int val = 1;
setsockopt(fd, IPPROTO_UDP, UDP_GRO, &val, sizeof(val));
```

配合 GRO，应用层一次 `recvmsg` 可以收到合并后的大包，减少系统调用次数和协议栈处理开销。

## 13. 诊断方法论

优化前必须先定位瓶颈。以下工具和方法论按诊断顺序排列：

### 13.1 快速检查清单

```bash
# 1. 查看 NIC 丢包
ethtool -S eth0 | grep -iE "drop|error|discard|miss"

# 2. 查看 SoftIRQ 统计
cat /proc/net/softnet_stat
# 每行对应一个 CPU，格式: [processed] [dropped] [time_squeeze]

# 3. 查看 socket 缓冲区溢出
cat /proc/net/snmp | grep Udp
# UdpInErrors, RcvbufErrors, SndbufErrors

# 4. 查看中断分布
cat /proc/interrupts | grep eth

# 5. 查看 CPU 使用率（关注 softirq 比例）
mpstat -P ALL 1

# 6. 查看网络流量
sar -n DEV 1
```

### 13.2 瓶颈定位矩阵

| 现象 | 可能原因 | 对应优化 |
|------|----------|----------|
| `ethtool -S` 显示 rx_dropped | Ring Buffer 满 | 增大 Ring Buffer / 增大 NAPI budget |
| `softnet_stat` 第 2 列非零 | Backlog 满 | 增大 `netdev_max_backlog` |
| `softnet_stat` 第 3 列非零 | SoftIRQ budget 耗尽 | 增大 `netdev_budget` |
| `/proc/net/snmp` RcvbufErrors | Socket 缓冲区满 | 增大 `rmem_max` / 应用层加速处理 |
| 中断集中在单个 CPU | 无 RPS / RSS 配置 | 启用 RPS 或 RSS |
| `mpstat` 显示 `%soft` 高 | 协议栈处理瓶颈 | GRO / Busy Poll / XDP |
| CPU 频率低于最高值 | 调速器未升频 | 设置 `performance` 调速器 |

## 14. 完整优化配置模板

以下是一个面向 ARM-Linux 嵌入式高吞吐量 UDP 场景的系统配置模板：

```bash
#!/bin/bash
# ARM-Linux Network Performance Tuning Script
# Target: High-throughput UDP on embedded systems

IFACE="eth0"

# --- CPU Frequency ---
echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# --- IRQ Affinity ---
systemctl stop irqbalance 2>/dev/null
# 假设 eth0 中断号通过 grep 获取
IRQ=$(grep ${IFACE} /proc/interrupts | awk '{print $1}' | tr -d ':' | head -1)
echo 1 > /proc/irq/${IRQ}/smp_affinity_list

# --- RPS (单队列 NIC 分散到多核) ---
echo f > /sys/class/net/${IFACE}/queues/rx-0/rps_cpus
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries
echo 4096 > /sys/class/net/${IFACE}/queues/rx-0/rps_flow_cnt

# --- Ring Buffer ---
ethtool -G ${IFACE} rx 4096 tx 4096 2>/dev/null

# --- Interrupt Coalescing ---
ethtool -C ${IFACE} adaptive-rx on 2>/dev/null

# --- Offloads ---
ethtool -K ${IFACE} gro on 2>/dev/null
ethtool -K ${IFACE} rx-checksumming on tx-checksumming on 2>/dev/null

# --- Kernel Parameters ---
sysctl -w net.core.rmem_max=8388608
sysctl -w net.core.wmem_max=8388608
sysctl -w net.core.rmem_default=1048576
sysctl -w net.core.netdev_max_backlog=25000
sysctl -w net.core.netdev_budget=600
sysctl -w net.core.netdev_budget_usecs=4000
sysctl -w net.ipv4.tcp_window_scaling=1
sysctl -w vm.swappiness=0

# --- (可选) Busy Polling ---
# sysctl -w net.core.busy_read=50
# sysctl -w net.core.busy_poll=50

echo "Network tuning applied for ${IFACE}"
```

## 15. 总结

ARM-Linux 网络性能优化是一个多维度的工程问题，需要从硬件、驱动、内核、协议栈和应用层协同调优。

**高收益优化**（建议优先实施）：

1. CPU 调速器设为 `performance`（ARM 嵌入式设备几乎无副作用）
2. 中断亲和性 + 禁用 irqbalance（消除缓存失效）
3. RPS 多核分流（单队列 ARM NIC 的必需项）
4. Socket 缓冲区和 Backlog 调优（按设备 RAM 比例设置）
5. GRO 开启（纯软件实现，零成本）

**中等收益优化**（按需评估）：

6. Ring Buffer 增大 + 中断合并调优
7. NAPI budget 增大
8. 禁用 Swap / `swappiness=0`
9. `recvmmsg`/`sendmmsg` 批量收发

**高级优化**（延迟敏感场景）：

10. Busy Polling（3x 延迟改善，但增加 CPU 功耗）
11. SCHED_FIFO + mlockall + CPU 隔离
12. PREEMPT_RT 内核
13. XDP 快速数据路径

**核心原则**：永远先诊断再优化。使用 `ethtool -S`、`/proc/net/softnet_stat`、`mpstat` 定位实际瓶颈，然后针对性地应用上述优化。盲目套用参数可能适得其反。

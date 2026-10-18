---
title: "PTP 精度优化: 从毫秒到微秒，哪些杠杆真的有用"
date: 2026-10-19T00:10:00
draft: false
categories: ["performance"]
tags: ["PTP", "time-sync", "hardware-timestamp", "PHC", "DMA", "socket", "netem", "embedded", "performance"]
summary: "按“先决定上限、再降低抖动、最后保证交付”的顺序，拆解硬件时间戳、timeval 改 timespec、DMA 缓冲对齐、丢包模拟、socket ack 与长稳测试的真实收益和代价。"
ShowToc: true
TocOpen: true
---

# PTP 精度优化：从毫秒到微秒，哪些杠杆真的有用

> **概述：** PTP 精度不是靠一个参数调出来的。时间戳打在哪、网卡和 PHY 是否支持硬件时间戳、数据缓冲是否对齐、丢包后能否恢复、socket 长时间运行会不会堵住，都会改变最终误差。这篇按“先决定上限，再降低抖动，最后保证交付”的顺序，拆开这些杠杆的真实收益和代价。

PTP 的精度问题，最后都会落到同一条链路上：时间戳从哪来，经过哪些队列，什么时候被读到，异常时怎么降级。链路里任何一段不匹配，都会让 `offset` 变大，或者让 `offset` 看起来正常但实际时间不可信。

本文的测试平台是 Linux 主时钟加车载传感器从时钟，主时钟用 `ptp4l`，从时钟运行 PTP 进程，链路经过普通交换机。平台细节做了脱敏，精度数字和缓冲区大小来自实测。

## 1. 先定验收口径：精度是分布

“精度 13 μs”这句话信息量不够。至少还要知道 p50、p99、最大值、测试时长和网络负载。p99 指 99% 的样本落在这个误差以内。均值 13 μs、偶尔跳到 5 ms 的链路，比均值 20 μs、最大 30 μs 的链路更危险。

| 精度区间 | 典型来源 | 适用场景 |
| --- | --- | --- |
| 100 ms 至 10 ms | 操作系统调度、软件时间戳 | 日志、低速状态上报 |
| 10 ms 至 1 ms | 软中断延迟、普通网卡 | 低频传感器、事件回溯 |
| 1 ms 至 100 μs | 软件时间戳 + 普通 PHY | 入门级多传感器对齐 |
| 100 μs 至 1 μs | 硬件时间戳 + 良好 PHY | 主流车载传感器 |
| 1 μs 以下 | 硬件时间戳 + 透明时钟 + 高精度 PHY | 高精度测量、部分激光雷达 |

这套平台记录到两组数字：

- 两端都用软件时钟、直连，均值约 13 μs；
- 主时钟用硬时钟、链路走二层直传、从时钟侧仍由系统时钟维持，均值约 1.5 μs。

这两组数字都依赖主时钟、PHY、拓扑和负载，不能直接写进产品规格。尤其是 1.5 μs 这组，不能把原因单独归给“二层直传”；硬件时间戳、主时钟质量和链路条件都在起作用。

测试条件至少记录下面这些项，否则数字无法复现：

| 项目 | 需要记录的内容 |
| --- | --- |
| 时间戳 | 主从两端分别用软件还是硬件时间戳 |
| 拓扑 | 直连还是交换机，交换机是否支持透明时钟 |
| 负载 | 网络吞吐、CPU 占用、是否有突发流量 |
| 统计 | p50、p99、最大值、失锁次数 |
| 时长 | 至少 24 小时，关键版本做 72 小时 |

## 2. 先决定上限：时间戳点和硬件

### 2.1 软件时间戳和硬件时间戳

软件时间戳在协议栈或应用层读取系统时间，误差包含中断响应、协议栈排队和线程调度。硬件时间戳在 MAC 或 PHY 侧记录报文进出时刻，绕开了这些软件路径。

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":24}}}%%
flowchart TB
    subgraph SW["软件时间戳"]
        A["协议栈"] --> B["调度"] --> C["误差"]
    end
    subgraph HW["硬件时间戳"]
        D["MAC/PHY"] --> E["时间戳"] --> F["误差"]
    end
    classDef soft fill:#FEE2E2,stroke:#DC2626,stroke-width:2px,color:#111827
    classDef hard fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#111827
    class A,B,C soft
    class D,E,F hard
```

先确认网卡能力：

```bash
ethtool -T eth0
```

输出里应能看到 `hardware-transmit`、`hardware-receive` 和 `hardware-raw-clock`。只有 `software-transmit` 或 `software-receive` 时，不要承诺微秒级绝对精度。

硬件时间戳也不是无条件准确。PHY 的收发延迟是否对称、线缆和变压器是否引入固定偏差、交换机是否补偿驻留时间，都会影响最终结果。硬件时间戳解决的是“时间戳在哪打”，不解决链路不对称。

### 2.2 计算间隔用单调时钟

需要计算时间间隔、超时或滤波窗口时，用 `CLOCK_MONOTONIC`，不要用 `CLOCK_REALTIME`。`CLOCK_REALTIME` 会被 PTP step、NTP 或管理员调整，用它算间隔会把时间跳变带进计算。只有需要 UTC 语义时，才读 `CLOCK_REALTIME`。

### 2.3 二层直传、UDP 和透明时钟

二层直传少一层 IP 协议栈，路径更短；UDP 单播适合跨网段，但经过的协议层更多。透明时钟会把报文在交换机内排队的时间写进修正字段，多级交换和多从时钟场景更有价值。

传输方式影响精度，但不能单独决定精度。选型要看现场网络、交换机能力和两端时间戳，不要只看实验室的某一个均值。

## 3. 再降低抖动：时间结构、DMA 和调度

### 3.1 timeval 到 timespec：去掉一次截断

旧代码用 `timeval` 保存时间，精度到微秒：

```c
struct timeval {
    time_t      tv_sec;
    suseconds_t tv_usec;
};
```

硬件时间戳本身是纳秒级，转成 `timeval` 后，微秒以下的位被截掉。换成 `timespec`，用 `clock_gettime(CLOCK_REALTIME, &ts)` 读取，可以保留纳秒：

```c
struct timespec {
    time_t tv_sec;
    long   tv_nsec;
};
```

这一步的价值是“不把已经拿到的纳秒时间戳在计算阶段降级”。它不改变时间戳来源，也不保证端到端达到纳秒级。端到端精度仍然受 PHY、链路、晶振和伺服影响。

现代 Linux 上 `clock_gettime` 通常走 vDSO，在用户态直接读取，开销很小，但不是绝对零成本。旧代码如果还在用 `gettimeofday`，改成 `timespec` 是低风险、可验证的一步。

### 3.2 DMA 缓冲和读取粒度必须对齐

这套平台出现过一次很难解释的抖动：PTP 状态在锁定和未锁定之间来回跳，`offset` 偶尔冲到毫秒级。PTP 配置没改，主时钟也没动。

根因在数据通路：

- 点云服务把读取粒度从 128 KB 改成 64 KB，想降低单次处理延迟；
- 传感器侧的 DMA 缓冲区仍是 128 KB；
- 在这套平台里，缓冲区的约定是“一次读取消费一个完整帧”，应用只读 64 KB 时，剩下半帧无法被正常覆盖；
- 消费者只能重试或等待下一次完整帧，CPU 被拖在数据面；
- PTP 报文处理被挤占，同步状态开始跳变。

把 DMA 缓冲区也改成 64 KB，读取和缓冲重新对齐，重试消失，PTP 状态恢复稳定，`offset` 均值回到 13 μs。

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":24}}}%%
flowchart TB
    subgraph BAD["错位"]
        A["写"] --> B["缓冲"] --> C["读"] --> D["剩"] --> E["重试"] --> F["饥饿"]
    end
    subgraph GOOD["对齐"]
        G["写"] --> H["缓冲"] --> I["读"] --> J["稳定"]
    end
    classDef bad fill:#FEE2E2,stroke:#DC2626,stroke-width:2px,color:#111827
    classDef good fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#111827
    class A,B,C,D,E,F bad
    class G,H,I,J good
```

这个问题的难点不在 PTP 协议，而在系统耦合。改数据通路的任何一段，都要同步检查 DMA 缓冲区、应用读取粒度、共享内存布局、寄存器宽度和队列深度。只改一处，其他层仍按旧尺寸工作，就会出现“优化了 A，结果 B 变差”。

### 3.3 进程优先级：通常不是第一步

一个常见假设是：把 PTP 进程优先级提到最高，报文处理更快，精度就会更好。这套平台的实测是否定的：

| 调度方式 | 时间戳 | 主时钟 | offset 均值 |
| --- | --- | --- | --- |
| 默认调度 | 软件时钟 | 硬时钟主时钟 | 13 μs |
| 实时调度（SCHED_FIFO） | 软件时钟 | 硬时钟主时钟 | 14 μs |

差异在噪声范围内。已经使用硬件时间戳时，报文在网卡侧就打上了时间，进程调度只影响“什么时候读到这个时间戳”，不影响时间戳本身。PHY 和链路带来的抖动，往往比调度延迟更大。

优先级在两类场景下仍然有用：

- 使用软件时间戳，时间戳由进程读取，调度延迟直接进入误差；
- 多核竞争激烈，PTP 线程会被数据面或日志线程挤占。

这里不要把 `ps` 里的 `PRI` 列和 `nice` 混为一谈。`nice` 的范围是 `-20..19`；实时线程应写成调度策略加实时优先级，例如 `SCHED_FIFO` 和 `1..99` 的优先级。表格里的“实时调度”指后者。

## 4. 丢包环境下的鲁棒性

一个在理想环境 1 μs、丢包就崩溃的系统，不如一个在 20% 丢包下仍能保持 100 μs 的系统。精度优化要和鲁棒性一起做。

可以用 `tc netem` 在网卡上模拟丢包：

```bash
tc qdisc add dev eth0 root netem loss 20%
tc qdisc del dev eth0 root
```

如果测试机是网桥结构，也可以在 `FORWARD` 链上丢包，但要先确认桥接流量真的经过 `iptables`：

```bash
modprobe br_netfilter
sysctl -w net.bridge.bridge-nf-call-iptables=1

iptables -I FORWARD -s 10.42.0.101 \
    -m statistic --mode random --probability 0.2 -j DROP
```

无论用哪种方式，都要在测试前确认计数器真的在增长：

```bash
tc -s qdisc show dev eth0
iptables -L FORWARD -v
```

至少测 1%、5%、20% 三档丢包，记录 PTP 从失锁到恢复的时间、`offset` 的 p99 和最大值、业务时间戳的异常比例，以及 LOST 告警是否在合理时间内触发。

## 5. 长稳：socket ack 和 31 小时累积

这套平台遇到过一个长稳问题：运行 30 多小时后，PTP 报 LOST（丢失），重启 `ptp4l` 才能恢复。

排查发现，`ptp4l` 每秒向本地应用发 8 次请求，应用每次回 6 字节的 `done\n\n`。旧代码只发送、不接收应答，ack 在 TCP 接收队列里持续累积：

```text
8 次/秒 × 6 字节 = 48 字节/秒
5.23 MB ÷ 48 字节/秒 ≈ 31 小时
```

TCP 接收队列（Recv-Q）满了以后，应用回复失败，`ptp4l` 不再发新请求，授时中断。这里是 `ptp4l` 和本地应用之间的 TCP 控制 socket，不是 PTP 协议报文本身。

修复要显式接收 ack，并处理无数据、被信号打断和短读：

```c
ssize_t n = recv(sock, buf, sizeof(buf), MSG_DONTWAIT);

if (n < 0) {
    if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
        /* 没有完整 ack，下一次再读 */
    } else {
        reset_socket();
    }
} else if (n != 6 || memcmp(buf, "done\n\n", 6) != 0) {
    reset_socket();
}
```

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":24}}}%%
flowchart LR
    A["8次"] --> B["6B"] --> C["不读"] --> D["5MB"] --> E["31h"]
    classDef cause fill:#FEE2E2,stroke:#DC2626,stroke-width:2px,color:#111827
    classDef result fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#111827
    class A,B,C cause
    class D,E result
```

修复后把请求频率提高到 1024 次/秒，跑了 1700 万次，Recv-Q 始终为 0；同时把两个 CPU 核打到 95% 负载，PTP 仍然正常。

这类 bug 短测发现不了。1 小时、24 小时、72 小时三档长稳，是发现累积型问题的基本手段。

## 6. 组合拳：顺序、反模式和验收

### 6.1 按收益排序

| 顺序 | 杠杆 | 作用 | 成本 | 建议 |
| --- | --- | --- | --- | --- |
| 1 | 硬件时间戳 + 时间戳点 | 决定上限 | 高 | 硬件支持时必做 |
| 2 | DMA 缓冲和读取对齐 | 降低抖动 | 中 | 必做 |
| 3 | socket ack 和长稳 | 保证交付 | 低 | 必做 |
| 4 | timeval 改 timespec | 去掉计算截断 | 低 | 必做 |
| 5 | 丢包环境模拟 | 验证鲁棒性 | 中 | 必做 |
| 6 | 透明时钟、传输方式 | 改善拓扑 | 高 | 视现场 |
| 7 | 进程优先级、绑核 | 降低调度抖动 | 低 | 可选 |

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":24}}}%%
flowchart TB
    subgraph U["决定上限"]
        A["硬件"]
        B["链路"]
    end
    subgraph J["降低抖动"]
        C["纳秒"]
        D["缓冲"]
        E["调度"]
    end
    subgraph L["保证交付"]
        F["丢包"]
        G["ack"]
        H["长稳"]
    end
    U --> J --> L
    classDef upper fill:#DBEAFE,stroke:#2563EB,stroke-width:2px,color:#111827
    classDef jitter fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#111827
    classDef deliver fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#111827
    class A,B upper
    class C,D,E jitter
    class F,G,H deliver
```

### 6.2 四个反模式

- **只追最低均值**：从 13 μs 压到 11 μs，可能换来更大的尾部抖动。
- **只看均值不看分布**：偶尔跳到 5 ms，比均值稍差但分布稳定更危险。
- **只测理想环境**：不搭丢包、不搭高 CPU、不搭多从时钟，等于在真空里做设计。
- **优化时不看抓包**：报文丢了 5%、时间戳跳了 10 μs，看数字会误判。

### 6.3 上线检查表

1. 时间结构已经用 `timespec`，不是 `timeval`。
2. `ethtool -T` 确认硬件时间戳能力；没有硬件时间戳时，规格书不写微秒级。
3. DMA 缓冲、读取粒度、共享内存布局已经对齐。
4. PTP 线程在单核竞争或软件时间戳场景下有足够优先级。
5. 丢包 1%、5%、20% 下都测过恢复时间和 p99。
6. socket ack 有显式 `recv`，Recv-Q 不会持续增长。
7. 24 小时和 72 小时长稳通过。
8. 记录 p50、p99、最大值和失锁次数。
9. 每次优化前后都有 baseline，配置和版本可追溯。
10. 业务时间戳的质量字段能反映 `OK`、`DEGRADED`、`INVALID`。

PTP 精度优化没有单一开关。先把时间戳来源和数据通路修好，再谈调度和参数；先把鲁棒性和长稳做出来，再追更小的均值。顺序对了，微秒级才有意义。

### 参考资料

- [linuxptp 文档](https://linuxptp.nwtime.org/documentation/)
- [Linux kernel: Timestamping](https://docs.kernel.org/networking/timestamping.html)
- [Linux kernel: PTP hardware clock infrastructure](https://docs.kernel.org/driver-api/ptp.html)
- [IEEE 1588-2019 标准概述](https://standards.ieee.org/ieee/1588/6825/)

### 相关文章

- [PTP 网络模块设计：车载传感器的授时链路](https://deguiliu.github.io/tech-notes/posts/architecture/ptp_network_module_design/)
- [GNSS 与 PTP 时间同步原理：嵌入式软件工程师视角](https://deguiliu.github.io/tech-notes/posts/architecture/gnss_ptp_time_sync_principles/)
- [QNX 上的 PTPD 授时精度验证：从 8 ms 时间戳台阶到百微秒级](https://deguiliu.github.io/tech-notes/posts/performance/qnx_ptpd_time_sync_accuracy/)

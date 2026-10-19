---
title: "PTP 时间同步排查: 状态正常，业务时间为什么仍不可用"
date: 2026-10-19T03:30:00
draft: false
categories: ["performance"]
tags: ["PTP", "gPTP", "linuxptp", "time-sync", "troubleshooting", "embedded", "Recv-Q", "DMA"]
summary: "从设备显示同步正常、业务时间却仍有偏差的现象出发，先交代案例设备的授时链路，再拆解三个现场案例，最后讨论状态码设计和跨进程状态管理。"
ShowToc: true
TocOpen: true
---

# PTP 时间同步排查：状态正常，业务时间为什么仍不可用

> **概述：** 设备显示“PTP 已锁定”，采样时间戳却仍有偏差；抓包能看到 Sync 和 Follow_Up，业务进程却报告时间失联。这两种现象都说明：**同步状态** 和 **时间可用性** 不是同一个判断。本文先交代一台案例设备的授时链路，再用三个现场案例说明怎样分段排查，最后讨论状态码和跨进程状态管理应该怎么改。案例设备内部用 `7/8/14` 表示锁定、未锁定和失联；你的设备可能叫 `SYNC_OK`、`TIME_VALID`、`HOLDOVER`，名称不同，判断链路是一样的。

这套案例设备是一台嵌入式传感器，内部至少运行三个进程：PTP 守护进程、时间服务、业务进程。PTP 守护进程负责收报文和估计偏差；时间服务负责把时间发布给业务；业务进程用时间给采样数据打戳。三个进程之间的状态并不天然一致，现场问题也往往出在这里。

## 1. 现象：同步状态和时间可用性不是一回事

现场常见两类互相矛盾的现象：

| 现象 | 设备看到的状态 | 业务看到的结果 |
| --- | --- | --- |
| 状态正常，时间仍有偏差 | `7` 或 `TIME_VALID` | 采样时间戳偏几十微秒到几毫秒 |
| 报文正常，业务却报失联 | PTP 报文计数在增长 | 时间服务不再更新，业务停用时间戳 |

第一类问题说明：状态码只能回答“时间服务认为能不能用”，不能回答“业务拿到的时间准不准”。第二类问题说明：网络授时和设备内部的时间传递是两段链路，前一段正常，不代表后一段正常。

案例设备用三个标签表示应用层状态：

| 标签 | 通用含义 | 业务上怎么用 |
| --- | --- | --- |
| `7` | 锁定 | 时间戳可用，但仍要看偏差和连续性 |
| `8` | 未锁定 | 慎用，主从可能已经不同步 |
| `14` | 失联 | 不可用，采样时间戳不可信 |

这三个标签是案例设备的应用层约定，不是 IEEE 1588 的状态编号。读者可以把它们对应到自己设备的“同步正常 / 保持 / 失联”，不必先记住数字。

先问三个问题，后面的排查才有落点：

1. 设备说的“同步正常”是哪一层的判断，协议层、伺服层，还是业务接口？
2. 业务读的是系统时钟、PHC，还是时间服务发布的另一份快照？
3. 网络报文正常时，设备内部的时间通道是否也在正常更新？

## 2. 案例设备：时间是怎么从网络送到业务的

案例设备的原状可以简化成下面这条链路：

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":24,"nodeSpacing":45,"rankSpacing":55}}}%%
flowchart LR
    GM["主时钟<br/>GNSS 或上位机"] --> SW["交换机"]
    SW --> NIC["从设备网卡<br/>硬件时间戳 / PHC"]
    NIC --> PTP["PTP 守护进程"]
    PTP --> CLK["PHC 或系统时钟"]
    PTP -->|"状态与偏差"| TS["时间服务"]
    CLK -->|"当前时间"| TS
    TS -->|"时间快照"| APP["业务进程"]
    PTP <-->|"请求-应答通道"| APP

    classDef master fill:#EDE9FE,stroke:#6D28D9,stroke-width:2px,color:#4C1D95
    classDef net fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E
    classDef proto fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F
    classDef dist fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D
    classDef app fill:#FCE7F3,stroke:#DB2777,stroke-width:2px,color:#831843
    class GM master
    class SW,NIC net
    class PTP,CLK proto
    class TS dist
    class APP app
    linkStyle default stroke:#64748B,stroke-width:1.5px
```

图 1　案例设备的原状。网络授时和设备内时间传递是两段链路，PTP 守护进程与业务进程之间还有一条自定义请求-应答通道。

这张图是案例设备的原状，不是推荐架构；推荐的状态管理方式放在第 5 章。先看清原来的连接关系，后面的故障才有落点。

### 2.1 两段链路

第一段是网络授时：主时钟发 Sync、Follow_Up 等报文，网卡或 PHY 记录报文进出时刻，PTP 守护进程估计主从偏差，调整 PHC 或系统时钟。硬件时间戳、软件时间戳和 `phc2sys` 的部署方式不同，这一段的具体路径也不同。

案例设备使用硬件时间戳，PTP 守护进程维护本地时钟与时间源的映射；有的部署由 `phc2sys` 把 PHC 同步到系统时钟，有的直接在驱动或应用里换算。读者要先确认自己的设备属于哪一种，再决定看 `phc2sys`、`date` 还是业务时间接口。

第二段是设备内传递：PTP 守护进程把状态、偏差和时间交给时间服务，时间服务再发布给业务。业务不一定直接读系统时间；在这台案例设备里，业务读的是时间服务发布的快照，另外还有一条请求-应答通道用于状态交互。

把两段分开看，就能解释很多“报文正常但业务失联”的现象：`pmc` 能读到 offset，只证明第一段和守护进程还在工作；业务是否拿到时间，还要看第二段。

所以，“网络对时正常”只说明第一段正常。业务仍可能因为时间服务状态没更新、IPC 积压或业务进程退出而拿不到时间；排查时不要把 `pmc` 正常当成整条链路正常。

### 2.2 观测点

| 观测点 | 在哪执行 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| `date` | 设备系统 | 系统时间与参考时间的偏差量级 | PTP 协议是否锁定 |
| `pmc port_stats_np` | PTP 守护进程 | 报文是否到达守护进程 | 时间服务是否收到快照 |
| `pmc time_status_np` | PTP 守护进程 | 守护进程看到的 offset 和主时钟信息 | 业务时间是否可用 |
| 状态接口 | 时间服务 | 时间服务对外发布的状态 | 协议层内部的真实原因 |
| `tcpdump` | 网卡或交换机镜像口 | 链路上有没有 PTP 报文 | 报文有没有被守护进程处理 |
| `Recv-Q` / 重连计数 | 守护进程与业务进程之间 | 内部通道是否积压 | PTP 报文是否丢失 |

## 3. 三个现场案例

每个案例都按同一顺序展开：原来怎么连接，最先观察到什么，哪项检查缩小了范围，修改后怎么验证。

### 3.1 缓冲读写粒度不一致：状态在 7 和 8 之间跳

**原来怎么连接。** 处理端从采集端的 DMA 缓冲读取数据，PTP 守护进程和处理任务共享 CPU。处理端把单次读取粒度减半，但采集端的缓冲仍按原大小配置。

**最先观察到什么。** PTP 状态在 `7` 和 `8` 之间以百毫秒级周期跳变；守护进程日志里线性回归伺服频繁报样本不足；相邻 Sync 的间隔大约翻倍；CPU 占用明显波动。

**哪项检查缩小了范围。** PTP 参数没有改动，改回缓冲配置后状态恢复，说明问题不在协议参数，而在业务读取路径。处理端每次只读一半，剩余数据触发重试，重试占用 CPU，PTP 守护进程的报文处理被延迟，伺服拿不到足够样本，状态被压回 `8`。

**修改后怎么验证。** 把 DMA 缓冲和读取粒度同步调整后，状态稳定在 `7`，现场记录的平均偏差在十微秒量级。这个数值只对应这套测试条件，不能当成所有平台的通用精度。当时没有采集线程调度跟踪，所以“CPU 饥饿”是合理解释，不是已经证实的完整因果链；已确认的是缓冲配置不一致，以及修改后状态恢复。

这个案例的教训不是“所有缓冲大小必须相等”，而是：**读写双方对格式、边界和单次读取大小的约定要一起改。**

### 3.2 授上了，但固定偏 37 秒

**原来怎么连接。** 主时钟通过 PTP 给设备授时，设备业务读系统时间。现场同时能看到 PTP 状态 `7` 和 `pmc` 的小 offset。

**最先观察到什么。** 设备 `date` 显示的时间和主时钟的 UTC 时间固定差 37 秒，不是随机漂移。

**哪项检查缩小了范围。** `clockdiff` 显示固定整数秒偏差，基本可以排除网络抖动。继续查时基：系统时间按 UTC 理解，PTP 硬时钟可能按 TAI 计时；如果主时钟的 announce 报文携带 UTC 偏移 37，而从设备又按这个值做补偿，就可能把 37 秒重复加一次。

**修改后怎么验证。** 让两端使用同一套时基：确认 `ptpTimescale` 是 UTC 还是 TAI，检查 `ptp4l` 启动参数是否带 `-O 0`，再把 `utc_offset` 配成与主时钟一致的值。修改后 `clockdiff` 不再出现固定 37 秒，业务时间与主时钟一致。

这里要区分两个对象：设备时间指 `date` 或业务读到的系统时间，主时钟时间指主时钟的 UTC 时间。`pmc` 的 offset 小，只说明主从协议估计接近；如果时基定义不同，业务时间仍可能整秒偏差。

### 3.3 运行约 30 小时后失联：Recv-Q 慢慢涨满

**原来怎么连接。** 除了标准 PTP 报文，案例设备的 PTP 守护进程还维护一条本机请求-应答通道，用来和业务进程交换时间与状态。这条通道不是标准 PTP 报文。

**最先观察到什么。** 现场运行 30 多小时后报 PTP LOST，必须下电或重启守护进程才能恢复。`pmc get port_stats_np` 正常，说明 PTP 报文交互没有断；`netstat -ant` 显示守护进程与业务进程之间的 TCP socket 的 `Recv-Q` 持续增长，最大到数 MB。

**哪项检查缩小了范围。** PTP 报文正常，排除网络和主时钟；`Recv-Q` 增长，把问题指向应用层 IPC。守护进程只发请求、不读应答，应答在自己的接收队列里累积；队列满后，业务进程的 `send` 阻塞或失败，停止处理新请求；守护进程的发送队列随后也满，停止发送，时间通道整体停摆。

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px"}}}%%
sequenceDiagram
    participant P as PTP 守护进程
    participant A as 业务进程
    P->>A: 请求（每秒若干次）
    A->>P: 固定 6 字节应答
    Note over P: 从不 recv<br/>应答在 Recv-Q 累积
    Note over A: 发送缓冲写满<br/>send 阻塞或失败
    Note over P,A: 请求-应答停摆
    Note over P: 外部看到 PTP LOST
```

图 2　应答未读取导致的长时失联。PTP 报文正常，不代表守护进程与业务进程之间的时间通道正常。

**修改后怎么验证。** 在守护进程里显式读取业务进程的应答，并处理“一次读到半条或多条应答”的情况；缓冲区写满时丢弃无法识别的数据并重新同步。修复后把同步频率提高到约 1000 次/秒做加速测试，累计千万次，`Recv-Q` 始终为 0；再给 CPU 加上接近满负载的压力，时间传递仍然正常。加速测试不能代替长时间运行、温度变化和偶发断链测试。

## 4. 通用排查：先分段，再选工具

### 4.1 三段判断

| 段 | 先看什么 | 在哪执行 | 正常长什么样 | 异常指向 |
| --- | --- | --- | --- | --- |
| 网络与协议 | `pmc port_stats_np` | PTP 守护进程 | `rx_sync` 持续增长 | 链路、配置、交换机、白名单 |
| 伺服与时钟 | `pmc time_status_np` | PTP 守护进程 | `master_offset` 小且稳定 | 主时钟、伺服样本、CPU 负载 |
| 设备内传递 | 状态接口、`Recv-Q` | 时间服务 / IPC | 状态持续更新，队列不增长 | 时间服务、业务进程、IPC |

常用命令和判读如下。命令旁边的“在哪执行”比命令本身更重要：

| 命令 | 在哪执行 | 看什么 | 典型异常 |
| --- | --- | --- | --- |
| `date` | 设备系统 | 当前系统时间 | 明显不是当前时间，先别调 PTP 参数 |
| `pmc -u -b1 -f <ptp-config> 'get port_stats_np'` | PTP 守护进程 | `rx_sync` 等计数 | 计数不增长，先查链路和配置 |
| `pmc -u -b1 -f <ptp-config> 'get time_status_np'` | PTP 守护进程 | `master_offset`、主时钟信息 | offset 很大或多次采样不变 |
| `clockdiff <sensor-ip>` | 主时钟侧或设备侧 | 与对端的偏差形态 | 固定整数秒、毫秒级跳动、`down` |
| `ss -tanp` / `netstat -ant` | 守护进程所在设备 | `Recv-Q`、连接状态 | 队列持续增长，连接反复重建 |
| `tcpdump -i eth0 -nn 'ether proto 0x88f7'` | 网卡或镜像口 | Sync、Follow_Up、Delay_Req | 只有单向报文，或完全没有报文 |
| 状态接口 | 时间服务 | 应用层状态、原因、最后更新时间 | 状态长期不变，或与 `pmc` 矛盾 |

### 4.2 判断路径

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":20,"nodeSpacing":40,"rankSpacing":45}}}%%
flowchart TD
    A["业务报时间异常"] --> B{"业务时间与参考时间偏差大?"}
    B -->|"是"| C["pmc 看 offset 与报文计数"]
    B -->|"否"| D["查时间服务与 IPC"]
    C -->|"rx_sync 不增长"| E["链路、配置、交换机、白名单"]
    C -->|"报文增长但 offset 异常"| F["伺服、主时钟、CPU 负载"]
    D -->|"状态 14"| G["守护进程/时间服务存活、Recv-Q"]
    D -->|"状态 8"| H["伺服样本、主时钟切换"]
    D -->|"状态 7 但业务仍错"| I["时基、时间换算、业务读取"]
    E --> J["修正后复测"]
    F --> J
    G --> J
    H --> J
    I --> J

    classDef start fill:#EDE9FE,stroke:#6D28D9,stroke-width:2px,color:#4C1D95
    classDef check fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E
    classDef fix fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F
    classDef done fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D
    class A start
    class B,C,D check
    class E,F,G,H,I fix
    class J done
    linkStyle default stroke:#64748B,stroke-width:1.5px
```

图 3　从业务现象回到链路的判断路径。业务报告失联、但协议偏差正常时，直接走“时间服务与 IPC”分支。

### 4.3 八种症状先看哪一段

| 症状 | 先看哪段 | 下一步 |
| --- | --- | --- |
| 完全授不上时 | 网络与协议 | `pmc port_stats_np` 看 `rx_sync` 是否增长 |
| 授上但偏 37 秒 | 时基 | `clockdiff` 看是否为固定整数秒 |
| 授上但偏 8 小时 | 时基 | 查 UTC 与本地时区是否混用 |
| 授上但抖动到毫秒 | 伺服/负载 | 看 offset 分布、CPU 占用和报文间隔 |
| 长时间运行后偶发 LOST | 设备内传递 | 看 `Recv-Q`、重连计数和业务时间更新 |
| `rogue peer delay response` 后静默 | 网络 | 抓 `0x88F7`，查交换机是否支持 gPTP |
| `received ... without timestamp` | 网络/驱动 | 查网卡初始化和上下电时序 |
| 多从设备建立同步超过 1 分钟 | 网络 | 抓 pdelay 报文，查交换机转发 |

## 5. 状态设计改进：从分散判断到单一写者

三个案例暴露的不只是 PTP 参数问题，还有状态管理问题。案例原有实现有四个典型缺陷：

| 原实现的问题 | 现象 | 后果 |
| --- | --- | --- |
| 时间同步消息和状态通知消息都能写同一个状态 | 一个把状态拉回 `7`，另一个又压回 `8` | 状态抖动，日志无法解释 |
| 状态码只有一个数字 | 协议、伺服、链路、IPC 的状态被压在一起 | 看到 `8` 或 `14` 不知道先查哪层 |
| 状态没有有效期和代次 | 进程重启或通道积压后仍可能沿用旧值 | 业务拿着过期时间继续打戳 |
| 业务进程各自判断状态 | 守护进程、时间服务、业务各有一份判断 | 监控页面和业务实际使用的时间不一致 |

改动方向不是把所有状态塞进一个巨大的状态机，而是分层：

- 局部状态机只产生事实：协议是否锁定、伺服是否收敛、链路是否 up、进程心跳是否正常。
- 时间服务是设备级状态的唯一写者，负责聚合、去抖、超时和恢复。
- 业务进程只读时间服务发布的快照，不自己拼状态。

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"},"flowchart":{"htmlLabels":true,"padding":24,"nodeSpacing":45,"rankSpacing":55}}}%%
flowchart LR
    P["PTP 守护进程<br/>协议状态与偏差"] -->|"事实"| A["时间服务<br/>设备级状态机<br/>唯一写者"]
    S["clock servo<br/>收敛结果"] -->|"事实"| A
    L["链路与心跳<br/>Link up/down"] -->|"事实"| A
    A -->|"只读快照"| B["业务进程 A"]
    A -->|"只读快照"| C["业务进程 B"]
    A -->|"状态变化事件"| M["监控与诊断"]

    classDef fact fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E
    classDef owner fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D
    classDef reader fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F
    classDef mon fill:#FCE7F3,stroke:#DB2777,stroke-width:2px,color:#831843
    class P,S,L fact
    class A owner
    class B,C reader
    class M mon
    linkStyle default stroke:#64748B,stroke-width:1.5px
```

图 4　单一写者的设备级状态机。局部状态机只报事实，时间服务统一派生状态，业务只读快照。

建议规则要写清楚，不能和原实现混着解释：

| 状态 | 原实现 | 建议规则 |
| --- | --- | --- |
| `7` | 收到有效时间同步消息，映射建立 | 协议锁定、伺服收敛、连续 `N` 个好样本、快照未过期 |
| `8` | 协议报告未锁定，时间同步消息可能仍在更新 | 保持或收敛中，时间仍可观察，但业务不应无条件信任 |
| `14` | 超过 30 秒没有 PTP 数据 | 有效时间同步消息超过 `valid_until` |

建议规则里，`8` 同时承担保持（holdover）和收敛中的角色。没有保持能力的平台可以把保持时间设为 0，链路断开就直接 `14`；有保持能力时，先让业务知道“时间还在，但不可信”，超过 `valid_until` 再停用。

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontFamily":"sans-serif","fontSize":"18px","lineColor":"#455A64"}}}%%
stateDiagram-v2
    [*] --> INIT: 上电或冷启动
    INIT --> UNLOCKED: 收到状态通知但尚未收敛
    INIT --> LOCKED: 连续 N 个好样本且协议锁定
    LOCKED --> UNLOCKED: 伺服未收敛或主时钟无效
    UNLOCKED --> LOCKED: 连续 N 个好样本且协议锁定
    LOCKED --> LOST: 有效时间同步消息超过 valid_until
    UNLOCKED --> LOST: 有效时间同步消息超过 valid_until
    LOST --> UNLOCKED: 恢复收到有效时间同步消息但未收敛
    LOST --> LOCKED: 恢复并连续 N 个好样本
    LOCKED --> LOCKED: good 样本持续更新

    classDef good fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D
    classDef warn fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F
    classDef bad fill:#FEE2E2,stroke:#DC2626,stroke-width:2px,color:#7F1D1D
    classDef init fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E
    class INIT init
    class LOCKED good
    class UNLOCKED warn
    class LOST bad
```

图 5　建议规则下的应用层状态机。`7` 需要连续 `N` 个好样本，`14` 只由有效时间同步消息超过 `valid_until` 触发。

图中的 `N` 不是 1。单条消息只能说明链路还在，不能说明伺服已经收敛；`N` 由同步周期和伺服算法决定。启动阶段如果没有有效时间同步消息，也要按 `valid_until` 超时进入 `14`；状态通知消息只更新原因，不能把设备从 `14` 拉回来。

这套设计不需要应用层共识。PTP 的时间源由协议选出，设备级状态只需要一个本地聚合者，不需要 Raft、额外选主或跨进程投票。

## 6. 边界与结论

`7` 不是精度证书，`8` 也不是“完全不可用”。前者只表示时间服务认为快照可用，后者表示协议层还没确认锁定但时间同步消息可能仍在更新。两者都不能替代偏差分布和业务时间验收，也不能用同一条告警规则处理 `8` 和 `14`。

30 秒是应用层阈值，不是 PTP 标准。阈值可以按产品需求调整，但要留出数个同步周期和重传、恢复的时间。不同实现、不同 profile 的状态路径也不同，监控规则要基于实际配置、通知路径和超时参数，不能拿一个现场的经验直接套到另一个现场。

性能优化要全链路对齐。DMA 缓冲、读取粒度、共享内存布局、寄存器宽度，任何一处改动都可能改变 PTP 的报文处理节奏。约 30 小时才复发的 `Recv-Q` 问题，靠 1 小时测试发现不了；改完一项，至少复测状态稳定性和时间传递，并安排覆盖已知故障周期的长稳测试。

## 附录 A：工业设备常见的时间同步位置

| 场景 | 常见同步方式 | 传感器侧做什么 | 状态怎么暴露 |
| --- | --- | --- | --- |
| 工业以太网端站 | IEEE 802.1AS / gPTP | PHY 或 MAC 附近打时间戳，端站参与同步 | 同步状态、主时钟标识、offset |
| PROFINET IRT 设备 | PROFINET 专用时间同步 | 按周期同步，诊断上报 | 同步丢失、设备诊断 |
| EtherCAT 从站 | Distributed Clocks | 本地时钟与参考时钟对齐 | 工作时钟与同步状态 |
| IO-Link 传感器 | 通常由 master 打时间戳 | 传感器本身不跑 PTP | master 或 PLC 记录时间 |
| 普通模拟量/数字量传感器 | 采集模块打时间戳 | 传感器不参与网络对时 | 采集模块状态 |

共同点不是状态码相同，而是三层分离：时间戳尽量靠近硬件生成，协议栈负责估计偏差，应用只读一个带有效期的状态。小体积传感器通常把时间戳放在采集端或主站，而不是在传感器里跑完整 PTP。

## 附录 B：快照字段与 IPC 读取要点

时间服务发布的快照至少要有这些字段：

| 字段 | 作用 |
| --- | --- |
| `seq` | 判断读取期间快照有没有被改写 |
| `state` | `7/8/14`，由协议状态、伺服状态和数据新鲜度共同决定 |
| `reason` | 原因码，例如无主时钟、伺服样本不足、链路断开、通道超时 |
| `since` | 进入当前状态的单调时刻 |
| `epoch` | 时间服务启动或时钟源切换的代次 |
| `last_time_msg_at` | 最后一条有效时间同步消息的单调时刻；状态通知消息不刷新它 |
| `valid_until` | `last_time_msg_at + timeout`，超过后进入 `14` |

IPC 读取有三条底线：

1. `recv` 前先检查缓冲区是否已满，避免把 `recv(..., 0, ...)` 的返回值 0 误判成对端关闭。
2. 一次读取可能拿到半条或多条应答，必须按完整帧解析；缓冲区写满且无法识别时，丢弃并重新同步。
3. `EAGAIN`/`EWOULDBLOCK`、`EINTR`、连接关闭和真正的错误要分开处理；生产代码建议使用环形缓冲区。

### 参考资料

- [LinuxPTP: ptp4l](https://linuxptp.nwtime.org/documentation/ptp4l/)，端口状态、伺服配置和时间戳选项。
- [LinuxPTP: pmc](https://linuxptp.nwtime.org/documentation/pmc/)，`time_status_np`、`port_stats_np` 等管理查询。
- [IEEE 1588-2019](https://standards.ieee.org/ieee/1588/6825/)，PTP 协议标准与状态定义。
- [IEEE 802.1AS](https://www.ieee802.org/1/pages/802.1as.html)，时间敏感网络中的 gPTP 时间同步。
- [IEC/IEEE 60802](https://1.ieee802.org/tsn/iec-ieee-60802/)，工业自动化 TSN 配置与时间同步选项。
- [Linux kernel: Timestamping](https://docs.kernel.org/networking/timestamping.html)，硬件时间戳与软件时间戳的生成和获取方式。

### 延伸阅读

- [PTP 网络模块设计：车载传感器的授时链路](https://deguiliu.github.io/tech-notes/posts/architecture/ptp_network_module_design/)
- [GNSS 与 PTP 时间同步原理](https://deguiliu.github.io/tech-notes/posts/architecture/gnss_ptp_time_sync_principles/)
- [QNX 上的 PTPD 授时精度验证](https://deguiliu.github.io/tech-notes/posts/performance/qnx_ptpd_time_sync_accuracy/)
- [PTP 授时精度优化：时间戳、链路不对称与系统干扰](https://deguiliu.github.io/tech-notes/posts/performance/ptp_precision_optimization/)

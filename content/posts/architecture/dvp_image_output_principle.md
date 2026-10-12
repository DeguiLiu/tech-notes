---
title: "DVP 图像输出通路: 像素时序、模块分工与实现边界"
date: 2026-08-25T08:00:00
draft: false
categories: ["architecture"]
tags: ["DVP", "VIDEO_O", "VOC", "图像输出"]
summary: "DVP 输出由并行像素数据、PCLK、HSYNC 和 VSYNC 共同组成，图像经 DDR 与 VIDEO_O 送入 VOC，实际缺口可能位于框架接入层。"
ShowToc: true
TocOpen: true
---

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/164045426)

结论先行：DVP 输出不是把一块图像内存直接送到引脚，而是由 `VIDEO_O` 顺序读取 DDR 中的像素，再由 VOC 生成 PCLK、HSYNC、VSYNC 和并行数据。硬件与底层驱动就绪并不等于链路可用，模块框架还必须把待输出图像接入 VOC。

## DVP 信号与输出时序

### 像素矩阵如何变成数据流

显示设备上的一幅图像，由**像素**构成矩阵。以 640×480 为例，即每行 640 个点、共 480 行。

芯片输出图像的实质，是按行、按列将每个像素的颜色值依序发送给接收方。这一依序发送的过程，即为**图像输出**。

### 数据线与三类同步信号

像素颜色值需配合同步信号，接收方才能判断当前数据对应第几行、第几列。数据线与同步信号共同构成 DVP（Digital Video Port，数字视频口）。

```mermaid
flowchart LR
    D["数据线<br/>并行传输像素颜色"]:::data
    P["像素时钟 PCLK<br/>每个周期传输一个像素"]:::clk
    H["行同步 HSYNC<br/>标记一行结束"]:::sync
    V["帧同步 VSYNC<br/>标记一帧结束"]:::sync
    P --> D
    P --> H
    P --> V
    classDef data fill:#d8f5e0,stroke:#2c9e5f
    classDef clk fill:#e9dffb,stroke:#7a5cc9
    classDef sync fill:#dbeafe,stroke:#2c6fd8
```

| 信号 | 说明 |
| --- | --- |
| PCLK（像素时钟） | 每个时钟周期，数据线更新一个像素值 |
| HSYNC（行同步） | 发送完一行像素后翻转，标记换行 |
| VSYNC（帧同步） | 发送完一帧图像后翻转，标记一帧结束 |
| 数据线（Data） | 并行传输一个像素的颜色，数据线越多，颜色精度越高 |

### 接口方向由发送方决定

DVP 是一套信号约定，本身不限定方向。发送像素与时钟的一方为「输出方」。

* 相机模组经 DVP 向芯片发送图像，相机为输出方，芯片为接收方。
* 芯片经 DVP 向外接显示设备发送图像，芯片为输出方。

本文讨论后者：芯片作为输出方，经 DVP 发送图像。

```mermaid
flowchart TB
    subgraph 输出["输出"]
    end
    subgraph 输入["输入"]
        S1["芯片"]:::soc
        L1["显示设备"]:::lcd
        C1["相机"]:::cam
        C2["芯片"]:::soc
    end
    S1 --> L1
    C1 --> C2
    classDef soc fill:#e9dffb,stroke:#7a5cc9
    classDef lcd fill:#dbeafe,stroke:#2c6fd8
    classDef cam fill:#d8f5e0,stroke:#2c9e5f
```

## 从图像来源到外接显示设备

DDR 内存中的图像需先被写入。图像来源主要有三类：相机输入、红外传感器成像、芯片内部生成的测试图案（用于调试）。

完整链路为：来源写入 DDR → VIDEO\_O 读出 → VOC 生成同步时序并发送 → 显示设备接收。

```mermaid
flowchart LR
    subgraph 来源["来源"]
        A1["相机输入"]:::in
        A2["红外传感器成像"]:::in
        B1["内部测试图案"]:::test
    end
    D["写入 DDR"]:::ddr
    V["VIDEO_O 读出"]:::vo
    C["VOC 生成时序<br/>并发送"]:::voc
    E["显示设备"]:::ext
    A1 --> D
    A2 --> D
    B1 --> D
    D --> V
    V --> C
    C --> E
    classDef in fill:#d8f5e0,stroke:#2c9e5f
    classDef test fill:#ffe6c7,stroke:#e07b1a
    classDef ddr fill:#fde2e2,stroke:#c94b4b
    classDef vo fill:#dbeafe,stroke:#2c6fd8
    classDef voc fill:#e9dffb,stroke:#7a5cc9
    classDef ext fill:#d8f5e0,stroke:#2c9e5f
```

链路各环节：

1. 来源：相机或红外传感器写入图像，或芯片生成测试图案。
2. 存储：图像写入 DDR 内存。
3. 读取：VIDEO\_O 按顺序从内存读出像素。
4. 输出：VOC 生成同步时序并按正确节奏发送。

其中，相机输入为「输入」方向，与 VOC 的输出方向相反。调试场景通常使用内部测试图案，可不接入相机即验证输出链路。

## 硬件模块分工与软件接入状态

### DDR、VIDEO_O 与 VOC 的职责

```mermaid
flowchart TB
    subgraph 存储["存储"]
        D["DDR 内存<br/>保存整帧图像"]:::ddr
    end
    subgraph 读取["读取"]
        V["VIDEO_O<br/>按顺序读出像素"]:::vo
    end
    subgraph 发送["发送"]
        C["VOC 输出控制器<br/>生成 PCLK / HSYNC / VSYNC<br/>输出像素到数据线"]:::voc
    end
    D --> V
    V --> C
    classDef ddr fill:#fde2e2,stroke:#c94b4b
    classDef vo fill:#dbeafe,stroke:#2c6fd8
    classDef voc fill:#e9dffb,stroke:#7a5cc9
```

### 当前缺口位于模块框架层

VOC 输出链路的硬件与底层驱动均已就绪，但模块框架层的接入代码尚未完成，该处代码被注释，返回「不支持」状态。

```mermaid
flowchart TB
    L1["硬件：VOC 模块"]:::done
    L2["底层驱动：可配置 VOC"]:::done
    L3["设备注册：已挂载输出设备"]:::done
    L4["模块框架层：图像接入 VOC<br/>尚未实现"]:::empty
    L1 --> L2
    L2 --> L3
    L3 --> L4
    classDef done fill:#d8f5e0,stroke:#2c9e5f
    classDef empty fill:#fde2e2,stroke:#c94b4b
```

即底层「如何配置 VOC 输出」的能力已存在，而「何时将某一帧图像交给 VOC 输出」的接入逻辑尚未接通，代码返回「未实现」。若要使用 DVP 输出图像，需补全的是该层接入代码，而非硬件。

## 完整链路与实现边界

整个 DVP 输出链路如下：

```mermaid
flowchart LR
    S1["图像来源<br/>相机 / 红外 / 测试图案"]:::src
    S2["写入 DDR"]:::ddr
    S3["VIDEO_O 读出"]:::vo
    S4["VOC 生成时序"]:::voc
    S5["数据线与时钟<br/>发送"]:::pin
    S6["显示设备"]:::ext
    S1 --> S2
    S2 --> S3
    S3 --> S4
    S4 --> S5
    S5 --> S6
    classDef src fill:#d8f5e0,stroke:#2c9e5f
    classDef ddr fill:#fde2e2,stroke:#c94b4b
    classDef vo fill:#dbeafe,stroke:#2c6fd8
    classDef voc fill:#e9dffb,stroke:#7a5cc9
    classDef pin fill:#ffe6c7,stroke:#e07b1a
    classDef ext fill:#d8f5e0,stroke:#2c9e5f
```

要点：

1. DVP 输出即芯片按行、按列发送像素，并配合同步信号对齐。
2. 图像先由来源写入 DDR，再由 VIDEO\_O 读出。
3. 负责输出的硬件模块为 VOC，底层驱动已就绪。
4. 模块框架层的图像接入代码尚未完成，需补全该部分方可实际输出。

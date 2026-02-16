---
title: "嵌入式配置序列化选型: struct/TLV/nanopb/capnproto 对比"
date: 2026-02-16
draft: false
categories: ["tools"]
tags: ["C", "embedded", "serialization", "struct", "TLV", "capnproto", "nanopb", "protobuf", "Flash", "persistence", "zero-copy", "NvM"]
summary: "嵌入式设备的配置数据需要在 Flash/NvM 与内存之间可靠存取。本文从最简的裸 struct memcpy 出发，逐级递进到自定义 TLV、nanopb (Protocol Buffers C 实现) 和 c-capnproto (零拷贝固定布局)，形成四档方案对比。重点分析各方案在版本兼容、读写性能、维护成本上的取舍，并结合 Flash 扇区擦除特性论证为何整体重写并非性能瓶颈。"
ShowToc: true
TocOpen: true
---

> 参考文章:
>
> - [嵌入式配置数据持久化方案对比 -- 自定义 TLV vs nanopb](https://blog.csdn.net/stallion5632/article/details/150866044)
>
> nanopb 官方: [github.com/nanopb/nanopb](https://github.com/nanopb/nanopb) |
> c-capnproto 仓库: [github.com/opensourcerouting/c-capnproto](https://github.com/opensourcerouting/c-capnproto)

## 1. 背景与决策

我们的设备启动时间敏感，配置加载后频繁随机读取各模块参数，最终选了 c-capnproto。这个决策并非拍脑袋，而是在裸 struct、自定义 TLV、nanopb 三种方案逐一评估后得出的结论。本文将完整呈现这个评估过程，让读者带着结论去理解对比。

嵌入式设备 (激光雷达、工业传感器、边缘网关) 的配置数据 (设备参数、校准值、用户设置) 需要持久化到 Flash/NvM，启动时加载回内存。这看似简单的需求，面临三个核心挑战:

| 挑战 | 说明 | 典型场景 |
|------|------|---------|
| **格式可演进** | 配置字段随固件升级增删，新旧固件需互相解析 | OTA 后旧配置不能丢失 |
| **数据完整性** | Flash 写入可能被断电中断，需防止配置损坏 | 工业现场意外断电 |
| **资源高效** | 在有限 ROM/RAM 下，序列化开销要小 | Cortex-M 系列 MCU |

四档方案形成清晰的递进关系:

```
裸 struct ──→ 自定义 TLV ──→ nanopb ──→ c-capnproto
最简基线      轻量自描述      声明式演进    零拷贝随机访问
零版本兼容    手动兼容        自动兼容      自动兼容
```

## 2. 裸 struct: 最简基线

大多数嵌入式团队的第一反应: 直接 `memcpy(struct)` + CRC + version 字段。这是最简方案，也是理解后续方案价值的参照物。

### 2.1 实现方式

```c
typedef struct __attribute__((packed)) {
    uint16_t version;           // 格式版本号
    char     device_id[32];     // 设备标识
    uint32_t scan_rate_hz;      // 采样率
    uint8_t  filter_mode;       // 滤波模式
    uint8_t  log_level;         // 日志等级
    float    noise_threshold;   // 噪声阈值
    uint32_t crc32;             // 校验 (必须放最后)
} device_config_t;

// 写入 Flash
bool config_save(const device_config_t *cfg) {
    device_config_t tmp = *cfg;
    tmp.crc32 = crc32_calc(&tmp, offsetof(device_config_t, crc32));
    return flash_write(CONFIG_ADDR, &tmp, sizeof(tmp));
}

// 从 Flash 加载
bool config_load(device_config_t *cfg) {
    flash_read(CONFIG_ADDR, cfg, sizeof(*cfg));
    return crc32_calc(cfg, offsetof(device_config_t, crc32)) == cfg->crc32;
}
```

### 2.2 优缺点分析

**优点:**

- **极简**: 代码量最少 (~20 行)，零依赖，零学习成本
- **性能最优**: 读写均为单次 memcpy，无任何解析开销
- **内存开销最小**: 结构体本身即为存储格式，无额外缓冲区

**缺点:**

- **零版本兼容**: struct 布局变化 (增删字段、调整顺序) 直接导致旧数据不可读。version 字段只能做"全有或全无"判断
- **编译器依赖**: padding、对齐规则由编译器和平台决定。`__attribute__((packed))` 可消除 padding，但在部分架构上引发非对齐访问惩罚
- **跨平台不可移植**: 不同编译器/架构生成不同布局，大小端差异需手动处理
- **无增量兼容**: 新固件无法解析旧格式中缺失的字段，旧固件无法跳过新格式中多出的字段

```c
// 版本升级的困境: 新增一个字段就要废弃全部旧数据
// V1
typedef struct __attribute__((packed)) {
    uint16_t version;       // = 1
    uint32_t scan_rate_hz;
    uint32_t crc32;
} config_v1_t;

// V2: 新增 laser_power，布局完全不同
typedef struct __attribute__((packed)) {
    uint16_t version;       // = 2
    uint32_t scan_rate_hz;
    uint8_t  laser_power;   // 新增
    uint32_t crc32;
} config_v2_t;

// 升级时必须: 检测 version → 按旧格式解析 → 手动迁移 → 写回新格式
// 每增加一个版本，迁移代码就多一条分支
```

裸 struct 适合字段固定、永不变更的场景 (如硬件寄存器映射)。一旦配置需要跨版本演进，就需要自描述格式 -- 这正是 TLV 的起点。

## 3. 自定义 TLV: 轻量与直接

### 3.1 数据格式

TLV (Type-Length-Value) 是最原始的自描述格式。每个数据块由类型标识、长度、实际数据三部分组成:

```
+--------+--------+------------------+
| Type   | Length | Value (payload)  |
| 2 bytes| 2 bytes| Length bytes      |
+--------+--------+------------------+
```

TLV 块可以嵌套，构建树状结构:

```
+------------------+------------------+
|      全局配置头 (CRC, Ver, Len)     |
+------------------+------------------+
| Type(Mod_A) | Len(24) | Payload_A   |
+------------------+------------------+
| Type(Mod_B) | Len(128)| Payload_B   |
+------------------+------------------+
                   |
                   +-----------------------------------+
                   | Type(Sub_B1) | Len(4) | Payload   |
                   +-----------------------------------+
                   | Type(Sub_B2) | Len(16)| Payload   |
                   +-----------------------------------+
                   | Type(Sub_X)  | Len(96)| Payload   | <-- 旧固件不认识，跳过
                   +-----------------------------------+
```

### 3.2 实现核心

TLV 头结构和解析器:

```c
typedef struct {
    uint16_t type;
    uint16_t length;
} tlv_header_t;

// TLV 解析器: 遍历 buffer，按 type 分发
bool tlv_parse(const uint8_t *buf, size_t total_len,
               tlv_handler_t *handlers, size_t handler_count) {
    size_t offset = 0;
    while (offset + sizeof(tlv_header_t) <= total_len) {
        const tlv_header_t *hdr = (const tlv_header_t *)(buf + offset);

        // 边界检查
        if (offset + sizeof(tlv_header_t) + hdr->length > total_len) {
            return false;  // 数据截断
        }

        const uint8_t *value = buf + offset + sizeof(tlv_header_t);
        bool handled = false;

        for (size_t i = 0; i < handler_count; i++) {
            if (handlers[i].type == hdr->type) {
                handlers[i].parse(value, hdr->length, handlers[i].dst);
                handled = true;
                break;
            }
        }

        if (!handled) {
            // 未知类型: 跳过 (向前兼容的关键)
            LOG_W("Unknown TLV type: 0x%04X, skip %u bytes",
                  hdr->type, hdr->length);
        }

        offset += sizeof(tlv_header_t) + hdr->length;
    }
    return true;
}
```

序列化同样直接:

```c
size_t tlv_write(uint8_t *buf, size_t buf_size,
                 uint16_t type, const void *value, uint16_t length) {
    size_t total = sizeof(tlv_header_t) + length;
    if (total > buf_size) { return 0; }

    tlv_header_t hdr = { .type = type, .length = length };
    memcpy(buf, &hdr, sizeof(hdr));
    memcpy(buf + sizeof(hdr), value, length);
    return total;
}
```

### 3.3 优缺点分析

**优点:**

- **零依赖**: 代码量极小 (~200 行)，不引入任何第三方库
- **CPU 开销低**: 反序列化基于指针偏移 + memcpy，无需复杂解析
- **向前兼容**: 跳过未知 type 字段即可，不会因新增字段导致旧固件崩溃
- **支持嵌套**: TLV 天然支持递归嵌套，可构建模块化配置

**缺点:**

- **维护成本高 (命令式开发)**: 每新增一个字段需手动修改**三处代码** -- 枚举定义、解析逻辑、序列化逻辑
- **无编译期类型检查**: memcpy 不会验证字段类型和长度匹配，错误只能在运行时发现
- **原地更新的局限性**: 字段长度变化时原地更新失效，会覆盖后续数据
- **缺少默认值机制**: 旧数据中不存在的新字段，需要手动填充默认值

```c
// 每次新增字段都需要修改三处:
// 1. 枚举定义
enum cfg_type { TYPE_GAIN_MODE, TYPE_SCENE_MODE, TYPE_LASER_POWER /* 新增 */ };
// 2. 解析逻辑
case TYPE_LASER_POWER:
    memcpy(&config.laser_power, ptr, sizeof(uint32_t));
    break;
// 3. 序列化逻辑
tlv_write(buf, buf_size, TYPE_LASER_POWER,
          &config.laser_power, sizeof(uint32_t));
```

## 4. nanopb: 声明式演进与紧凑编码

### 4.1 核心理念

[nanopb](https://github.com/nanopb/nanopb) 是 Protocol Buffers 的 C 语言实现，专为嵌入式系统设计。核心理念: **用 `.proto` 文件声明配置结构，工具自动生成编解码代码**。开发者只需维护 schema，不需要手写解析逻辑。

```bash
# 声明式开发流程
# 1. 编写 .proto 文件 (schema)
# 2. 工具自动生成编解码代码
protoc --nanopb_out=. config.proto
# 产出: config.pb.c, config.pb.h
# 3. 嵌入式代码调用 pb_encode / pb_decode
```

### 4.2 Schema 定义

```protobuf
syntax = "proto2";  // nanopb 推荐使用 proto2 (支持 required/optional/default)
import "nanopb.proto";

// 全局选项: 限制最大消息大小，防止内存溢出
option (nanopb_fileopt).max_size = 512;

message LidarConfig {
    // required: 必须存在，缺失则解码失败
    required string device_id = 1 [(nanopb).max_size = 32];
    required uint32 scan_rate_hz = 2;

    // optional: 可选，可缺省，旧固件不认识的新字段会被自动忽略
    optional bool enable_filtering = 3 [default = true];
    optional uint32 log_level = 4 [default = 2];

    // 嵌套消息: 结构化管理子模块配置
    message AlgorithmParams {
        required float noise_threshold = 1;
        optional bool enable_outlier_removal = 2 [default = true];
    }
    optional AlgorithmParams alg_params = 5;
}
```

nanopb 的关键 `.proto` 选项:

| 选项 | 作用 | 示例 |
|------|------|------|
| `(nanopb).max_size` | 限制 string/bytes 最大长度 | `[(nanopb).max_size = 32]` |
| `(nanopb).max_count` | 限制 repeated 字段最大数量 | `[(nanopb).max_count = 10]` |
| `(nanopb_fileopt).max_size` | 限制整个消息最大编码大小 | `option (nanopb_fileopt).max_size = 512;` |
| `(nanopb).type` | 指定字段类型 (FT_STATIC/FT_CALLBACK) | `[(nanopb).type = FT_STATIC]` |

### 4.3 编解码使用

nanopb 生成的代码提供静态结构体和流式编解码 API:

```c
#include "config.pb.h"

// === 编码 (序列化) ===
LidarConfig config = LidarConfig_init_default;  // 所有字段初始化为默认值
strcpy(config.device_id, "LIDAR-001");
config.scan_rate_hz = 200;
config.enable_filtering = true;
config.has_alg_params = true;  // 标记 optional 嵌套消息存在
config.alg_params.noise_threshold = 0.05f;

uint8_t buffer[512];
pb_ostream_t stream = pb_ostream_from_buffer(buffer, sizeof(buffer));
bool ok = pb_encode(&stream, LidarConfig_fields, &config);
size_t encoded_size = stream.bytes_written;
// encoded_size 通常远小于 sizeof(LidarConfig)，varint 编码紧凑

// === 解码 (反序列化) ===
LidarConfig loaded = LidarConfig_init_default;  // 先填充默认值
pb_istream_t istream = pb_istream_from_buffer(buffer, encoded_size);
ok = pb_decode(&istream, LidarConfig_fields, &loaded);

// 访问解码后的字段
printf("scan_rate = %u\n", loaded.scan_rate_hz);
printf("filtering = %d\n", loaded.enable_filtering);
```

**关键细节**: `LidarConfig_init_default` 宏会将所有 optional 字段初始化为 `.proto` 中定义的默认值。解码旧数据时，旧数据中不存在的新字段保持默认值 -- 这是向前兼容的核心机制。

### 4.4 静态分配 vs 回调分配

nanopb 提供两种内存策略:

```protobuf
// 静态分配 (默认): 字段直接嵌入结构体，编译期确定大小
required string device_id = 1 [(nanopb).max_size = 32];
// 生成: char device_id[32];

// 回调分配: 通过回调函数逐块处理，适合大数据或流式处理
optional bytes firmware_chunk = 10 [(nanopb).type = FT_CALLBACK];
// 生成: pb_callback_t firmware_chunk;
```

| 策略 | 内存模型 | 适用场景 |
|------|---------|---------|
| FT_STATIC (默认) | 编译期固定大小，零 malloc | 配置参数、小型消息 |
| FT_CALLBACK | 回调式逐块处理 | 大文件传输、流式数据 |

嵌入式配置持久化场景推荐全部使用 FT_STATIC，编译期即可确定内存占用。

### 4.5 varint 编码: 紧凑的秘密

Protocol Buffers 的核心编码是 varint (变长整数):

```
值        varint 编码      字节数
0         0x00             1
127       0x7F             1
128       0x80 0x01        2
300       0xAC 0x02        2
16383     0xFF 0x7F        2
16384     0x80 0x80 0x01   3
```

每个字段的编码格式: `[field_number << 3 | wire_type] [varint length/value] [data]`

**与 c-capnproto 固定布局的关键对比**:

```
// 同一个 struct { uint8_t a; uint32_t b; uint16_t c; }

c-capnproto 存储: 8 字节 (固定，64-bit 对齐)
+--------+--------+--------+--------+
| a(1B)  | pad(1B)| c(2B)  | b(4B)  |
+--------+--------+--------+--------+

nanopb 存储 (a=1, b=100, c=50): 6 字节 (变长)
+------+------+------+
|08 01 |10 64 |18 32 |
+------+------+------+
 a=1    b=100  c=50

nanopb 存储 (a=0, b=0, c=0): 0 字节 (全默认，不编码)
```

当大量字段保持默认值时 (嵌入式配置的常态)，nanopb 的编码体积可以远小于 c-capnproto。

### 4.6 优缺点分析

**优点:**

- **声明式演进**: 新增字段只需修改 `.proto` 一行，工具自动生成编解码代码
- **自动版本兼容**: 旧固件忽略未知字段，新固件为缺失字段填充默认值
- **varint 紧凑编码**: 小值和默认值占用极少空间，文件体积小
- **跨语言生态**: `.proto` 文件可生成 C/C++/Python/Go/Java 等多语言解析器
- **ROM 开销小**: nanopb 库本身约 4KB ROM，适合资源受限 MCU
- **基本安全检查**: 解码时检查字段长度，防止缓冲区溢出

**缺点:**

- **需要完整解码**: 读取任何字段前，必须将整个消息解码到 C 结构体中 (非零拷贝)
- **整体重写**: 更新配置需要完整编码后写回 (不支持原地修改)
- **工具链依赖**: 需要 PC 端 protoc 编译器 + nanopb 插件，增加构建复杂度
- **解码性能低于 TLV**: 需要解析 varint 和 wire type，比纯 memcpy 慢
- **proto2 vs proto3 选择**: nanopb 推荐 proto2 (支持 required/default)，与主流 proto3 存在差异

## 5. c-capnproto: 零拷贝与固定布局

### 5.1 核心理念

[c-capnproto](https://github.com/opensourcerouting/c-capnproto) 是 Cap'n Proto 的纯 C (C99) 实现。核心理念: **数据在内存中的布局即为最终存储格式 (wire format)**。读取时通过编译期确定的偏移量直接访问字段，跳过了"解析 -> 拷贝 -> 构建结构体"步骤。

```bash
# 声明式开发流程 (类似 nanopb)
capnp compile -oc config.capnp
# 产出: config.capnp.c, config.capnp.h (纯 C，无 C++ 依赖)
```

> **注意**: capnp 编译器 (capnpc-c) 本身是 C++ 程序，仅在 PC 端运行。生成的代码是纯 C，可直接在 MCU 上编译。

### 5.2 Schema 定义与生成代码

```capnp
@0xf4b7a151b72a445d;

struct DeviceConfig {
  deviceId @0 :Text;
  sampleRate @1 :UInt32 = 100;      # 采样率，默认 100
  filterMode @2 :UInt8 = 0;         # 滤波模式

  struct AlgorithmParams {
    noiseThreshold @0 :Float32 = 0.1;
    enableOutlierRemoval @1 :Bool = true;
  }
  algorithmParams @3 :AlgorithmParams;

  # 新增字段只需在此处声明，自动兼容
  logLevel @4 :UInt32 = 2;
}
```

编译器生成的 C 代码包含结构体定义和读写函数:

```c
// config.capnp.h (生成代码)
typedef struct { capn_ptr p; } DeviceConfig_ptr;

struct DeviceConfig {
    uint32_t sampleRate;
    uint8_t  filterMode;
    uint32_t logLevel;
    capn_text deviceId;
    AlgorithmParams_ptr algorithmParams;
};

// 生成的 API: 创建、读取、写入
DeviceConfig_ptr new_DeviceConfig(struct capn_segment *s);
void read_DeviceConfig(struct DeviceConfig *s, DeviceConfig_ptr p);
void write_DeviceConfig(const struct DeviceConfig *s, DeviceConfig_ptr p);
```

读写函数内部通过 `capn_read8/16/32/64` 直接从 buffer 偏移位置读取，结合 XOR 默认值解码 (见 5.5 节)。

### 5.3 字段 ID 机制与版本兼容

Cap'n Proto 使用**显式字段 ID** (`@0`, `@1`, ...) 替代隐式字段顺序:

**ID 管理原则:**

- **唯一性**: 同一 struct 内 ID 必须唯一
- **稳定性**: 一旦分配，ID 永不改变、永不重用
- **可跳跃**: 支持非连续 ID，便于后续插入新字段
- **类型不可变**: 不支持改变已有字段的类型 (如 UInt8 -> UInt16)

**向后兼容 (旧程序读新数据):**

```c
// 旧程序只知道 @0 和 @1 字段
// @4 的 logLevel 字段存在于数据中，但旧程序不访问它，不受影响
struct DeviceConfig cfg;
read_DeviceConfig(&cfg, root);
uint32_t rate = cfg.sampleRate;  // 正常读取
```

**向前兼容 (新程序读旧数据):**

```c
// 新程序读取旧数据时，@4 字段在数据中不存在
// read_DeviceConfig 内部访问时，XOR 零值返回 schema 中定义的默认值 2
struct DeviceConfig cfg;
read_DeviceConfig(&cfg, root);
uint32_t level = cfg.logLevel;  // 返回默认值 2
```

### 5.4 零拷贝读取: 固定布局与指针头

Cap'n Proto 的 struct 在内存中被分为 **data section** (存放基本类型) 和 **pointer section** (存放引用类型如 Text、List、子 struct)。所有字段在 data section 中的偏移量在编译期确定。

```
DeviceConfig 在 buffer 中的布局 (8 字节对齐):
+----------------------------------------------------------+
| Data Section (基本类型字段)                                |
| Byte 0-3: sampleRate (UInt32)                            |
| Byte 4:   filterMode (UInt8)                             |
| Byte 5-7: padding                                        |
| Byte 8-11: logLevel (UInt32)                             |
| Byte 12-15: padding                                      |
+----------------------------------------------------------+
| Pointer Section (引用类型字段)                             |
| Byte 16-23: deviceId 指针头 (8 字节)                      |
| Byte 24-31: algorithmParams 指针头 (8 字节)               |
+----------------------------------------------------------+
```

指针是 Cap'n Proto 实现动态数据和版本兼容的核心机制。每个指针固定 8 字节:

```c
// 指针头位域布局
union wire_pointer_t {
    uint64_t raw;
    struct {
        uint64_t type   : 2;   // 0=struct, 1=list, 2=far pointer
        uint64_t offset : 30;  // 相对偏移 (单位: 8 字节)
        uint64_t extra  : 32;  // struct: data/pointer size; list: count
    } __attribute__((packed));
};
```

**跳过未知字段的工作原理**: 当旧固件遇到新版本数据中多出的字段时，虽然不理解其业务含义，但可以解析指针头的 `type`、`offset` 和 `extra`，精确计算出该字段占用的总字节数，从而安全跳过。这使得旧固件能向前兼容新配置文件。

### 5.5 XOR 默认值编码

Cap'n Proto 的默认值机制: **字段值在存储时与默认值做 XOR**。

```c
// 假设 schema 中 sampleRate 的默认值为 100
// 存储时: stored = actual_value XOR 100
// 读取时: actual = stored XOR 100
//
// 当 actual_value == 100 时: stored = 100 XOR 100 = 0   (零存储)
// 当 actual_value == 200 时: stored = 200 XOR 100 = 172
// 当数据缺失 (全零) 时: actual = 0 XOR 100 = 100       (自动返回默认值)
```

设计优势:

- 零值存储 = 默认配置，无需特殊处理
- 向前兼容: 新程序读旧数据，缺失字段自动返回默认值
- 无额外空间开销: 不需要 presence bit 或 optional 标记

### 5.6 内存模型与读取模式

c-capnproto 使用 **arena 分配** 而非逐个 malloc:

```c
// 静态分配方式: 预分配 segment buffer
static uint8_t seg_buf[1024] __attribute__((aligned(8)));

struct capn ctx;
struct capn_segment seg;
memset(seg_buf, 0, sizeof(seg_buf));  // 必须零初始化 (XOR 编码要求)
capn_init_malloc(&ctx);
capn_append_segment(&ctx, &seg, seg_buf, sizeof(seg_buf));
```

根据 Flash 访问速度和系统资源，可选择两种读取模式:

```c
// 模式一: 零拷贝 (Flash 支持字节级随机访问时推荐)
void config_read_zero_copy(const uint8_t *flash_data, size_t size) {
    struct capn ctx;
    capn_init_mem(&ctx, flash_data, size, 0);  // 直接使用 Flash 数据

    DeviceConfig_ptr root;
    root.p = capn_getp(capn_root(&ctx), 0, 0);

    struct DeviceConfig cfg;
    read_DeviceConfig(&cfg, root);  // 从 buffer 直接读取
    printf("sampleRate = %u\n", cfg.sampleRate);
    capn_free(&ctx);
}

// 模式二: 全拷贝 (Flash 访问较慢时，先拷贝到 RAM)
void config_read_with_copy(const uint8_t *flash_data, size_t size) {
    uint8_t *ram_buf = malloc(size);
    memcpy(ram_buf, flash_data, size);

    struct capn ctx;
    capn_init_mem(&ctx, ram_buf, size, 0);
    // ... 后续同零拷贝模式 ...
    capn_free(&ctx);
    free(ram_buf);
}
```

### 5.7 更新机制

Cap'n Proto 是 **write-once, read-many** 设计。Builder 用于一次性构建消息，不支持原地修改:

```c
// 更新流程: 旧 Reader -> 新 Builder -> 写回 Flash
struct capn old_ctx, new_ctx;
// ... 从 Flash 加载旧数据到 old_ctx ...

capn_init_malloc(&new_ctx);
struct capn_segment *new_seg = capn_append_segment(&new_ctx, ...);
DeviceConfig_ptr new_root = new_DeviceConfig(new_seg);

// 复制旧值 + 修改目标字段
struct DeviceConfig old_cfg, new_cfg;
read_DeviceConfig(&old_cfg, old_root);
new_cfg = old_cfg;                   // 复制所有旧值
new_cfg.sampleRate = 200;            // 修改目标字段
write_DeviceConfig(&new_cfg, new_root);
```

### 5.8 优缺点分析

**优点:**

- **零拷贝读取**: 从 Flash/buffer 直接指针偏移访问字段，O(1) 随机访问
- **声明式演进**: 修改 .capnp 一行，工具自动处理兼容性
- **XOR 默认值**: 优雅的缺省机制，无额外空间开销
- **编译期类型检查**: 生成的访问函数有明确的类型签名
- **纯 C 实现**: 生成代码无 C++ 依赖，可直接在 MCU 上编译

**缺点:**

- **文件体积偏大**: 固定布局为所有字段预留空间 (64-bit 对齐)，即使未设置也占空间
- **整体重写**: write-once 设计，更新配置需要 Reader -> Builder -> 写回全量数据
- **内存短暂翻倍**: 更新时同时持有旧数据和新 Builder
- **不检查输入边界**: 生成的代码假定输入可信，需在外部添加 CRC 校验
- **零初始化要求**: 所有 segment buffer 必须零初始化，否则 XOR 编码会读出错误值

## 6. 四方案横向对比

### 6.1 核心特性对比

| 维度 | 裸 struct | 自定义 TLV | nanopb | c-capnproto |
|------|----------|-----------|--------|-------------|
| **代码量** | ~20 行 | ~200 行手写 | 库 (~4KB) + 生成代码 | 生成代码 + capn.c |
| **外部依赖** | 无 | 无 | PC 端 protoc + 插件 | PC 端 capnp 编译器 |
| **新增字段** | 改 struct + 迁移 | 手动修改 3 处 | 修改 .proto 1 行 | 修改 .capnp 1 行 |
| **类型安全** | 有 (C 编译器) | 无 (memcpy) | 编译期结构体检查 | 编译期类型检查 |
| **版本兼容** | **无** | 手动 (跳过未知 type) | 自动 (字段号 + 默认值) | 自动 (字段 ID + XOR) |
| **读取性能** | **memcpy 最快** | memcpy + switch | 流式解码 (需拷贝) | 零拷贝 O(1) 访问 |
| **随机访问** | O(1) 直接偏移 | 遍历查找 O(n) | 需完整解码后访问 | **O(1) 直接偏移** |
| **更新方式** | 整体重写 | 原地更新 | 整体重写 (encode) | 整体重写 (Builder) |
| **文件体积** | sizeof(struct) | 紧凑 | **紧凑 (varint)** | 偏大 (64-bit 对齐) |
| **字节序** | 平台相关 | 需手动处理 | varint 天然跨平台 | 小端 + flip 转换 |
| **输入安全** | 需 CRC 外部校验 | 需自行校验 | 有基本长度检查 | 不检查边界 |
| **跨语言** | 无 | 无 | **.proto 多语言生成** | .capnp 多语言 |

### 6.2 资源开销对比

| 资源 | 裸 struct | 自定义 TLV | nanopb | c-capnproto |
|------|----------|-----------|--------|-------------|
| ROM 占用 | 极小 | < 1 KB | ~4 KB (库) + 生成代码 | 生成代码 + capn.c (~2 KB) |
| RAM (读取) | sizeof(struct) | sizeof(config_t) | sizeof(config_t) | **零额外** (直接读 buffer) |
| RAM (更新) | sizeof(struct) | sizeof(config_t) | sizeof(config_t) + 编码 buffer | **2x** (旧 Reader + 新 Builder) |
| 文件大小 (10 字段，半数默认) | ~60 B | ~80 B | ~40 B | ~120 B |

### 6.3 读取路径对比

```
裸 struct:
  Flash -> memcpy -> 直接使用结构体
                     ↑ 最快，但零兼容

自定义 TLV:
  Flash -> 读取到 RAM -> 遍历 TLV 块 -> switch/case -> memcpy 到结构体
                                                        ↑ 需要遍历查找

nanopb:
  Flash -> 读取到 RAM -> pb_decode() -> 逐字段解码 varint -> 填充结构体
                                                              ↑ 完整解码

c-capnproto:
  Flash -> 读取到 RAM (或直接访问) -> 指针偏移 -> 读取字段
                                                  ↑ 零拷贝，O(1)
```

### 6.4 整体重写并非性能瓶颈

评估 TLV 原地更新与 nanopb/c-capnproto 整体重写的性能差异时，需要理解 Flash 的物理特性:

- **读取**: 按字节或字进行，速度极快
- **写入**: 只能将 1 变为 0，不能将 0 变为 1
- **擦除**: 将整个扇区 (通常 4KB) 恢复为全 1，是唯一能将 0 变回 1 的操作

这意味着**即使只修改一个字节，只要需要将 0 变为 1**，就必须: 读取整个扇区到 RAM -> 修改目标字节 -> 擦除整个扇区 -> 将整个扇区写回。

| 对比维度 | TLV 原地更新 | nanopb/c-capnproto 整体重写 |
|---------|-------------|---------------------------|
| 理论写入量 | 极少 (单个字段) | 整个文件 |
| 实际写入量 | **至少一个扇区 (4KB)** | 整个文件 (通常 < 4KB) |
| 单点更新 | 读-擦-写一个扇区 | 读-擦-写整个文件 |
| 多点更新 | 读-擦-写**多个**扇区 | 读-擦-写整个文件 **(一次)** |
| 原子性 | 难以实现，易产生中间态 | 可结合双区存储保证原子性 |

**关键结论**: 当配置文件小于一个扇区 (4KB，绝大多数嵌入式配置的情况) 时，TLV 的原地更新和整体重写在 Flash 层面的实际开销相同 -- 都是读-擦-写一个扇区。当需要更新分散在多个扇区的字段时，整体重写反而更高效。TLV 的"原地更新"优势仅存在于理论上。

这一事实支持 c-capnproto 的选型: 既然 Flash 层面开销相同，那么整体重写带来的简洁性、原子性和版本兼容性就是净收益。

### 6.5 存储鲁棒性

无论使用哪种序列化方案，配置写入 Flash 时都需要解决断电保护问题。通用做法:

- **CRC-32 校验**: 在序列化数据前添加配置头 (magic + version + flags + length + crc32)。加载时先验证 magic 和 CRC，任一不匹配则拒绝使用。c-capnproto 生成的代码不做边界检查，CRC 校验是防止损坏数据被错误解析的最后防线
- **双区存储 (Dual-bank)**: 新配置写入备用 Bank，CRC 校验通过后切换 active 标记。写入中断电时，另一个 Bank 数据完好。c-capnproto 和 nanopb 的整体重写模式天然适配双区存储

## 7. 版本演进实践

假设需要在配置中新增一个控制激光功率的 `laser_power` 字段。四种方案的修改量和兼容性行为:

### 7.1 裸 struct: 版本号 + 迁移代码

```c
// V1
typedef struct __attribute__((packed)) {
    uint16_t version;  // = 1
    uint32_t scan_rate_hz;
    uint32_t crc32;
} config_v1_t;

// V2: 新增 laser_power
typedef struct __attribute__((packed)) {
    uint16_t version;  // = 2
    uint32_t scan_rate_hz;
    uint8_t  laser_power;  // 新增
    uint32_t crc32;
} config_v2_t;

// 加载时需手动迁移
bool config_load(config_v2_t *cfg) {
    uint16_t ver;
    flash_read(CONFIG_ADDR, &ver, sizeof(ver));
    if (ver == 1) {
        config_v1_t old;
        flash_read(CONFIG_ADDR, &old, sizeof(old));
        cfg->version = 2;
        cfg->scan_rate_hz = old.scan_rate_hz;
        cfg->laser_power = 100;  // 手动填默认值
        config_save(cfg);        // 迁移后写回
    } else if (ver == 2) {
        flash_read(CONFIG_ADDR, cfg, sizeof(*cfg));
    }
    return true;
}
```

每增加一个版本，迁移代码就多一条 `if` 分支。版本积累后维护成本线性增长。

### 7.2 TLV: 手动修改三处

```c
// 1. 枚举定义 -- 新增
enum cfg_type { /* ... */ TYPE_LASER_POWER };

// 2. 结构体 -- 新增
typedef struct {
    /* ... */
    uint8_t laser_power;
} user_cfg_t;

// 3. 解析函数 -- 新增 case
case TYPE_LASER_POWER:
    memcpy(&config.laser_power, ptr, sizeof(uint8_t));
    break;

// 4. 序列化函数 -- 新增
tlv_write(buf, buf_size, TYPE_LASER_POWER,
          &config.laser_power, sizeof(uint8_t));
```

兼容性: 新固件可解析旧文件 (手动填默认值)。旧固件遇到新字段会跳过。

### 7.3 nanopb: 修改 .proto 一行

```protobuf
optional uint32 laser_power = 6 [default = 100];
```

重新运行 `protoc --nanopb_out=. config.proto`，编解码代码自动更新。

兼容性:

- **新固件读旧数据**: `laser_power` 在旧数据中不存在，`pb_decode` 自动填充默认值 100
- **旧固件读新数据**: 旧固件不认识字段号 6，自动忽略该字段

### 7.4 c-capnproto: 修改 .capnp 一行

```capnp
laserPower @5 :UInt32 = 100;
```

重新运行 `capnp compile -oc config.capnp`，访问器代码自动更新。

兼容性:

- **新固件读旧数据**: @5 字段在旧数据中不存在，XOR 零值返回默认值 100
- **旧固件读新数据**: 旧程序不访问 @5 字段，不受影响

### 7.5 演进对比

| 操作 | 裸 struct | TLV | nanopb | c-capnproto |
|------|----------|-----|--------|-------------|
| Schema 修改 | 改 struct 定义 | 无 schema | 1 行 | 1 行 |
| 代码修改 | struct + 迁移函数 | 3-4 处手动 | **0 (自动生成)** | **0 (自动生成)** |
| 编译期检查 | 有 (C 类型系统) | 无 | 有 (结构体类型) | 有 (访问器签名) |
| 默认值处理 | 手动逐版本迁移 | 手动 | 自动 (init_default) | 自动 (XOR) |
| 向后兼容 | 无 (旧数据不可读) | 手动 (跳过) | **自动** | **自动** |
| 向前兼容 | 无 | 手动 (跳过) | **自动** | **自动** |
| 人为出错概率 | 高 | 高 | 低 | 低 |

## 8. 总结与选型建议

| 决策点 | 推荐方案 |
|--------|----------|
| 配置字段固定、永不变更 | **裸 struct** -- 最简最快 |
| 配置简单 (< 10 字段)、偶尔变更 | **自定义 TLV** -- 零依赖 |
| 配置复杂、迭代频繁、多端共享 | **nanopb** -- varint 紧凑 + 跨语言 |
| 配置加载后频繁随机访问字段 | **c-capnproto** -- 零拷贝 O(1) |
| 断电保护 | CRC-32 + 双区存储 (四者通用) |
| 数据来源不可信 | nanopb (有基本校验) + 外部 CRC |
| 频繁写入场景 | LittleFS 等磨损均衡文件系统 |
| 长期可维护性 | **nanopb** 或 **c-capnproto** -- 声明式演进 |

四种方案各有定位:

- **裸 struct**: 零开销基线。适合字段固定、永不变更的场景。一旦需要版本兼容，代价急剧上升。
- **自定义 TLV**: 零依赖的轻量方案。适合字段少且稳定的简单配置。维护成本随字段数量线性增长。
- **nanopb**: 声明式演进 + varint 紧凑编码 + 跨语言生态。是综合推荐方案 -- 维护成本最低、protobuf 生态最成熟。
- **c-capnproto**: 零拷贝读取 + O(1) 随机访问。在"加载一次、频繁读取"的场景有独特性能优势。Flash 扇区擦除特性决定了整体重写并非性能瓶颈，这使得 c-capnproto 的 write-once 设计不构成实际劣势。

## 参考资料

1. [嵌入式配置数据持久化方案对比 -- 自定义 TLV vs nanopb](https://blog.csdn.net/stallion5632/article/details/150866044)
2. [nanopb 官方文档](https://jpa.kapsi.fi/nanopb/)
3. [nanopb GitHub](https://github.com/nanopb/nanopb)
4. [c-capnproto GitHub](https://github.com/opensourcerouting/c-capnproto)
5. [Cap'n Proto 官方文档](https://capnproto.org/)
6. [Protocol Buffers Language Guide](https://protobuf.dev/programming-guides/proto2/)

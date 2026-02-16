---
title: "ARMv8 CRC 性能实测: 硬件指令快 8 倍, NEON 反而更慢"
date: 2026-02-15
draft: false
categories: ["performance"]
tags: ["ARM", "ARMv8", "CRC32", "NEON", "SIMD", "performance", "embedded"]
summary: "对比两组实验: ARMv8 CRC32 硬件指令 (crc32cx) vs 软件查表法，以及 NEON SIMD vs 简单 C 循环的字节累加校验和。结果表明 CRC32 硬件指令比查表快 8 倍以上，而 NEON 手写的字节累加在 -O2 下反而比编译器自动优化的标量代码慢。"
ShowToc: true
TocOpen: true
---

> 本文比较了两组实验: (1) 使用 NEON (Advanced SIMD) 指令和简单 C 循环实现字节累加 CRC 校验和; (2) 使用 ARMv8 CRC32 扩展指令 (`crc32cx`) 和软件查表法实现 CRC32。结果表明，CRC32 硬件指令比软件查表快 8 倍以上，而 NEON 手写的字节累加 CRC 在 -O2 下反而比编译器自动优化的标量代码慢。

## 1. 实验方法

### 1.1 NEON (Advanced SIMD) 尝试加速字节累加 CRC 校验和 vs 简单 C 循环

```c
#include <arm_neon.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define DATA_SIZE 1024 * 1024  // 1MB

// BUG: vaddq_u8 是 8-bit 无符号加法，每个 lane 在超过 255 后会回绕 (wrap around)。
// 对于 1MB 数据，每个 lane 累加值远超 255，结果与 crc_Simple 的 int 累加不一致。
// 正确做法应使用 vaddw_u8 / vpaddlq_u8 将 8-bit 扩展到 16-bit 或 32-bit 再累加。
bool crc_NEON(uint8_t* data, uint32_t len) {
    int sum = 0;
    uint32_t i = 1;
    uint8x16_t v_sum = vdupq_n_u8(0);

    for (; i + 16 <= len; i += 16) {
        uint8x16_t v_data = vld1q_u8(data + i);
        v_sum = vaddq_u8(v_sum, v_data);
    }

    uint8_t temp[16];
    vst1q_u8(temp, v_sum);
    for (int j = 0; j < 16; j++) { sum += temp[j]; }
    for (; i < len; i++) { sum += data[i]; }

    // 注意: data[len] 访问看似越界，但此处的约定是 data 数组实际分配了 len+1 字节，
    // data[len] 存放的是校验字节。调用方需确保分配足够空间。
    return ((uint8_t)((0x100 - (sum & 0xff)) & 0xff) == data[len]);
}

bool crc_Simple(uint8_t* data, uint32_t len) {
    int sum = 0;
    for (uint32_t i = 1; i < len; i++) { sum += data[i]; }
    // 同上，data[len] 是校验字节，调用方需确保 data 分配了 len+1 字节。
    return ((uint8_t)((0x100 - (sum & 0xff)) & 0xff) == data[len]);
}
```

ARM64 开发板测试结果:

```
# -O0 (未优化)
NEON crc耗时: 1562.000000 微秒
简单crc耗时: 11247.000000 微秒

# -O2 (优化后)
NEON crc耗时: 815.000000 微秒
简单crc耗时: 5.000000 微秒
```

### 1.2 ARMv8 CRC32 硬件扩展指令 vs 软件查表法 CRC32

```c
// 注意: 此函数使用的是 ARMv8 CRC32 扩展指令 (crc32cx/crc32cw/crc32ch/crc32cb)，
// 这些是标量整数流水线指令，不是 NEON (Advanced SIMD) 指令。
// 需要 -march=armv8-a+crc 编译选项。
//
// 严格别名警告: 将 uint8_t* 强制转换为 uint64_t*/uint32_t*/uint16_t* 违反了
// C/C++ 严格别名规则 (strict aliasing)，在 -O2 下可能导致未定义行为。
// 生产代码应使用 memcpy 或 __attribute__((may_alias)) 类型来安全地读取数据。
uint32_t crc32_do_HW(const void *const in_buf, uint32_t crc,
                       const uint64_t in_buf_len) {
    int64_t bytes = in_buf_len;
    const uint8_t *data = (const uint8_t *)(in_buf);
    while (bytes >= sizeof(uint64_t)) {
        __asm__("crc32cx %w[c], %w[c], %x[v]"
                : [c] "+r"(crc)
                : [v] "r"(*((uint64_t *)data)));
        data += sizeof(uint64_t);
        bytes -= sizeof(uint64_t);
    }
    if (bytes & sizeof(uint32_t)) {
        __asm__("crc32cw %w[c], %w[c], %w[v]"
                : [c] "+r"(crc)
                : [v] "r"(*((uint32_t *)data)));
        data += sizeof(uint32_t);
        bytes -= sizeof(uint32_t);
    }
    if (bytes & sizeof(uint16_t)) {
        __asm__("crc32ch %w[c], %w[c], %w[v]"
                : [c] "+r"(crc)
                : [v] "r"(*((uint16_t *)data)));
        data += sizeof(uint16_t);
        bytes -= sizeof(uint16_t);
    }
    if (bytes & sizeof(uint8_t)) {
        __asm__("crc32cb %w[c], %w[c], %w[v]"
                : [c] "+r"(crc)
                : [v] "r"(*((uint8_t *)data)));
    }
    return crc;
}
```

ARM64 开发板测试结果:

```
# -O2 优化
HW CRC32耗时: 700.000000 微秒
simple CRC32耗时: 4796.000000 微秒

# -O0 未优化
HW CRC32耗时: 1265.000000 微秒
simple CRC32耗时: 13898.000000 微秒
```

## 2. 加速原理

本文涉及两种完全不同的指令集，需要区分:

**ARMv8 CRC32 扩展指令 (标量整数流水线)**

`crc32cx`/`crc32cw`/`crc32ch`/`crc32cb` 是 ARMv8-A 架构的 CRC32 扩展指令，属于标量整数流水线，与 NEON (Advanced SIMD) 无关。这些指令在硬件中直接实现 CRC32C (Castagnoli) 多项式运算:

- `crc32cx`: 一条指令处理 8 字节 (64-bit)
- `crc32cw`: 一条指令处理 4 字节 (32-bit)
- `crc32ch`: 一条指令处理 2 字节 (16-bit)
- `crc32cb`: 一条指令处理 1 字节 (8-bit)

相比之下，软件查表法 (如 Sarwate 算法) 每次迭代通常处理 1-4 字节，且每次需要查表 + 异或操作。硬件指令将整个 GF(2) 多项式除法在单周期内完成，因此获得 8 倍以上的加速。

**NEON (Advanced SIMD)**

实验 1.1 中的 `crc_NEON` 使用了 NEON 的 `vld1q_u8`/`vaddq_u8` 等 128-bit SIMD 指令来并行累加字节，这才是真正的 NEON 指令。但这只是简单的字节求和校验，不是 CRC32 算法。

## 3. 性能分析

**实验 1.2: ARMv8 CRC32 硬件指令 vs 软件查表法**

CRC32 硬件指令 (`crc32cx`) 每条指令在单周期内处理 8 字节数据，将 GF(2) 多项式除法完全卸载到硬件。软件查表法每次迭代处理 1-4 字节，且需要内存访问 (查表) + 异或运算。硬件指令的加速是本质性的——算法复杂度不变，但每步的执行开销从多条指令降到单条指令。这解释了 8 倍以上的性能差距。

**实验 1.1: NEON 手写 CRC 校验和 vs 编译器优化的标量代码**

-O0 下 NEON 版本快 7 倍，符合预期: NEON 一次加载 16 字节并行累加，而标量代码逐字节处理。

-O2 下标量代码反而快 160 倍 (815μs vs 5μs)，原因是:

1. `crc_Simple` 的字节累加循环模式极其规整，编译器在 -O2 下会自动向量化 (auto-vectorization)，生成与手写 NEON 等价甚至更优的 SIMD 代码
2. 编译器自动向量化还能结合循环展开、指令调度等优化，整体效果优于手写 intrinsics
3. 手写 NEON intrinsics 被编译器视为不透明操作，阻止了进一步的优化 (如循环展开、寄存器分配优化)
4. 此外 `crc_NEON` 存在 `vaddq_u8` 的 8-bit 溢出 bug (见代码注释)，虽然不影响性能，但会导致结果错误

## 4. 结论

- ARMv8 CRC32 硬件扩展指令 (`crc32cx`) 可以显著加速 CRC32 计算 (8 倍以上)，这些是标量整数指令，不是 NEON
- 对于简单的字节累加 CRC 校验和，编译器 -O2 自动向量化后的代码反而比手写 NEON intrinsics 更快
- 手写 SIMD 代码需要注意数据类型宽度 (如 `vaddq_u8` 的 8-bit 溢出) 和严格别名规则
- 编译器优化能力不可忽视，简单规整的循环应优先让编译器自动向量化，仅在编译器无法优化的复杂算法中才考虑手写 intrinsics

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/140112767)

---

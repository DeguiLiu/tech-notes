# NEON 指令集对 CRC32 加速明显，但在 CRC 计算中反而造成性能下降的分析

> 本文比较了使用 NEON 指令集和简单 C 循环实现 CRC 和 CRC32 校验的性能。结果表明，启用 NEON 指令集可以显著提高 CRC32 的性能，而对 CRC 的影响则相反。在 -O2 编译优化的情况下，NEON CRC32 的速度比简单 C 循环快 8 倍以上，而 NEON CRC 竟然出乎意料的比简单 C 循环慢 20%。

## 1. 实验方法

### 1.1 NEON 指令集尝试加速 CRC 和简单 CRC 对比

```c
#include <arm_neon.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define DATA_SIZE 1024 * 1024  // 1MB

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

    return ((uint8_t)((0x100 - (sum & 0xff)) & 0xff) == data[len]);
}

bool crc_Simple(uint8_t* data, uint32_t len) {
    int sum = 0;
    for (uint32_t i = 1; i < len; i++) { sum += data[i]; }
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

### 1.2 NEON 指令集加速 CRC32 和普通 CRC32 对比

```c
uint32_t crc32_do_NEON(const void *const in_buf, uint32_t crc,
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
NEON CRC32耗时: 700.000000 微秒
simple CRC32耗时: 4796.000000 微秒

# -O0 未优化
NEON CRC32耗时: 1265.000000 微秒
simple CRC32耗时: 13898.000000 微秒
```

## 2. NEON 加速原理

NEON 加速 CRC32 利用 SIMD（单指令多数据）技术，一次性处理多个数据元素。对于 CRC32，NEON 提供了 `vmulq_u8`、`vaddq_u8`、`vxorq_u8` 等指令，可以并行处理 8 个字节的数据，显著提高计算效率。

## 3. 性能分析

NEON CRC32 性能明显优于简单 C 循环，因为 NEON 指令集可以一次性处理 8 个字节的数据，相当于将每条指令的执行效率提高了 8 倍。

然而，对于简单 CRC 校验和，NEON 指令集在 -O2 优化下反而降低了性能。这是因为简单的字节累加循环在 -O2 下被编译器深度优化（自动向量化、循环展开），而手写 NEON 代码反而阻止了编译器的优化。

## 4. 结论

- NEON 指令集可以显著提高 CRC32 的性能（8倍以上加速）
- 对于简单的字节累加 CRC，编译器 -O2 优化后的标量代码反而更快
- 在实际应用中，应根据算法复杂度选择合适的实现方式
- 编译器优化能力不可忽视，手写 SIMD 不一定比编译器自动向量化更快

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/140112767)

---

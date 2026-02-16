---
title: "C++17 vs C 二进制体积: 嵌入式场景的实测与分析"
date: 2026-02-15
draft: false
categories: ["performance"]
tags: ["ARM", "C++17", "CRC", "embedded", "lock-free", "logging", "message-bus", "newosp"]
summary: "基于 GCC 13 / x86-64 实测数据，面向 ARM-Linux 工业嵌入式开发者"
ShowToc: true
TocOpen: true
---

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/158074852)

> 基于 GCC 13 / x86-64 实测数据，面向 ARM-Linux 工业嵌入式开发者

---

## 核心结论

"C 语言生成的二进制更小"在禁用 RTTI 和异常的工业嵌入式场景下不成立。
实测表明，等价功能的 C 和 C++17 代码在 `-fno-exceptions -fno-rtti -Os` 下，
.text 段差异在 1-4% 以内，C++ 在某些场景下反而更小。

---

## 一、原文观点逐条验证

### 观点 1："RTTI 和异常是 C++ 体积膨胀的罪魁祸首"

**正确。** 这两个特性是 C++ 二进制体积超出 C 的主要原因：

| 特性 | 体积开销来源 | 典型开销 |
|------|------------|---------|
| RTTI | 每个多态类生成 `typeinfo` 结构和名称字符串 | 每类 ~50-200 字节 |
| 异常 | 展开表 (`.eh_frame`)、着陆区 (`.gcc_except_table`) | .eh_frame 可占 .text 的 10-30% |

工业嵌入式项目通常开启 `-fno-exceptions -fno-rtti`，此时这些开销归零。
[newosp](https://github.com/DeguiLiu/newosp) 用 `expected<V,E>` 替代异常，用模板/CRTP 替代虚函数，从设计上规避了这两项开销。

### 观点 2："if constexpr 做物理剪枝，C++ 可能更小"

**正确，且有实测数据支撑。**

用 GCC `-O2 -fno-inline` 编译（禁止内联以观察函数体本身），对比处理函数的汇编：

**C++ `if constexpr` -- `process<int>()` 仅 6 条指令，无分支：**

```asm
process<int>(int):
    mov    %edi,%edx              ; 参数直接传递
    lea    0xe87(%rip),%rsi       ; "integer: %d\n"
    xor    %eax,%eax
    jmp    __printf_chk@plt       ; 尾调用
```

float 分支**完全不存在**：无 `test/jne` 跳转，无 `movsd` 浮点加载，
无 `"float: %f\n"` 字符串引用。

**C runtime `if` -- `process()` 有 14 条指令，包含条件分支：**

```asm
process:
    test   %edi,%edi              ; if (t == TYPE_INT)
    jne    .L_float               ; 条件跳转
    mov    (%rsi),%edx            ; integer 分支
    lea    ...,%rsi               ; "integer: %d\n"
    jmp    __printf_chk@plt
.L_float:
    movsd  (%rsi),%xmm0          ; float 分支 (死代码)
    lea    ...,%rsi               ; "float: %f\n"
    jmp    __printf_chk@plt
```

即使 `main()` 只调用 `process(TYPE_INT, &x)`，float 分支的代码和字符串常量
**仍然存在于二进制中**。

| 对比项 | `if constexpr` | C runtime `if` |
|--------|---------------|----------------|
| .text 段 | 1421 字节 | 1525 字节 (+7.3%) |
| 死分支消除 | 语言标准保证 | 依赖优化器，不保证 |
| .rodata 字符串 | 仅保留使用的 | 死分支的字符串也保留 |

关键区别：`if constexpr` 的分支消除发生在**模板实例化阶段**，早于任何优化 pass。
C 的运行时 `if` 依赖优化器的常量传播，在函数未被内联时（动态库导出、函数指针调用）
优化器**无法消除死分支**。

### 观点 3："内联让 C++ 更小"

**部分正确，需要区分场景。**

内联的体积效应是双向的：

| 场景 | 体积效应 | 原因 |
|------|---------|------|
| 极小函数 (getter/setter, < 5 条指令) | **减小** | 消除 call/ret 序列 (通常 5-10 字节) |
| 中等函数 (10-50 条指令) | **增大** | 函数体在每个调用点复制 |
| 大函数 | 编译器通常不内联 | `-Os` 下尤其保守 |

原文说"内联后的代码往往比函数调用更精简"过于绝对。准确的说法是：

- C 的 `void*` + 函数指针**阻止**编译器内联，编译器被迫生成间接调用
- C++ 模板让编译器**有机会**内联，但是否内联取决于优化级别和函数大小
- `-Os` 下编译器对内联非常保守，优先控制体积

### 观点 4："空基类优化 (EBCO) 让 C++ 不浪费空间"

**结论正确，但 C 的对比描述有误。**

原文说"C 语言中一个空的 struct 至少占 1 字节"，实测 GCC C 模式下：

```
C:   sizeof(struct Empty) = 0    (GCC 扩展，ISO C 未定义)
C++: sizeof(Empty)        = 1    (标准要求，保证唯一地址)
```

**C 的空结构体 sizeof 为 0（GCC 扩展），不是 1。**
但 sizeof 为 0 会导致 `Empty arr[10]` 所有元素地址相同，引发其他问题。

C++ 的 EBCO 价值在于：

| 组合方式 | sizeof | 说明 |
|---------|--------|------|
| `struct A { Empty e; int x; }` | 8 | 成员: 1 字节 + 3 padding + 4 int |
| `struct B : Empty { int x; }` | 4 | EBCO: 基类零开销 |

嵌入式 C++ 中策略类、tag 类、空 allocator 应该用**继承**而非**成员组合**，
通过 EBCO 实现零开销。C++20 的 `[[no_unique_address]]` 可让成员也享受此优化。

### 观点 5："模板膨胀可以控制"

**正确。** 但原文遗漏了最重要的控制手段：

| 手段 | 说明 |
|------|------|
| `-ffunction-sections -Wl,--gc-sections` | 链接器移除未引用的函数段 |
| `-Os` | 编译器优先控制体积 |
| LTO (`-flto`) | 跨编译单元合并重复实例化 |
| 类型无关代码下沉 | 模板中与 T 无关的逻辑提取到非模板基类 |
| 显式实例化 (`extern template`) | 控制实例化位置，避免重复 |

实测 `-ffunction-sections -Wl,--gc-sections` 对两种语言都只移除了约 4 字节，
说明紧凑的代码本身就没有多少死代码可清除。此选项在大型项目中效果更明显。

---

## 二、实测数据：等价功能的消息总线

### 测试代码

C 版本：`void*` + `enum MsgType` + 函数指针 dispatch，手动类型转换。
C++ 版本：`std::variant` + 模板 subscribe + `std::visit` dispatch，编译期类型安全。

两者功能等价：注册两种消息类型的订阅，发布并处理消息。

### 编译配置

```
C:   gcc -Os -s -fno-asynchronous-unwind-tables
C++: g++ -Os -s -fno-exceptions -fno-rtti -fno-asynchronous-unwind-tables -std=c++17
```

### 实测结果

| 配置 | C .text | C++ .text | 差值 | 文件大小 |
|------|---------|-----------|------|---------|
| `-O2` | 2177 B | 2085 B | **C++ 小 92 字节 (-4.2%)** | 相同 |
| `-Os` | 2036 B | 2062 B | C++ 大 26 字节 (+1.3%) | 相同 |
| `-Os + gc-sections` | 2032 B | 2058 B | C++ 大 26 字节 (+1.3%) | 相同 |

- .data 和 .bss 段两者完全相同 (600-608 / 288-320 字节)
- ELF 文件总大小完全相同 (14464-14472 字节)
- strip 后大小完全相同

### 数据解读

1. **`-O2` 下 C++ 反而更小**：编译器对模板代码做了更激进的优化，
   C 版本的 `publish()` 需要循环匹配 + 函数指针间接调用，
   C++ 的 `std::visit` 被优化器展开为更紧凑的跳转表。

2. **`-Os` 下差异仅 26 字节**：`-Os` 抑制了内联，`std::variant` 的
   visitation 机制多出约 26 字节分发逻辑。在实际项目中（几十 KB .text），
   这个差异可忽略。

3. **gc-sections 效果有限**：两种实现都很紧凑，几乎无死代码可移除。

---

## 三、C++ 体积更小的真实场景

### 3.1 多配置系统

C 的通用函数包含所有配置的运行时分支：

```c
// C: 所有分支都编译进二进制
void parse(int format, const char* path) {
    if (format == FMT_INI)  { /* INI 解析 ~200 行 */ }
    if (format == FMT_JSON) { /* JSON 解析 ~300 行 */ }
    if (format == FMT_YAML) { /* YAML 解析 ~250 行 */ }
}
// 即使项目只用 INI，JSON 和 YAML 的代码仍在二进制中
```

C++ 的 `if constexpr` 只保留启用的后端：

```cpp
// C++: 编译期确定，未启用的后端不生成任何代码
template <typename... Backends>
void parse(const char* path) {
    if constexpr (has<IniBackend>())  { /* INI 解析 */ }
    if constexpr (has<JsonBackend>()) { /* JSON 解析 */ }  // 未启用 -> 不存在
}
```

### 3.2 静态多态 vs 虚函数表

```cpp
// C 函数指针表: 每个"接口"一个函数指针数组
struct Transport {
    int (*send)(void* ctx, const void* data, uint32_t size);
    int (*recv)(void* ctx, void* buf, uint32_t size);
    void (*close)(void* ctx);
};
// 每个实例: 3 个指针 = 24 字节 (64 位平台)
// 每次调用: 间接跳转，不可内联
```

```cpp
// C++ CRTP: 零额外存储，编译期解析
template <typename Derived>
struct Transport {
    void Send(const void* data, uint32_t size) {
        static_cast<Derived*>(this)->DoSend(data, size);  // 内联
    }
};
// 每个实例: 0 字节额外开销
// 每次调用: 直接调用或内联
```

CRTP 消除了函数指针表的 24 字节/实例存储，以及间接调用的 call 序列。
当实例数量多时（如 64 个连接各持有一个 Transport），差异显著。

### 3.3 constexpr 查表 vs 运行时初始化

```c
// C: 运行时初始化 CRC 表 -> 表存在 .data 段 (可写) 或运行时计算
static uint16_t crc_table[256];
void init_crc_table() { /* 运行时计算 256 个值 */ }
// init_crc_table 函数本身也占 .text 空间
```

```cpp
// C++: constexpr 编译期计算 -> 表直接放入 .rodata 段 (只读)
static constexpr auto crc_table = [] {
    std::array<uint16_t, 256> t{};
    for (uint32_t i = 0; i < 256; ++i) { /* 编译期计算 */ }
    return t;
}();
// 无 init 函数，无运行时计算，表在 Flash 中只读
```

constexpr 消除了初始化函数的 .text 开销和 .data 段的可写拷贝。
对 Flash 受限的 MCU，.rodata（XIP 直接执行）比 .data（需要拷贝到 RAM）更节省。

---

## 四、C++ 体积更大的真实场景

公平起见，列出 C++ 确实会增大体积的情况：

| 场景 | 原因 | 体积影响 |
|------|------|---------|
| 标准库容器 (`std::vector`, `std::map`) | 模板实例化 + 异常处理代码 | 数 KB |
| `std::iostream` | 拖入整套 IO 子系统 | +100-200 KB |
| 大量不同类型的模板实例化 | 每个类型生成独立代码 | 线性增长 |
| 虚函数 + RTTI | typeinfo 结构和展开表 | 每类 ~50-200 字节 |
| 未禁用异常 | .eh_frame 展开表 | .text 的 10-30% |

工业嵌入式的应对策略：

1. 禁用 `-fno-exceptions -fno-rtti` -- 消除运行时类型信息和展开表
2. 避免标准库容器 -- 用 `FixedVector<T,N>` 等固定容量容器替代
3. 不使用 iostream -- 用 `printf` 或自定义日志
4. 控制模板实例化数量 -- 类型无关代码下沉到非模板基类
5. 开启 `-Os -flto -ffunction-sections -Wl,--gc-sections`

---

## 五、嵌入式关键编译选项

### 必选项

```makefile
# 消除 C++ 运行时开销
CXXFLAGS += -fno-exceptions -fno-rtti

# 体积优化
CXXFLAGS += -Os

# 链接器移除未引用段
CXXFLAGS += -ffunction-sections -fdata-sections
LDFLAGS  += -Wl,--gc-sections

# 移除展开表 (无异常时不需要)
CXXFLAGS += -fno-asynchronous-unwind-tables -fno-unwind-tables
```

### 可选项

```makefile
# 全程序优化 (跨编译单元合并重复实例化)
CXXFLAGS += -flto
LDFLAGS  += -flto

# 合并相同内容的段 (如相同的模板实例化)
LDFLAGS  += -Wl,--icf=safe

# 移除符号表 (最终发布)
LDFLAGS  += -s
# 或发布后 strip
# arm-none-eabi-strip -s firmware.elf
```

### 体积审查工具

```bash
# 查看各段大小
size firmware.elf

# 按符号大小排序，找出最大的函数
nm --size-sort -r firmware.elf | head -20

# 查看模板实例化产生的符号
nm firmware.elf | c++filt | grep "AsyncBus" | sort -k2

# 对比两次构建的体积变化
bloaty new.elf -- old.elf
```

## 六、总结

1. **开启 `-fno-exceptions -fno-rtti`** -- 这是 C++ 体积与 C 持平的前提条件。
   不开启就不要比较。

2. **`-Os` 而非 `-O2`** -- `-O2` 下 C++ 内联更激进，可能反而更小，
   但也可能因过度内联膨胀。`-Os` 让编译器主动控制体积。

3. **.text 段差异在 1-4% 以内** -- 等价功能的 C/C++ 代码，在禁用 RTTI/异常后，
   体积差异可忽略。选择语言时不应以体积为主要考量。

4. **C++ 的体积优势在大型系统中更明显** -- `if constexpr` 剪枝、CRTP 消除 vtable、
   constexpr 查表等技术，在功能复杂的系统中累积的体积节省超过模板实例化的开销。

5. **真正影响体积的是设计决策，不是语言** -- 是否使用 iostream、是否引入标准库容器、
   是否控制模板实例化数量，这些设计选择的影响远大于 C vs C++ 的语言差异。

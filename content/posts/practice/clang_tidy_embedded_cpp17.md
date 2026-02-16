---
title: "Clang-Tidy 嵌入式 C++17 实战: 从配置到 CI 集成的完整指南"
date: 2026-02-16
draft: false
categories: ["practice"]
tags: ["C++17", "clang-tidy", "static-analysis", "embedded", "CMake", "CI", "ARM", "code-quality"]
summary: "将两篇 clang-tidy 基础教程整合并扩展为面向嵌入式 C++17 的完整实战指南。涵盖针对 -fno-exceptions/-fno-rtti 场景的精选 check 集合、嵌入式专属 check (concurrency、performance、bugprone)、HeaderFilterRegex 精确控制、CMake CMAKE_CXX_CLANG_TIDY 原生集成、GNU parallel 并行加速、GitHub Actions CI 门禁，以及 NOLINT 注释的正确用法。"
ShowToc: true
TocOpen: true
---

> 原始教程: [Clang-Tidy 完整配置与 CMake 集成](https://blog.csdn.net/stallion5632/article/details/139545885) | [多进程 shell 脚本加速](https://blog.csdn.net/stallion5632/article/details/140122323)
>
> 目标平台: ARM-Linux (Cortex-A53/A72) | C++17, `-fno-exceptions -fno-rtti`
>
> 官方文档: [Clang-Tidy Checks List](https://clang.llvm.org/extra/clang-tidy/checks/list.html)

---

## 1. 为什么嵌入式项目需要 clang-tidy

cpplint 和 clang-format 解决的是**风格**问题 (命名、缩进、include 排序)，而 clang-tidy 解决的是**语义**问题:

| 工具 | 分析层级 | 能力 |
|------|---------|------|
| clang-format | 词法 (token) | 缩进、空格、换行 |
| cpplint | 正则匹配 | 命名规范、头文件 guard、include 排序 |
| **clang-tidy** | **AST (抽象语法树)** | 空指针解引用、窄化转换、use-after-move、线程安全 |

嵌入式 C++17 项目 (如 newosp) 常用 `-fno-exceptions -fno-rtti` 编译，这意味着:
- 运行时类型检查缺失，类型错误更难发现
- 异常路径被截断，错误处理依赖返回值检查
- `reinterpret_cast`、`placement new` 等底层操作更多

这些场景恰好是 clang-tidy 的 bugprone、cppcoreguidelines、performance 系列 check 的发力点。

---

## 2. 安装

推荐使用 LLVM 官方脚本安装最新版 (当前 18):

```bash
wget https://apt.llvm.org/llvm.sh
chmod +x llvm.sh
sudo ./llvm.sh 18

# 验证
clang-tidy-18 --version
```

`apt-get install clang-tidy` 安装的版本通常较旧，缺少 C++17 相关 check (如 `modernize-use-structured-binding`、`bugprone-unchecked-optional-access`)。

---

## 3. 嵌入式 C++17 专用 .clang-tidy 配置

原始教程的配置面向 C++11 桌面项目。以下是针对嵌入式 C++17 重新设计的配置:

```yaml
---
# 嵌入式 C++17 clang-tidy 配置
# 目标: ARM-Linux, -fno-exceptions -fno-rtti, header-only 库

Checks: >
  -*,
  bugprone-*,
  -bugprone-easily-swappable-parameters,
  -bugprone-exception-escape,
  -bugprone-unhandled-exception-at-new,
  cert-*,
  -cert-err60-cpp,
  clang-analyzer-core.*,
  clang-analyzer-cplusplus.*,
  clang-analyzer-deadcode.*,
  concurrency-mt-unsafe,
  cppcoreguidelines-init-variables,
  cppcoreguidelines-misleading-capture-default-by-value,
  cppcoreguidelines-narrowing-conversions,
  cppcoreguidelines-no-malloc,
  cppcoreguidelines-prefer-member-initializer,
  cppcoreguidelines-pro-type-cstyle-cast,
  cppcoreguidelines-pro-type-member-init,
  cppcoreguidelines-slicing,
  cppcoreguidelines-special-member-functions,
  google-build-using-namespace,
  google-explicit-constructor,
  google-readability-casting,
  misc-const-correctness,
  misc-redundant-expression,
  misc-static-assert,
  misc-unconventional-assign-operator,
  misc-unused-parameters,
  modernize-deprecated-headers,
  modernize-loop-convert,
  modernize-redundant-void-arg,
  modernize-use-bool-literals,
  modernize-use-default-member-init,
  modernize-use-emplace,
  modernize-use-equals-default,
  modernize-use-equals-delete,
  modernize-use-nodiscard,
  modernize-use-noexcept,
  modernize-use-nullptr,
  modernize-use-override,
  modernize-use-using,
  performance-*,
  -performance-avoid-endl,
  readability-braces-around-statements,
  readability-container-size-empty,
  readability-else-after-return,
  readability-identifier-naming,
  readability-implicit-bool-conversion,
  readability-make-member-function-const,
  readability-misleading-indentation,
  readability-non-const-parameter,
  readability-redundant-control-flow,
  readability-simplify-boolean-expr,
  readability-static-accessed-through-instance

# 仅检查项目头文件，排除第三方和系统头文件
HeaderFilterRegex: '(include/osp/|include/mccc/|src/)'

# 严格模式: 将以下 check 升级为编译错误
WarningsAsErrors: >
  bugprone-use-after-move,
  bugprone-dangling-handle,
  bugprone-infinite-loop,
  cppcoreguidelines-no-malloc,
  performance-move-const-arg

FormatStyle: file

CheckOptions:
  # --- 命名规范 (Google Style) ---
  - key: readability-identifier-naming.NamespaceCase
    value: lower_case
  - key: readability-identifier-naming.ClassCase
    value: CamelCase
  - key: readability-identifier-naming.StructCase
    value: CamelCase
  - key: readability-identifier-naming.EnumCase
    value: CamelCase
  - key: readability-identifier-naming.EnumConstantCase
    value: CamelCase
  - key: readability-identifier-naming.EnumConstantPrefix
    value: k
  - key: readability-identifier-naming.FunctionCase
    value: CamelCase
  - key: readability-identifier-naming.MethodCase
    value: CamelCase
  - key: readability-identifier-naming.ParameterCase
    value: lower_case
  - key: readability-identifier-naming.LocalVariableCase
    value: lower_case
  - key: readability-identifier-naming.MemberCase
    value: lower_case
  - key: readability-identifier-naming.MemberSuffix
    value: _
  - key: readability-identifier-naming.ConstantCase
    value: CamelCase
  - key: readability-identifier-naming.ConstantPrefix
    value: k
  - key: readability-identifier-naming.TemplateParameterCase
    value: CamelCase
  - key: readability-identifier-naming.TypeAliasCase
    value: CamelCase
  - key: readability-identifier-naming.MacroDefinitionCase
    value: UPPER_CASE

  # --- 嵌入式专属调优 ---
  - key: cppcoreguidelines-special-member-functions.AllowSoleDefaultDtor
    value: 'true'
  - key: modernize-use-noexcept.ReplacementString
    value: 'noexcept'
  - key: performance-move-const-arg.CheckTriviallyCopyableMove
    value: 'true'
  - key: readability-function-size.LineThreshold
    value: '200'
  - key: readability-function-cognitive-complexity.Threshold
    value: '40'
  - key: bugprone-narrowing-conversions.WarnOnIntegerNarrowingConversion
    value: 'true'
  - key: bugprone-narrowing-conversions.WarnOnFloatingPointNarrowingConversion
    value: 'true'
  - key: misc-const-correctness.WarnPointersAsValues
    value: 'true'
...
```

### 3.1 配置设计原则

**原则 1: 先禁全部 (`-*`)，再精选启用**

原始教程和本文都采用 `-*` 起手。理由: clang-tidy 有 500+ check，全部启用会产生大量噪声。嵌入式项目需要精选与目标平台相关的 check。

**原则 2: 禁用异常相关 check**

`-fno-exceptions` 项目中，以下 check 会产生误报:
- `bugprone-exception-escape`: 检测异常泄漏，但异常已禁用
- `bugprone-unhandled-exception-at-new`: 检测 new 的异常，但使用 placement new
- `cert-err60-cpp`: 检测异常类拷贝构造，无异常场景不适用

**原则 3: HeaderFilterRegex 必须配置**

```yaml
HeaderFilterRegex: '(include/osp/|include/mccc/|src/)'
```

不配置此项 (或设为空)，clang-tidy **只检查源文件，跳过头文件中的警告**。对于 header-only 库这意味着大部分代码不会被检查。设为 `.*` 则会检查系统头文件和第三方库 (Catch2、sockpp 等)，产生大量不可修复的噪声。

**原则 4: WarningsAsErrors 精选致命级 check**

只将确定是 bug 的 check 升级为错误 (阻断 CI):
- `bugprone-use-after-move`: 移动后使用，100% 是 bug
- `bugprone-dangling-handle`: 悬挂引用
- `cppcoreguidelines-no-malloc`: 嵌入式项目禁止裸 malloc

### 3.2 嵌入式高价值 check 详解

#### bugprone 系列 (bug 检测)

| check | 说明 | 嵌入式价值 |
|-------|------|-----------|
| `bugprone-use-after-move` | 检测 `std::move` 后继续使用对象 | 高: 无异常下 UB 难以调试 |
| `bugprone-narrowing-conversions` | `uint32_t → uint16_t` 隐式截断 | 高: 嵌入式常用固定宽度类型 |
| `bugprone-infinite-loop` | 检测死循环 | 高: RTOS 任务中死循环影响看门狗 |
| `bugprone-sizeof-expression` | `sizeof(ptr)` vs `sizeof(*ptr)` | 高: DMA 缓冲区大小计算 |
| `bugprone-signal-handler` | signal handler 中调用非异步安全函数 | 高: ARM-Linux 信号处理 |
| `bugprone-misplaced-widening-cast` | 宽化转换位置错误 | 高: 32-bit ARM 上整数溢出 |

#### concurrency 系列 (并发安全)

| check | 说明 | 嵌入式价值 |
|-------|------|-----------|
| `concurrency-mt-unsafe` | 检测多线程不安全函数 (strtok, rand, localtime) | 高: 嵌入式多线程/中断环境 |

这个 check 在原始教程中缺失，但对嵌入式极其重要。`strtok`、`rand`、`asctime` 等函数使用静态内部缓冲区，在多线程环境中会产生数据竞争。

#### performance 系列

| check | 说明 | 嵌入式价值 |
|-------|------|-----------|
| `performance-move-const-arg` | 对 trivially-copyable 类型使用 std::move 无效 | 高: 避免误导性代码 |
| `performance-unnecessary-value-param` | 大对象按值传参应改为 const& | 高: 栈空间有限 |
| `performance-noexcept-move-constructor` | 移动构造函数缺少 noexcept | 高: 影响 std::vector 扩容策略 |
| `performance-trivially-destructible` | 有 trivial 析构但未利用 | 中: 影响 memcpy 优化路径 |

#### modernize 系列 (针对 C++17 精选)

```
modernize-use-structured-binding   # auto [a, b] = pair (C++17)
modernize-use-nodiscard            # [[nodiscard]] 标注返回值必须检查
modernize-use-default-member-init  # int x{0} 替代构造函数初始化列表
```

注意: `modernize-use-trailing-return-type` (建议 `auto f() -> int`) 在嵌入式团队中争议较大，建议不启用。

---

## 4. CMake 集成

### 4.1 CMAKE_CXX_CLANG_TIDY (推荐)

CMake 3.6+ 原生支持将 clang-tidy 嵌入编译过程:

```cmake
cmake_minimum_required(VERSION 3.14)
project(MyEmbeddedProject LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# 可选: 只在 Debug 构建或指定选项时启用
option(ENABLE_CLANG_TIDY "Enable clang-tidy static analysis" OFF)

if(ENABLE_CLANG_TIDY)
  find_program(CLANG_TIDY_EXE NAMES clang-tidy-18 clang-tidy)
  if(CLANG_TIDY_EXE)
    # 使用项目根目录的 .clang-tidy 配置
    set(CMAKE_CXX_CLANG_TIDY
      "${CLANG_TIDY_EXE}"
      "--config-file=${CMAKE_SOURCE_DIR}/.clang-tidy"
    )
    message(STATUS "clang-tidy enabled: ${CLANG_TIDY_EXE}")
  else()
    message(WARNING "clang-tidy not found, static analysis disabled")
  endif()
endif()

# 库目标
add_library(mylib INTERFACE)
target_include_directories(mylib INTERFACE include/)
```

使用:

```bash
# 普通构建 (无 clang-tidy)
cmake -B build && cmake --build build

# 启用 clang-tidy 的构建 (每个源文件编译时自动检查)
cmake -B build -DENABLE_CLANG_TIDY=ON && cmake --build build
```

**优势**: 每个 `.cpp` 文件编译时自动运行 clang-tidy，增量编译只检查修改的文件。无需手动指定文件列表。

**注意**: `CMAKE_CXX_CLANG_TIDY` 只在 **编译源文件时** 触发。header-only 库如果没有 `.cpp` 文件，需要通过测试文件间接触发检查。

### 4.2 自定义 target (适合 CI)

```cmake
# 查找所有项目源文件 (排除第三方)
file(GLOB_RECURSE PROJECT_SOURCES
  ${CMAKE_SOURCE_DIR}/src/*.cpp
  ${CMAKE_SOURCE_DIR}/tests/*.cpp
  ${CMAKE_SOURCE_DIR}/examples/*.cpp
)

# 独立的 clang-tidy target
add_custom_target(clang-tidy
  COMMAND ${CLANG_TIDY_EXE}
    -p ${CMAKE_BINARY_DIR}
    --config-file=${CMAKE_SOURCE_DIR}/.clang-tidy
    ${PROJECT_SOURCES}
  COMMENT "Running clang-tidy on project sources"
  VERBATIM
)
```

```bash
cmake --build build --target clang-tidy
```

---

## 5. 并行执行: run-clang-tidy 与 GNU parallel

### 5.1 run-clang-tidy (LLVM 官方)

LLVM 提供的 `run-clang-tidy` 脚本内置并行支持:

```bash
# -j: 并行线程数 (默认 CPU 核数)
# -p: compile_commands.json 目录
# -config-file: 配置文件路径
run-clang-tidy-18 -j$(nproc) -p build -config-file=.clang-tidy
```

这是最简单的并行方案，但输出格式不易定制。

### 5.2 GNU parallel + 过滤脚本

原始教程提供了 GNU parallel 方案。以下是优化后的版本:

```bash
#!/bin/bash
set -euo pipefail

SOURCE_DIR="${1:?Usage: $0 <source_dir> [build_dir]}"
BUILD_DIR="${2:-build}"

# 验证
[[ -d "$SOURCE_DIR" ]] || { echo "Error: $SOURCE_DIR not found"; exit 1; }
[[ -f "$BUILD_DIR/compile_commands.json" ]] || {
  echo "Error: compile_commands.json not found in $BUILD_DIR"
  echo "Run: cmake -B $BUILD_DIR -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
  exit 1
}

CLANG_TIDY="clang-tidy-18"
CONFIG_FILE="$(pwd)/.clang-tidy"
FAIL_DIR=$(mktemp -d)
trap "rm -rf $FAIL_DIR" EXIT

# 过滤函数: 去除系统头文件和无用警告
filter_output() {
  awk '
    /^[0-9]+ warnings? generated/ { next }
    /^Suppressed [0-9]+ warnings/ { next }
    /^Use -header-filter=/ { next }
    /^Use -system-headers/ { next }
    { print }
  '
}
export -f filter_output

# 查找源文件 (排除第三方)
find "$SOURCE_DIR" -type f \( -name '*.cpp' -o -name '*.cc' \) \
  ! -path '*/third_party/*' ! -path '*/_deps/*' \
  | parallel -j"$(nproc)" --halt soon,fail=1 --linebuffer \
    "$CLANG_TIDY {} -p '$BUILD_DIR' --config-file='$CONFIG_FILE' \
     --warnings-as-errors='bugprone-use-after-move,bugprone-dangling-handle' \
     2>&1 | filter_output \
     || touch '$FAIL_DIR/failed_{#}'"

# 检查结果
if compgen -G "$FAIL_DIR/failed_*" > /dev/null; then
  echo "clang-tidy detected issues."
  exit 1
fi
echo "clang-tidy: all checks passed."
```

改进点:

| 原始版本 | 优化版本 |
|---------|---------|
| `set -e` | `set -euo pipefail` (更严格的错误处理) |
| 手动指定 `-j4` | `-j$(nproc)` (自动匹配核数) |
| 失败文件留在 build 目录 | `mktemp -d` + `trap` 自动清理 |
| 无文件排除 | `! -path '*/third_party/*'` 排除第三方 |
| `--no-notice` (已废弃) | 移除 |
| 无 `--halt` | `--halt soon,fail=1` 首个错误后尽快停止 |

---

## 6. NOLINT: 精确抑制误报

### 6.1 行级抑制

```cpp
// 抑制单行的特定 check
auto* raw = reinterpret_cast<uint8_t*>(buffer);  // NOLINT(cppcoreguidelines-pro-type-reinterpret-cast)

// 抑制单行所有 check (谨慎使用)
void* ctx = static_cast<void*>(this);  // NOLINT
```

### 6.2 下一行抑制

```cpp
// NOLINTNEXTLINE(bugprone-narrowing-conversions)
uint16_t len = static_cast<uint16_t>(total_size);
```

### 6.3 区间抑制

```cpp
// NOLINTBEGIN(cppcoreguidelines-pro-type-reinterpret-cast)
auto* hdr = reinterpret_cast<FrameHeader*>(buf);
auto* payload = reinterpret_cast<uint8_t*>(buf + sizeof(FrameHeader));
auto* crc = reinterpret_cast<uint16_t*>(buf + total - 2);
// NOLINTEND(cppcoreguidelines-pro-type-reinterpret-cast)
```

### 6.4 嵌入式常见的合理抑制场景

```cpp
// 1. 硬件寄存器地址映射 (必须 reinterpret_cast)
auto* gpio = reinterpret_cast<volatile GpioRegs*>(0x40020000);  // NOLINT(cppcoreguidelines-pro-type-reinterpret-cast,performance-no-int-to-ptr)

// 2. placement new (不是堆分配)
::new (&storage_) T(std::forward<Args>(args)...);  // NOLINT(cppcoreguidelines-owning-memory)

// 3. POSIX 回调 void* context (C 接口兼容)
auto* self = static_cast<Pipeline*>(ctx);  // NOLINT(cppcoreguidelines-pro-type-static-cast-downcast)

// 4. 位操作 (有意的窄化)
// NOLINTNEXTLINE(bugprone-narrowing-conversions)
uint8_t crc_lo = static_cast<uint8_t>(crc16 & 0xFF);
```

---

## 7. GitHub Actions CI 集成

```yaml
name: Static Analysis

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  clang-tidy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install LLVM 18
        run: |
          wget https://apt.llvm.org/llvm.sh
          chmod +x llvm.sh
          sudo ./llvm.sh 18
          sudo apt-get install -y clang-tidy-18

      - name: Generate compile_commands.json
        run: cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

      - name: Run clang-tidy
        run: |
          run-clang-tidy-18 -j$(nproc) -p build \
            -config-file=.clang-tidy \
            'tests/.*\.cpp' 'examples/.*\.cpp'

  clang-format:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Check formatting
        run: |
          find include/ tests/ examples/ -name '*.hpp' -o -name '*.cpp' \
            | xargs clang-format-18 --dry-run --Werror

  cpplint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install cpplint
        run: pip install cpplint
      - name: Run cpplint
        run: |
          cpplint --recursive --quiet \
            --filter=-legal/copyright,-build/header_guard \
            include/ tests/
```

三层质量门禁:
1. **clang-format**: 格式一致性 (词法级)
2. **cpplint**: 命名规范、头文件规则 (正则级)
3. **clang-tidy**: bug 检测、性能分析、现代化建议 (AST 级)

---

## 8. check 分类速查表

### 8.1 嵌入式必选 (建议所有项目启用)

| 分类 | check | 说明 |
|------|-------|------|
| 安全 | `bugprone-use-after-move` | 移动后使用 |
| 安全 | `bugprone-dangling-handle` | 悬挂引用/指针 |
| 安全 | `bugprone-sizeof-expression` | sizeof 误用 |
| 安全 | `bugprone-signal-handler` | 信号处理安全 |
| 安全 | `concurrency-mt-unsafe` | 多线程不安全函数 |
| 类型 | `bugprone-narrowing-conversions` | 窄化转换 |
| 类型 | `cppcoreguidelines-pro-type-cstyle-cast` | C 风格转换 |
| 内存 | `cppcoreguidelines-no-malloc` | 禁止裸 malloc |
| 内存 | `bugprone-infinite-loop` | 死循环 (看门狗友好) |
| 分析 | `clang-analyzer-core.*` | 空指针、内存泄漏 |
| 分析 | `clang-analyzer-cplusplus.*` | 对象生命周期 |

### 8.2 C++17 现代化 (建议新项目启用)

| check | 说明 | 自动修复 |
|-------|------|---------|
| `modernize-use-nullptr` | NULL/0 → nullptr | Y |
| `modernize-use-override` | 添加 override | Y |
| `modernize-use-equals-default` | 默认构造 = default | Y |
| `modernize-use-emplace` | push_back → emplace_back | Y |
| `modernize-use-nodiscard` | 添加 [[nodiscard]] | Y |
| `modernize-use-using` | typedef → using | Y |
| `modernize-deprecated-headers` | stdio.h → cstdio | Y |
| `modernize-loop-convert` | C 风格循环 → range-for | Y |
| `modernize-use-default-member-init` | 类内默认初始化 | Y |

### 8.3 争议较大 / 建议不启用

| check | 原因 |
|-------|------|
| `modernize-use-trailing-return-type` | `auto f() -> int` 团队接受度低 |
| `readability-magic-numbers` | 嵌入式中寄存器地址和协议常量太多 |
| `cppcoreguidelines-avoid-do-while` | do-while 在协议解析中有合理用途 |
| `cppcoreguidelines-pro-bounds-pointer-arithmetic` | 嵌入式必须操作 buffer 指针 |
| `bugprone-easily-swappable-parameters` | 误报率极高 |
| `readability-identifier-length` | 循环变量 `i`、`j` 是合理的 |

---

## 9. 与 cpplint 的分工

两个工具有部分重叠，但定位不同:

| 检查项 | cpplint | clang-tidy | 推荐 |
|--------|---------|-----------|------|
| 命名规范 | `readability/naming` | `readability-identifier-naming` | 二选一 |
| include 排序 | `build/include_order` | (无) | cpplint |
| 头文件 guard | `build/header_guard` | (无) | cpplint |
| C 风格转换 | `readability/casting` | `google-readability-casting` | clang-tidy (更精确) |
| 窄化转换 | (无) | `bugprone-narrowing-conversions` | clang-tidy |
| 空指针检测 | (无) | `clang-analyzer-core.NullDereference` | clang-tidy |
| use-after-move | (无) | `bugprone-use-after-move` | clang-tidy |

推荐策略: cpplint 负责风格 (include、命名、注释)，clang-tidy 负责语义 (bug、性能、现代化)。两者在 CI 中并行运行。

---

## 10. 总结

| 步骤 | 内容 |
|------|------|
| 1. 配置 | `.clang-tidy` 精选 check，禁用异常相关，配置 HeaderFilterRegex |
| 2. 本地 | `cmake -DENABLE_CLANG_TIDY=ON` 编译时自动检查 |
| 3. 批量 | `run-clang-tidy-18 -j$(nproc)` 全量扫描 |
| 4. CI | GitHub Actions 三层门禁 (format + cpllint + tidy) |
| 5. 抑制 | `NOLINT(check-name)` 精确标注，禁止裸 NOLINT |

---

## 参考

- [Clang-Tidy 官方文档](https://clang.llvm.org/extra/clang-tidy/)
- [Clang-Tidy Checks 完整列表](https://clang.llvm.org/extra/clang-tidy/checks/list.html)
- [Clang-Tidy 配置与 CMake 集成](https://blog.csdn.net/stallion5632/article/details/139545885) -- 原始教程 1
- [多进程 shell 脚本加速 clang-tidy](https://blog.csdn.net/stallion5632/article/details/140122323) -- 原始教程 2
- [CMake CMAKE_CXX_CLANG_TIDY 文档](https://cmake.org/cmake/help/latest/variable/CMAKE_LANG_CLANG_TIDY.html)
- [LLVM Discussion Forums](https://forums.llvm.org/)
- [C++ Core Guidelines](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines)
- [CERT C++ Coding Standard](https://wiki.sei.cmu.edu/confluence/display/cplusplus)

---
title: "newosp 调试 Shell: 多后端架构与运行时控制命令设计"
date: 2026-02-17
draft: false
categories: ["tools"]
tags: ["ARM", "C++17", "MISRA", "RTOS", "callback", "embedded", "lock-free", "memory-pool", "newosp", "serial", "state-machine", "zero-heap", "POSIX", "telnet", "debug"]
summary: "工业嵌入式系统需要在 TCP telnet、串口、stdin 等不同环境下统一调试。newosp 的 Shell 模块通过函数指针 I/O 抽象实现多后端统一架构，通过 TCLAP 风格的子命令分发实现运行时控制（日志级别、配置修改、统计重置、生命周期转换），18 个命令覆盖诊断与控制两大需求，全程零堆分配、-fno-exceptions 兼容。"
ShowToc: true
TocOpen: true
---

> [newosp](https://github.com/DeguiLiu/newosp) 项目地址
>
> 相关文章:
> - [嵌入式 Linux 调试 Shell 设计: 从 RT-Thread MSH 到自研 embsh](../embedded_cli_msh/) -- Shell 引擎选型与设计决策
> - [telsh: 从 boost::asio 到纯 POSIX 的 Telnet Shell 重构](../telsh_refactoring/) -- Telnet 协议与会话管理实现

---

## 1. 结论前置

newosp 的调试 Shell 模块在一个 header-only 文件中提供完整的嵌入式调试能力:

| 能力 | 实现 |
|------|------|
| 多后端统一 | TCP telnet / stdin / UART 三后端，函数指针 I/O 抽象，一套命令到处运行 |
| 运行时控制 | 子命令分发 + 类型安全参数解析，支持动态改日志、改配置、重置统计、切换生命周期 |
| 零侵入桥接 | 模块本身不依赖 shell.hpp，shell_commands.hpp 单向引用模块头文件 |
| 资源开销 | ConsoleShell 300B 栈 + 0 堆 + 1 线程；GlobalCmdRegistry 64 槽 ~2KB |

**18 个命令总览:**

| 类型 | 命令 | 功能 |
|------|------|------|
| 控制 | `osp_log`, `osp_config`, `osp_bus`, `osp_lifecycle` | 运行时修改日志级别、配置参数、重置统计、状态机转换 |
| 诊断 | `osp_watchdog`, `osp_faults`, `osp_sysmon` 等 14 个 | 只读查询系统各层状态 |

**设计约束:**
- C++17 header-only, `-fno-exceptions -fno-rtti`
- 零堆分配 (函数指针 + 静态局部变量 + 栈缓冲)
- 命令签名 `int (*)(int argc, char* argv[])` 与 POSIX main 一致

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────┐
│                   应用层                             │
│  shell_cmd::RegisterLog/Config/Bus/Lifecycle/...    │
│  (zero-intrusion bridge: 模块不依赖 shell.hpp)      │
├─────────────────────────────────────────────────────┤
│                   命令系统                           │
│  GlobalCmdRegistry (64 槽)                          │
│  ShellDispatch (子命令分发)                          │
│  ShellParseInt/Uint/Bool (类型安全参数解析)          │
├─────────────────────────────────────────────────────┤
│                   Shell 引擎                         │
│  ShellRunSession (逐字符读取/分词/查表/执行)        │
│  ShellPrintf (thread-local 会话路由)                │
│  Tab 补全 / 历史 / ESC 序列 / IAC 协议             │
├─────────────────────────────────────────────────────┤
│                   I/O 抽象层                         │
│  ShellWriteFn / ShellReadFn (函数指针)              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ TCP send │  │ POSIX    │  │ POSIX    │          │
│  │ /recv    │  │ write/   │  │ write/   │          │
│  │ (telnet) │  │ read     │  │ read     │          │
│  │          │  │ (stdin)  │  │ (UART)   │          │
│  └──────────┘  └──────────┘  └──────────┘          │
└─────────────────────────────────────────────────────┘
```

四层各司其职:
- **I/O 抽象层**: 函数指针隔离底层差异, 后端可运行时切换
- **Shell 引擎**: 行编辑、分词、Tab 补全、历史, 与后端无关
- **命令系统**: 注册/查找/分发/参数解析, 与 Shell 引擎解耦
- **应用层**: shell_commands.hpp 桥接模块状态, 零侵入

---

## 3. I/O 抽象: 函数指针方案

### 3.1 方案选型

| 方案 | 优点 | 缺点 | 判定 |
|------|------|------|------|
| 虚基类 `IShellBackend` | 语义清晰 | vtable 开销, 违反"不优先 virtual" | 否 |
| 模板参数 `ShellT<Backend>` | 编译期绑定, 可内联 | Printf 类型不统一, 无法运行时选择后端 | 否 |
| 函数指针 | 零 vtable, 运行时可选, POSIX 自然映射 | 无法内联 | **选用** |

Shell I/O 不是热路径 (人类打字速度 ~10 字符/秒), 无法内联的代价可忽略. 函数指针与 POSIX `read(2)/write(2)` 签名天然一致, 无需适配层.

### 3.2 实现

```cpp
// 与 POSIX read/write 签名一致
using ShellWriteFn = ssize_t (*)(int fd, const void* buf, size_t len);
using ShellReadFn  = ssize_t (*)(int fd, void* buf, size_t len);

// TCP 后端: send() with MSG_NOSIGNAL (避免 SIGPIPE)
inline ssize_t ShellTcpWrite(int fd, const void* buf, size_t len) {
  return ::send(fd, buf, len, MSG_NOSIGNAL);
}

// POSIX 后端: write()/read() 适用于 stdin、stdout、UART
inline ssize_t ShellPosixWrite(int fd, const void* buf, size_t len) {
  return ::write(fd, buf, len);
}
```

不用 `std::function` 或 `FixedFunction` 的原因: Shell I/O 函数是无状态的 (不需要捕获), 原始函数指针最轻量.

### 3.3 统一会话结构

所有后端共享同一个 ShellSession:

```cpp
struct ShellSession {
  int read_fd = -1;               // 读端 fd (stdin/socket/uart)
  int write_fd = -1;              // 写端 fd (stdout/socket/uart)
  ShellWriteFn write_fn = nullptr;  // 后端特定写函数
  ShellReadFn  read_fn  = nullptr;  // 后端特定读函数
  bool telnet_mode = false;         // TCP 需要 \r\n, IAC 处理
  char line_buf[256] = {};          // 命令行缓冲 (栈分配)
  uint32_t line_pos = 0;
  std::atomic<bool> active{false};
};
```

关键点: `read_fd/write_fd` 分开 (stdin 和 stdout 是不同 fd); `telnet_mode` 区分 TCP 和 POSIX 行结束符; 256 字节 line_buf 栈分配, 零堆.

### 3.4 ShellPrintf: thread-local 会话路由

命令回调只调 `ShellPrintf(fmt, ...)`, 不关心底层后端:

```cpp
inline int ShellPrintf(const char* fmt, ...) {
  ShellSession* sess = detail::CurrentSession();  // thread-local
  if (sess == nullptr) return -1;
  char buf[256];
  va_list args;
  va_start(args, fmt);
  int n = std::vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  if (n > 0) sess->write_fn(sess->write_fd, buf, n);
  return n;
}
```

每个会话运行在独立线程, `CurrentSession()` 返回 thread-local 指针, 天然隔离, 无需传参或全局锁. 命令执行前设置指针, 执行后清空:

```
cmd->func(argc, argv)
  -> ShellPrintf("...")
    -> CurrentSession()->write_fn(write_fd, buf, len)
       // TCP: send()  |  Console: write(1, ...)  |  UART: write(uart_fd, ...)
```

### 3.5 三个后端

| 后端 | 场景 | 线程 | 堆 | 特性 |
|------|------|------|-----|------|
| `DebugShell` | 实验室 TCP telnet | 1 accept + N session | ~4KB | IAC 协议, 可选认证, 多连接 |
| `ConsoleShell` | SSH 远程, 无 telnet | 1 | 0 | termios raw mode, 管道测试 |
| `UartShell` | 早期硬件调试, 现场 | 1 | 0 | cfmakeraw, 支持 9600~921600 |

```cpp
// 实验室: telnet localhost 5090
osp::DebugShell::Config tcp_cfg{.port = 5090, .max_connections = 2};
osp::DebugShell tcp_shell(tcp_cfg);
tcp_shell.Start();

// SSH 远程: ./my_app --console
osp::ConsoleShell console_shell;
console_shell.Start();

// 串口调试: minicom -D /dev/ttyUSB0
osp::UartShell::Config uart_cfg{.device = "/dev/ttyS0", .baudrate = 115200};
osp::UartShell uart_shell(uart_cfg);
uart_shell.Start();
```

---

## 4. 命令系统

### 4.1 全局命令注册表

```cpp
struct ShellCmd {
  const char* name;    // 命令名 (字面量, 无拷贝)
  ShellCmdFn func;     // int (*)(int argc, char* argv[])
  const char* desc;    // 帮助文本
};

class GlobalCmdRegistry {
  ShellCmd cmds_[64];  // 固定 64 槽, 零堆分配
  uint32_t count_ = 0;
  // Meyer's 单例: 所有后端共享同一个注册表
  static GlobalCmdRegistry& Instance();
  bool Register(const char* name, ShellCmdFn func, const char* desc);
  const ShellCmd* Find(const char* name) const;
  uint32_t AutoComplete(const char* prefix, char* out, size_t out_size) const;
};
```

注册方式:

```cpp
// 方式一: 宏 (全局函数, 静态注册)
int my_cmd(int argc, char* argv[]) { ... }
OSP_SHELL_CMD(my_cmd, "My command");

// 方式二: 模板 Register 函数 (带上下文捕获)
template <typename T>
void RegisterWatchdog(T& wd) {
  static T* s_wd = &wd;  // 静态局部变量捕获, 零堆分配
  static auto cmd = [](int, char*[]) -> int { ... };
  GlobalCmdRegistry::Instance().Register("osp_watchdog", +cmd, "...");
}
```

`+cmd` 将无捕获 lambda 转为函数指针 (C++ 标准保证). `static` 确保对象指针在整个程序生命周期内有效, 且无堆分配.

### 4.2 子命令分发: ShellDispatch

当命令需要多个操作时 (如 `osp_bus status` 和 `osp_bus reset`), 手工解析 argv 容易出错且重复. ShellDispatch 参考 TCLAP 思想, 用声明式子命令表实现分发:

```cpp
struct ShellSubCmd {
  const char* name;       // 子命令名
  const char* args_desc;  // 参数描述 (e.g. "<level>"), nullptr=无参数
  const char* help;       // 帮助文本
  ShellCmdFn handler;     // 处理函数
};

int ShellDispatch(int argc, char* argv[],
                  const ShellSubCmd* table, uint32_t count,
                  ShellCmdFn default_fn = nullptr) noexcept;
```

行为:
- **无子命令** (`argc <= 1`): 调用 `default_fn` (保持向后兼容), 若为 nullptr 则打印帮助
- **`help`**: 自动生成格式化帮助表
- **匹配子命令**: 调用 `handler(argc-1, argv+1)` (argv 左移, 子命令名变 argv[0])
- **未匹配**: 打印错误提示

使用示例:

```cpp
static const ShellSubCmd kSubs[] = {
    {"status", nullptr,    "Show statistics",  sub_status},
    {"reset",  nullptr,    "Reset counters",   sub_reset},
};

static auto cmd = [](int argc, char* argv[]) -> int {
    return ShellDispatch(argc, argv, kSubs, 2U, sub_status);
    //                                          ^^^^^^^^^^
    //                    default_fn: 无参数时执行 status (向后兼容)
};
```

效果:

```
osp> osp_bus              # 调用 default_fn (show_status), 向后兼容
osp> osp_bus status       # 显式调用 status
osp> osp_bus reset        # 重置计数器
osp> osp_bus help         # 自动生成帮助:
  status       - Show statistics
  reset        - Reset counters
osp> osp_bus bogus        # Unknown subcommand: bogus (try 'osp_bus help')
```

### 4.3 类型安全参数解析

命令参数作为 `char*` 传入, 直接 `atoi` 不安全 (undefined behavior on overflow). Shell 提供三个解析函数, 返回 `optional`:

```cpp
[[nodiscard]] optional<int32_t>  ShellParseInt(const char* str) noexcept;
[[nodiscard]] optional<uint32_t> ShellParseUint(const char* str) noexcept;
[[nodiscard]] optional<bool>     ShellParseBool(const char* str) noexcept;
```

- `ShellParseInt`: `strtol` base 10, 拒绝 null / 空串 / 尾部垃圾 / 溢出
- `ShellParseUint`: `strtoul`, 额外拒绝前导 `-`
- `ShellParseBool`: 大小写无关匹配 `true/1/yes/on` 和 `false/0/no/off`

使用示例:

```cpp
auto num = ShellParseUint(argv[1]);
if (!num.has_value() || num.value() > 5U) {
    ShellPrintf("Invalid level: %s (expected 0-5)\r\n", argv[1]);
    return -1;
}
```

辅助函数:

```cpp
[[nodiscard]] bool ShellArgCheck(int argc, int min_argc,
                                 const char* usage) noexcept;
// argc < min_argc 时自动打印 "Usage: <usage>" 并返回 false
```

---

## 5. 运行时控制命令

### 5.1 从只读到可控

嵌入式系统调试的两类需求:

| 需求 | 传统做法 | 问题 |
|------|---------|------|
| **查状态** (只读) | 诊断命令打印统计 | 已解决, 14 个诊断命令覆盖 |
| **改行为** (可控) | 重新编译 + 烧录 + 重启 | 现场调试无法重编译, 修改一个日志级别要停机 |

Shell 的控制命令解决第二类需求: 在运行时动态调整系统行为, **仅修改内存, 重启恢复原值**, 适合现场调试和问题排查.

### 5.2 osp_log: 日志级别控制

```
osp> osp_log
[osp_log] level: INFO (1)

osp> osp_log level debug
[osp_log] level set to DEBUG

osp> osp_log level 3
[osp_log] level set to ERROR
```

实现要点:

```cpp
inline void RegisterLog() {
  // ...
  static auto sub_level = [](int argc, char* argv[]) -> int {
    if (!ShellArgCheck(argc, 2, "osp_log level <0-5|debug|...>"))
      return -1;

    // 先尝试数字
    auto num = ShellParseUint(argv[1]);
    if (num.has_value()) {
      if (num.value() > 5U) { /* 范围检查 */ return -1; }
      log::SetLevel(static_cast<log::Level>(num.value()));
      return 0;
    }

    // 再尝试名称 (大小写无关)
    static const struct { const char* name; log::Level level; } kNames[] = {
        {"debug", log::Level::kDebug}, {"info",  log::Level::kInfo},
        {"warn",  log::Level::kWarn},  {"error", log::Level::kError},
        {"fatal", log::Level::kFatal}, {"off",   log::Level::kOff},
    };
    for (const auto& n : kNames) {
      if (detail::ShellStrCaseEq(argv[1], n.name)) {
        log::SetLevel(n.level);
        return 0;
      }
    }
    return -1;
  };
  // ShellDispatch 子命令表: status, level
}
```

### 5.3 osp_config: 配置查看与运行时修改

```
osp> osp_config
[osp_config] all entries (3):
  [net] port = 8080
  [net] host = 192.168.1.100
  [log] level = 3

osp> osp_config set net port 9090
[net] port = 9090 (set)

osp> osp_config get net port
[net] port = 9090
```

ConfigStore 新增两个 public 方法支撑此命令:

```cpp
class ConfigStore {
 public:
  // 运行时设置 (upsert: 存在则更新, 不存在则新增)
  bool SetString(const char* section, const char* key, const char* value) {
    return AddEntry(section, key, value);  // protected AddEntry 已实现 upsert
  }

  // 遍历所有条目
  template <typename Fn>
  void ForEach(Fn&& visitor) const {
    for (uint32_t i = 0; i < count_; ++i)
      visitor(entries_[i].section, entries_[i].key, entries_[i].value);
  }
};
```

**仅修改内存**: `SetString` 不触发文件写入, 重启后丢失. 这是有意的设计 -- 现场调试改配置不应永久影响设备.

### 5.4 osp_bus: 统计重置

```
osp> osp_bus
[osp_bus] AsyncBus Statistics
  published:     12450
  dropped:       2
  backpressure:  Normal

osp> osp_bus reset
[osp_bus] Statistics reset.
```

向后兼容: 无参数调用仍显示统计 (通过 `default_fn = show_status`).

### 5.5 osp_lifecycle: 生命周期状态机转换

```
osp> osp_lifecycle
[osp_lifecycle] LifecycleNode
  state: Unconfigured (unconfigured)

osp> osp_lifecycle configure
[osp_lifecycle] Configure OK.

osp> osp_lifecycle activate
[osp_lifecycle] Activate OK.

osp> osp_lifecycle cleanup
[osp_lifecycle] Cleanup failed: InvalidTransition
```

6 个子命令对应状态机转换:

```
Unconfigured --configure--> Inactive --activate--> Active
     ^                         |                     |
     +------cleanup-----------+    <--deactivate---+
     |                                              |
     +--shutdown--> Finalized <------shutdown-------+
```

实现使用 `expected<void, LifecycleError>` 返回值, 转换失败打印错误类型 (`InvalidTransition` / `CallbackFailed` / `AlreadyFinalized`):

```cpp
static auto try_transition = [](const char* name,
                                expected<void, LifecycleError> result) -> int {
  if (result.has_value()) {
    ShellPrintf("[osp_lifecycle] %s OK.\r\n", name);
    return 0;
  }
  ShellPrintf("[osp_lifecycle] %s failed: %s\r\n",
              name, lifecycle_error_name(result.get_error()));
  return -1;
};
```

---

## 6. 诊断命令

14 个只读命令按架构层分组, 通过模板 Register 函数零侵入注册:

| 层 | 命令 | 输出内容 |
|----|------|----------|
| **可靠性** | `osp_watchdog` | 各线程名称、超时阈值、心跳间隔、是否超时 |
| | `osp_faults` | 各优先级报告/丢弃数、队列使用率、最近 N 条故障 |
| **通信** | `osp_pool` | 分发/处理消息数、队列满次数 |
| **网络** | `osp_transport` | 收包/丢包/乱序/重复数、丢包率 |
| | `osp_serial` | 帧/字节收发数、CRC/同步/超时错误、重传次数 |
| **服务** | `osp_nodes` | HSM 节点 ID、状态、心跳间隔、丢失心跳数 |
| | `osp_nodes_basic` | 基础节点连接状态 |
| | `osp_service` | 服务 HSM 当前状态 |
| | `osp_discovery` | 发现 HSM 状态、丢失节点数 |
| **应用** | `osp_qos` | QoS 配置各字段 (可靠性/历史/时限/生存期) |
| | `osp_app` | 应用名、实例数、待处理消息 |
| **基础** | `osp_sysmon` | CPU 使用率/温度、内存使用、磁盘使用 |
| | `osp_mempool` | 容量、已用、空闲 |
| | `help` | 列出所有已注册命令 |

示例输出:

```
osp> osp_watchdog
[osp_watchdog] ThreadWatchdog (3/8 active, 0 timed out)
  [0] main_loop            timeout=1000ms  last_beat=12ms_ago  OK
  [1] sensor_thread        timeout=500ms   last_beat=45ms_ago  OK
  [2] comm_thread          timeout=2000ms  last_beat=1501ms_ago  TIMEOUT

osp> osp_sysmon
[osp_sysmon] SystemMonitor
  CPU:  total=15%  user=10%  sys=5%  iowait=0%
  Temp: 42.3 C
  Mem:  total=1048576kB  avail=524288kB  used=50%
  Disk[0]: total=16106127360B  avail=8053063680B  used=50%
```

---

## 7. 零侵入桥接模式

`shell_commands.hpp` 的设计原则: 模块 (bus, watchdog, config...) 完全不知道 Shell 的存在. 桥接文件单向依赖:

```
shell_commands.hpp ──include──> shell.hpp
                   ──include──> bus.hpp, watchdog.hpp, config.hpp, ...

bus.hpp ──X──> shell.hpp  (bus 不依赖 shell)
```

不需要 Shell 的场景 (如 MCU 移植), 不 include shell_commands.hpp 即可, 零开销.

注册实现模式:

```cpp
template <typename WatchdogType>
inline void RegisterWatchdog(WatchdogType& wd) {
  // (1) 静态局部指针: 程序生命周期有效, 零堆分配
  static WatchdogType* s_wd = &wd;

  // (2) 无捕获 lambda: +cmd 转为函数指针
  static auto cmd = [](int /*argc*/, char* /*argv*/[]) -> int {
    ShellPrintf("[osp_watchdog] ...\r\n");
    s_wd->ForEachSlot([](const WatchdogSlotInfo& info) {
      ShellPrintf("  [%u] %-20s ...\r\n", info.slot_id, info.name);
    });
    return 0;
  };

  // (3) 注册到全局命令表
  GlobalCmdRegistry::Instance().Register("osp_watchdog", +cmd, "...");
}
```

用户代码:

```cpp
#include "osp/shell_commands.hpp"

// 按需注册, 不需要的命令不注册 = 零开销
osp::shell_cmd::RegisterWatchdog(watchdog);
osp::shell_cmd::RegisterFaults(collector);
osp::shell_cmd::RegisterLog();
osp::shell_cmd::RegisterConfig(config);
osp::shell_cmd::RegisterBusStats(bus);
osp::shell_cmd::RegisterLifecycle(lifecycle_node);
```

---

## 8. 无硬件测试

Shell 的 I/O 抽象使得测试不依赖物理硬件:

### 8.1 pipe(2) 模拟 ConsoleShell

```cpp
TEST_CASE("ConsoleShell executes help command") {
    int cmd_pipe[2], out_pipe[2];
    ::pipe(cmd_pipe);   // Shell 从 cmd_pipe[0] 读命令
    ::pipe(out_pipe);   // Shell 向 out_pipe[1] 写输出

    osp::ConsoleShell::Config cfg;
    cfg.read_fd = cmd_pipe[0];
    cfg.write_fd = out_pipe[1];
    cfg.raw_mode = false;  // pipe 不需要 termios

    osp::ConsoleShell shell(cfg);
    shell.Start();

    ::write(cmd_pipe[1], "help\n", 5);  // 注入命令
    // 从 out_pipe[0] 读输出, 验证包含 "help"
}
```

### 8.2 openpty() 模拟 UartShell

```cpp
TEST_CASE("UartShell executes via PTY") {
    int master_fd, slave_fd;
    ::openpty(&master_fd, &slave_fd, nullptr, nullptr, nullptr);

    osp::UartShell::Config cfg;
    cfg.override_fd = slave_fd;  // Shell 使用 PTY slave 端
    osp::UartShell shell(cfg);
    shell.Start();

    ::write(master_fd, "osp_bus\n", 8);  // 通过 master 注入命令
    // 从 master_fd 读输出
}
```

### 8.3 命令回调: MockSession

控制命令测试使用 pipe-backed MockSession 捕获 ShellPrintf 输出:

```cpp
struct MockSession {
  osp::detail::ShellSession session{};
  int capture_read_fd, capture_write_fd;  // pipe pair

  MockSession() {
    int pipefd[2];
    ::pipe(pipefd);
    capture_read_fd = pipefd[0];
    capture_write_fd = pipefd[1];
    session.write_fd = capture_write_fd;
    session.write_fn = osp::detail::ShellPosixWrite;
    // ...
  }

  std::string DrainOutput() { /* read from capture_read_fd */ }
};

// RAII guard: 设置/清除 thread-local CurrentSession
struct SessionGuard {
  explicit SessionGuard(MockSession& m) {
    osp::detail::CurrentSession() = &m.session;
  }
  ~SessionGuard() { osp::detail::CurrentSession() = nullptr; }
};
```

测试示例:

```cpp
TEST_CASE("osp_log level debug sets DEBUG") {
    osp::shell_cmd::RegisterLog();
    MockSession mock;
    SessionGuard guard(mock);

    const auto* cmd = GlobalCmdRegistry::Instance().Find("osp_log");
    char arg0[] = "osp_log", arg1[] = "level", arg2[] = "debug";
    char* argv[] = {arg0, arg1, arg2};
    int rc = cmd->func(3, argv);
    CHECK(rc == 0);
    CHECK(osp::log::GetLevel() == osp::log::Level::kDebug);
}
```

测试覆盖: 762 tests, ASan + UBSan clean.

---

## 9. 资源开销

| 组件 | 栈 | 堆 | 线程 | 说明 |
|------|-----|-----|------|------|
| GlobalCmdRegistry (64 槽) | ~2 KB | 0 | 0 | Meyer's 单例 |
| DebugShell (2 连接) | ~1 KB | ~4 KB | 3 | accept + 2 session |
| ConsoleShell | ~300 B | 0 | 1 | 单会话 |
| UartShell | ~300 B | 0 | 1 | 单会话 |
| 18 个命令回调 | ~150 B | 0 | 0 | 静态局部变量 |
| ShellDispatch 子命令表 | 静态 | 0 | 0 | const 数组 |

ConsoleShell/UartShell 比 DebugShell 少 ~4KB 堆和 2 个线程, 适合资源受限场景.

---

## 10. 经验总结

1. **函数指针是嵌入式 I/O 抽象的最佳平衡点**. 比虚基类轻量, 比模板灵活, 对非热路径场景足够

2. **thread-local 是 Shell 会话路由的自然选择**. 每个会话独立线程, 天然隔离, 无需传参

3. **子命令分发要带 default_fn**. `osp_bus` 无参数时仍显示统计, 与升级前行为一致, 用户无感知升级

4. **运行时修改仅限内存**. 现场调试改配置/日志级别不应永久影响设备, 重启恢复原值是刻意的安全边界

5. **零侵入桥接优于修改模块接口**. 模块不知道 Shell 存在, 不需要 Shell 时零开销, 移植到无 Shell 平台零改动

6. **pipe(2) 和 openpty() 是测试 I/O 的利器**. 不需要物理硬件也能测试完整的 Shell 交互流程

---

## 参考

- newosp Shell 命令参考: [docs/shell_commands_zh.md](https://github.com/DeguiLiu/newosp/blob/main/docs/shell_commands_zh.md)
- newosp 项目: https://github.com/DeguiLiu/newosp
- RT-Thread FinSH/MSH: Shell 引擎灵感来源 (ShellSplit 分词器)

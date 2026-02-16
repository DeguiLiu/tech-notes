---
title: "嵌入式 C++17 调试 Shell: 从 TCP-only 到多后端统一架构"
date: 2026-02-15
draft: false
categories: ["blog"]
tags: ["ARM", "C++17", "LiDAR", "MISRA", "RTOS", "callback", "embedded", "lock-free", "memory-pool", "message-bus", "newosp", "serial", "state-machine"]
summary: "工业嵌入式系统在实验室、早期调试、现场部署、CI 测试等不同阶段，调试环境差异巨大。newosp 的 DebugShell 原本只支持 TCP telnet，本文介绍如何将其扩展为 TCP/串口/stdin/管道四后端统一架构，一套命令在所有环境下可用。"
ShowToc: true
TocOpen: true
---

> [newosp](https://github.com/DeguiLiu/newosp) 项目地址
>
> 相关文章:
> - [newosp 深度解析: C++17 事件驱动架构](../newosp_event_driven_architecture/) -- newosp 框架的核心架构与全景

---

## 1. 问题: 没网的嵌入式设备怎么调试?

工业嵌入式系统 (激光雷达、机器人控制器、边缘计算) 在开发和部署的不同阶段, 调试环境差异很大:

- **实验室**: 设备有网络, telnet/SSH 随意连
- **早期调试**: Zynq-7000 开发板刚启动, 网络驱动还没就绪, 只有串口
- **现场部署**: SSH 能连上, 但设备没装 telnet 客户端
- **CI 测试**: 需要管道输入命令, 自动验证输出

newosp 的调试 Shell (DebugShell) 原本只支持 TCP telnet. 这意味着在上述后三种场景下, 整套 15 个内置诊断命令完全无法使用. 对于排查线上故障、早期硬件调试来说, 这是一个实际的可用性问题.

本文介绍如何通过函数指针 I/O 抽象, 在不修改任何现有命令的前提下, 让同一套 Shell 引擎同时支持 TCP、stdin/stdout、UART 三种后端.

---

## 2. 设计约束

在嵌入式 C++17 项目中做 I/O 抽象, 需要在几个方案之间取舍:

### 2.1 三种抽象方案对比

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| **虚基类** `IShellBackend` | OOP 语义清晰, 运行时多态 | vtable 开销, 违反 "不优先 virtual" 原则 | 通用框架 |
| **模板参数** `DebugShellT<Backend>` | 编译期绑定, 可内联 | Printf 类型不统一, `DebugShellT<Tcp>::Printf` vs `DebugShellT<Uart>::Printf` | 单后端项目 |
| **函数指针** | 零 vtable, 运行时可选, 类型统一 | 无法内联 (但 Shell I/O 不是热路径) | 嵌入式多后端 |

对于调试 Shell 这个场景:
- I/O 操作 (read/write) 不是热路径, 无法内联的代价可忽略
- 命令回调中的 `Printf` 需要类型统一 -- 不能让用户的命令回调依赖于后端模板参数
- 运行时可选 (如 `--console` 命令行参数) 是实际需求

最终选择 **函数指针方案**.

### 2.2 核心约束

- 不改变 `OSP_SHELL_CMD` 宏、`int (*)(int, char*[])` 命令签名、`DebugShell::Printf()` API
- 不引入动态分配 (零堆分配)
- 保持 `-fno-exceptions -fno-rtti` 兼容
- 所有后端共享同一个 GlobalCmdRegistry (64 槽 Meyer's 单例)

---

## 3. 核心实现

### 3.1 I/O 函数指针

```cpp
namespace detail {

// 写函数: 参数与 POSIX write(2) 一致
using ShellWriteFn = ssize_t (*)(int fd, const void* buf, size_t len);
// 读函数: 参数与 POSIX read(2) 一致
using ShellReadFn  = ssize_t (*)(int fd, void* buf, size_t len);

// TCP 后端: send() with MSG_NOSIGNAL (避免 SIGPIPE)
inline ssize_t ShellTcpWrite(int fd, const void* buf, size_t len) {
  return ::send(fd, buf, len, MSG_NOSIGNAL);
}
inline ssize_t ShellTcpRead(int fd, void* buf, size_t len) {
  return ::recv(fd, buf, len, 0);
}

// POSIX 后端: write()/read() 适用于 stdin、stdout、UART 设备
inline ssize_t ShellPosixWrite(int fd, const void* buf, size_t len) {
  return ::write(fd, buf, len);
}
inline ssize_t ShellPosixRead(int fd, void* buf, size_t len) {
  return ::read(fd, buf, len);
}

}  // namespace detail
```

为什么不用 `std::function` 或 `FixedFunction`? 因为 Shell I/O 函数是无状态的 (不需要捕获), 原始函数指针是最轻量的选择, 也符合 MISRA C++ 偏好.

### 3.2 统一会话结构

所有后端共享同一个 ShellSession:

```cpp
struct ShellSession {
  int read_fd = -1;               // 读取文件描述符
  int write_fd = -1;              // 写入文件描述符
  std::thread thread;
  char line_buf[128] = {};        // 命令行缓冲区 (栈分配)
  uint32_t line_pos = 0;
  std::atomic<bool> active{false};

  ShellWriteFn write_fn = nullptr;  // <-- 后端特定写函数
  ShellReadFn  read_fn  = nullptr;  // <-- 后端特定读函数
  bool telnet_mode = false;         // TCP 需要特殊的 \r\n 处理
};
```

关键设计点:
- `read_fd` 和 `write_fd` 分开, 因为 stdin 和 stdout 是不同的 fd
- `telnet_mode` 区分 TCP (发送 `\r\n`) 和 POSIX (发送 `\n` 或 `\r`)
- 128 字节 line_buf 栈分配, 足够嵌入式命令行

### 3.3 共享会话循环

从 DebugShell::SessionLoop 提取出 `ShellRunSession()` 自由函数, 所有后端复用:

```
ShellRunSession(session, running_flag, prompt):
  1. 通过 session.write_fn 写入提示符
  2. 逐字符读取 (session.read_fn):
     - '\r'/'\n' -> 分词 (ShellSplit) + 查表 (GlobalCmdRegistry::Find) + 执行
     - 0x7F/0x08 -> 退格
     - 0x03      -> Ctrl-C 清行
     - '\t'      -> Tab 补全 (AutoComplete)
     - 普通字符  -> 追加到 line_buf + 回显
  3. 循环直到 running=false 或 read 返回 <= 0
```

命令执行前设置 `thread-local CurrentSession()`, 命令执行后清空. 这样 `Printf()` 始终路由到正确的后端:

```
cmd->func(argc, argv)
  -> DebugShell::Printf("...")
    -> CurrentSession()->write_fn(write_fd, buf, len)
       // TCP 后端: send()
       // Console 后端: write(STDOUT_FILENO, ...)
       // UART 后端: write(uart_fd, ...)
```

### 3.4 三个 Shell 后端

#### DebugShell (TCP telnet, 原有后端)

```cpp
class DebugShell final {
  struct Config {
    uint16_t port = 5090;
    uint32_t max_connections = 2;
    const char* prompt = "osp> ";
  };
  // AcceptLoop: listen + accept + 启动 session 线程
  // SessionLoop: 委托给 ShellRunSession() + TCP close
};
```

与原有行为 100% 兼容. `using DebugShell = ...` 类型别名不变.

#### ConsoleShell (stdin/stdout)

```cpp
class ConsoleShell final {
  struct Config {
    const char* prompt = "osp> ";
    int read_fd = -1;    // -1 = STDIN_FILENO
    int write_fd = -1;   // -1 = STDOUT_FILENO
    bool raw_mode = true; // 设置 termios 非规范模式
  };
};
```

- 单会话, 单线程, Start() 启动后台线程
- `raw_mode=true` 时关闭 ICANON 和 ECHO (逐字符读取, 无回显)
- Stop() 时恢复原始 termios
- `read_fd/write_fd` 可覆盖为 pipe fd (测试用)

#### UartShell (UART 串口)

```cpp
class UartShell final {
  struct Config {
    const char* device = "/dev/ttyS0";
    uint32_t baudrate = 115200;
    const char* prompt = "osp> ";
    int override_fd = -1;  // 测试用: PTY fd
  };
};
```

- `::open(device, O_RDWR | O_NOCTTY)` 打开串口
- `cfmakeraw()` + 配置波特率 (支持 9600~921600)
- 单会话, 单线程, 与 ConsoleShell 结构类似
- `override_fd` 可设为 PTY fd (测试用)

---

## 4. 15 个内置诊断命令

`shell_commands.hpp` 是一个零侵入桥接文件. 它包含 shell.hpp 和各模块头文件, 通过模板 Register 函数将模块运行时状态暴露为 shell 命令. 模块本身不依赖 shell.

### 4.1 实现模式

```cpp
template <typename WatchdogType>
inline void RegisterWatchdog(WatchdogType& wd) {
  static WatchdogType* s_wd = &wd;
  static auto cmd = [](int, char*[]) -> int {
    DebugShell::Printf("[osp_watchdog] ...\r\n");
    s_wd->ForEachSlot([](const WatchdogSlotInfo& info) {
      DebugShell::Printf("  [%u] %-20s timeout=%lums ...\r\n", ...);
    });
    return 0;
  };
  osp::detail::GlobalCmdRegistry::Instance().Register(
      "osp_watchdog", +cmd, "Show thread watchdog status");
}
```

关键点:
- `static WatchdogType* s_wd = &wd` -- 静态局部变量捕获对象指针, 无堆分配
- `+cmd` -- 将无捕获 lambda 转换为函数指针 (C++ 标准保证)
- 模板函数 -- 编译期绑定具体类型, 无虚函数

### 4.2 按架构层分组

| 层 | 命令 | 输出内容 |
|----|------|----------|
| 可靠性 | `osp_watchdog` | 各 slot 名称/超时/心跳/是否超时 |
| 可靠性 | `osp_faults` | 统计/队列使用率/最近 N 条故障 |
| 通信 | `osp_bus` | published/dropped/backpressure |
| 通信 | `osp_pool` | dispatched/processed/queue_full |
| 网络 | `osp_transport` | lost/reordered/loss_rate |
| 网络 | `osp_serial` | frames/bytes/errors 全量统计 |
| 服务 | `osp_nodes` | HSM 节点状态/心跳 |
| 服务 | `osp_nodes_basic` | 节点连接状态 |
| 服务 | `osp_service` | 服务 HSM 状态 |
| 服务 | `osp_discovery` | 发现状态/丢失节点数 |
| 应用 | `osp_lifecycle` | 生命周期状态 (粗+细) |
| 应用 | `osp_qos` | QoS 配置各字段 |
| 应用 | `osp_app` | 应用名/实例数/待处理消息 |
| 基础 | `osp_sysmon` | CPU/温度/内存/磁盘 |
| 基础 | `osp_mempool` | 容量/已用/空闲 |

这 15 个命令覆盖了嵌入式系统调试的主要需求: 系统健康 (sysmon)、通信状态 (bus/transport)、服务状态 (nodes/service)、应用状态 (lifecycle/app)、故障诊断 (faults/watchdog).

---

## 5. 无硬件测试方案

嵌入式调试 Shell 的测试难点: ConsoleShell 需要 stdin/stdout, UartShell 需要串口设备. 在 CI 环境中这些都不可用. 解决方案:

### 5.1 ConsoleShell: pipe(2) 模拟

```cpp
TEST_CASE("ConsoleShell executes help command") {
    int cmd_pipe[2], out_pipe[2];
    ::pipe(cmd_pipe);  // cmd_pipe[0]=read, cmd_pipe[1]=write
    ::pipe(out_pipe);  // out_pipe[0]=read, out_pipe[1]=write

    osp::ConsoleShell::Config cfg;
    cfg.read_fd = cmd_pipe[0];   // Shell 从 pipe 读取命令
    cfg.write_fd = out_pipe[1];  // Shell 向 pipe 写入输出
    cfg.raw_mode = false;        // pipe 不需要 termios

    osp::ConsoleShell shell(cfg);
    shell.Start();

    // 发送命令
    const char* cmd = "help\n";
    ::write(cmd_pipe[1], cmd, strlen(cmd));

    // 从 out_pipe[0] 读取输出, 验证包含 "help" 关键字
    char buf[1024];
    ssize_t n = ::read(out_pipe[0], buf, sizeof(buf) - 1);
    buf[n] = '\0';
    REQUIRE(strstr(buf, "help") != nullptr);
}
```

### 5.2 UartShell: openpty() PTY 模拟

```cpp
TEST_CASE("UartShell executes command via PTY") {
    int master_fd, slave_fd;
    ::openpty(&master_fd, &slave_fd, nullptr, nullptr, nullptr);

    osp::UartShell::Config cfg;
    cfg.override_fd = slave_fd;  // Shell 使用 PTY slave 端

    osp::UartShell shell(cfg);
    shell.Start();

    // 通过 master_fd 发送命令
    const char* cmd = "help\n";
    ::write(master_fd, cmd, strlen(cmd));

    // 从 master_fd 读取输出
    // ...
}
```

PTY (pseudo-terminal) 的行为与真实串口一致, 但不需要物理硬件.

---

## 6. 使用示例

### 6.1 运行时选择后端

```cpp
#include "osp/shell.hpp"
#include "osp/shell_commands.hpp"

int main(int argc, char* argv[]) {
    // 注册内置诊断命令
    osp::shell_cmd::RegisterBusStats(bus);
    osp::shell_cmd::RegisterWatchdog(watchdog);
    osp::shell_cmd::RegisterFaults(fault_collector);

    // 根据命令行参数选择后端
    bool use_console = HasFlag(argc, argv, "--console");

    osp::ConsoleShell console_shell;
    osp::DebugShell::Config tcp_cfg;
    tcp_cfg.port = 5092;
    osp::DebugShell tcp_shell(tcp_cfg);

    if (use_console) {
        console_shell.Start();  // stdin/stdout 交互
    } else {
        tcp_shell.Start();      // telnet localhost 5092
    }

    // ... 应用主循环 ...

    if (use_console) {
        console_shell.Stop();
    } else {
        tcp_shell.Stop();
    }
}
```

### 6.2 典型调试场景

**Zynq-7000 串口调试**:
```
PC$ minicom -D /dev/ttyUSB0 -b 115200
osp> osp_bus
[osp_bus] AsyncBus Statistics
  published:     12345
  dropped:           5
  backpressure:  Normal
osp> osp_watchdog
[osp_watchdog] ThreadWatchdog (2/8 active, 0 timed out)
  [0] main_loop    timeout=500ms  last=12ms  OK
  [1] sensor_read  timeout=100ms  last=3ms   OK
```

**SSH 无 telnet 客户端**:
```
ssh root@device
./my_app --console
osp> osp_faults
[osp_faults] FaultCollector Statistics
  total_reported: 2  total_dropped: 0
```

---

## 7. 资源开销

| 组件 | 栈 | 堆 | 线程 |
|------|-----|-----|------|
| GlobalCmdRegistry (64 槽) | ~2 KB | 0 | 0 |
| DebugShell (2 连接) | ~1 KB | ~4 KB | 3 |
| ConsoleShell | ~300 B | 0 | 1 |
| UartShell | ~300 B | 0 | 1 |
| 15 个诊断命令 | ~120 B | 0 | 0 |

ConsoleShell/UartShell 相比 DebugShell 减少了 ~4KB 堆分配和 2 个线程, 适合资源受限的嵌入式场景.

---

## 8. 经验总结

1. **函数指针是嵌入式 I/O 抽象的最佳平衡点** -- 比虚基类轻量, 比模板参数灵活, 对非热路径场景完全足够

2. **thread-local 是 Shell 会话路由的自然选择** -- 每个会话运行在自己的线程, thread-local 指针天然隔离, 无需传参或全局锁

3. **pipe(2) 和 openpty() 是测试嵌入式 I/O 的利器** -- 不需要物理硬件也能完整测试 Shell 逐字符交互

4. **零侵入桥接比修改模块接口更优** -- `shell_commands.hpp` 不修改任何模块的公共 API, 用户按需 include, 不需要 Shell 的场景零开销

5. **后端切换应该在会话层面而非命令层面** -- 命令回调只需要 `Printf()`, 不关心底层是 TCP、串口还是 stdin. 抽象层在 session 而非 command

---

## 参考

- newosp Shell 设计文档: `docs/design_shell_commands_zh.md`
- RT-Thread FinSH/MSH: Shell 引擎灵感来源 (ShellSplit 分词器)
- newosp 项目: https://github.com/DeguiLiu/newosp

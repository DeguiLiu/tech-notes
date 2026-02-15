# 在嵌入式 Linux 上用 Embedded CLI 打造 RT-Thread MSH 风格的调试命令

> 在 RT-Thread 中，MSH（Micro Shell）是一款即插即用的命令交互工具。Embedded CLI 则是一款无 RTOS 依赖、纯 C 实现的轻量级命令行框架，本文使用该开源组件在 Linux 上实现类似 MSH 功能。

## 1 背景

RT-Thread MSH 具备以下优势：
- 串口、Telnet、USB CDC 即插即用
- 命令注册宏自动加入命令表
- 自动 `help` 列出所有命令
- 支持参数解析、历史记录、Tab 补全

## 2 Embedded CLI 简介

- 语言：纯 C，无第三方依赖
- 功能：命令注册 API、自动 help、参数解析（argc/argv）、历史记录与 Tab 补全
- 输入源：任意字节流（UART、TCP、USB CDC、管道等）
- GitHub: https://github.com/funbiscuit/embedded-cli

## 3 嵌入式 Linux 集成思路

目标是在 `/dev/ttyPS0`（Zynq PS UART）或 `/dev/ttyUSB0` 上运行 CLI，复刻 MSH 的注册与交互体验。

1. 打开并配置串口设备
2. 用宏或 `embeddedCliAddCommand()` 注册业务命令
3. 主循环读取串口字节，交给 CLI 核心处理
4. 可扩展至 TCP（Telnet）、USB CDC 等其他输入源

## 4 命令注册与集成实现

### 4.1 C++ 版 MSH_CMD_EXPORT 宏

```cpp
// msh_cli_export.h
#ifndef MSH_CLI_EXPORT_H
#define MSH_CLI_EXPORT_H

#include "embedded_cli.h"

extern EmbeddedCli *cli;

struct CommandInfo {
    const char*               name;
    const char*               description;
    embeddedCliCommandFunc_t  func;
};

#define MSH_CMD_EXPORT(name, desc)                              \
    int name(int argc, char** argv);                            \
    static void name##_wrapper(EmbeddedCli* c, CliCommand* cmd) {\
        (void)c;                                                \
        /* 注意: 实际 Embedded CLI 使用 cmd->args 字符串 */  \
        /* 此处为简化示例，实际需解析 args 为 argc/argv */    \
        name(cmd->argc, cmd->argv);                            \
    }                                                           \
    const CommandInfo name##_info = {                           \
        #name, desc, name##_wrapper                             \
    };                                                          \
    int name(int argc, char** argv)

#endif
```

### 4.2 串口初始化

```c
#include <termios.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>

int uart_init(const char* dev, speed_t baud) {
    int fd = open(dev, O_RDWR | O_NOCTTY);
    if (fd < 0) {
        perror("open");
        return -1;
    }

    struct termios tty;
    if (tcgetattr(fd, &tty) != 0) {
        perror("tcgetattr");
        close(fd);
        return -1;
    }

    cfsetospeed(&tty, baud);
    cfsetispeed(&tty, baud);

    tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cflag &= ~(PARENB | CSTOPB | CRTSCTS);
    tty.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG);
    tty.c_iflag &= ~(IXON | IXOFF | IXANY);
    tty.c_oflag &= ~OPOST;

    // 阻塞读取，至少读 1 字节
    tty.c_cc[VMIN] = 1;
    tty.c_cc[VTIME] = 0;

    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("tcsetattr");
        close(fd);
        return -1;
    }

    return fd;
}
```

### 4.3 业务命令模块示例

```cpp
// led_commands.cpp
#include "msh_cli_export.h"
#include <iostream>

MSH_CMD_EXPORT(led_on,  "Turn on the LED") {
    std::cout << "LED is now on." << std::endl;
    return 0;
}

MSH_CMD_EXPORT(led_off, "Turn off the LED") {
    std::cout << "LED is now off." << std::endl;
    return 0;
}
```

### 4.4 全局注册与主程序

```cpp
// main.cpp
#include "msh_cli_export.h"
#include <unistd.h>

EmbeddedCli *cli = nullptr;

extern const CommandInfo led_on_info;
extern const CommandInfo led_off_info;

static const CommandInfo* all_cmds[] = {
    &led_on_info, &led_off_info, nullptr
};

void register_all_commands() {
    for (const CommandInfo** p = all_cmds; *p; ++p) {
        // 注意: 实际 Embedded CLI API 使用 embeddedCliAddBinding()
        // 此处为简化示例，实际调用方式可能不同
        embeddedCliAddBinding(cli, {
            (*p)->name,
            (*p)->description,
            true,  // tokenizeArgs
            nullptr,  // context
            (*p)->func
        });
    }
}

int main() {
    int fd = uart_init("/dev/ttyPS0", B115200);
    if (fd < 0) return -1;

    cli = embeddedCliNew(embeddedCliDefaultConfig());
    if (!cli) {
        close(fd);
        return -1;
    }

    register_all_commands();

    char c;
    while (read(fd, &c, 1) > 0) {
        embeddedCliReceiveChar(cli, c);
        embeddedCliProcess(cli);
    }

    embeddedCliFree(cli);
    close(fd);
    return 0;
}
```

## 5 API 适配说明

**重要提示**: 上述代码为简化示例，实际 Embedded CLI API 存在以下差异：

1. **命令注册**: 实际使用 `embeddedCliAddBinding()` 而非 `embeddedCliAddCommand()`
2. **参数传递**: `CliCommand` 结构体使用 `args` 字符串，需自行解析为 `argc/argv`
3. **包装函数**: 需根据实际 API 调整 wrapper 函数的参数解析逻辑

建议参考 Embedded CLI 官方文档进行适配，或使用字符串解析库（如 `strtok`）将 `cmd->args` 转换为 `argc/argv` 格式。

## 6 兼容未来 MSH 设计思路

```
> help
led_on   : Turn on the LED
led_off  : Turn off the LED
help     : show commands

> led_on
LED is now on.
```

## 7 兼容未来 MSH 设计思路

保持与 RT-Thread MSH 同名宏和业务函数签名，未来迁移仅需替换宏定义：

```cpp
#ifdef __LINUX__
  #include "msh_cli_export.h"
#else
  #include <finsh.h>   // RT-Thread 原生 MSH
#endif
```

## 8 总结

- Embedded CLI 可在嵌入式 Linux 上零成本复刻 RT-Thread MSH 的交互体验
- C++ 优化宏设计结合集中注册，保持业务层与 MSH 完全一致
- 未来迁移到 RT-Thread，仅需替换宏定义，业务代码无需改动

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/150989949)

---

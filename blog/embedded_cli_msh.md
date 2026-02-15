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
        name(cmd->argc, cmd->argv);                            \
    }                                                           \
    static const CommandInfo name##_info = {                    \
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

int uart_init(const char* dev, speed_t baud) {
    int fd = open(dev, O_RDWR | O_NOCTTY);
    struct termios tty;
    tcgetattr(fd, &tty);
    cfsetospeed(&tty, baud);
    cfsetispeed(&tty, baud);

    tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cflag &= ~(PARENB | CSTOPB | CRTSCTS);
    tty.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG);
    tty.c_iflag &= ~(IXON | IXOFF | IXANY);
    tty.c_oflag &= ~OPOST;

    tcsetattr(fd, TCSANOW, &tty);
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
        embeddedCliAddCommand(cli, (*p)->name, (*p)->description, (*p)->func);
    }
}

int main() {
    int fd = uart_init("/dev/ttyPS0", B115200);
    cli = embeddedCliNew(embeddedCliDefaultConfig());
    if (!cli) return -1;

    register_all_commands();

    char c;
    while (read(fd, &c, 1) > 0) {
        embeddedCliReceiveChar(cli, c);
        embeddedCliProcess(cli);
    }

    embeddedCliFree(cli);
    return 0;
}
```

## 5 效果展示

```
> help
led_on   : Turn on the LED
led_off  : Turn off the LED
help     : show commands

> led_on
LED is now on.
```

## 6 兼容未来 MSH 设计思路

保持与 RT-Thread MSH 同名宏和业务函数签名，未来迁移仅需替换宏定义：

```cpp
#ifdef __LINUX__
  #include "msh_cli_export.h"
#else
  #include <finsh.h>   // RT-Thread 原生 MSH
#endif
```

## 7 总结

- Embedded CLI 可在嵌入式 Linux 上零成本复刻 RT-Thread MSH 的交互体验
- C++ 优化宏设计结合集中注册，保持业务层与 MSH 完全一致
- 未来迁移到 RT-Thread，仅需替换宏定义，业务代码无需改动

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/150989949)

---

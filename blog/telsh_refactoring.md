# telsh: 从 boost::asio 到纯 POSIX -- 嵌入式 Telnet 调试 Shell 的重构实践

## 背景

在嵌入式 Linux 产品开发中，telnet 调试 shell 是一个常见需求：通过网络连接到设备，执行诊断命令、查看运行状态、修改配置参数。本文为 C++17 header-only 纯 POSIX 实现的过程，最终产物是 [telsh](https://gitee.com/liudegui/telsh) 项目。


## 方案

### 设计原则

- C++17，零 boost 依赖，纯 POSIX socket
- Header-only，方便嵌入式项目集成
- 零堆分配（固定容量数组），适合资源受限环境
- 每个 session 独立状态，无全局变量

### 架构

```
telsh/
  include/
    osp/                        -- 复用自 newosp 项目
      platform.hpp              -- 平台检测、断言
      vocabulary.hpp            -- FixedFunction、FixedString
      log.hpp                   -- 日志宏
    telsh/
      command_registry.hpp      -- 命令注册 (64 条, 零堆分配)
      telnet_session.hpp        -- 会话管理 (IAC/认证/历史)
      telnet_server.hpp         -- 服务器 (固定 session 池)
  tests/                        -- Catch2 测试 (28 cases)
  examples/                     -- 示例
```

### 关键设计决策

#### 1. 统一命令签名

```cpp
//统一函数指针签名
using CmdFn = int (*)(int argc, char* argv[], void* ctx);
```

所有命令参数都是字符串 (argv)，由命令自行解析类型。`void* ctx` 传递用户上下文，替代成员函数绑定。编译期类型安全，零堆分配。

注册接口：

```cpp
// 自由函数
registry.Register("reboot", "Reboot device", my_reboot_fn);

// 带上下文的回调 (替代成员函数绑定)
Counter counter;
registry.Register("count", "Show counter", count_fn, &counter);

// 宏自动注册
TELSH_CMD(hello, "Print greeting") {
    (void)ctx;
    telsh::TelnetServer::Printf("Hello, %s!\r\n", argv[1]);
    return 0;
}
```

#### 2. Per-session IAC 状态机

```cpp
class TelnetSession {
  // IAC 状态是成员变量，不是全局 static
  enum class IacPhase : uint8_t { kNormal, kIac, kNego, kSub };
  struct IacState {
    IacPhase phase = IacPhase::kNormal;
    uint8_t prev_byte = 0;
  };
  IacState iac_;  // 每个 session 独立
};
```

#### 3. 固定 session 池替代裸 new

```cpp
class TelnetServer {
  static constexpr uint32_t kMaxSessions = 8;
  struct SessionSlot {
    TelnetSession session;
    std::thread thread;
    std::atomic<bool> active{false};
  };
  SessionSlot slots_[kMaxSessions];  // 栈上固定数组
};
```

- 不动态分配，session 数量编译期确定
- `std::thread` joinable（不 detach），生命周期可控
- `Stop()` 时关闭 socket 解除 `recv()` 阻塞，然后 join 所有线程

#### 4. 命令行原地解析

```cpp
// ShellSplit: 原地修改 cmdline，返回 argc/argv
// 支持单引号、双引号
inline int ShellSplit(char* cmdline, char* argv[], int max_args);
```

不分配内存，不依赖 `boost::split`，直接在输入缓冲区上操作。


## 从 newosp 复用的组件

telsh 从 [newosp](https://gitee.com/liudegui/newosp) 项目拷贝了 3 个 header-only 文件作为基础设施：

- `platform.hpp` (169 行) -- 平台检测、`OSP_ASSERT` 宏、编译器提示
- `vocabulary.hpp` (858 行) -- `FixedFunction`、`FixedString`、`ScopeGuard`
- `log.hpp` (391 行) -- `OSP_LOG_INFO/WARN/ERROR` 日志宏

这些文件无外部依赖，可以独立使用。这也是 newosp header-only 设计的优势：任何模块都可以单独拷贝到其他项目中使用。

## 使用示例

```cpp
#include "telsh/telnet_server.hpp"

// 用宏注册命令
TELSH_CMD(hello, "Print greeting") {
    telsh::TelnetServer::Printf("Hello!\r\n");
    return 0;
}

int main() {
    telsh::ServerConfig config;
    config.port = 2500;
    config.username = "admin";
    config.password = "1234";

    telsh::TelnetServer server(
        telsh::CommandRegistry::Instance(), config);
    server.Start();

    // ... 主循环 ...
    server.Stop();
}
```

```bash
$ telnet 127.0.0.1 2500
username: admin
password: ****
Login OK.
telsh> help
Available commands:
  hello            - Print greeting
telsh> hello
Hello!
telsh> exit
Bye.
```


项目地址：
- Gitee: https://gitee.com/liudegui/telsh
- GitHub: https://github.com/DeguiLiu/telsh

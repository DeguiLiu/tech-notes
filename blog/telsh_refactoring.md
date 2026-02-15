# telsh: 从 boost::asio 到纯 POSIX -- 嵌入式 Telnet 调试 Shell 的重构实践

## 背景

在嵌入式 Linux 产品开发中，telnet 调试 shell 是一个常见需求：通过网络连接到设备，执行诊断命令、查看运行状态、修改配置参数。很多团队的做法是基于 boost::asio 快速搭建一个 telnet server，配合 boost::any 做命令注册。

这种方案在 PC 端开发时没什么问题，但部署到嵌入式平台后，boost 的体积和编译时间成为痛点。本文记录了将一个 ~1050 行的 boost 依赖 telnet-server 重构为 C++17 header-only 纯 POSIX 实现的过程，最终产物是 [telsh](https://gitee.com/liudegui/telsh) 项目。

## 原始代码的问题

原始 telnet-server 基于 C++11 + boost，约 1050 行代码，存在以下问题：

### 1. boost 依赖过重

```
boost::asio      -- TCP 服务器
boost::any       -- 命令回调类型擦除
boost::algorithm -- 字符串分割
boost::lexical_cast -- 类型转换
```

对一个嵌入式调试工具来说，引入整个 boost 得不偿失。

### 2. 内存安全缺陷

```cpp
// 原始代码: 裸 new + detach 线程
TelnetSession* new_session = new TelnetSession(io_service_, regist_);
std::thread([new_session]() { new_session->start(); }).detach();
```

`new` 出来的 session 对象，只在 accept 失败时 `delete`。正常断开连接后，session 对象泄漏。`detach()` 的线程无法 join，生命周期不可控。

### 3. 全局状态导致多 session 互相干扰

```cpp
// 原始代码: IAC 状态机使用全局 static 变量
static int32_t seen_iac = 0;
static enum TEL_STATE state = TEL_NORMAL;
```

当多个 telnet 客户端同时连接时，它们共享同一个 IAC 解析状态，导致协议解析错乱。

### 4. 命令注册类型不安全

```cpp
// 原始代码: boost::any + 运行时参数数量匹配
std::map<std::string, std::tuple<boost::any, uint8_t>> func_map_;

// 调用时按参数数量硬编码分发
if (args_num == 1) regist_->trigger(cmd_key);
else if (args_num == 2) regist_->trigger(cmd_key, args_vec[1]);
else if (args_num == 3) regist_->trigger(cmd_key, args_vec[1], args_vec[2]);
// ... 最多支持 5 个参数
```

运行时 `any_cast` 失败会抛异常，参数数量不匹配返回错误码但无编译期检查。

## 重构方案

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

#### 1. 统一命令签名替代 boost::any

```cpp
// 旧: std::function<void(Args...)> + boost::any 类型擦除
// 新: 统一函数指针签名
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

## 对比

| 维度 | 旧 (telnet-server) | 新 (telsh) |
|------|-------------------|------------|
| 语言标准 | C++11 | C++17 |
| 依赖 | boost (asio/any/algorithm) | 纯 POSIX |
| 代码量 | ~1050 行 (14 文件) | ~900 行 (3 个 hpp) |
| 堆分配 | new TelnetSession, std::map, std::string | 零 |
| Session 管理 | 裸 new + detach | 固定池 + joinable thread |
| IAC 状态 | 全局 static (多 session 冲突) | per-session 成员变量 |
| 命令注册 | boost::any + 运行时匹配 | 函数指针 + void* ctx |
| 参数限制 | 最多 5 个 (硬编码) | 无限制 (argc/argv) |
| 测试 | 无 | 28 Catch2 test cases |
| 编译时间 | 慢 (boost 头文件) | 快 (header-only, 无外部依赖) |

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

## 总结

这次重构的核心收获：

1. 嵌入式场景下，boost 往往是过度设计。POSIX socket API 足够简洁，不需要 asio 的抽象层。
2. `void* ctx` + 函数指针是嵌入式 C++ 中替代 `std::function` + 类型擦除的实用模式，零开销且类型安全。
3. 固定容量数组 + 编译期确定的资源上限，比动态分配更适合嵌入式环境。
4. Header-only 设计让模块可以在项目间自由复用，不需要链接库。

项目地址：
- Gitee: https://gitee.com/liudegui/telsh
- GitHub: https://github.com/DeguiLiu/telsh

# 现代 C++17 相比 C 的不可替代优势

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/158074805)

> 基于 newosp 工业嵌入式基础设施库的实践总结

## 核心论点

C++17 的模板、variant、constexpr、RAII、强类型系统让编译器在编译期捕获类型不匹配、
内存越界、资源泄漏、未处理错误等问题，同时生成比手写 C 更优的机器码。
C 把这些全部推迟到运行时，靠程序员自觉、代码审查和 sanitizer 事后发现。

---

## 一、类型安全 -- 编译器拒绝类型混用

### 1.1 模板：类型不匹配在编译期报错

```cpp
// C++: 编译器拒绝不在 variant 中的类型
AsyncBus<std::variant<SensorData, MotorCmd>> bus;
bus.Subscribe<GpsData>(handler);  // 编译错误: GpsData 不是合法消息类型
```

```c
// C: 编译通过，运行时静默错误
subscribe(bus, GPS_TAG, handler);  // tag 写错 -> 把 SensorData 按 GpsData 解释
                                    // 编译器无任何警告
```

编译器在模板实例化时验证 `GpsData` 是否属于 `variant` 类型列表。
C 的 `void*` 让编译器对类型一无所知。

### 1.2 variant + visit：未处理的类型分支在编译期报错

```cpp
// C++: 忘处理 SystemStatus -> 编译错误
std::visit(overloaded{
    [](const SensorData& d) { process(d); },
    [](const MotorCmd& d)   { execute(d); },
    // 缺少 SystemStatus -> 编译器报错，不会生成二进制
}, payload);
```

```c
// C: 缺少 case -> 运行时静默丢消息
switch (msg->tag) {
    case SENSOR: handle_sensor(msg->data); break;
    case MOTOR:  handle_motor(msg->data);  break;
    // 忘了 STATUS -> 消息丢失，可能运行数天才发现
}
```

新增消息类型时，C++ 在所有未更新的 visit 处报编译错误，强制补全。
C 的 `-Wswitch` 只是警告，经常被忽略。

### 1.3 强类型包装：语义不同的同类型值在编译期区分

```cpp
// C++: NewType 阻止隐式混用
using NodeId  = NewType<uint32_t, struct NodeIdTag>;
using TimerId = NewType<uint32_t, struct TimerIdTag>;

void Remove(TimerId id);
Remove(node_id);  // 编译错误: NodeId 不能隐式转 TimerId
```

```c
// C: typedef 不阻止混用
typedef uint32_t NodeId;
typedef uint32_t TimerId;

void remove(TimerId id);
remove(node_id);  // 编译通过，运行时传错 ID，删错定时器
```

C 的 `typedef` 只是别名，编译器视两者为同一类型。
C++ 的 `NewType<T, Tag>` 创建了真正不同的类型。

### 1.4 enum class：枚举值不泄漏、不隐式转整型

```cpp
// C++: 作用域枚举，编译器阻止隐式转换
enum class Priority : uint8_t { kLow, kMedium, kHigh };
int x = Priority::kLow;       // 编译错误: 不能隐式转 int
if (Priority::kLow == 0) {}   // 编译错误: 不能与 int 比较
```

```c
// C: 枚举值泄漏到全局，可与任意整数混用
enum Priority { LOW, MEDIUM, HIGH };
enum LogLevel { LOW, HIGH };  // 编译错误: LOW/HIGH 重定义
int x = LOW;                  // 编译通过，LOW 就是 int 0
if (LOW == false) {}          // 编译通过，比较毫无意义
```

### 1.5 not_null：空指针解引用在构造期拦截

```cpp
// C++: 类型系统标注"不可能为空"
void Process(not_null<Sensor*> sensor) {
  sensor->Read();  // 调用者保证非空，函数内无需检查
}
Process(nullptr);  // 编译期或构造期断言失败
```

```c
// C: 指针永远可能为空
void process(Sensor* sensor) {
  if (!sensor) return;  // 每个函数都要防御性检查
  sensor->read();       // 忘了检查 -> SIGSEGV
}
```

---

## 二、内存安全 -- 编译器管理资源生命周期

### 2.1 RAII：资源泄漏在结构上不可能发生

```cpp
// C++: 编译器在每条退出路径自动插入析构
expected<TcpSocket, SocketError> Connect(const char* host) {
  auto fd = ::socket(AF_INET, SOCK_STREAM, 0);
  ScopeGuard guard([fd]{ ::close(fd); });

  if (::connect(fd, ...) < 0) return unexpected(kConnectFailed); // 自动 close
  if (::setsockopt(...) < 0) return unexpected(kOptionFailed);   // 自动 close
  guard.Dismiss();
  return TcpSocket(fd);  // 成功路径，所有权转移给 TcpSocket
}
```

```c
// C: 每条路径手动 close，漏一个就泄漏
int connect_to(const char* host) {
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (connect(fd, ...) < 0) { close(fd); return -1; }
  if (setsockopt(...) < 0) { return -1; }  // 忘了 close -> fd 泄漏
  return fd;                                // 编译器不警告
}
```

关键差异：编译器**自动**在 return、goto 等所有退出路径插入析构函数调用。
C 编译器对资源释放没有任何义务。

### 2.2 FixedVector：越界访问有边界检查，容量在编译期确定

```cpp
// C++: 编译期确定容量，运行时边界检查
FixedVector<SensorData, 256> buffer;
buffer.push_back(data);     // 满了返回 false 或断言
buffer[300];                 // Debug: OSP_ASSERT 失败

static_assert(sizeof(buffer) == sizeof(SensorData) * 256 + /*overhead*/,
              "unexpected size");  // 编译期验证内存布局
```

```c
// C: 数组越界 -> 静默内存损坏
struct SensorData buffer[256];
int count = 0;
buffer[count++] = data;      // count 超过 256 -> 越界写入，破坏栈上其他变量
buffer[300] = data;           // 编译器不警告，运行时踩内存
```

FixedVector 的容量是类型的一部分（`FixedVector<T, 256>` 和 `FixedVector<T, 512>`
是不同类型），编译器可以在编译期验证大小。C 的裸数组没有边界信息。

### 2.3 Move 语义：所有权转移由编译器追踪

```cpp
// C++: 所有权明确转移，源对象进入已知空状态
auto socket = TcpSocket::Connect("host", 8080);
auto socket2 = std::move(socket);  // 所有权转移
socket.Send(data);  // 静态分析工具警告 use-after-move

// C++17 mandatory copy elision: 返回值直接在调用者栈帧构造
auto s = TcpSocket::Connect("host", 8080);  // 保证零拷贝
```

```c
// C: 所有权靠注释和约定
int fd = connect_to("host");
int fd2 = fd;           // 复制了 fd，两处都能 close
close(fd);              // 关闭后 fd2 变成悬空句柄
write(fd2, data, len);  // 写入已关闭的 fd -> 未定义行为
```

C 没有语言级别的所有权概念。`int fd2 = fd` 后，编译器不知道谁负责 close。

### 2.4 FixedFunction SBO：回调闭包零堆分配

```cpp
// C++: 小 lambda 内联存储在对象内部，无堆分配
FixedFunction<void(), 16> callback = [x, y]{ process(x, y); };
// 捕获 <= 16 字节 -> 存储在栈上的 buffer_ 中，无 malloc
```

```c
// C: 闭包需要手动分配 context 结构体
struct ctx { int x; int y; };
struct ctx* c = malloc(sizeof(struct ctx));  // 堆分配
c->x = x; c->y = y;
register_callback(process_wrapper, c);
// 谁负责 free(c)? 回调执行后? 注销时? 容易泄漏或 double-free
```

### 2.5 expected：错误处理由编译器强制检查

```cpp
// C++: 不检查就访问值 -> 断言失败
auto result = pool.CreateChecked(args...);
auto ptr = result.value();  // 未检查 has_value() -> Debug 断言

// 链式处理，编译器追踪每条路径
and_then(result, [](auto* p) { return p->Init(); })
  .or_else([](auto err) { OSP_LOG_ERROR("init", "failed: %d", err); });
```

```c
// C: 返回值被忽略 -> 编译器不警告
void* ptr = pool_alloc(&pool);     // 返回 NULL 表示失败
memcpy(ptr, data, size);           // ptr == NULL -> SIGSEGV
```

---

## 三、编译器优化 -- 类型信息越多，优化越强

### 3.1 if constexpr：编译期消除死分支

```cpp
if constexpr (std::is_trivially_copyable_v<T>) {
  std::memcpy(&buf[pos], &val, sizeof(T));  // POD: 只生成这条
} else {
  new (&buf[pos]) T(std::move(val));         // 非POD: 只生成这条
}
// 二进制中只有一条路径，另一条完全不存在
```

C 只能用 `#ifdef`（无法基于类型属性选择）或运行时 `if`（每次调用都判断）。

### 3.2 模板实例化：每个配置生成专用代码

```cpp
using LightBus = AsyncBus<SmallPayload, 256, 64>;   // 编译器生成版本 A
using HighBus  = AsyncBus<LargePayload, 4096, 256>;  // 编译器生成版本 B
```

编译器对每个版本独立优化：`& (256-1)` 编译为单条 `AND` 指令（立即数），
循环展开针对具体 depth。C 的 `void*` + `size_t` 让编译器丢失常量信息，
地址计算变成运行时乘法。

### 3.3 constexpr：保证编译期求值

```cpp
constexpr auto id = MakeIID(1, 2);  // 编译结果: 立即数 0x00010002
inline constexpr QosProfile kQosSensorData{...};  // 直接嵌入 .rodata
```

C 的 `const` 不是编译期常量。`constexpr` 是编译器合同：
必须在编译期确定，否则报错。

### 3.4 CRTP + static_assert：零 vtable 编译期多态

```cpp
template <typename Derived>
struct NodeBase {
  void Process() {
    static_cast<Derived*>(this)->DoProcess();  // 编译期解析，可内联
  }
};
// DoProcess() 直接内联到调用点，零间接跳转
```

C 的函数指针表：运行时间接调用，阻止内联，分支预测器需要学习跳转目标。

### 3.5 static_assert + 模板参数：配置违规在编译期拦截

```cpp
template <uint32_t QueueDepth>
class AsyncBus {
  static_assert((QueueDepth & (QueueDepth - 1)) == 0,
                "QueueDepth must be power of 2");
  static_assert(QueueDepth >= 16, "Too small");
};

AsyncBus<Payload, 300> bus;  // 编译失败: 300 不是 2 的幂
```

```c
#define QUEUE_DEPTH 300
uint32_t idx = seq & (QUEUE_DEPTH - 1);  // 300 非 2^N，掩码失效
// idx 值完全错误，数据写到错误位置，可能运行数天才崩溃
```

---

## 四、总结

### 什么时候发现 bug

| 错误类型 | C++17 | C |
|---------|-------|---|
| 类型不匹配 | 编译失败 | 运行时崩溃或静默错误 |
| 分支遗漏 | `visit` 编译失败 | 运行时丢消息 |
| 配置违规 | `static_assert` 编译失败 | 运行数天后数据损坏 |
| 资源泄漏 | 结构上不可能 (RAII) | valgrind / 线上 OOM |
| 空指针解引用 | `not_null` 构造期拦截 | SIGSEGV |
| 数组越界 | `FixedVector` 断言 | 栈/堆损坏，难以定位 |
| 错误未处理 | `expected` 断言 | 错误码被忽略 |
| ID 类型混用 | `NewType` 编译失败 | 传错 ID，操作错误对象 |
| 所有权不清 | move 语义 + 分析器警告 | double-free 或悬空指针 |

### 编译器优化差异

| 能力 | C++17 | C11 |
|-----|-------|-----|
| 根据类型属性消除分支 | `if constexpr` | 不可能 |
| 为不同参数生成专用代码 | 模板实例化 | `void*` 阻止特化 |
| 保证编译期求值 | `constexpr` 合同 | `const` 建议 |
| 消除虚函数开销 | CRTP 内联 | 函数指针不可内联 |
| 消除返回值拷贝 | mandatory elision | NRVO 可选 |

### 本质

C++17 让编译器掌握更多信息：模板给类型和常量，`constexpr` 给求值合同，
RAII 给生命周期，`variant` 给完整类型列表，`NewType` 给语义区分。
**信息越多，编译器能做的检查和优化就越多。**

C 的 `void*`、宏、手动 cleanup 在**隐藏信息**。编译器看到的只是一个指针
和一个整数，无法做类型检查、无法追踪资源生命周期、无法生成特化代码。

**C++ 让编译器替你犯更少的错，同时生成更快的代码。C 让你自己负责一切。**

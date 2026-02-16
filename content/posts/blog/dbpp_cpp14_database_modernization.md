---
title: "从 C++03 到 C++14: 数据库抽象层的现代化重写实践"
date: 2026-02-16
draft: false
categories: ["blog"]
tags: ["C++14", "RAII", "move-semantics", "SQLite3", "embedded", "refactoring", "database"]
summary: "以 dbpp 对 DatabaseLayer 的现代化重写为案例，系统展示如何将一个 C++03 风格的数据库封装库改造为符合 MISRA C++ 标准的 C++14 实现。涵盖 RAII 资源管理、move-only 语义替代 const_cast hack、零异常错误处理、零全局状态等关键改造点，附完整前后对比代码。"
ShowToc: true
TocOpen: true
---

将一个能跑的 C++03 项目重写为 C++14，投入是否值得？当原始代码中同时存在裸 new/delete、const_cast 模拟移动语义、全局静态变量、sprintf 缓冲区溢出这四类问题时，答案是明确的: 重写的收益不在于新功能，而在于消除这些定时炸弹。

本文以 [dbpp](https://gitee.com/liudegui/dbpp) 对 [DatabaseLayer](https://gitee.com/liudegui/DatabaseLayer) 的现代化重写为案例，逐一展示 C++14 如何系统性地解决 C++03 遗留问题。

## 1. 原始代码问题诊断

DatabaseLayer 是一个 SQLite3 + MySQL 的统一数据库操作封装，功能完整，但代码风格停留在 C++03 时代。以下是按严重程度排列的问题清单:

| 问题 | 位置 | 严重程度 | 风险 |
|------|------|----------|------|
| 裸 new/delete | Exception, ResultSet, Statement 中 `new std::string`, `new vector` | 高 | 内存泄漏 |
| const_cast 模拟移动语义 | 所有拷贝构造和赋值运算符 | 高 | 未定义行为 |
| 全局静态变量 | `s_DBName`, `s_nValue`, `s_dwValue` | 高 | 线程不安全 |
| sprintf 缓冲区溢出 | `tableExists`, `createDB`, `dropDB` 中的 `char[256]` | 中 | 安全漏洞 |
| `#undef NULL` / `#define NULL 0` | 宏污染全局命名空间 | 中 | 编译问题 |
| 内嵌 sqlite3.c 源码 (7386 行) | 维护负担 | 低 | 版本过时 |
| 虚基类 + `#ifdef` typedef | 设计层面 | 低 | 架构缺陷 |

下面逐一分析每个问题的本质和解决方案。

## 2. 裸 new/delete -> RAII

### 2.1 问题: 资源泄漏风险

```cpp
// DatabaseLayer 原始代码: 裸 new，析构路径不完整
class CppMySQLException {
    CppMySQLException(const char* msg)
        : message_(new std::string(msg)) {}  // 裸 new

    ~CppMySQLException() { delete message_; }  // 需要手动 delete

    // 拷贝构造中有 new，但如果 new 抛异常? -> 泄漏
};

class CppMySQLResultSet {
    CppMySQLResultSet()
        : pszData_(new std::vector<...>()) {}   // 裸 new

    // 如果构造函数中后续操作抛异常，pszData_ 泄漏
};
```

每个 `new` 都是一个潜在的泄漏点。如果异常路径没有覆盖到，或者中间有 return，资源就丢失了。

### 2.2 解决: RAII 管理所有数据库资源

dbpp 的每个类持有一个数据库资源，析构函数自动释放:

```cpp
// dbpp: RAII 管理 sqlite3* 连接
class Sqlite3Db {
 public:
    Sqlite3Db() = default;
    ~Sqlite3Db() { Close(); }  // 析构自动 close

    Error Open(const char* path) {
        Close();  // 先释放旧连接 (幂等)
        int32_t rc = sqlite3_open(path, &db_);
        if (rc != SQLITE_OK) {
            Error err = Error::Make(ErrorCode::kError,
                                    db_ ? sqlite3_errmsg(db_) : "open failed");
            if (db_ != nullptr) {
                sqlite3_close(db_);
                db_ = nullptr;
            }
            return err;
        }
        return Error::Ok();
    }

    void Close() {
        if (db_ != nullptr) {
            sqlite3_close(db_);
            db_ = nullptr;
        }
    }

 private:
    sqlite3* db_ = nullptr;  // 唯一资源
};
```

四个类各自管理一种资源:

| 类 | 持有资源 | 获取方式 | 释放方式 |
|----|----------|----------|----------|
| `Sqlite3Db` | `sqlite3*` | `sqlite3_open()` | `sqlite3_close()` |
| `Sqlite3Query` | `sqlite3_stmt*` | `sqlite3_prepare_v2()` + `sqlite3_step()` | `sqlite3_finalize()` |
| `Sqlite3ResultSet` | `char**` | `sqlite3_get_table()` | `sqlite3_free_table()` |
| `Sqlite3Statement` | `sqlite3_stmt*` | `sqlite3_prepare_v2()` | `sqlite3_finalize()` |

RAII 保证: 无论正常退出还是异常退出，资源一定被释放。零手动 delete。

## 3. const_cast hack -> Move 语义

### 3.1 问题: const_cast 模拟移动

DatabaseLayer 中最危险的模式: 用 `const_cast` 在拷贝构造函数中修改源对象，模拟 C++11 的移动语义:

```cpp
// DatabaseLayer 原始代码: const_cast hack
class CppSQLite3Query {
    CppSQLite3Query(const CppSQLite3Query& rQuery) {
        // 拷贝构造却修改源对象 -> 违反 const 契约
        mpStmt = rQuery.mpStmt;
        const_cast<CppSQLite3Query&>(rQuery).mpStmt = 0;  // 偷走资源

        mbEof = rQuery.mbEof;
        const_cast<CppSQLite3Query&>(rQuery).mbEof = true;  // 修改源对象
    }
};
```

这段代码做的事情和 C++11 移动语义完全一样 (转移资源所有权)，但手段是错误的:

- 违反 `const` 契约 (声明接受 `const&` 却修改了源对象)
- C++ 标准中 `const_cast` 去除底层 const 后修改对象是**未定义行为**
- 编译器可能基于 const 假设做优化，导致不可预测的结果

### 3.2 解决: move-only 语义

dbpp 用标准 C++11 移动语义替代:

```cpp
// dbpp: 标准移动语义
class Sqlite3Query {
 public:
    // 禁止拷贝
    Sqlite3Query(const Sqlite3Query&) = delete;
    Sqlite3Query& operator=(const Sqlite3Query&) = delete;

    // 移动构造: 转移所有权
    Sqlite3Query(Sqlite3Query&& other) noexcept
        : db_(other.db_),
          stmt_(other.stmt_),
          eof_(other.eof_),
          num_fields_(other.num_fields_) {
        other.db_ = nullptr;
        other.stmt_ = nullptr;     // 源对象资源清零
        other.eof_ = true;
        other.num_fields_ = 0;
    }

    // 移动赋值: 先释放自己的资源，再接管
    Sqlite3Query& operator=(Sqlite3Query&& other) noexcept {
        if (this != &other) {
            Finalize();            // 释放当前资源
            db_ = other.db_;
            stmt_ = other.stmt_;
            eof_ = other.eof_;
            num_fields_ = other.num_fields_;
            other.db_ = nullptr;   // 源对象清零
            other.stmt_ = nullptr;
            other.eof_ = true;
            other.num_fields_ = 0;
        }
        return *this;
    }
};
```

关键区别:

| 维度 | const_cast hack | move 语义 |
|------|----------------|-----------|
| 接口声明 | `(const T&)` 接受 const 引用 | `(T&&)` 接受右值引用 |
| 语义 | 声称拷贝，实际移动 | 明确声明移动 |
| 调用方感知 | 不知道源对象被修改 | `std::move()` 显式转移所有权 |
| 编译器保证 | 无 (UB) | 标准行为 |

使用方式对比:

```cpp
// DatabaseLayer: 看起来是拷贝，实际源对象被清空 (惊吓)
CppSQLite3Query q1 = db.execQuery("SELECT * FROM emp;");
CppSQLite3Query q2 = q1;  // q1 被偷走资源，但代码看不出来

// dbpp: 移动语义，所有权转移显式可见
auto q1 = db.ExecQuery("SELECT * FROM emp;");
auto q2 = std::move(q1);  // 明确: q1 不再持有资源
// Sqlite3Query q3 = q1;  // 编译错误: 拷贝被禁止
```

## 4. throw 异常 -> Error 结构体

### 4.1 问题: 异常与嵌入式不兼容

DatabaseLayer 大量使用 `throw`:

```cpp
// DatabaseLayer: throw 异常
void CppSQLite3DB::execDML(const char* szSQL) {
    char* szError = 0;
    int nRet = sqlite3_exec(mpDB, szSQL, 0, 0, &szError);
    if (nRet != SQLITE_OK) {
        throw CppSQLite3Exception(nRet, szError);  // throw
    }
}
```

嵌入式 C++ 项目通常使用 `-fno-exceptions` 编译 (减少二进制大小，消除 unwind 表开销)。throw 在这种环境下直接导致编译失败。

### 4.2 解决: Error 结构体 + 输出参数

dbpp 用 `Error` 结构体替代异常:

```cpp
// 错误码: enum class，固定宽度
enum class ErrorCode : int32_t {
    kOk = 0,
    kError = -1,
    kNotOpen = -2,
    kBusy = -3,
    kConstraint = -5,
    kNullParam = -9,
    // ...
};

// 错误信息: 错误码 + 固定大小消息缓冲区 (无堆分配)
struct Error {
    static constexpr uint32_t kMaxMessageLen = 256;

    ErrorCode code = ErrorCode::kOk;
    char message[kMaxMessageLen] = {};

    bool ok() const { return code == ErrorCode::kOk; }
    explicit operator bool() const { return ok(); }

    static Error Ok() { return Error{}; }
    static Error Make(ErrorCode c, const char* msg = nullptr) {
        Error e;
        e.Set(c, msg);
        return e;
    }
};
```

API 中通过可选的 `Error*` 输出参数报告错误:

```cpp
// dbpp: 错误通过输出参数返回 (可选)
int32_t ExecDml(const char* sql, Error* out_error = nullptr) {
    if (db_ == nullptr) {
        if (out_error != nullptr) {
            out_error->Set(ErrorCode::kNotOpen, "Database not open");
        }
        return -1;
    }

    char* errmsg = nullptr;
    int32_t rc = sqlite3_exec(db_, sql, nullptr, nullptr, &errmsg);
    if (rc == SQLITE_OK) {
        return sqlite3_changes(db_);
    }

    if (out_error != nullptr) {
        out_error->Set(ErrorCode::kError,
                       errmsg ? errmsg : sqlite3_errmsg(db_));
    }
    if (errmsg != nullptr) { sqlite3_free(errmsg); }
    return -1;
}
```

调用方可以选择是否处理错误:

```cpp
// 忽略错误 (简单场景)
db.ExecDml("INSERT INTO emp VALUES(1, 'Alice');");

// 检查错误
dbpp::Error err;
db.ExecDml("INVALID SQL", &err);
if (!err.ok()) {
    std::printf("error %d: %s\n", static_cast<int>(err.code), err.message);
}
```

设计要点:

- `Error` 是值类型，栈分配，无堆内存
- 消息缓冲区固定 256 字节，用 `snprintf` 填充 (无溢出)
- `out_error` 参数默认 nullptr，不关心错误时可以省略
- 兼容 `-fno-exceptions`

## 5. 全局状态 -> 零全局状态

### 5.1 问题: 全局静态变量

DatabaseLayer 中有全局静态变量用于临时数据:

```cpp
// DatabaseLayer: 全局静态变量 (线程不安全)
static char s_DBName[512];    // 全局共享
static int  s_nValue;         // 多线程访问 -> 数据竞争
static DWORD s_dwValue;
```

多线程环境下，两个线程同时操作不同的数据库连接会互相覆盖全局缓冲区。

### 5.2 解决: 状态全部在实例中

dbpp 没有任何全局变量或静态成员变量:

```cpp
// dbpp: 每个连接独立，零全局状态
class Sqlite3Db {
 private:
    sqlite3* db_ = nullptr;  // 所有状态在实例中
};

class Sqlite3Query {
 private:
    sqlite3* db_ = nullptr;
    sqlite3_stmt* stmt_ = nullptr;
    bool eof_ = true;
    int32_t num_fields_ = 0;
    // 无全局变量、无 static 成员
};
```

每个 `Sqlite3Db` 实例独立持有自己的连接，多个实例可以在不同线程中安全使用。

## 6. sprintf -> snprintf

```cpp
// DatabaseLayer: sprintf 缓冲区溢出风险
char szSQL[256];
sprintf(szSQL, "SELECT count(*) FROM sqlite_master "
        "WHERE type='table' AND name='%s'", szTable);
// 如果 szTable 超过 ~200 字符 -> 缓冲区溢出

// dbpp: snprintf 安全写入
char sql[256];
std::snprintf(sql, sizeof(sql),
    "SELECT count(*) FROM sqlite_master "
    "WHERE type='table' AND name='%s'", table);
// sizeof(sql) 限制写入长度，不会溢出
```

## 7. 架构简化: 去掉虚基类

### 7.1 问题: 虚基类 + #ifdef

DatabaseLayer 用虚基类 `DatabaseLayer` 定义统一接口，然后通过 `#ifdef` 选择实现:

```cpp
// DatabaseLayer: 虚基类 + typedef 切换
class DatabaseLayer {
    virtual int execDML(const char* sql) = 0;    // 虚函数开销
    virtual ResultSet execQuery(const char* sql) = 0;
};

#ifdef USE_MYSQL
    typedef CppMySQLDB DatabaseImpl;
#else
    typedef CppSQLite3DB DatabaseImpl;
#endif
```

这种设计同时承担了两种开销: 虚函数调用的运行时开销，和 `#ifdef` 切换的编译时复杂度。而且无法在运行时切换后端。

### 7.2 解决: 具体类直接使用

dbpp 直接提供 `Sqlite3Db` 具体类:

```cpp
// dbpp: 直接使用具体类，无虚函数
dbpp::Sqlite3Db db;
db.Open(":memory:");
db.ExecDml("CREATE TABLE emp(empno INTEGER, empname TEXT);");
auto q = db.ExecQuery("SELECT * FROM emp;");
```

如果未来需要多后端，可以通过模板参数化实现编译期多态:

```cpp
// 未来扩展方向: 模板参数化 (编译期多态, 零虚函数开销)
template <typename Backend>
class Database {
    Backend backend_;
 public:
    auto ExecQuery(const char* sql) { return backend_.ExecQuery(sql); }
};

using SqliteDb = Database<Sqlite3Backend>;
using MysqlDb = Database<MysqlBackend>;
```

## 8. 完整使用示例

### 8.1 基本 CRUD

```cpp
#include "dbpp/sqlite3_db.hpp"

int main() {
    dbpp::Sqlite3Db db;
    db.Open(":memory:");

    // CREATE
    db.ExecDml("CREATE TABLE emp(empno INTEGER, empname TEXT);");

    // INSERT
    db.ExecDml("INSERT INTO emp VALUES(1, 'Alice');");
    db.ExecDml("INSERT INTO emp VALUES(2, 'Bob');");

    // SELECT (前向遍历)
    auto q = db.ExecQuery("SELECT * FROM emp ORDER BY empno;");
    while (!q.Eof()) {
        std::printf("empno=%d empname=%s\n",
                    q.GetInt(0), q.GetString(1));
        q.NextRow();
    }
    q.Finalize();

    // COUNT
    int32_t count = db.ExecScalar("SELECT count(*) FROM emp;");
    std::printf("total: %d\n", count);

    // UPDATE / DELETE
    int32_t updated = db.ExecDml("UPDATE emp SET empname='Boss' WHERE empno=1;");
    int32_t deleted = db.ExecDml("DELETE FROM emp WHERE empno > 5;");

    return 0;  // db 析构自动 close
}
```

### 8.2 预编译语句 + 事务

```cpp
dbpp::Sqlite3Db db;
db.Open(":memory:");
db.ExecDml("CREATE TABLE emp(empno INTEGER, empname TEXT);");

// 事务 + 预编译语句批量插入
db.BeginTransaction();

auto stmt = db.CompileStatement("INSERT INTO emp VALUES(?, ?);");
for (int32_t i = 0; i < 100; ++i) {
    char name[32];
    std::snprintf(name, sizeof(name), "Emp%02d", i);
    stmt.Bind(1, i);         // 1-based 参数索引
    stmt.Bind(2, name);
    stmt.ExecDml();
    stmt.Reset();             // 重置绑定，复用语句
}
stmt.Finalize();

db.Commit();  // 或 db.Rollback()
```

### 8.3 随机访问结果集

```cpp
// GetResultSet: 全量加载到内存，支持 SeekRow() 随机访问
auto rs = db.GetResultSet("SELECT * FROM emp ORDER BY empno;");

// 反向遍历
for (int32_t i = static_cast<int32_t>(rs.NumRows()) - 1; i >= 0; --i) {
    rs.SeekRow(static_cast<uint32_t>(i));
    std::printf("%s | %s\n", rs.FieldValue(0), rs.FieldValue(1));
}
rs.Finalize();
```

### 8.4 Query vs ResultSet 选择

| 维度 | Sqlite3Query (前向) | Sqlite3ResultSet (随机) |
|------|--------------------|-----------------------|
| 内存 | 按需读取 (低) | 全量加载 (高) |
| 访问模式 | 只能前向 Eof()/NextRow() | 支持 SeekRow() 随机访问 |
| 底层 API | `sqlite3_step()` | `sqlite3_get_table()` |
| 适用场景 | 流式处理大结果集 | 需要随机访问或多次遍历 |

## 9. 改造总结

### 9.1 前后对比

| 维度 | DatabaseLayer (C++03) | dbpp (C++14) |
|------|----------------------|--------------|
| 资源管理 | 裸 new/delete | RAII + 析构自动释放 |
| 拷贝语义 | const_cast hack (UB) | 禁止拷贝，仅移动 |
| 错误处理 | throw 异常 | Error 结构体 (无异常) |
| 线程安全 | 全局 static 变量 | 零全局状态 |
| 缓冲区 | sprintf | snprintf |
| 后端切换 | 虚基类 + #ifdef | 具体类 (模板扩展预留) |
| 依赖管理 | 内嵌 7386 行 sqlite3.c | bundled amalgamation + FetchContent |
| 测试 | CppUnit | Catch2 v3 (51 cases, ASan+UBSan) |
| CI | 无 | GitHub Actions (Linux + macOS) |
| 代码规范 | 无 | MISRA C++ / Google Style |

### 9.2 可复用的改造模式

这次改造中应用的模式适用于任何 C++03 -> C++14 迁移:

1. **裸指针 -> RAII**: 每个类管理恰好一种资源，析构函数负责释放
2. **const_cast -> move**: 禁止拷贝 (`= delete`)，实现移动构造和移动赋值
3. **throw -> Error 结构体**: 固定大小值类型，通过输出参数返回，兼容 `-fno-exceptions`
4. **全局 static -> 实例成员**: 所有状态都在对象实例中，零全局变量
5. **sprintf -> snprintf**: 所有格式化输出使用安全版本
6. **内嵌源码 -> FetchContent**: 让 CMake 管理依赖版本

### 9.3 项目信息

- 仓库: [GitHub](https://github.com/DeguiLiu/dbpp) | [Gitee](https://gitee.com/liudegui/dbpp)
- 原始项目: [DatabaseLayer](https://gitee.com/liudegui/DatabaseLayer)
- 设计文档: [docs/design_zh.md](https://gitee.com/liudegui/dbpp/blob/master/docs/design_zh.md)
- 许可证: MIT

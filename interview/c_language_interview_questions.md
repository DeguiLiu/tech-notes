# 中高级软件工程师的 C 语言面试题

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/139711334)

> 30 道精选题目，覆盖内存模型、并发编程、数据结构、编译器行为等 C 语言核心主题。
> 面向嵌入式 Linux / RTOS 方向的中高级工程师。

---

## 一、语言核心

### Q1: volatile 关键字的作用及其应用场景

**答案:**

`volatile` 告诉编译器：每次访问该变量时必须从内存重新读取，不得使用寄存器缓存值，也不得省略看似"无用"的读写操作。

**正确的应用场景:**

| 场景 | 说明 |
|------|------|
| MMIO 硬件寄存器 | `volatile uint32_t *reg = (volatile uint32_t *)0x40021000;` 确保每次读写都到达硬件 |
| 信号处理函数 | 与主程序共享的变量应声明为 `volatile sig_atomic_t` |
| `setjmp`/`longjmp` | `longjmp` 恢复后，非 volatile 局部变量的值不确定 |
| 只读硬件状态寄存器 | `const volatile uint32_t *status` 表示软件只读、硬件可写 |

**常见误区 -- volatile 不适用于多线程同步:**

`volatile` 仅阻止编译器优化（不省略读写、不重排 volatile 访问之间的顺序），但：
- 不保证原子性（64 位变量在 32 位平台上的读写可能被拆成两条指令）
- 不插入 CPU 内存屏障（ARM 等弱内存序架构上，CPU 仍可乱序执行）
- 不提供 happens-before 关系

多线程同步应使用 `_Atomic`（C11）、`pthread_mutex`、或平台特定的内存屏障指令。

> 参考: Linux 内核文档 `Documentation/process/volatile-considered-harmful.rst`

---

### Q2: 内存对齐（Memory Alignment）

**答案:**

内存对齐是指数据在内存中的存储地址按照特定规则排列。大多数处理器要求 N 字节的数据类型存放在 N 的倍数地址上。

**需要对齐的原因:**
- 性能: 未对齐访问在 x86 上需要两次总线事务，在 ARM Cortex-M（未开启非对齐访问）上直接触发 HardFault
- DMA: 许多 DMA 控制器要求源/目标地址按 4 字节或更大边界对齐
- 原子性: 某些平台上只有对齐的访问才是原子的

**示例:**

```c
#include <stdio.h>
#include <stddef.h>  /* offsetof */

struct Example {
    char a;    /* 1 字节, offset 0 */
    /* 3 字节填充 */
    int b;     /* 4 字节, offset 4 */
    short c;   /* 2 字节, offset 8 */
    /* 2 字节尾部填充 (保证数组中相邻元素的 b 成员对齐) */
};

int main(void) {
    printf("Size of Example: %zu\n", sizeof(struct Example));   /* 通常为 12 */
    printf("Offset of a: %zu\n", offsetof(struct Example, a));  /* 0 */
    printf("Offset of b: %zu\n", offsetof(struct Example, b));  /* 4 */
    printf("Offset of c: %zu\n", offsetof(struct Example, c));  /* 8 */
    return 0;
}
```

**控制对齐的方式:**
- C11: `_Alignas(N)` / `_Alignof(type)`
- GCC: `__attribute__((aligned(N)))`
- `#pragma pack(N)` -- 非标准，行为因编译器而异，慎用

---

### Q3: 严格别名规则（Strict Aliasing Rule）

**答案:**

C 标准（C99 6.5p7）规定：通过与对象有效类型不兼容的指针类型访问对象是未定义行为。编译器依赖此规则进行优化（如将变量保持在寄存器中）。

**例外:** `char *` / `unsigned char *` / `uint8_t *` 可以别名任何类型。

**安全的类型双关方式:**

```c
#include <string.h>
#include <stdint.h>

/* 方式 1: memcpy (推荐, 编译器会优化掉实际拷贝) */
float int_bits_to_float(uint32_t bits) {
    float f;
    memcpy(&f, &bits, sizeof(f));
    return f;
}

/* 方式 2: union (C99/C11 允许, implementation-defined; C++ 中是 UB) */
union FloatBits {
    uint32_t u;
    float f;
};
```

**违反后果:** 在 `-O2` 以上优化级别，编译器可能生成错误代码。可用 `-fno-strict-aliasing` 禁用此优化（Linux 内核使用此选项），但会损失优化机会。

---

### Q4: 浮点数比较

**答案:**

浮点数存在精度损失，直接用 `==` 比较不可靠。但使用固定 epsilon 同样有问题：
- 对于大数（如 `1e10`），固定 epsilon 远小于浮点精度，几乎所有"接近"的数都被判为不等
- 对于小数（如 `1e-10`），固定 epsilon 远大于数值本身，差异很大的数被判为相等

**推荐: 相对误差 + 绝对误差混合比较:**

```c
#include <math.h>
#include <float.h>
#include <stdbool.h>

bool areAlmostEqual(double a, double b, double relTol, double absTol) {
    /* 处理 NaN */
    if (isnan(a) || isnan(b)) return false;
    /* 处理 Inf */
    if (isinf(a) || isinf(b)) return a == b;

    double diff = fabs(a - b);
    if (diff <= absTol) return true;  /* 处理接近零的情况 */
    double larger = fmax(fabs(a), fabs(b));
    return diff <= larger * relTol;
}

int main(void) {
    /* relTol: 相对容差, 通常取 N * DBL_EPSILON (N 取决于累积运算次数) */
    /* absTol: 绝对容差, 根据业务场景确定 (如传感器精度) */
    double a = 0.1 + 0.2;
    double b = 0.3;
    if (areAlmostEqual(a, b, 1e-9, 1e-12)) {
        printf("approximately equal\n");
    }
    return 0;
}
```

**关键常量:**
- `FLT_EPSILON` ≈ 1.19e-7 (float 的 1.0 与下一个可表示值的差)
- `DBL_EPSILON` ≈ 2.22e-16 (double)

---

### Q5: 线程安全的单例模式（C 语言）

**答案:**

经典的双重检查锁定（DCLP）在 C 中需要原子操作保证正确性，否则存在 data race（UB）。

**错误版本（常见但有 bug）:**

```c
/* 错误: instance 的读写没有同步保证 */
Singleton *instance = NULL;
Singleton *getInstance() {
    if (instance == NULL) {           /* data race: 锁外读取 */
        pthread_mutex_lock(&mutex);
        if (instance == NULL) {
            instance = malloc(...);   /* 可能被重排到初始化之前 */
            instance->data = 0;
        }
        pthread_mutex_unlock(&mutex);
    }
    return instance;
}
```

问题:
1. 外层 `if` 在锁外读取 `instance`，与锁内写入构成 data race
2. 编译器/CPU 可能将 `instance` 赋值重排到 `instance->data = 0` 之前

**正确版本 1: C11 原子操作:**

```c
#include <stdatomic.h>
#include <pthread.h>
#include <stdlib.h>

typedef struct { int data; } Singleton;

static _Atomic(Singleton *) g_instance = NULL;
static pthread_mutex_t g_mutex = PTHREAD_MUTEX_INITIALIZER;

Singleton *getInstance(void) {
    Singleton *p = atomic_load_explicit(&g_instance, memory_order_acquire);
    if (p == NULL) {
        pthread_mutex_lock(&g_mutex);
        p = atomic_load_explicit(&g_instance, memory_order_relaxed);
        if (p == NULL) {
            p = (Singleton *)malloc(sizeof(Singleton));
            if (p != NULL) {
                p->data = 0;  /* 初始化在 store-release 之前完成 */
                atomic_store_explicit(&g_instance, p, memory_order_release);
            }
        }
        pthread_mutex_unlock(&g_mutex);
    }
    return p;
}
```

**正确版本 2: pthread_once（更简洁）:**

```c
#include <pthread.h>
#include <stdlib.h>

typedef struct { int data; } Singleton;

static Singleton *g_instance = NULL;
static pthread_once_t g_once = PTHREAD_ONCE_INIT;

static void initSingleton(void) {
    g_instance = (Singleton *)malloc(sizeof(Singleton));
    if (g_instance != NULL) {
        g_instance->data = 0;
    }
}

Singleton *getInstance(void) {
    pthread_once(&g_once, initSingleton);
    return g_instance;
}
```

---

### Q6: 堆栈溢出（Stack Overflow）

**答案:**

堆栈溢出发生在程序使用的栈空间超过���统分配的上限时。

**常见原因:**
- 递归调用过深（无终止条件或数据规模过大）
- 栈上分配过大的局部变量（如 `char buf[1024*1024]`）
- VLA（变长数组）使用不当
- `alloca()` 分配过大

**防止方法:**

| 方法 | 说明 |
|------|------|
| 迭代替代递归 | 用显式栈（堆上分配）模拟递归 |
| 限制局部变量大小 | 大缓冲区用 `malloc` 或静态分配 |
| 编译器保护 | `-fstack-protector` 插入栈哨兵（canary） |
| MPU 保护 | 在栈底设置不可访问的 guard page |
| RTOS 运行时检测 | FreeRTOS: `uxTaskGetStackHighWaterMark()` |
| 静态分析 | GCC `-fstack-usage` 输出每个函数的栈使用量 |

**嵌入式注意:** RTOS 任务栈通常只有 1-8 KB，必须严格控制栈使用。

---

### Q7: memcpy / strcpy / strncpy / memmove

**解释:**

| 函数 | 特点 |
|------|------|
| `memcpy` | 不处理重叠，最快 |
| `memmove` | 处理重叠，安全 |
| `strcpy` | 无边界检查，危险 |
| `strncpy` | 最多复制 n 字节，源短于 n 时用 `\0` 填充剩余空间（性能陷阱）；源长于等于 n 时不追加 `\0`（安全陷阱） |

**实现 memmove:**

```c
#include <stdint.h>
#include <stddef.h>

void *my_memmove(void *dest, const void *src, size_t n) {
    if (dest == NULL || src == NULL || n == 0U) {
        return dest;
    }
    uint8_t *d = (uint8_t *)dest;
    const uint8_t *s = (const uint8_t *)src;

    if (d < s) {
        for (size_t i = 0U; i < n; i++) {
            d[i] = s[i];
        }
    } else if (d > s) {
        for (size_t i = n; i != 0U; i--) {
            d[i - 1U] = s[i - 1U];
        }
    }
    return dest;
}
```

**推荐替代 strncpy 的安全方案:**

```c
/* strlcpy 语义: 始终 NUL 终止, 返回源字符串长度 */
size_t my_strlcpy(char *dst, const char *src, size_t size) {
    size_t i;
    for (i = 0U; i + 1U < size && src[i] != '\0'; i++) {
        dst[i] = src[i];
    }
    if (size > 0U) {
        dst[i] = '\0';
    }
    /* 计算源字符串剩余长度 */
    while (src[i] != '\0') {
        i++;
    }
    return i;
}
```

> MISRA C 推荐使用 `strnlen` + `memcpy` 替代 `strncpy`。

---

### Q8: 实现 memcmp 和 strcmp

```c
#include <stddef.h>

/* C 标准要求按 unsigned char 逐字节比较 */
int my_memcmp(const void *s1, const void *s2, size_t n) {
    const unsigned char *p1 = (const unsigned char *)s1;
    const unsigned char *p2 = (const unsigned char *)s2;
    for (size_t i = 0U; i < n; i++) {
        if (p1[i] != p2[i]) {
            return (int)p1[i] - (int)p2[i];
        }
    }
    return 0;
}

int my_strcmp(const char *s1, const char *s2) {
    while (*s1 != '\0' && *s1 == *s2) {
        s1++;
        s2++;
    }
    return (int)(unsigned char)*s1 - (int)(unsigned char)*s2;
}
```

**关键点:** 比较时必须转为 `unsigned char`，否则在 `char` 为 signed 的平台上，值 > 127 的字节比较结果会出错。

---

### Q9: 生产者-消费者模型（FIFO 环形缓冲区）

```c
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <stdint.h>
#include <unistd.h>

#define BUFFER_SIZE 10U

static int32_t buffer[BUFFER_SIZE];
static uint32_t in_idx = 0U;   /* 写入位置 */
static uint32_t out_idx = 0U;  /* 读取位置 */
static uint32_t count = 0U;

static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t not_full = PTHREAD_COND_INITIALIZER;
static pthread_cond_t not_empty = PTHREAD_COND_INITIALIZER;

static void *producer(void *param) {
    (void)param;
    for (int32_t i = 0; i < 20; i++) {
        int32_t item = rand() % 100;
        pthread_mutex_lock(&mutex);
        while (count == BUFFER_SIZE) {  /* while 防止虚假唤醒 */
            pthread_cond_wait(&not_full, &mutex);
        }
        buffer[in_idx] = item;
        in_idx = (in_idx + 1U) % BUFFER_SIZE;
        count++;
        printf("Produced: %d (count=%u)\n", item, count);
        pthread_cond_signal(&not_empty);
        pthread_mutex_unlock(&mutex);
        usleep(50000);
    }
    return NULL;
}

static void *consumer(void *param) {
    (void)param;
    for (int32_t i = 0; i < 20; i++) {
        pthread_mutex_lock(&mutex);
        while (count == 0U) {  /* while 防止虚假唤醒 */
            pthread_cond_wait(&not_empty, &mutex);
        }
        int32_t item = buffer[out_idx];
        out_idx = (out_idx + 1U) % BUFFER_SIZE;
        count--;
        printf("Consumed: %d (count=%u)\n", item, count);
        pthread_cond_signal(&not_full);
        pthread_mutex_unlock(&mutex);
        usleep(80000);
    }
    return NULL;
}

int main(void) {
    pthread_t prod, cons;
    pthread_create(&prod, NULL, producer, NULL);
    pthread_create(&cons, NULL, consumer, NULL);
    pthread_join(prod, NULL);
    pthread_join(cons, NULL);
    return 0;
}
```

**关键点:**
- 使用环形缓冲区实现 FIFO 语义（先进先出），而非栈式 LIFO
- 条件变量等待必须用 `while` 循环（防止虚假唤醒 spurious wakeup）
- 生产者和消费者有限次迭代，程序可正常退出

---

### Q10: 快速排序（Quick Sort）

```c
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>

static void swap(int32_t *a, int32_t *b) {
    int32_t temp = *a;
    *a = *b;
    *b = temp;
}

/* 三数取中选 pivot, 避免已排序数组退化为 O(n^2) */
static int32_t median_of_three(int32_t arr[], int32_t low, int32_t high) {
    int32_t mid = low + (high - low) / 2;
    if (arr[low] > arr[mid]) swap(&arr[low], &arr[mid]);
    if (arr[low] > arr[high]) swap(&arr[low], &arr[high]);
    if (arr[mid] > arr[high]) swap(&arr[mid], &arr[high]);
    swap(&arr[mid], &arr[high]);  /* pivot 放到 high 位置 */
    return arr[high];
}

static int32_t partition(int32_t arr[], int32_t low, int32_t high) {
    int32_t pivot = median_of_three(arr, low, high);
    int32_t i = low - 1;
    for (int32_t j = low; j < high; j++) {
        if (arr[j] < pivot) {
            i++;
            swap(&arr[i], &arr[j]);
        }
    }
    swap(&arr[i + 1], &arr[high]);
    return i + 1;
}

static void quickSort(int32_t arr[], int32_t low, int32_t high) {
    if (low < high) {
        int32_t pi = partition(arr, low, high);
        quickSort(arr, low, pi - 1);
        quickSort(arr, pi + 1, high);
    }
}

int main(void) {
    int32_t arr[] = {10, 7, 8, 9, 1, 5};
    int32_t n = (int32_t)(sizeof(arr) / sizeof(arr[0]));
    quickSort(arr, 0, n - 1);
    printf("Sorted: ");
    for (int32_t i = 0; i < n; i++) {
        printf("%d ", arr[i]);
    }
    printf("\n");
    return 0;
}
```

**嵌入式注意:**
- 递归深度最坏 O(n)，栈空间有限时应使用尾递归优化或迭代版本
- 小数组（n < 16）切换到插入排序通常更快（减少函数调用开销）

---

### Q11: LRU 缓存机制

**核心思路:** 双向链表维护访问顺序 + 哈希表实现 O(1) 查找。

**注意:** 哈希表必须处理冲突（链地址法或开放寻址），否则不同 key 映射到同一槽位会导致数据丢失。

```c
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

/* --- 双向链表节点 --- */
typedef struct Node {
    int32_t key;
    int32_t value;
    struct Node *prev;
    struct Node *next;
} Node;

/* --- 哈希表条目 (链地址法处理冲突) --- */
typedef struct HashEntry {
    int32_t key;
    Node *node;
    struct HashEntry *next;  /* 冲突链 */
} HashEntry;

typedef struct {
    int32_t capacity;
    int32_t size;
    int32_t bucket_count;
    Node *head;   /* 哨兵头 */
    Node *tail;   /* 哨兵尾 */
    HashEntry **buckets;
} LRUCache;

static Node *createNode(int32_t key, int32_t value) {
    Node *n = (Node *)malloc(sizeof(Node));
    if (n != NULL) { n->key = key; n->value = value; n->prev = NULL; n->next = NULL; }
    return n;
}

static void removeNode(Node *node) {
    node->prev->next = node->next;
    node->next->prev = node->prev;
}

static void addToHead(LRUCache *c, Node *node) {
    node->next = c->head->next;
    node->prev = c->head;
    c->head->next->prev = node;
    c->head->next = node;
}

/* 哈希查找 (遍历冲突链) */
static Node *hashGet(LRUCache *c, int32_t key) {
    int32_t idx = (key >= 0 ? key : -key) % c->bucket_count;
    HashEntry *e = c->buckets[idx];
    while (e != NULL) {
        if (e->key == key) return e->node;
        e = e->next;
    }
    return NULL;
}

static void hashPut(LRUCache *c, int32_t key, Node *node) {
    int32_t idx = (key >= 0 ? key : -key) % c->bucket_count;
    HashEntry *e = (HashEntry *)malloc(sizeof(HashEntry));
    if (e != NULL) { e->key = key; e->node = node; e->next = c->buckets[idx]; c->buckets[idx] = e; }
}

static void hashRemove(LRUCache *c, int32_t key) {
    int32_t idx = (key >= 0 ? key : -key) % c->bucket_count;
    HashEntry **pp = &c->buckets[idx];
    while (*pp != NULL) {
        if ((*pp)->key == key) {
            HashEntry *tmp = *pp;
            *pp = (*pp)->next;
            free(tmp);
            return;
        }
        pp = &(*pp)->next;
    }
}

LRUCache *lruCreate(int32_t capacity) {
    LRUCache *c = (LRUCache *)calloc(1U, sizeof(LRUCache));
    if (c == NULL) return NULL;
    c->capacity = capacity;
    c->bucket_count = capacity * 2;  /* 负载因子 0.5 */
    c->head = createNode(0, 0);
    c->tail = createNode(0, 0);
    c->head->next = c->tail;
    c->tail->prev = c->head;
    c->buckets = (HashEntry **)calloc((size_t)c->bucket_count, sizeof(HashEntry *));
    return c;
}

int32_t lruGet(LRUCache *c, int32_t key) {
    Node *node = hashGet(c, key);
    if (node == NULL) return -1;
    removeNode(node);
    addToHead(c, node);
    return node->value;
}

void lruPut(LRUCache *c, int32_t key, int32_t value) {
    Node *node = hashGet(c, key);
    if (node != NULL) {
        node->value = value;
        removeNode(node);
        addToHead(c, node);
    } else {
        if (c->size == c->capacity) {
            Node *victim = c->tail->prev;
            removeNode(victim);
            hashRemove(c, victim->key);
            free(victim);
            c->size--;
        }
        Node *newNode = createNode(key, value);
        addToHead(c, newNode);
        hashPut(c, key, newNode);
        c->size++;
    }
}

int main(void) {
    LRUCache *cache = lruCreate(2);
    lruPut(cache, 1, 1);
    lruPut(cache, 2, 2);
    printf("Get 1: %d\n", lruGet(cache, 1));  /* 1 */
    lruPut(cache, 3, 3);                       /* 驱逐 key=2 */
    printf("Get 2: %d\n", lruGet(cache, 2));   /* -1 */
    lruPut(cache, 4, 4);                       /* 驱逐 key=1... 不对, key=1 刚被访问, 驱逐 key=3 */
    printf("Get 1: %d\n", lruGet(cache, 1));   /* 1 */
    printf("Get 3: %d\n", lruGet(cache, 3));   /* -1 */
    printf("Get 4: %d\n", lruGet(cache, 4));   /* 4 */
    return 0;
}
```

---

## 二、指针与类型系统

### Q12: 指针数组 vs 数组指针

```c
int *arr[10];   /* 指针数组: 10 个 int* 的数组, sizeof = 10 * sizeof(int*) */
int (*p)[10];   /* 数组指针: 指向 int[10] 的指针, sizeof = sizeof(int*) */
```

**解读复杂声明的"右左法则":**
1. 从标识符开始
2. 先看右边（数组 `[]`、函数 `()`）
3. 再看左边（指针 `*`）
4. 遇到括号则改变方向

```c
/* 函数指针数组 */
int (*fptrs[10])(int, int);
/* fptrs 是一个数组[10], 元素是指针, 指向 int(int,int) 函数 */
```

---

### Q13: 函数指针

```c
#include <stdio.h>

typedef int (*BinOp)(int, int);  /* typedef 简化声明 */

static int add(int a, int b) { return a + b; }
static int sub(int a, int b) { return a - b; }

int main(void) {
    /* C 中 void f() 表示参数未指定, void f(void) 才是无参数 */
    BinOp ops[] = {add, sub};
    for (int i = 0; i < 2; i++) {
        printf("ops[%d](10, 3) = %d\n", i, ops[i](10, 3));
    }
    return 0;
}
```

**嵌入式典型用法:**
- 函数指针表（dispatch table）替代 `switch-case`
- 回调函数注册（中断处理、事件驱动）
- 模拟 C++ 虚函数表（vtable）

---

### Q14: 指针算术（Pointer Arithmetic）

指针加减运算以所指类型的大小为单位。`ptr + n` 实际偏移 `n * sizeof(*ptr)` 字节。

**合法范围:** 指针算术仅在同一数组对象内（含末尾后一个位置）合法，超出范围是 UB。

```c
#include <stdio.h>
#include <stddef.h>

int main(void) {
    int arr[] = {10, 20, 30, 40, 50};
    int *p = arr;
    int *end = arr + 5;  /* 末尾后一个位置, 合法但不可解引用 */

    for (; p < end; p++) {
        printf("%d ", *p);
    }
    printf("\n");

    /* 两个指针相减得到 ptrdiff_t */
    ptrdiff_t diff = end - arr;  /* 5 */
    printf("diff = %td\n", diff);
    return 0;
}
```

> 注: `void *` 不能进行算术运算（GCC 扩展允许，按 1 字节计算，但非标准）。

---

### Q15: 变长数组（VLA）

C99 引入 VLA，C11 改为可选特性（`__STDC_NO_VLA__`）。

**嵌入式中几乎不应使用 VLA:**
- MISRA C:2012 Rule 18.8 明确禁止（Required 级别）
- 分配在栈上，无法检测分配失败（不像 `malloc` 返回 NULL）
- 栈空间在嵌入式系统中极其有限
- 许多嵌入式编译器不支持

**推荐替代方案:**

```c
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

#define MAX_SIZE 256U

void processData(uint32_t size) {
    if (size == 0U || size > MAX_SIZE) {
        fprintf(stderr, "Invalid size: %u\n", size);
        return;
    }
    /* 方案 1: 固定大小数组 (栈上, 编译期确定) */
    int32_t fixed_buf[MAX_SIZE];

    /* 方案 2: 动态分配 (堆上, 可检测失败) */
    int32_t *dyn_buf = (int32_t *)malloc(size * sizeof(int32_t));
    if (dyn_buf == NULL) {
        fprintf(stderr, "malloc failed\n");
        return;
    }
    /* ... 使用 dyn_buf ... */
    free(dyn_buf);
}
```

---

### Q16: 内存池（Memory Pool）

**固定块大小内存池** 是嵌入式中最常用的方案：无碎片化、O(1) 分配/释放。

```c
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#define BLOCK_SIZE  64U
#define BLOCK_COUNT 16U

typedef union Block {
    union Block *next;                    /* 空闲时: 指向下一个空闲块 */
    _Alignas(max_align_t) char data[BLOCK_SIZE];  /* 使用时: 用户数据 */
} Block;

typedef struct {
    Block pool[BLOCK_COUNT];
    Block *free_list;
    uint32_t used_count;
} MemPool;

void mempool_init(MemPool *mp) {
    mp->free_list = &mp->pool[0];
    mp->used_count = 0U;
    for (uint32_t i = 0U; i < BLOCK_COUNT - 1U; i++) {
        mp->pool[i].next = &mp->pool[i + 1U];
    }
    mp->pool[BLOCK_COUNT - 1U].next = NULL;
}

void *mempool_alloc(MemPool *mp) {
    if (mp->free_list == NULL) {
        return NULL;  /* 池耗尽 */
    }
    Block *blk = mp->free_list;
    mp->free_list = blk->next;
    mp->used_count++;
    return blk->data;
}

bool mempool_free(MemPool *mp, void *ptr) {
    if (ptr == NULL) return false;
    /* 边界检查: 确保 ptr 在池范围内 */
    uint8_t *p = (uint8_t *)ptr;
    uint8_t *pool_start = (uint8_t *)&mp->pool[0];
    uint8_t *pool_end = (uint8_t *)&mp->pool[BLOCK_COUNT];
    if (p < pool_start || p >= pool_end) return false;

    Block *blk = (Block *)((uint8_t *)ptr - offsetof(Block, data));
    blk->next = mp->free_list;
    mp->free_list = blk;
    mp->used_count--;
    return true;
}

int main(void) {
    MemPool mp;
    mempool_init(&mp);

    void *p1 = mempool_alloc(&mp);
    void *p2 = mempool_alloc(&mp);
    printf("Alloc: %p, %p (used=%u)\n", p1, p2, mp.used_count);

    mempool_free(&mp, p1);
    printf("After free: used=%u\n", mp.used_count);
    return 0;
}
```

**关键设计点:**
- `_Alignas(max_align_t)` 保证对齐，避免 ARM 上的 HardFault
- 边界检查防止 double-free 和野指针归还
- 固定块大小 = 零碎片化
- 多线程环境需加锁或使用无锁链表

---

## 三、预处理器与编译

### Q17: 预处理器指令和宏

**常见陷阱与最佳实践:**

```c
/* 陷阱 1: 运算符优先级 */
#define SQUARE_BAD(x)  x * x
/* SQUARE_BAD(1+2) 展开为 1+2*1+2 = 5, 而非 9 */

#define SQUARE(x)  ((x) * (x))  /* 正确: 括号包围参数和整体 */

/* 陷阱 2: 多次求值 */
#define MAX(a, b)  ((a) > (b) ? (a) : (b))
/* MAX(i++, j) 可能导致 i 被递增两次 */

/* 陷阱 3: 多语句宏 */
#define SWAP_BAD(a, b)  { int t = a; a = b; b = t; }
/* if (cond) SWAP_BAD(x, y); else ... 编译错误 */

#define SWAP(a, b)  do { int t = (a); (a) = (b); (b) = t; } while (0)
/* do-while(0) 包装, 可安全用于 if-else */
```

**高级用法:**

```c
/* 字符串化 (#) 和标记粘贴 (##) */
#define STRINGIFY(x)  #x
#define CONCAT(a, b)  a##b

/* 可变参数宏 */
#define LOG(fmt, ...)  fprintf(stderr, "[%s:%d] " fmt "\n", \
                               __FILE__, __LINE__, ##__VA_ARGS__)

/* X-Macro: 编译期生成枚举和字符串表 */
#define STATE_LIST \
    X(IDLE)        \
    X(RUNNING)     \
    X(ERROR)

typedef enum {
#define X(name) STATE_##name,
    STATE_LIST
#undef X
    STATE_COUNT
} State;

static const char *state_names[] = {
#define X(name) #name,
    STATE_LIST
#undef X
};
```

> MISRA C Rule 20.x 系列对宏使用有严格限制，生产代码中应优先使用 `static inline` 函数替代函数式宏。

---

### Q18: static 关键字

| 位置 | 作用 |
|------|------|
| 函数内局部变量 | 生命周期延长到程序结束，但作用域不变（仅函数内可见） |
| 文件作用域变量/函数 | 内部链接（internal linkage），仅当前编译单元可见 |
| C99 函数参数 `int arr[static 10]` | 提示编译器数组至少有 10 个元素，可据此优化 |

```c
#include <stdio.h>

void counter(void) {
    static int n = 0;  /* 仅初始化一次, C 中在程序启动时初始化 */
    n++;
    printf("called %d times\n", n);
}

/* static 函数不导出符号, 减小二进制体积 */
static int helper(int x) { return x * 2; }

/* C99: arr 至少 10 个元素 */
void process(int arr[static 10]) {
    /* 编译器可假设 arr != NULL 且至少 10 个元素 */
}
```

> 注: C 中静态局部变量在程序启动时初始化（零初始化或常量初始化），不是首次调用时 -- 这与 C++ 不同。

---

### Q19: 联合体（Union）

联合体所有成员共享同一块内存，大小等于最大成员的大小（加上可能的尾部填充）。

**Tagged Union (带类型标签的联合体)** 是 C 中实现"变体类型"的标准模式:

```c
#include <stdio.h>
#include <string.h>
#include <stdint.h>

typedef enum { VAL_INT, VAL_FLOAT, VAL_STR } ValueType;

typedef struct {
    ValueType type;  /* 类型标签 */
    union {
        int32_t i;
        float f;
        char str[20];
    } data;
} Value;

void printValue(const Value *v) {
    switch (v->type) {
    case VAL_INT:   printf("int: %d\n", v->data.i); break;
    case VAL_FLOAT: printf("float: %f\n", v->data.f); break;
    case VAL_STR:   printf("str: %s\n", v->data.str); break;
    }
}

int main(void) {
    Value v1 = {.type = VAL_INT, .data.i = 42};
    Value v2 = {.type = VAL_STR};
    strncpy(v2.data.str, "Hello", sizeof(v2.data.str) - 1U);
    v2.data.str[sizeof(v2.data.str) - 1U] = '\0';

    printValue(&v1);
    printValue(&v2);
    return 0;
}
```

**典型应用:** 网络协议解析（协议头 + 不同载荷类型）、配置系统、解释器值类型。

> MISRA C:2012 Rule 19.2 (Advisory): union 的使用需要文档化偏差说明。

---

### Q20: 位域（Bit Fields）

位域允许在结构体中定义比基本类型更小的位段，但可移植性问题严重:

| 属性 | 标准规定 |
|------|---------|
| 分配单元大小 | implementation-defined |
| 位域排列顺序（MSB/LSB first） | implementation-defined |
| 是否跨越存储单元边界 | implementation-defined |
| `int` 位域是 signed 还是 unsigned | implementation-defined |

**因此，位域不应用于映射硬件寄存器或网络协议字段。**

**正确做法: 位掩码 + 移位操作:**

```c
#include <stdint.h>

/* 硬件寄存器字段定义 */
#define REG_ENABLE_BIT   (1U << 0)
#define REG_MODE_MASK    (0x3U << 1)
#define REG_MODE_SHIFT   1U
#define REG_PRIO_MASK    (0x7U << 3)
#define REG_PRIO_SHIFT   3U

static inline void reg_set_mode(volatile uint32_t *reg, uint32_t mode) {
    *reg = (*reg & ~REG_MODE_MASK) | ((mode << REG_MODE_SHIFT) & REG_MODE_MASK);
}

static inline uint32_t reg_get_prio(volatile uint32_t *reg) {
    return (*reg & REG_PRIO_MASK) >> REG_PRIO_SHIFT;
}
```

位域仅适合编译器内部使用的标志位（不涉及跨平台序列化）:

```c
typedef struct {
    unsigned int active : 1;
    unsigned int priority : 3;  /* 0-7 */
    unsigned int mode : 2;      /* 0-3 */
} TaskFlags;
/* 注意: 赋值超出范围时, unsigned 位域截断 (implementation-defined) */
```

---

## 四、嵌入式与系统编程（补充题目）

### Q21: C 语言中的内存布局

**答案:**

典型的 C 程序内存布局（从低地址到高地址）:

```
+------------------+ 低地址
| .text (代码段)    | 只读, 存放机器指令
+------------------+
| .rodata (只读数据)| 只读, 字符串字面量、const 全局变量
+------------------+
| .data (已初始化)  | 读写, 已初始化的全局/静态变量
+------------------+
| .bss (未初始化)   | 读写, 未初始化的全局/静态变量 (启动时清零)
+------------------+
| heap (堆)        | 向高地址增长, malloc/free
+------------------+
|       ...        | 未映射区域
+------------------+
| stack (栈)       | 向低地址增长, 局部变量/函数调用
+------------------+ 高地址
```

**面试常考:**

```c
int g_init = 42;          /* .data */
int g_uninit;              /* .bss */
const int g_const = 100;   /* .rodata */
static int s_var = 1;      /* .data (内部链接) */

void func(void) {
    int local = 0;                /* stack */
    static int s_local = 0;       /* .data */
    char *p = malloc(100);        /* p 在 stack, *p 在 heap */
    const char *str = "hello";    /* str 在 stack, "hello" 在 .rodata */
}
```

**嵌入式注意:**
- MCU 的 `.text` 和 `.rodata` 通常在 Flash 中
- `.data` 段需要启动代码从 Flash 拷贝到 RAM（scatter loading）
- `.bss` 段由启动代码清零
- 链接脚本（linker script）控制各段的地址分配

---

### Q22: 中断安全与可重入函数

**答案:**

可重入函数（reentrant function）可以被中断后再次安全调用，要求:
- 不使用全局/静态变量（或使用时有保护）
- 不调用不可重入的函数（如 `strtok`、`rand`、`malloc`）
- 不修改自身代码

```c
#include <stdint.h>

/* 不可重入: 使用静态变量 */
int bad_counter(void) {
    static int count = 0;
    return ++count;  /* 中断中再次调用会破坏 count */
}

/* 可重入: 通过参数传递状态 */
int good_counter(int *count) {
    return ++(*count);
}

/* 中断安全的共享变量访问 (裸机/RTOS) */
static volatile uint32_t g_ticks = 0U;

/* ISR 中写入 */
void SysTick_Handler(void) {
    g_ticks++;  /* 单写者, 32位原子写 (ARM Cortex-M) */
}

/* 主循环中读取 */
uint32_t getTicks(void) {
    uint32_t ticks;
    /* 方案 1: 关中断 (最简单, 适合短临界区) */
    __disable_irq();
    ticks = g_ticks;
    __enable_irq();
    return ticks;

    /* 方案 2: 双读一致性检查 (不关中断) */
    /* uint32_t t1, t2;
       do { t1 = g_ticks; t2 = g_ticks; } while (t1 != t2);
       return t1; */
}
```

**C 标准库中不可重入的常见函数:**
`strtok`, `localtime`, `asctime`, `rand`, `strerror`, `getenv`
-- 它们的可重入版本通常以 `_r` 后缀命名（如 `strtok_r`）。

---

### Q23: 链表操作 -- 反转单链表

```c
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

typedef struct Node {
    int32_t data;
    struct Node *next;
} Node;

/* 迭代法反转 */
Node *reverseList(Node *head) {
    Node *prev = NULL;
    Node *curr = head;
    while (curr != NULL) {
        Node *next_node = curr->next;
        curr->next = prev;
        prev = curr;
        curr = next_node;
    }
    return prev;
}

/* 检测环 (Floyd 快慢指针) */
int hasCycle(Node *head) {
    Node *slow = head;
    Node *fast = head;
    while (fast != NULL && fast->next != NULL) {
        slow = slow->next;
        fast = fast->next->next;
        if (slow == fast) return 1;
    }
    return 0;
}

int main(void) {
    Node *head = NULL;
    for (int32_t i = 5; i >= 1; i--) {
        Node *n = (Node *)malloc(sizeof(Node));
        n->data = i;
        n->next = head;
        head = n;
    }
    /* 1->2->3->4->5 */
    head = reverseList(head);
    /* 5->4->3->2->1 */
    for (Node *p = head; p != NULL; p = p->next) {
        printf("%d ", p->data);
    }
    printf("\n");
    return 0;
}
```

---

### Q24: 字节序（Endianness）检测与转换

```c
#include <stdio.h>
#include <stdint.h>
#include <string.h>

/* 运行时检测字节序 */
static int is_little_endian(void) {
    uint32_t val = 1U;
    uint8_t byte;
    memcpy(&byte, &val, 1U);  /* 安全的类型双关 */
    return byte == 1U;
}

/* 字节序转换 (不依赖编译器内建) */
static uint16_t swap16(uint16_t x) {
    return (uint16_t)((x >> 8) | (x << 8));
}

static uint32_t swap32(uint32_t x) {
    return ((x >> 24) & 0x000000FFU) |
           ((x >>  8) & 0x0000FF00U) |
           ((x <<  8) & 0x00FF0000U) |
           ((x << 24) & 0xFF000000U);
}

/* 网络字节序 (大端) 转换 */
static uint32_t hton32(uint32_t host) {
    if (is_little_endian()) {
        return swap32(host);
    }
    return host;
}

int main(void) {
    printf("System is %s-endian\n",
           is_little_endian() ? "little" : "big");

    uint32_t val = 0x12345678U;
    uint32_t net = hton32(val);
    printf("Host: 0x%08X -> Network: 0x%08X\n", val, net);
    return 0;
}
```

**嵌入式注意:**
- ARM Cortex-M 默认小端，但可配置为大端
- 网络协议（TCP/IP）使用大端（网络字节序）
- 序列化/反序列化时必须显式处理字节序，不要依赖 `memcpy` 结构体

---

### Q25: 环形缓冲区（Ring Buffer）

无锁 SPSC（单生产者单消费者）环形缓冲区是嵌入式中最常用的数据结构之一:

```c
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define RING_SIZE 256U  /* 必须是 2 的幂 */
#define RING_MASK (RING_SIZE - 1U)

typedef struct {
    uint8_t buf[RING_SIZE];
    volatile uint32_t head;  /* 写入位置 (生产者更新) */
    volatile uint32_t tail;  /* 读取位置 (消费者更新) */
} RingBuffer;

void ring_init(RingBuffer *rb) {
    rb->head = 0U;
    rb->tail = 0U;
}

uint32_t ring_count(const RingBuffer *rb) {
    return (rb->head - rb->tail) & RING_MASK;
}

uint32_t ring_free(const RingBuffer *rb) {
    return RING_SIZE - 1U - ring_count(rb);  /* 留一个空位区分满/空 */
}

bool ring_put(RingBuffer *rb, uint8_t byte) {
    if (ring_free(rb) == 0U) return false;
    rb->buf[rb->head & RING_MASK] = byte;
    rb->head = (rb->head + 1U) & RING_MASK;
    return true;
}

bool ring_get(RingBuffer *rb, uint8_t *byte) {
    if (ring_count(rb) == 0U) return false;
    *byte = rb->buf[rb->tail & RING_MASK];
    rb->tail = (rb->tail + 1U) & RING_MASK;
    return true;
}
```

**关键点:**
- 大小为 2 的幂，用位掩码替代取模（零开销）
- 留一个空位区分满和空（`head == tail` 为空，`head + 1 == tail` 为满）
- SPSC 场景下，`head` 仅由生产者写、`tail` 仅由消费者写，天然无竞争
- 在 ARM 上需要 `__DMB()` 内存屏障保证写入顺序（弱内存序平台）

---

### Q26: 位操作技巧

```c
#include <stdint.h>

/* 设置第 n 位 */
static inline uint32_t bit_set(uint32_t val, uint32_t n) {
    return val | (1U << n);
}

/* 清除第 n 位 */
static inline uint32_t bit_clear(uint32_t val, uint32_t n) {
    return val & ~(1U << n);
}

/* 翻转第 n 位 */
static inline uint32_t bit_toggle(uint32_t val, uint32_t n) {
    return val ^ (1U << n);
}

/* 测试第 n 位 */
static inline uint32_t bit_test(uint32_t val, uint32_t n) {
    return (val >> n) & 1U;
}

/* 计算置位数 (popcount) -- Brian Kernighan 算法 */
static uint32_t popcount(uint32_t x) {
    uint32_t count = 0U;
    while (x != 0U) {
        x &= (x - 1U);  /* 清除最低位的 1 */
        count++;
    }
    return count;
}

/* 判断是否为 2 的幂 */
static inline int is_power_of_two(uint32_t x) {
    return (x != 0U) && ((x & (x - 1U)) == 0U);
}

/* 向上对齐到 alignment (alignment 必须是 2 的幂) */
static inline uint32_t align_up(uint32_t val, uint32_t alignment) {
    return (val + alignment - 1U) & ~(alignment - 1U);
}

/* 找到最低置位位 (CTZ - Count Trailing Zeros) */
/* GCC: __builtin_ctz(x), ARM: __CLZ(__RBIT(x)) */
```

---

### Q27: C 语言实现面向对象 -- 函数指针表

```c
#include <stdio.h>
#include <stdint.h>

/* "基类": 形状 */
typedef struct Shape Shape;

typedef struct {
    double (*area)(const Shape *self);
    void (*draw)(const Shape *self);
    const char *name;
} ShapeVTable;

struct Shape {
    const ShapeVTable *vt;  /* 虚函数表指针 */
};

/* "派生类": 圆形 */
typedef struct {
    Shape base;
    double radius;
} Circle;

static double circle_area(const Shape *self) {
    const Circle *c = (const Circle *)self;
    return 3.14159265 * c->radius * c->radius;
}

static void circle_draw(const Shape *self) {
    const Circle *c = (const Circle *)self;
    printf("Drawing circle (r=%.1f)\n", c->radius);
}

static const ShapeVTable circle_vt = {
    .area = circle_area,
    .draw = circle_draw,
    .name = "Circle"
};

/* "派生类": 矩形 */
typedef struct {
    Shape base;
    double width, height;
} Rect;

static double rect_area(const Shape *self) {
    const Rect *r = (const Rect *)self;
    return r->width * r->height;
}

static void rect_draw(const Shape *self) {
    const Rect *r = (const Rect *)self;
    printf("Drawing rect (%.1f x %.1f)\n", r->width, r->height);
}

static const ShapeVTable rect_vt = {
    .area = rect_area,
    .draw = rect_draw,
    .name = "Rect"
};

/* 多态调用 */
void printShape(const Shape *s) {
    printf("%s: area=%.2f\n", s->vt->name, s->vt->area(s));
    s->vt->draw(s);
}

int main(void) {
    Circle c = {.base.vt = &circle_vt, .radius = 5.0};
    Rect r = {.base.vt = &rect_vt, .width = 3.0, .height = 4.0};

    Shape *shapes[] = {&c.base, &r.base};
    for (int i = 0; i < 2; i++) {
        printShape(shapes[i]);
    }
    return 0;
}
```

**嵌入式优势:**
- `const ShapeVTable` 放在 `.rodata`（Flash），零 RAM 开销
- 编译期确定的 vtable，无动态分配
- 与 Nginx、Linux 内核的模块化架构思路一致

---

### Q28: _Atomic 与内存序（C11）

```c
#include <stdatomic.h>
#include <stdint.h>
#include <stdbool.h>

/* 无锁标志 (替代 volatile bool) */
static _Atomic bool g_running = true;

/* 生产者-消费者: 发布-获取语义 */
static _Atomic uint32_t g_data_ready = 0U;
static uint32_t g_payload = 0U;  /* 非原子, 由 data_ready 保护 */

void producer(void) {
    g_payload = 42U;  /* 普通写 */
    atomic_store_explicit(&g_data_ready, 1U, memory_order_release);
    /* release: 保证 g_payload=42 在 data_ready=1 之前对消费者可见 */
}

void consumer(void) {
    while (atomic_load_explicit(&g_data_ready, memory_order_acquire) == 0U) {
        /* 自旋等待 */
    }
    /* acquire: 保证看到 release 之前的所有写入 */
    uint32_t val = g_payload;  /* 保证读到 42 */
    (void)val;
}
```

**内存序速查:**

| 内存序 | 语义 | 典型用途 |
|--------|------|---------|
| `relaxed` | 仅保证原子性，不保证顺序 | 计数器、统计 |
| `acquire` | 本操作之后的读写不会被重排到本操作之前 | 锁获取、数据消费 |
| `release` | 本操作之前的读写不会被重排到本操作之后 | 锁释放、数据发布 |
| `acq_rel` | acquire + release | CAS 循环 |
| `seq_cst` | 全局顺序一致（默认，最强，最慢） | 简单场景 |

**嵌入式注意:**
- ARM Cortex-M 单核: `relaxed` + `atomic_signal_fence` 即可（阻止编译器重排，无需硬件屏障）
- ARM Cortex-A 多核: 必须使用 `acquire/release`（映射为 DMB 指令）
- x86 是强内存序（TSO），`acquire/release` 几乎零开销

---

### Q29: 错误处理模式

C 语言没有异常机制，常见的错误处理模式:

**模式 1: 返回错误码 (最常用)**

```c
#include <stdint.h>

typedef enum {
    ERR_OK = 0,
    ERR_NULL_PTR,
    ERR_INVALID_PARAM,
    ERR_TIMEOUT,
    ERR_NO_MEMORY
} ErrorCode;

ErrorCode sensor_read(uint32_t channel, int32_t *out_value) {
    if (out_value == NULL) return ERR_NULL_PTR;
    if (channel > 7U) return ERR_INVALID_PARAM;
    /* ... 读取硬件 ... */
    *out_value = 42;
    return ERR_OK;
}
```

**模式 2: goto 清理 (Linux 内核风格)**

```c
#include <stdlib.h>

int init_subsystem(void) {
    int *buf1 = NULL, *buf2 = NULL, *buf3 = NULL;

    buf1 = malloc(100);
    if (buf1 == NULL) goto fail_buf1;

    buf2 = malloc(200);
    if (buf2 == NULL) goto fail_buf2;

    buf3 = malloc(300);
    if (buf3 == NULL) goto fail_buf3;

    /* 全部成功 */
    return 0;

fail_buf3:
    free(buf2);
fail_buf2:
    free(buf1);
fail_buf1:
    return -1;
}
```

**模式 3: 回调式错误处理 (适合库)**

```c
typedef void (*ErrorHandler)(int code, const char *msg, void *ctx);

typedef struct {
    ErrorHandler on_error;
    void *error_ctx;
} Config;

void process(Config *cfg) {
    if (/* 出错 */ 0) {
        if (cfg->on_error != NULL) {
            cfg->on_error(-1, "something failed", cfg->error_ctx);
        }
    }
}
```

---

### Q30: 编译期断言与类型安全

```c
#include <stdint.h>
#include <stddef.h>

/* C11 _Static_assert: 编译期检查 */
_Static_assert(sizeof(int) >= 4, "int must be at least 32 bits");
_Static_assert(sizeof(void *) == sizeof(size_t),
               "pointer and size_t must have same width");

/* 数组大小安全宏 (防止指针误用) */
#define ARRAY_SIZE(arr) \
    (sizeof(arr) / sizeof((arr)[0]) + \
     sizeof(typeof(int[1 - 2 * \
         __builtin_types_compatible_p(typeof(arr), typeof(&(arr)[0]))])))
/* 如果 arr 是指针而非数组, 编译报错 (GCC 扩展) */

/* 可移植版本 (C11) */
#define ARRAY_SIZE_PORTABLE(arr) \
    (sizeof(arr) / sizeof((arr)[0]))

/* 结构体字段偏移检查 */
_Static_assert(offsetof(struct { char a; int b; }, b) == 4,
               "unexpected padding");

/* 编译期确保枚举值不超过存储类型 */
typedef enum {
    CMD_START = 0,
    CMD_STOP = 1,
    CMD_RESET = 2,
    CMD_MAX
} Command;
_Static_assert(CMD_MAX <= 255, "Command must fit in uint8_t");
```

**嵌入式典型用法:**
- 检查结构体大小与硬件寄存器映射匹配
- 检查缓冲区大小是 2 的幂（环形缓冲区要求）
- 检查平台假设（指针大小、对齐、字节序相关常量）

---

## 附录: 面试高频考点速查

| 主题 | 关键知识点 |
|------|-----------|
| volatile | 仅阻止编译器优化，不保证原子性和内存序 |
| 内存对齐 | `_Alignas`, `offsetof`, DMA 对齐要求 |
| 严格别名 | `memcpy` 是最安全的类型双关方式 |
| 浮点比较 | 相对误差 + 绝对误差混合比较 |
| DCLP | C11 需要 `_Atomic` + acquire/release |
| strncpy | 不保证 NUL 终止，推荐 `strlcpy` 语义 |
| 生产者-消费者 | FIFO 环形缓冲区 + 条件变量 while 循环 |
| LRU | 双向链表 + 哈希表（必须处理冲突） |
| VLA | MISRA 禁止，嵌入式不应使用 |
| 内存池 | 固定块大小 + 对齐 + 边界检查 |
| 位域 | 不可移植，硬件寄存器用位掩码替代 |
| 函数指针 | `void f()` vs `void f(void)` 的区别 |
| 可重入 | 不使用全局/静态变量，不调用不可重入函数 |
| 字节序 | 序列化时显式处理，不依赖 memcpy 结构体 |
| _Atomic | acquire/release 语义，ARM 单核可用 relaxed + signal_fence |
| 错误处理 | 返回错误码 / goto 清理 / 回调 |
| 编译期检查 | `_Static_assert` 验证平台假设 |

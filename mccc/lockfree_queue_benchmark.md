# 多线程性能测试：ConcurrentQueue、std::atomic_flag 和 std::mutex

> 本文对比了多线程环境下，ConcurrentQueue（无锁队列）、std::atomic_flag 和 std::mutex 三种同步机制的性能表现。
>
> 完整测试代码: [lock_test](https://gitee.com/liudegui/lock_test)

## 1. 概要

测试目标：
1. 比较 `std::mutex` 和 `std::atomic_flag` 的性能差异
2. 测试多线程环境下的 [concurrentqueue](https://github.com/cameron314/concurrentqueue) 队列性能
3. 测试单线程环境下的 [readerwriterqueue](https://github.com/cameron314/readerwriterqueue) 性能

结论：
- 对于小型数据结构的多线程操作，推荐使用 `std::atomic_flag` 替代 `std::mutex`
- 对于较大的数据结构，`std::mutex` 在保证线程安全的同时提供了更好的性能
- 新的业务代码可以根据需求适当使用无锁队列 `ConcurrentQueue`

## 2. 性能测试结果

### 2.1 测试一：30 线程并发，1 万条 2KB 数据

| 测试平台 | 队列类型 | pushTime (ms) | popTime (ms) |
| --- | --- | --- | --- |
| Linux-arm1 | ConcurrentQueue | 595.476 | 328.856 |
| Linux-arm1 | atomic_flag | 412.675 | 955.207 |
| Linux-arm1 | std::mutex | 946.301 | 907.553 |
| Linux-arm2 | ConcurrentQueue | 1584.1 | 333.36 |
| Linux-arm2 | atomic_flag | 576.209 | 1479.5 |
| Linux-arm2 | std::mutex | 1133.68 | 1107.63 |
| Linux-arm3 | ConcurrentQueue | 1005.89 | 244.84 |
| Linux-arm3 | atomic_flag | 355.606 | 402.343 |
| Linux-arm3 | std::mutex | 597.448 | 739.805 |
| Linux-x86 | ConcurrentQueue | 140.899 | 80.4264 |
| Linux-x86 | atomic_flag | 136.703 | 136.91 |
| Linux-x86 | std::mutex | 231.019 | 213.732 |
| Windows | ConcurrentQueue | 200.119 | 142.239 |
| Windows | atomic_flag | 602.542 | 482.394 |
| Windows | std::mutex | 483.498 | 306.393 |

结论：
- Linux 平台：push 时 atomic_flag 优于 ConcurrentQueue，pop 时 ConcurrentQueue 最佳
- Windows 平台：ConcurrentQueue 表现始终最好

### 2.2 测试二：30 线程并发，1 万条 20KB 数据

| 测试平台 | 队列类型 | pushTime (ms) | popTime (ms) |
| --- | --- | --- | --- |
| Linux-arm1 | ConcurrentQueue | 6936.41 | 732.457 |
| Linux-arm1 | atomic_flag | 4256.77 | 7103.61 |
| Linux-arm1 | std::mutex | 5165.12 | 4044.51 |
| Linux-arm2 | ConcurrentQueue | 18411.2 | 942.713 |
| Linux-arm2 | atomic_flag | 5750.07 | 11232.5 |
| Linux-arm2 | std::mutex | 7236.02 | 6573.35 |
| Linux-arm3 | ConcurrentQueue | 5399.57 | 247.965 |
| Linux-arm3 | atomic_flag | 2253.27 | 1285.47 |
| Linux-arm3 | std::mutex | 2625.55 | 1588.46 |
| Linux-x86 | ConcurrentQueue | 2231.09 | 183.022 |
| Linux-x86 | atomic_flag | 1117.93 | 715.601 |
| Linux-x86 | std::mutex | 1288.95 | 805.378 |
| Windows | ConcurrentQueue | 8047.59 | 5098.22 |
| Windows | atomic_flag | 16736.2 | 26468.2 |
| Windows | std::mutex | 21498.3 | 50173.6 |

结论：随着数据项大小增大，atomic_flag 性能不再领先，ConcurrentQueue 的 pop 性能持续优异。

## 3. 硬件配置

- Linux-x86: Intel i7-9700 @ 3.00GHz (8 cores), 16GB RAM
- Linux-arm1: 96 cores ARM, 64GB RAM
- Linux-arm2: 96 cores ARM, 256GB RAM
- Windows: Intel i7-6500U @ 2.50GHz, 8GB RAM

> 原文链接: [CSDN](https://blog.csdn.net/stallion5632/article/details/125551132)

// benchmark_five_principles.cpp
// Benchmark for "Five Counter-Intuitive Principles of High-Performance System Design"
//
// Article: content/posts/performance/high_performance_system_design_principles.md
// Online:  https://deguiliu.github.io/tech-notes/posts/performance/high_performance_system_design_principles/
//
// Compile: g++ -O2 -std=c++17 -pthread -o benchmark benchmark_five_principles.cpp
// Run:     ./benchmark
//
// Each principle is tested independently with measurable results.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <fstream>
#include <sched.h>
#include <string>
#include <sys/resource.h>
#endif

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

static constexpr uint64_t kWarmupIterations = 100'000;

struct BenchResult {
    double throughput_mps;  // million ops per second
    double latency_ns;      // average nanoseconds per op
    uint64_t iterations;
};

using Clock = std::chrono::high_resolution_clock;

template <typename Fn>
BenchResult run_bench(const char* /*name*/, uint64_t iterations, Fn&& fn) {
    // Warmup
    for (uint64_t i = 0; i < kWarmupIterations; ++i) {
        fn(i);
    }

    auto start = Clock::now();
    for (uint64_t i = 0; i < iterations; ++i) {
        fn(i);
    }
    auto end = Clock::now();

    double elapsed_ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
            .count();
    double latency = elapsed_ns / static_cast<double>(iterations);
    double throughput = static_cast<double>(iterations) / (elapsed_ns / 1e9) / 1e6;

    return {throughput, latency, iterations};
}

#if defined(__linux__)
static uint64_t get_voluntary_ctx_switches() {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return static_cast<uint64_t>(ru.ru_nvcsw);
}

static uint64_t get_involuntary_ctx_switches() {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return static_cast<uint64_t>(ru.ru_nivcsw);
}
#endif

static void print_separator() {
    std::printf(
        "======================================================================"
        "==========\n");
}

// ---------------------------------------------------------------------------
// Principle 1: State Machine (atomic load) vs If-Else Chain
// ---------------------------------------------------------------------------

enum class State : uint32_t { kInit, kRunning, kPaused, kStopping, kStopped };

// Simulate a multi-condition if-else chain (no state machine)
struct NoStateMachine {
    volatile bool initialized = true;
    volatile bool running = true;
    volatile bool paused = false;
    volatile bool stopping = false;

    uint64_t process_ifelse(uint64_t data) {
        if (!initialized) return 0;
        if (stopping) return 0;
        if (paused) return 0;
        if (!running) return 0;
        return data + 1;  // actual work
    }
};

// State machine: single atomic load
struct WithStateMachine {
    std::atomic<State> state{State::kRunning};

    uint64_t process_atomic(uint64_t data) {
        if (state.load(std::memory_order_acquire) != State::kRunning) {
            return 0;
        }
        return data + 1;  // actual work
    }
};

static void bench_principle1() {
    print_separator();
    std::printf("Principle 1: State Machine (atomic load) vs If-Else Chain\n");
    print_separator();

    constexpr uint64_t kIterations = 50'000'000;

    NoStateMachine no_sm;
    auto r1 = run_bench("if-else", kIterations, [&](uint64_t i) {
        return no_sm.process_ifelse(i);
    });

    WithStateMachine with_sm;
    auto r2 = run_bench("atomic-state", kIterations, [&](uint64_t i) {
        return with_sm.process_atomic(i);
    });

    std::printf("  %-30s %8.2f M/s  %8.1f ns/op\n", "If-Else chain:",
                r1.throughput_mps, r1.latency_ns);
    std::printf("  %-30s %8.2f M/s  %8.1f ns/op\n", "Atomic state load:",
                r2.throughput_mps, r2.latency_ns);

    double diff_pct =
        (r2.throughput_mps - r1.throughput_mps) / r1.throughput_mps * 100.0;
    std::printf("  Throughput difference: %+.1f%%\n\n", diff_pct);
}

// ---------------------------------------------------------------------------
// Principle 1b: Multi-threaded state check (more realistic)
//   Writer thread flips state; reader thread checks state on hot path.
//   Compares: 4 volatile bools vs 1 atomic<State>
// ---------------------------------------------------------------------------

static void bench_principle1_mt() {
    std::printf("Principle 1b: Multi-threaded state check (writer + reader)\n");
    print_separator();

    constexpr uint64_t kIterations = 20'000'000;
    constexpr uint64_t kFlipInterval = 1'000'000;  // writer flips every N

    // --- volatile bools version (spread across cache lines) ---
    struct VolatileBools {
        alignas(64) volatile bool initialized = true;
        alignas(64) volatile bool running = true;
        alignas(64) volatile bool paused = false;
        alignas(64) volatile bool stopping = false;
    } vb;

    std::atomic<bool> done1{false};
    std::thread writer1([&] {
        uint64_t n = 0;
        while (!done1.load(std::memory_order_relaxed)) {
            if (++n % kFlipInterval == 0) {
                vb.paused = true;
                vb.paused = false;
            }
        }
    });

    auto r1 = run_bench("volatile-bools-mt", kIterations, [&](uint64_t i) {
        if (!vb.initialized) return uint64_t(0);
        if (vb.stopping) return uint64_t(0);
        if (vb.paused) return uint64_t(0);
        if (!vb.running) return uint64_t(0);
        return i + 1;
    });
    done1.store(true, std::memory_order_relaxed);
    writer1.join();

    // --- atomic state version ---
    std::atomic<State> astate{State::kRunning};
    std::atomic<bool> done2{false};
    std::thread writer2([&] {
        uint64_t n = 0;
        while (!done2.load(std::memory_order_relaxed)) {
            if (++n % kFlipInterval == 0) {
                astate.store(State::kPaused, std::memory_order_release);
                astate.store(State::kRunning, std::memory_order_release);
            }
        }
    });

    auto r2 = run_bench("atomic-state-mt", kIterations, [&](uint64_t i) {
        if (astate.load(std::memory_order_acquire) != State::kRunning)
            return uint64_t(0);
        return i + 1;
    });
    done2.store(true, std::memory_order_relaxed);
    writer2.join();

    std::printf("  %-30s %8.2f M/s  %8.1f ns/op\n",
                "4x volatile bool:", r1.throughput_mps, r1.latency_ns);
    std::printf("  %-30s %8.2f M/s  %8.1f ns/op\n",
                "1x atomic<State>:", r2.throughput_mps, r2.latency_ns);
    double diff = (r2.throughput_mps - r1.throughput_mps) /
                  r1.throughput_mps * 100.0;
    std::printf("  Throughput difference: %+.1f%%\n\n", diff);
}

// ---------------------------------------------------------------------------
// Principle 2: Context Switches — sleep vs adaptive backoff
// ---------------------------------------------------------------------------

// Simple lock-free SPSC queue for benchmarking
template <uint32_t Capacity>
struct SpscQueue {
    static_assert((Capacity & (Capacity - 1)) == 0, "must be power of 2");
    alignas(64) std::atomic<uint64_t> head{0};
    alignas(64) std::atomic<uint64_t> tail{0};
    alignas(64) uint64_t buf[Capacity]{};

    bool try_push(uint64_t val) {
        uint64_t t = tail.load(std::memory_order_relaxed);
        uint64_t h = head.load(std::memory_order_acquire);
        if (t - h >= Capacity) return false;
        buf[t & (Capacity - 1)] = val;
        tail.store(t + 1, std::memory_order_release);
        return true;
    }

    bool try_pop(uint64_t& val) {
        uint64_t h = head.load(std::memory_order_relaxed);
        uint64_t t = tail.load(std::memory_order_acquire);
        if (h >= t) return false;
        val = buf[h & (Capacity - 1)];
        head.store(h + 1, std::memory_order_release);
        return true;
    }

    uint64_t size() const {
        return tail.load(std::memory_order_acquire) -
               head.load(std::memory_order_acquire);
    }
};

class AdaptiveBackoff {
    uint32_t idle_count_ = 0;
    static constexpr uint32_t kSpinThreshold = 64;
    static constexpr uint32_t kYieldThreshold = 256;

public:
    void wait() {
        ++idle_count_;
        if (idle_count_ < kSpinThreshold) {
            for (int i = 0; i < 32; ++i) {
#if defined(__x86_64__)
                __builtin_ia32_pause();
#elif defined(__aarch64__)
                asm volatile("yield");
#else
                // fallback: compiler barrier
                asm volatile("" ::: "memory");
#endif
            }
        } else if (idle_count_ < kYieldThreshold) {
            std::this_thread::yield();
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    void reset() { idle_count_ = 0; }
};

static void bench_principle2() {
    print_separator();
    std::printf("Principle 2: Context Switches — sleep vs adaptive backoff\n");
    print_separator();

    constexpr uint32_t kQueueSize = 4096;
    constexpr uint64_t kMessages = 1'000'000;
    static constexpr uint32_t kBurstSize = 50;
    // Gap ~50us: adaptive backoff catches it in spin phase (~10us),
    // but sleep(1ms) overshoots by 950us, accumulating messages.
    static constexpr uint32_t kGapUs = 50;

    // Producer sends bursts with gaps to create idle periods
    auto producer_fn = [](SpscQueue<kQueueSize>& q, uint64_t msgs) {
        for (uint64_t i = 0; i < msgs;) {
            // Send a burst
            for (uint32_t b = 0; b < kBurstSize && i < msgs; ++b, ++i) {
                while (!q.try_push(i)) {
                    std::this_thread::yield();
                }
            }
            // Gap between bursts — consumer will be idle during this time
            auto target = Clock::now() + std::chrono::microseconds(kGapUs);
            while (Clock::now() < target) {}
        }
    };

    // --- Version 1: Always sleep(1ms) when queue empty ---
    {
        SpscQueue<kQueueSize> q;
        std::atomic<bool> done{false};
        uint64_t consumed = 0;

#if defined(__linux__)
        uint64_t ctx_before = get_voluntary_ctx_switches();
        uint64_t ictx_before = get_involuntary_ctx_switches();
#endif
        auto t_start = Clock::now();

        std::thread consumer([&] {
            uint64_t val;
            while (!done.load(std::memory_order_relaxed) || q.size() > 0) {
                if (q.try_pop(val)) {
                    ++consumed;
                } else {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
            }
        });

        producer_fn(q, kMessages);
        done.store(true, std::memory_order_release);
        consumer.join();

        auto t_end = Clock::now();
        double elapsed_ms = std::chrono::duration_cast<
            std::chrono::milliseconds>(t_end - t_start).count();

#if defined(__linux__)
        uint64_t vctx = get_voluntary_ctx_switches() - ctx_before;
        uint64_t ictx = get_involuntary_ctx_switches() - ictx_before;
        std::printf("  [sleep(1ms)]   %lu msgs in %.0f ms  "
                    "vol_csw: %lu  invol_csw: %lu\n",
                    consumed, elapsed_ms, vctx, ictx);
#else
        std::printf("  [sleep(1ms)]   %lu msgs in %.0f ms\n",
                    consumed, elapsed_ms);
#endif
    }

    // --- Version 2: Adaptive backoff ---
    {
        SpscQueue<kQueueSize> q;
        std::atomic<bool> done{false};
        uint64_t consumed = 0;

#if defined(__linux__)
        uint64_t ctx_before = get_voluntary_ctx_switches();
        uint64_t ictx_before = get_involuntary_ctx_switches();
#endif
        auto t_start = Clock::now();

        std::thread consumer([&] {
            AdaptiveBackoff backoff;
            uint64_t val;
            while (!done.load(std::memory_order_relaxed) || q.size() > 0) {
                if (q.try_pop(val)) {
                    ++consumed;
                    backoff.reset();
                } else {
                    backoff.wait();
                }
            }
        });

        producer_fn(q, kMessages);
        done.store(true, std::memory_order_release);
        consumer.join();

        auto t_end = Clock::now();
        double elapsed_ms = std::chrono::duration_cast<
            std::chrono::milliseconds>(t_end - t_start).count();

#if defined(__linux__)
        uint64_t vctx = get_voluntary_ctx_switches() - ctx_before;
        uint64_t ictx = get_involuntary_ctx_switches() - ictx_before;
        std::printf("  [adaptive]     %lu msgs in %.0f ms  "
                    "vol_csw: %lu  invol_csw: %lu\n",
                    consumed, elapsed_ms, vctx, ictx);
#else
        std::printf("  [adaptive]     %lu msgs in %.0f ms\n",
                    consumed, elapsed_ms);
#endif
    }
    std::printf("\n");
}

// ---------------------------------------------------------------------------
// Principle 3: Producer-Consumer Rate Balance
// ---------------------------------------------------------------------------

template <uint32_t Capacity>
struct MpscDropQueue {
    static_assert((Capacity & (Capacity - 1)) == 0, "must be power of 2");
    std::atomic<uint64_t> head{0};
    std::atomic<uint64_t> tail{0};
    uint64_t buf[Capacity]{};
    std::atomic<uint64_t> drop_count{0};

    void push_or_drop(uint64_t val) {
        uint64_t t = tail.load(std::memory_order_relaxed);
        uint64_t h = head.load(std::memory_order_acquire);
        if (t - h >= Capacity) {
            drop_count.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        buf[t & (Capacity - 1)] = val;
        tail.store(t + 1, std::memory_order_release);
    }

    bool try_pop(uint64_t& val) {
        uint64_t h = head.load(std::memory_order_relaxed);
        uint64_t t = tail.load(std::memory_order_acquire);
        if (h >= t) return false;
        val = buf[h & (Capacity - 1)];
        head.store(h + 1, std::memory_order_release);
        return true;
    }
};

static void bench_principle3() {
    print_separator();
    std::printf("Principle 3: Producer-Consumer Rate Balance\n");
    print_separator();

    static constexpr uint64_t kDuration_ms = 2000;

    auto run_config = [](const char* label, uint32_t produce_delay_ns,
                         uint32_t consume_delay_ns, auto& queue) {
        std::atomic<bool> done{false};
        std::atomic<uint64_t> produced{0};
        std::atomic<uint64_t> consumed{0};

        std::thread producer([&] {
            uint64_t seq = 0;
            while (!done.load(std::memory_order_relaxed)) {
                queue.push_or_drop(seq++);
                produced.fetch_add(1, std::memory_order_relaxed);
                if (produce_delay_ns > 0) {
                    auto target = Clock::now() +
                        std::chrono::nanoseconds(produce_delay_ns);
                    while (Clock::now() < target) {}
                }
            }
        });

        std::thread consumer([&] {
            uint64_t val;
            while (!done.load(std::memory_order_relaxed)) {
                if (queue.try_pop(val)) {
                    consumed.fetch_add(1, std::memory_order_relaxed);
                }
                if (consume_delay_ns > 0) {
                    auto target = Clock::now() +
                        std::chrono::nanoseconds(consume_delay_ns);
                    while (Clock::now() < target) {}
                }
            }
            // drain remaining
            while (queue.try_pop(val)) {
                consumed.fetch_add(1, std::memory_order_relaxed);
            }
        });

        std::this_thread::sleep_for(std::chrono::milliseconds(kDuration_ms));
        done.store(true, std::memory_order_release);
        producer.join();
        consumer.join();

        uint64_t p = produced.load();
        uint64_t c = consumed.load();
        uint64_t d = queue.drop_count.load();
        double drop_pct = p > 0 ? static_cast<double>(d) / p * 100.0 : 0.0;
        double p_rate = static_cast<double>(p) / (kDuration_ms / 1000.0) / 1e6;
        double c_rate = static_cast<double>(c) / (kDuration_ms / 1000.0) / 1e6;

        std::printf("  %-42s P=%.2fM/s C=%.2fM/s drop=%.1f%%\n",
                    label, p_rate, c_rate, drop_pct);
    };

    // Config A: fast producer, slow consumer, small queue (4K)
    {
        MpscDropQueue<4096> q;
        run_config("A: P=fast C=slow  Q=4K", 0, 500, q);
    }
    // Config B: fast producer, slow consumer, large queue (64K)
    {
        MpscDropQueue<65536> q;
        run_config("B: P=fast C=slow  Q=64K", 0, 500, q);
    }
    // Config C: balanced rate, small queue (4K)
    {
        MpscDropQueue<4096> q;
        run_config("C: P=balanced C=balanced Q=4K", 200, 200, q);
    }
    std::printf("\n");
}

// ---------------------------------------------------------------------------
// Principle 4: Measurement — different metrics tell different stories
//   (Integrated into Principle 2 output; also show batch vs single)
// ---------------------------------------------------------------------------

static void bench_principle4() {
    print_separator();
    std::printf("Principle 4: Batch Processing vs Single Processing\n");
    std::printf("  (Demonstrates how batching reduces per-message overhead)\n");
    print_separator();

    constexpr uint64_t kTotal = 10'000'000;

    // Shared state that must be updated (simulates shared counter/stats)
    volatile uint64_t shared_counter = 0;

    // Single processing: update shared state per message
    {
        auto start = Clock::now();
        for (uint64_t i = 0; i < kTotal; ++i) {
            // Each message reads + writes shared state (cache line bounce)
            uint64_t val = shared_counter;
            val += i * 3 + 1;
            shared_counter = val;
        }
        auto end = Clock::now();
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            end - start).count();
        std::printf("  %-30s %8.2f M/s  %6.1f ns/op\n", "Single (1 store/msg):",
                    kTotal / (ns / 1e9) / 1e6, ns / kTotal);
    }

    shared_counter = 0;

    // Batch processing: accumulate locally, update shared state per batch
    {
        constexpr uint32_t kBatch = 64;
        auto start = Clock::now();
        for (uint64_t i = 0; i < kTotal; i += kBatch) {
            uint64_t local = 0;
            uint32_t count = (i + kBatch <= kTotal) ? kBatch :
                             static_cast<uint32_t>(kTotal - i);
            for (uint32_t j = 0; j < count; ++j) {
                local += (i + j) * 3 + 1;
            }
            // One shared state update per batch (not per message)
            uint64_t val = shared_counter;
            val += local;
            shared_counter = val;
        }
        auto end = Clock::now();
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            end - start).count();
        std::printf("  %-30s %8.2f M/s  %6.1f ns/op\n",
                    "Batch-64 (1 store/64msg):",
                    kTotal / (ns / 1e9) / 1e6, ns / kTotal);
    }
    std::printf("\n");
}

// ---------------------------------------------------------------------------
// Principle 5: Optimization Boundaries — spin wait on multi-core vs same-core
// ---------------------------------------------------------------------------

static void bench_principle5() {
    print_separator();
    std::printf("Principle 5: Spin Wait — multi-core vs single-core\n");
    print_separator();

    constexpr uint64_t kMessages = 2'000'000;

    auto run_test = [](const char* label, int pin_core) {
        SpscQueue<4096> q;
        std::atomic<bool> done{false};
        uint64_t consumed = 0;

        auto t_start = Clock::now();

        std::thread consumer([&, pin_core] {
#if defined(__linux__)
            if (pin_core >= 0) {
                cpu_set_t cpuset;
                CPU_ZERO(&cpuset);
                CPU_SET(pin_core, &cpuset);
                sched_setaffinity(0, sizeof(cpuset), &cpuset);
            }
#endif
            uint64_t val;
            while (!done.load(std::memory_order_relaxed) || q.size() > 0) {
                if (q.try_pop(val)) {
                    ++consumed;
                } else {
                    // Spin wait (no sleep, no yield)
                    for (int i = 0; i < 32; ++i) {
#if defined(__x86_64__)
                        __builtin_ia32_pause();
#elif defined(__aarch64__)
                        asm volatile("yield");
#else
                        asm volatile("" ::: "memory");
#endif
                    }
                }
            }
        });

#if defined(__linux__)
        if (pin_core >= 0) {
            cpu_set_t cpuset;
            CPU_ZERO(&cpuset);
            CPU_SET(pin_core, &cpuset);
            sched_setaffinity(0, sizeof(cpuset), &cpuset);
        }
#endif

        for (uint64_t i = 0; i < kMessages; ++i) {
            while (!q.try_push(i)) {
                // spin
                for (int j = 0; j < 32; ++j) {
#if defined(__x86_64__)
                    __builtin_ia32_pause();
#elif defined(__aarch64__)
                    asm volatile("yield");
#else
                    asm volatile("" ::: "memory");
#endif
                }
            }
        }
        done.store(true, std::memory_order_release);
        consumer.join();

        auto t_end = Clock::now();
        double elapsed_ms = std::chrono::duration_cast<
            std::chrono::milliseconds>(t_end - t_start).count();
        double throughput = consumed / (elapsed_ms / 1000.0) / 1e6;

        std::printf("  %-30s %8.2f M/s  (%.0f ms)\n",
                    label, throughput, elapsed_ms);

#if defined(__linux__)
        // Reset affinity
        if (pin_core >= 0) {
            cpu_set_t cpuset;
            CPU_ZERO(&cpuset);
            for (int i = 0; i < 8; ++i) CPU_SET(i, &cpuset);
            sched_setaffinity(0, sizeof(cpuset), &cpuset);
        }
#endif
    };

    // Multi-core: producer and consumer on different cores (no pinning)
    run_test("Multi-core (no pin):", -1);

    // Single-core: both pinned to core 0
    run_test("Single-core (pin to 0):", 0);

    std::printf("\n");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

int main() {
    std::printf("\n");
    std::printf("Benchmark: Five Counter-Intuitive Principles of "
                "High-Performance System Design\n");
    std::printf("Platform: ");
#if defined(__x86_64__)
    std::printf("x86_64");
#elif defined(__aarch64__)
    std::printf("aarch64");
#else
    std::printf("unknown");
#endif
    std::printf("  Compiler: ");
#if defined(__clang__)
    std::printf("Clang %d.%d.%d", __clang_major__, __clang_minor__,
                __clang_patchlevel__);
#elif defined(__GNUC__)
    std::printf("GCC %d.%d.%d", __GNUC__, __GNUC_MINOR__,
                __GNUC_PATCHLEVEL__);
#endif
    std::printf("  Threads: %u\n\n",
                std::thread::hardware_concurrency());

    bench_principle1();
    bench_principle1_mt();
    bench_principle2();
    bench_principle3();
    bench_principle4();
    bench_principle5();

    std::printf("Done.\n");
    return 0;
}

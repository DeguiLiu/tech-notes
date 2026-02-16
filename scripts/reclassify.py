#!/usr/bin/env python3
"""Reclassify articles into new category folders.

Run from repo root:
    python3 scripts/reclassify.py
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "content" / "posts"

CLASSIFICATION = {
    "architecture": [
        "embedded_streaming_data_architecture.md",
        "lidar_pipeline_newosp.md",
        "mcu_secondary_bootloader.md",
        "rtos_vs_linux_heterogeneous_soc.md",
        "rtos_ao_cooperative_scheduling.md",
        "fpga_arm_soc_lidar_feasibility.md",
        "dual_core_arm_rtthread_smp.md",
        "newosp_concurrency_io_architecture.md",
        "newosp_event_driven_architecture.md",
        "embedded_ab_firmware_upgrade_engine.md",
    ],
    "performance": [
        # benchmarks
        "mccc_zero_heap_optimization_benchmark.md",
        "message_bus_benchmark_methodology.md",
        "eventpp_arm_optimization_report.md",
        "arm_linux_lock_contention_benchmark.md",
        "armv8_crc32_hardware_vs_neon_benchmark.md",
        "parallel_matmul_benchmark.md",
        "cpp_performance_memory_branch_compiler.md",
        "arm_linux_network_optimization.md",
        "cpp17_binary_size_vs_c.md",
        "object_pool_hidden_costs.md",
        "message_bus_competitive_benchmark.md",
        "cpp14_message_bus_optimization.md",
        "embedded_callback_zero_overhead.md",
        # concurrency
        "lockfree_programming_fundamentals.md",
        "lockfree_async_log.md",
        "spsc_ringbuffer_design.md",
        "memory_barrier_hardware.md",
        "deadlock_priority_inversion_practice.md",
        "embedded_deadlock_prevention_lockfree.md",
        "cpp_singleton_thread_safety_dclp.md",
        "unix_domain_socket_realtime.md",
        "tcp_ringbuffer_short_write.md",
        "shared_memory_ipc_lockfree_ringbuffer.md",
        "mccc_lockfree_mpsc_design.md",
        "cpp11_threadsafe_pubsub_bus.md",
        "mccc_message_passing.md",
    ],
    "practice": [
        "dbpp_cpp14_database_modernization.md",
        "clang_tidy_embedded_cpp17.md",
        "mccc_bus_cpp17_practice.md",
        "mccc_bus_api_reference.md",
        "newosp_industrial_embedded_library.md",
        "ztask_scheduler.md",
        "ztask_cpp_modernization.md",
        "qpc_active_object_hsm.md",
        "behavior_tree_tick_mechanism.md",
        "cpp17_claims_in_newosp.md",
    ],
    "pattern": [
        "cpp_design_patterns_embedded.md",
        "compile_time_dispatch_optimization.md",
        "smart_pointer_pitfalls_embedded.md",
        "cpp17_what_c_cannot_do.md",
        "c_oop_nginx_modular_architecture.md",
        "c_hsm_data_driven_framework.md",
        "c_strategy_state_pattern.md",
    ],
    "tools": [
        "rtthread_msh_linux_multibackend.md",
        "telnet_debug_shell_posix_refactoring.md",
        "embedded_ssh_scp_automation.md",
        "lmdb_embedded_linux_zero_copy.md",
        "embedded_config_serialization.md",
        "cpp14_pluggable_log_library_design.md",
        "perf_lock_contention_diagnosis.md",
        "perf_performance_analysis.md",
        "uart_protocol_parsing.md",
        "newosp_ospgen_codegen.md",
        "newosp_shell_multibackend.md",
    ],
}

INDEX_TITLES = {
    "architecture": "架构设计",
    "performance": "性能优化",
    "practice": "工程实践",
    "pattern": "设计模式",
    "tools": "开发工具",
}


def find_file(filename):
    """Find a file across all category dirs."""
    for d in POSTS.iterdir():
        if not d.is_dir():
            continue
        candidate = d / filename
        if candidate.exists():
            return candidate
    return None


def update_category(filepath, new_cat):
    """Update categories field in front matter."""
    text = filepath.read_text(encoding="utf-8")
    text = re.sub(
        r'^categories:\s*\[.*?\]',
        f'categories: ["{new_cat}"]',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    filepath.write_text(text, encoding="utf-8")


def main():
    moved = 0
    updated = 0
    errors = []

    for cat, files in CLASSIFICATION.items():
        dest_dir = POSTS / cat
        dest_dir.mkdir(exist_ok=True)

        for fname in files:
            src = find_file(fname)
            if src is None:
                errors.append(f"NOT FOUND: {fname}")
                continue

            dest = dest_dir / fname
            if src == dest:
                update_category(dest, cat)
                updated += 1
                continue

            result = subprocess.run(
                ["git", "mv", str(src), str(dest)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                errors.append(f"git mv failed: {fname}: {result.stderr}")
                continue

            update_category(dest, cat)
            moved += 1

    # Create _index.md for new categories
    for cat, title in INDEX_TITLES.items():
        idx = POSTS / cat / "_index.md"
        if not idx.exists():
            idx.write_text(f'---\ntitle: "{title}"\n---\n', encoding="utf-8")
            print(f"  Created _index.md: {cat}/")

    # Remove empty old dirs (blog, mccc, newosp, etc.)
    for d in sorted(POSTS.iterdir()):
        if not d.is_dir() or d.name in CLASSIFICATION:
            continue
        remaining = [f for f in d.iterdir() if f.name != "_index.md"]
        if not remaining:
            idx = d / "_index.md"
            if idx.exists():
                subprocess.run(["git", "rm", str(idx)], capture_output=True)
            try:
                d.rmdir()
            except OSError:
                pass
            print(f"  Removed empty dir: {d.name}/")

    print(f"\nMoved: {moved}, Updated in-place: {updated}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    main()

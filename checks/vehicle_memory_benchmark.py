"""RAM-бенчмарк: полные имена в кэше (A) против компактных model_id (B).

A — замороженная схема v3 и старый VehicleMemory: каждая запись клиента хранит
    полную строку названия модели.
B — схема v5 и новый VehicleMemory: записи клиента хранят model_id, названия
    лежат в общем bounded-кэше model_id -> canonical_name.

Запуск:
  .venv/bin/python checks/vehicle_memory_benchmark.py --customers 1000,10000,100000
  .venv/bin/python checks/vehicle_memory_benchmark.py --customers 100000 --json /tmp/memory.json
"""
from __future__ import annotations

import argparse
import gc
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(FOLDER))

import legacy_vehicles  # noqa: E402
import vehicles  # noqa: E402
from vehicle_benchmark import (build_a, build_b, customer_ids, db_stats, peak_rss_kb,  # noqa: E402
                               rss_kb)


def make_memory(arch: str, path: Path, cache_mb: float):
    if arch == "A":
        return legacy_vehicles.VehicleMemory(path, cache_mb=cache_mb, adaptive=False)
    return vehicles.VehicleMemory(path, cache_mb=cache_mb, adaptive=False)


def percentiles(samples):
    ordered = sorted(samples)
    return {"p50_us": round(ordered[len(ordered) // 2] * 1_000_000, 2),
            "p95_us": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1_000_000, 2),
            "mean_us": round(statistics.mean(ordered) * 1_000_000, 2)}


def timed_users(memory, ids):
    samples = []
    for telegram_id in ids:
        started = time.perf_counter()
        memory.for_user(telegram_id)
        samples.append(time.perf_counter() - started)
    return samples


def cold_warm(arch: str, path: Path, customers: int) -> dict:
    memory = make_memory(arch, path, 256)
    ids = customer_ids(customers * 2)[:1000]
    cold = timed_users(memory, ids)
    cold_reads = memory.db_reads
    cold_hits, cold_misses = memory.cache.hits, memory.cache.misses
    warm = timed_users(memory, ids)
    result = {
        "customers": len(ids),
        "cold": percentiles(cold),
        "warm": percentiles(warm),
        "cold_db_reads": cold_reads,
        "warm_db_reads": memory.db_reads - cold_reads,
        "cold_hits": cold_hits, "cold_misses": cold_misses,
        "warm_hits": memory.cache.hits - cold_hits,
        "warm_misses": memory.cache.misses - cold_misses,
    }
    memory.close()
    return result


def rss_load(arch: str, path: Path, customers: int) -> dict:
    memory = make_memory(arch, path, 256)
    ids = customer_ids(customers * 2)
    gc.collect()
    before = rss_kb()
    started = time.perf_counter()
    for telegram_id in ids:
        memory.for_user(telegram_id)
    elapsed = time.perf_counter() - started
    gc.collect()
    after = rss_kb()
    result = {
        "customers": len(ids), "load_sec": round(elapsed, 3),
        "per_customer_us": round(elapsed / len(ids) * 1_000_000, 2),
        "rss_before_kb": before, "rss_after_kb": after, "rss_delta_kb": after - before,
        "cache_entries": len(memory.cache), "cache_bytes": memory.cache.bytes_used,
        "cache_kb": round(memory.cache.bytes_used / 1024, 1),
        "model_cache_entries": len(getattr(memory, "model_cache", ())),
        "model_cache_bytes": getattr(getattr(memory, "model_cache", None), "bytes_used", 0),
        "model_cache_kb": round(getattr(getattr(memory, "model_cache", None), "bytes_used", 0) / 1024, 1),
        "db_reads": memory.db_reads,
        "cache_hits": memory.cache.hits, "cache_misses": memory.cache.misses,
        "peak_rss_kb": peak_rss_kb(),
    }
    memory.close()
    return result


def traced_load(arch: str, path: Path, customers: int) -> dict:
    memory = make_memory(arch, path, 256)
    ids = customer_ids(customers * 2)
    gc.collect()
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    for telegram_id in ids:
        memory.for_user(telegram_id)
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = {
        "customers": len(ids),
        "traced_current_kb": round((current - baseline) / 1024, 1),
        "traced_peak_kb": round((peak - baseline) / 1024, 1),
        "cache_bytes": memory.cache.bytes_used,
        "model_cache_bytes": getattr(getattr(memory, "model_cache", None), "bytes_used", 0),
    }
    memory.close()
    return result


def capacity_4mb(arch: str, path: Path, customers: int) -> dict:
    memory = make_memory(arch, path, 4)
    ids = customer_ids(customers * 2)
    started = time.perf_counter()
    for telegram_id in ids:
        memory.for_user(telegram_id)
    load_sec = time.perf_counter() - started
    entries = len(memory.cache)
    cache_bytes = memory.cache.bytes_used
    hits, misses = memory.cache.hits, memory.cache.misses
    recent = ids[-1000:]
    recent_started = time.perf_counter()
    for telegram_id in recent:
        memory.for_user(telegram_id)
    recent_sec = time.perf_counter() - recent_started
    recent_hits = memory.cache.hits - hits
    recent_misses = memory.cache.misses - misses
    oldest = ids[:1000]
    hits_before_old, misses_before_old = memory.cache.hits, memory.cache.misses
    old_started = time.perf_counter()
    for telegram_id in oldest:
        memory.for_user(telegram_id)
    old_sec = time.perf_counter() - old_started
    old_hits = memory.cache.hits - hits_before_old
    old_misses = memory.cache.misses - misses_before_old
    result = {
        "customers": len(ids), "load_sec": round(load_sec, 3),
        "entries": entries, "cache_bytes": cache_bytes,
        "cache_kb": round(cache_bytes / 1024, 1),
        "capacity_hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
        "recent_1000": {"sec": round(recent_sec, 4), "hits": recent_hits,
                        "misses": recent_misses,
                        "hit_rate": round(recent_hits / (recent_hits + recent_misses), 4)
                        if recent_hits + recent_misses else None},
        "oldest_1000": {"sec": round(old_sec, 4), "hits": old_hits, "misses": old_misses,
                        "hit_rate": round(old_hits / (old_hits + old_misses), 4)
                        if old_hits + old_misses else None},
        "model_cache_entries": len(getattr(memory, "model_cache", ())),
        "model_cache_kb": round(getattr(getattr(memory, "model_cache", None), "bytes_used", 0) / 1024, 1),
    }
    memory.close()
    return result


def batch_resolution(arch: str, path: Path) -> dict:
    memory = make_memory(arch, path, 256)
    telegram_id = customer_ids(4)[1]  # у этой пары обе машины активны
    memory.for_user(telegram_id)
    if not hasattr(memory, "model_cache"):
        memory.close()
        return {"supported": False}
    memory.model_cache.clear()
    calls = []
    original = memory.db.model_names
    def counted(model_ids):
        calls.append(set(model_ids))
        return original(model_ids)
    memory.db.model_names = counted
    shown = memory.for_user(telegram_id)
    memory.close()
    return {"supported": True, "calls": len(calls),
            "ids_per_call": [len(ids) for ids in calls],
            "names": [car.name for car in shown]}


def worker(arch: str, customers: int, temp_dir: str | None) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"carwash-memory-{arch}-", dir=temp_dir) as folder:
        path = Path(folder) / "vehicles.sqlite3"
        vehicle_count = customers * 2
        if arch == "A":
            build_a(path, vehicle_count)
        else:
            build_b(path, vehicle_count)
        db_bytes = db_stats(path)["db_bytes"]
        return {
            "arch": arch, "customers": customers, "vehicles": vehicle_count,
            "db_bytes": db_bytes, "db_mb": round(db_bytes / 1024 / 1024, 2),
            "cold_warm": cold_warm(arch, path, customers),
            "rss_load": rss_load(arch, path, customers),
            "traced_load": traced_load(arch, path, customers),
            "capacity_4mb": capacity_4mb(arch, path, customers),
            "batch_resolution": batch_resolution(arch, path),
            "peak_rss_kb": peak_rss_kb(),
        }


def print_table(results: dict) -> None:
    print("\n=== RAM-кэш: полные имена (A) против model_id (B) ===")
    header = ("customers", "arch", "db_MB", "cache_KB", "model_KB", "entries",
              "rss_delta_MB", "traced_KB", "cold_us", "warm_us", "warm_hit", "4MB_entries", "4MB_recent_hit")
    print("  ".join(f"{name:>13}" for name in header))
    for customers in sorted(results):
        for arch in ("A", "B"):
            item = results[customers][arch]
            rss = item["rss_load"]
            traced = item["traced_load"]
            cold = item["cold_warm"]
            capacity = item["capacity_4mb"]
            row = (customers, arch, item["db_mb"], rss["cache_kb"], rss["model_cache_kb"],
                   rss["cache_entries"], round(rss["rss_delta_kb"] / 1024, 1),
                   traced["traced_current_kb"], cold["cold"]["p50_us"], cold["warm"]["p50_us"],
                   cold["warm_hits"] / max(1, cold["warm_hits"] + cold["warm_misses"]),
                   capacity["entries"], capacity["recent_1000"]["hit_rate"])
            print("  ".join(f"{str(value):>13}" for value in row))
    print("\n=== Батч-разрешение имён (B) ===")
    for customers in sorted(results):
        print(customers, json.dumps(results[customers]["B"]["batch_resolution"], ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customers", default="1000,10000,100000")
    parser.add_argument("--temp-dir", default="")
    parser.add_argument("--json", default="")
    parser.add_argument("--worker", choices=("A", "B"))
    parser.add_argument("--count", type=int, default=0)
    arguments = parser.parse_args()
    if arguments.worker:
        print(json.dumps(worker(arguments.worker, arguments.count, arguments.temp_dir or None),
                         ensure_ascii=False))
        return 0
    scales = [int(value) for value in arguments.customers.split(",") if value.strip()]
    results = {}
    for customers in scales:
        pair = {}
        for arch in ("A", "B"):
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", arch,
                       "--count", str(customers)]
            if arguments.temp_dir:
                command.extend(["--temp-dir", arguments.temp_dir])
            process = subprocess.run(command, capture_output=True, text=True)
            if process.returncode != 0:
                print(process.stderr, file=sys.stderr)
                raise SystemExit(f"RAM-бенчмарк {arch} x {customers} завершился с ошибкой")
            pair[arch] = json.loads(process.stdout)
        results[customers] = pair
        print(f"готово: {customers} клиентов", flush=True)
    print_table(results)
    if arguments.json:
        Path(arguments.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {arguments.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

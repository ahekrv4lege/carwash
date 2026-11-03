"""Воспроизводимый A/B-бенчмарк памяти автомобилей.

A — прежняя схема v3: car_name/car_key повторяются в каждой строке клиента.
B — новая схема v4: общий справочник vehicle_models + model_id.

Запуск из корня проекта:
  .venv/bin/python checks/vehicle_benchmark.py --scales 1000,10000,100000,1000000
  .venv/bin/python checks/vehicle_benchmark.py --scales 1000 --skip-ai

Каждый масштаб и архитектура считаются в отдельном процессе, чтобы пик RSS
не смешивался. Настоящие API, Google Sheets и production SQLite не используются.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import random
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(FOLDER))

import legacy_vehicles  # noqa: E402
import vehicles  # noqa: E402
from vehicles import car_key, plate_key  # noqa: E402

CLASSES = ("Легковой", "Кроссовер", "Внедорожник", "Минивэн")
TARIFFS = ("Базовый", "Стандарт", "Экспресс")
COMMON_MODELS = (
    "Toyota Camry 3.5", "Toyota Camry 2.5", "Toyota RAV4", "Toyota Corolla",
    "Kia Rio", "Kia Rio X-Line", "Kia Sportage", "Hyundai Solaris", "Hyundai Creta",
    "BMW 5 Series", "BMW X5", "BMW 3 Series", "Mercedes E-Class", "Mercedes GLC",
    "Lada Vesta", "Lada Granta", "Renault Logan", "Skoda Octavia", "Volkswagen Polo",
    "Nissan Qashqai", "Mazda CX-5", "Mazda3", "Ford Focus", "Ford Kuga",
    "Audi A4", "Audi Q5", "Volvo XC60", "Volvo S90", "Mitsubishi Lancer",
    "Mitsubishi Outlander", "Peugeot 408", "Citroen C4", "Haval F7", "Haval Dargo",
    "Chery Tiggo 7 Pro", "Geely Atlas", "UAZ Patriot", "GAZelle Next", "Suzuki Vitara",
    "Subaru Forester",
)
TAIL_MODELS = tuple(f"Rare Brand {index} Model {index}" for index in range(1, 5001))
AI_DELAY_SEC = 0.05


def rss_kb() -> int:
    try:
        with open(f"/proc/{os.getpid()}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def peak_rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _mix(index: int) -> int:
    """Детерминированный 64-битный микс: независимые доли частых/редких моделей."""
    value = (index + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return value ^ (value >> 31)


def pick_name(index: int, avoid_key: str | None = None) -> str:
    """85% — частые модели, 15% — длинный хвост; одинаковые ключи не дублируются."""
    for attempt in range(8):
        mixed = _mix(index + attempt * 0x9E3779B9)
        pool = COMMON_MODELS if mixed % 100 < 85 else TAIL_MODELS
        start = (mixed >> 32) % len(pool)
        for offset in range(len(pool)):
            name = pool[(start + offset) % len(pool)]
            if avoid_key is None or car_key(name) != avoid_key:
                return name
    return pool[start]


def record_at(index: int):
    """Одна запись набора. Функция, чтобы не держать 1M кортежей в памяти."""
    customer = index // 2
    telegram_id = 1_000_000 + customer
    if index % 2 == 0:
        name = pick_name(index)
        key = car_key(name)
    else:
        first_name = pick_name(index - 1)
        name = pick_name(index, avoid_key=car_key(first_name))
        key = car_key(name)
    status = "deleted" if index % 20 == 0 else "active"
    day = index % 28 + 1
    month = index % 12 + 1
    created = f"2025-{month:02d}-{day:02d} 09:00:00"
    updated = f"2026-{month:02d}-{day:02d} 18:00:00"
    services = '["Мойка"]' if index % 3 else '["Мойка", "Воск"]'
    plate = f"А{index % 1000:03d}АА{index % 100:02d}"
    return (index + 1, telegram_id, name, key, CLASSES[index % len(CLASSES)],
            status, created, updated, TARIFFS[index % len(TARIFFS)], services, plate)


def iter_records(count: int):
    """Одинаковый поток записей для A и B. У клиента не больше двух разных машин."""
    for index in range(count):
        yield record_at(index)


def legacy_schema(connection):
    connection.execute(
        "CREATE TABLE vehicles ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,"
        " car_key TEXT NOT NULL, car_name TEXT NOT NULL, size_class TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " last_tariff TEXT NOT NULL DEFAULT '', last_services TEXT NOT NULL DEFAULT '',"
        " last_plate TEXT NOT NULL DEFAULT '')")
    connection.execute("CREATE UNIQUE INDEX vehicles_active_key "
                       "ON vehicles (telegram_id, car_key) WHERE status = 'active'")


def connect(path: Path, *, foreign_keys=False) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA temp_store=MEMORY")
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys=ON")
    return connection


def build_a(path: Path, count: int) -> float:
    connection = connect(path)
    started = time.perf_counter()
    try:
        legacy_schema(connection)
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO vehicles (id, telegram_id, car_key, car_name, size_class, status, "
            "created_at, updated_at, last_tariff, last_services, last_plate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((row[0], row[1], row[3], row[2], *row[4:]) for row in iter_records(count)))
        connection.execute("COMMIT")
    finally:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    return time.perf_counter() - started


def build_b(path: Path, count: int) -> tuple[float, int]:
    database = vehicles.VehicleDatabase(path)
    database.list_active(0)
    database.close()
    models = {}
    for _, _, name, key, *_ in iter_records(count):
        models.setdefault(key, name)
    connection = connect(path, foreign_keys=True)
    started = time.perf_counter()
    try:
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO vehicle_models (model_key, canonical_name, created_at) VALUES (?, ?, ?)",
            ((key, name, "2025-01-01 00:00:00") for key, name in models.items()))
        model_ids = dict(connection.execute("SELECT model_key, model_id FROM vehicle_models"))
        connection.executemany(
            "INSERT INTO vehicles (id, telegram_id, model_id, plate_key, size_class, status, "
            "created_at, updated_at, last_tariff, last_services, last_plate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((row[0], row[1], model_ids[row[3]], plate_key(row[10]), *row[4:])
             for row in iter_records(count)))
        connection.execute("COMMIT")
    finally:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    return time.perf_counter() - started, len(models)


V4_MODELS_TABLE = (
    "CREATE TABLE vehicle_models (model_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "model_key TEXT NOT NULL UNIQUE, canonical_name TEXT NOT NULL, created_at TEXT NOT NULL)"
)
V4_VEHICLES_TABLE = (
    "CREATE TABLE vehicles ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,"
    " model_id INTEGER NOT NULL REFERENCES vehicle_models(model_id) ON DELETE RESTRICT,"
    " size_class TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
    " last_tariff TEXT NOT NULL DEFAULT '', last_services TEXT NOT NULL DEFAULT '',"
    " last_plate TEXT NOT NULL DEFAULT '')"
)
V4_INDEX = ("CREATE UNIQUE INDEX vehicles_active_model "
            "ON vehicles(telegram_id, model_id) WHERE status = 'active'")


def build_v4(path: Path, count: int) -> float:
    """Схема v4 без plate_key: исходная база для проверки миграции v4 -> v5."""
    models = {}
    for _, _, name, key, *_ in iter_records(count):
        models.setdefault(key, name)
    connection = connect(path, foreign_keys=True)
    started = time.perf_counter()
    try:
        connection.execute("BEGIN")
        connection.execute(V4_MODELS_TABLE)
        connection.execute(V4_VEHICLES_TABLE)
        connection.execute(V4_INDEX)
        connection.executemany(
            "INSERT INTO vehicle_models (model_key, canonical_name, created_at) VALUES (?, ?, ?)",
            ((key, name, "2025-01-01 00:00:00") for key, name in models.items()))
        model_ids = dict(connection.execute("SELECT model_key, model_id FROM vehicle_models"))
        connection.executemany(
            "INSERT INTO vehicles (id, telegram_id, model_id, size_class, status, "
            "created_at, updated_at, last_tariff, last_services, last_plate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((row[0], row[1], model_ids[row[3]], *row[4:]) for row in iter_records(count)))
        connection.execute("PRAGMA user_version = 4")
        connection.execute("COMMIT")
    finally:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
    return time.perf_counter() - started


def db_stats(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        kinds = dict(connection.execute("SELECT name, type FROM sqlite_master"))
        try:
            dbstat = connection.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
        except sqlite3.OperationalError:
            dbstat = []
    finally:
        connection.close()
    sizes = {"": 0, "-wal": 0, "-shm": 0}
    for suffix in sizes:
        try:
            sizes[suffix] = os.path.getsize(str(path) + suffix)
        except OSError:
            pass
    table_bytes = sum(size for name, size in dbstat if kinds.get(name) == "table")
    index_bytes = sum(size for name, size in dbstat if kinds.get(name) == "index")
    return {"db_bytes": sizes[""], "wal_bytes": sizes["-wal"], "total_bytes": sum(sizes.values()),
            "page_size": page_size, "page_count": page_count, "freelist_pages": freelist,
            "table_bytes": table_bytes, "index_bytes": index_bytes,
            "dbstat": [{"name": name, "bytes": size} for name, size in dbstat]}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def latency(values):
    return {"mean_ms": round(statistics.mean(values) * 1000, 4),
            "p50_ms": round(percentile(values, 0.50) * 1000, 4),
            "p95_ms": round(percentile(values, 0.95) * 1000, 4),
            "max_ms": round(max(values) * 1000, 4)}


def timed_calls(function, count):
    samples = []
    result = None
    for _ in range(count):
        started = time.perf_counter()
        result = function()
        samples.append(time.perf_counter() - started)
    return latency(samples), result


def customer_ids(count):
    return [1_000_000 + index for index in range(max(1, count // 2))]


def model_id_map(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        return dict(connection.execute("SELECT model_key, model_id FROM vehicle_models"))
    finally:
        connection.close()


def read_metrics(path: Path, arch: str, count: int) -> dict:
    rng = random.Random(42)
    indexes = [rng.randrange(count) for _ in range(500)]
    records = {index: record_at(index) for index in set(indexes)}
    models = model_id_map(path) if arch == "B" else {}
    result = {}
    for cache_kb in (512, 65536):
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA cache_size=-%d" % cache_kb)
        connection.execute("PRAGMA shrink_memory")
        if arch == "A":
            lookup_sql = ("SELECT id, car_name, size_class, last_tariff, last_services, last_plate "
                          "FROM vehicles WHERE telegram_id = ? AND car_key = ? AND status = 'active'")
            list_sql = ("SELECT id, car_name, size_class, last_tariff, last_services, last_plate "
                        "FROM vehicles WHERE telegram_id = ? AND status = 'active' "
                        "ORDER BY updated_at DESC, id DESC")
        else:
            lookup_sql = ("SELECT v.id, m.canonical_name, v.size_class, v.last_tariff, "
                          "v.last_services, v.last_plate FROM vehicles v "
                          "JOIN vehicle_models m ON m.model_id = v.model_id "
                          "WHERE v.telegram_id = ? AND v.model_id = ? AND v.status = 'active'")
            list_sql = ("SELECT v.id, m.canonical_name, v.size_class, v.last_tariff, "
                        "v.last_services, v.last_plate FROM vehicles v "
                        "JOIN vehicle_models m ON m.model_id = v.model_id "
                        "WHERE v.telegram_id = ? AND v.status = 'active' "
                        "ORDER BY v.updated_at DESC, v.id DESC")
        state = {"lookup": 0, "list": 0}
        def lookup():
            index = indexes[state["lookup"] % len(indexes)]
            state["lookup"] += 1
            row = records[index]
            if arch == "A":
                return connection.execute(lookup_sql, (row[1], row[3])).fetchone()
            return connection.execute(lookup_sql, (row[1], models[row[3]])).fetchone()
        def listing():
            index = indexes[state["list"] % len(indexes)]
            state["list"] += 1
            return connection.execute(list_sql, (records[index][1],)).fetchall()
        for _ in range(20):
            lookup()
            listing()
        lookup_latency, _ = timed_calls(lookup, 1000)
        list_latency, _ = timed_calls(listing, 500)
        connection.close()
        result[f"cache_{cache_kb}kb"] = {"lookup": lookup_latency, "customer_list": list_latency}
    # Повторное открытие и первый запрос: ОС-кэш уже тёплый, поэтому это
    # стоимость SQLite/Python, а не диска.
    connection = sqlite3.connect(path)
    started = time.perf_counter()
    connection.execute("PRAGMA cache_size=-512")
    connection.execute(list_sql, (records[indexes[0]][1],)).fetchall()
    result["reopen_first_read_ms"] = round((time.perf_counter() - started) * 1000, 4)
    connection.close()
    return result


def update_metrics(path: Path) -> dict:
    connection = connect(path)
    try:
        ids = [row[0] for row in connection.execute("SELECT id FROM vehicles LIMIT 500")]
        samples = []
        for index, vehicle_id in enumerate(ids):
            started = time.perf_counter()
            connection.execute(
                "UPDATE vehicles SET size_class = ?, updated_at = ?, last_tariff = ?, "
                "last_services = ?, last_plate = ? WHERE id = ?",
                (CLASSES[index % len(CLASSES)], "2026-03-01 12:00:00", TARIFFS[index % 3],
                 '["Мойка"]', "О 000 ОО 00", vehicle_id))
            samples.append(time.perf_counter() - started)
        return latency(samples)
    finally:
        connection.close()


def duplicate_metrics(path: Path, arch: str) -> dict:
    database = legacy_vehicles.VehicleDatabase(path) if arch == "A" else vehicles.VehicleDatabase(path)
    samples = []

    def once(index):
        started = time.perf_counter()
        database.upsert(9_999_999, "Benchmark Shared Model", "Легковой",
                        "2026-04-01 10:00:00", tariff="Базовый",
                        services=["Мойка"], plate="В 001 ВВ 77")
        samples.append(time.perf_counter() - started)

    for index in range(300):
        once(index)
    database.close()
    connection = sqlite3.connect(path)
    try:
        models = connection.execute("SELECT COUNT(*) FROM vehicle_models").fetchone()[0] if arch == "B" else None
        rows = connection.execute("SELECT COUNT(*) FROM vehicles WHERE telegram_id = 9999999").fetchone()[0]
    finally:
        connection.close()
    return {**latency(samples), "catalog_models": models, "vehicle_rows": rows}


def concurrent_metrics(path: Path, arch: str) -> dict:
    errors = []

    def worker(thread_index):
        database = legacy_vehicles.VehicleDatabase(path) if arch == "A" else vehicles.VehicleDatabase(path)
        try:
            for index in range(100):
                database.upsert(8_000_000 + thread_index * 1000 + index, "Concurrent Shared Model",
                                "Легковой", "2026-05-01 10:00:00")
        except Exception as error:  # pragma: no cover - не должно происходить
            errors.append(f"{type(error).__name__}: {error}")
        finally:
            database.close()

    import threading

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT COUNT(*) FROM vehicles WHERE telegram_id >= 8000000 AND telegram_id < 8004000").fetchone()[0]
        models = connection.execute("SELECT COUNT(*) FROM vehicle_models").fetchone()[0] if arch == "B" else None
    finally:
        connection.close()
    return {"seconds": round(elapsed, 4), "inserts_per_second": round(400 / elapsed, 1),
            "errors": errors, "rows": rows, "catalog_models": models}


def cache_metrics(path: Path, arch: str, count: int) -> dict:
    before = rss_kb()
    memory = (legacy_vehicles.VehicleMemory(path, cache_mb=256) if arch == "A"
              else vehicles.VehicleMemory(path, cache_mb=256))
    ids = customer_ids(count)[:1000]
    started = time.perf_counter()
    for telegram_id in ids:
        memory.for_user(telegram_id)
    load_sec = time.perf_counter() - started
    loaded_reads = memory.db_reads
    started = time.perf_counter()
    for telegram_id in ids:
        memory.for_user(telegram_id)
    repeat_sec = time.perf_counter() - started
    result = {"customers": len(ids), "entries": len(memory.cache),
              "cache_bytes": memory.cache.bytes_used,
              "cache_kb": round(memory.cache.bytes_used / 1024, 1),
              "model_cache_entries": len(getattr(memory, "model_cache", ())),
              "model_cache_bytes": getattr(getattr(memory, "model_cache", None), "bytes_used", 0),
              "db_reads_first_pass": loaded_reads,
              "db_reads_second_pass": memory.db_reads,
              "load_ms": round(load_sec * 1000, 2), "repeat_ms": round(repeat_sec * 1000, 2),
              "cache_hits": memory.cache.hits, "cache_misses": memory.cache.misses,
              "rss_delta_kb": rss_kb() - before}
    memory.close()
    return result


def worker(arch: str, count: int, temp_dir: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"carwash-bench-{arch}-", dir=temp_dir) as folder:
        path = Path(folder) / "vehicles.sqlite3"
        gc.collect()
        start_rss = rss_kb()
        if arch == "A":
            bulk_sec = build_a(path, count)
            model_count = None
        else:
            bulk_sec, model_count = build_b(path, count)
        after_bulk_rss = rss_kb()
        stats = db_stats(path)
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        result = {
            "arch": arch, "scale": count, "bulk_sec": round(bulk_sec, 3),
            "vehicles": rows, "catalog_models": model_count, "integrity": integrity,
            "rss_start_kb": start_rss, "rss_after_bulk_kb": after_bulk_rss,
            "rss_bulk_delta_kb": after_bulk_rss - start_rss,
            **stats,
            "reads": read_metrics(path, arch, count),
            "update": update_metrics(path),
            "duplicate": duplicate_metrics(path, arch),
            "concurrent": concurrent_metrics(path, arch),
            "cache": cache_metrics(path, arch, count),
            "peak_rss_kb": peak_rss_kb(),
        }
        return result


def migration_worker(count: int, temp_dir: str | None = None, from_version: int = 3) -> dict:
    with tempfile.TemporaryDirectory(prefix="carwash-bench-migrate-", dir=temp_dir) as folder:
        path = Path(folder) / f"legacy-v{from_version}.sqlite3"
        if from_version == 4:
            build_v4(path, count)
        else:
            build_a(path, count)
        before = db_stats(path)
        connection = sqlite3.connect(path)
        try:
            ids_before = [row[0] for row in connection.execute("SELECT id FROM vehicles ORDER BY id")]
            rows_before = len(ids_before)
        finally:
            connection.close()
        gc.collect()
        start_rss = rss_kb()
        started = time.perf_counter()
        database = vehicles.VehicleDatabase(path)
        database.list_active(1_000_000)
        migration_sec = time.perf_counter() - started
        database.close()
        # Закрытие последнего соединения и checkpoint убирают WAL; удалённая
        # старая таблица остаётся свободными страницами до VACUUM.
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        after_migration = db_stats(path)
        vacuum_started = time.perf_counter()
        connection = sqlite3.connect(path)
        connection.execute("VACUUM")
        connection.close()
        vacuum_sec = time.perf_counter() - vacuum_started
        after = db_stats(path)
        connection = sqlite3.connect(path)
        try:
            rows_after = connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
            ids_after = [row[0] for row in connection.execute("SELECT id FROM vehicles ORDER BY id")]
            model_count = connection.execute("SELECT COUNT(*) FROM vehicle_models").fetchone()[0]
            plate_filled = connection.execute(
                "SELECT COUNT(*) FROM vehicles WHERE plate_key <> ''").fetchone()[0]
            plate_unknown = connection.execute(
                "SELECT COUNT(*) FROM vehicles WHERE plate_key = ''").fetchone()[0]
            plate_sample = connection.execute(
                "SELECT plate_key FROM vehicles ORDER BY id LIMIT 1").fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
        return {"scale": count, "from_version": from_version,
                "migration_sec": round(migration_sec, 3),
                "vacuum_sec": round(vacuum_sec, 3),
                "rows_before": rows_before, "rows_after": rows_after,
                "ids_preserved": ids_before == ids_after, "catalog_models": model_count,
                "plate_filled": plate_filled, "plate_unknown": plate_unknown,
                "plate_sample": plate_sample,
                "integrity": integrity, "foreign_key_problems": len(foreign_keys),
                "before": before, "after_migration": after_migration, "after": after,
                "bytes_saved": before["db_bytes"] - after["db_bytes"],
                "migration_growth": after_migration["db_bytes"] - before["db_bytes"],
                "rss_delta_kb": rss_kb() - start_rss, "peak_rss_kb": peak_rss_kb()}


# ---------- AI-сценарии: одинаковый сценарий, разная реализация памяти ----------

def _respond(reply, facts):
    return ("respond", {"reply": reply, "facts": facts, "tentative": {}})


def _count_calls(database, counts):
    for name in ("upsert", "deactivate", "list_active", "list_compact_active"):
        if not hasattr(database, name):
            continue
        original = getattr(database, name)
        key = "list_active" if name in {"list_active", "list_compact_active"} else name
        def wrapper(*args, _original=original, _key=key, **kwargs):
            counts[_key] = counts.get(_key, 0) + 1
            return _original(*args, **kwargs)
        setattr(database, name, wrapper)


async def _ai_case(memory_class, workflow, patch_target):
    from unittest.mock import AsyncMock, Mock, patch

    from ai import Agent, ChatUser
    from tests.fakes import fixture
    from tests.test_conversation import response

    with tempfile.TemporaryDirectory(prefix=f"carwash-ai-{workflow}-") as folder:
        store, _spreadsheet = fixture(folder)
        with patch("ai.AsyncOpenAI"), patch(patch_target, memory_class):
            agent = Agent(store.config, store)
        transport = Mock(close=AsyncMock())
        agent.client = transport
        agent._provider_clients = [transport]
        counts = {"upsert": 0, "deactivate": 0, "list_active": 0}
        calls = {"n": 0, "chars": 0}
        outputs = []
        user = ChatUser(700 + hash(workflow) % 100, None, "Bench")
        if workflow == "returning_one":
            agent.cars.remember(user.id, "Toyota Camry 3.5", "Легковой",
                                tariff="Базовый", services=["Мойка"], plate="а 123 ав 77")
        elif workflow == "returning_several":
            agent.cars.remember(user.id, "Toyota Camry 3.5", "Легковой",
                                tariff="Базовый", services=["Мойка"], plate="а 123 ав 77")
            agent.cars.remember(user.id, "Kia Rio", "Легковой",
                                tariff="Стандарт", services=["Воск"], plate="в 456 вв 99")
        elif workflow == "repeated_booking":
            agent.cars.remember(user.id, "Toyota Camry 3.5", "Легковой",
                                tariff="Базовый", services=["Мойка"], plate="а 123 ав 77")
        agent.cars.cache.drop(user.id)
        _count_calls(agent.cars.db, counts)

        if workflow == "first_mention":
            text = "У меня камри 3.5"
            outputs = [response(calls=[_respond("Запомнила.", {
                "car": ["камри 3.5", "камри 3.5"],
                "car_model": ["Toyota Camry 3.5", "камри 3.5"]})])]
        elif workflow == "returning_one":
            text = "Здравствуйте, сколько стоит мойка?"
            outputs = [response(text="Сейчас подскажу.")]
        elif workflow == "returning_several":
            text = "Здравствуйте, сколько стоит мойка?"
            outputs = [response(text="Сейчас подскажу.")]
        elif workflow == "new_model":
            text = "У меня Aurus Senat"
            outputs = [response(calls=[_respond("Запомнила.", {
                "car": ["Aurus Senat", "Aurus Senat"],
                "car_model": ["Aurus Senat", "Aurus Senat"]})])]
        elif workflow == "correction":
            text = "У меня камри 3.5"
            outputs = [
                response(calls=[_respond("Запомнила.", {
                    "car": ["камри 3.5", "камри 3.5"],
                    "car_model": ["Toyota Camry 3.5", "камри 3.5"]})]),
                response(calls=[_respond("Исправила.", {
                    "car": ["лансер", "лансер"],
                    "car_model": ["Mitsubishi Lancer", "лансер"]})]),
            ]
        elif workflow == "repeated_booking":
            text = ("Запишите меня как в прошлый раз 01.01.2099 в 10:00, "
                    "зовут Никита, телефон 79131233221")
            outputs = [response(calls=[
                _respond("", {"client_name": ["Никита", "Никита"],
                              "phone": ["79131233221", "79131233221"]}),
                ("create_booking", {})])]
        else:
            raise ValueError(workflow)

        async def create(**kwargs):
            calls["n"] += 1
            calls["chars"] += sum(len(str(message.get("content") or ""))
                                  for message in kwargs.get("messages", []))
            await asyncio.sleep(AI_DELAY_SEC)
            if calls["n"] > len(outputs):
                raise AssertionError(f"{workflow}: лишний AI-вызов #{calls['n']}")
            return outputs[calls["n"] - 1]

        transport.chat.completions.create = AsyncMock(side_effect=create)
        started = time.perf_counter()
        if workflow == "correction":
            await agent.reply(user, text)
            await agent.reply(user, "Нет, у меня лансер")
        else:
            await agent.reply(user, text)
        wall = time.perf_counter() - started
        result = {"workflow": workflow, "ai_calls": calls["n"], "prompt_chars": calls["chars"],
                  "db_upserts": counts["upsert"], "db_lists": counts["list_active"],
                  "db_deactivations": counts["deactivate"], "db_reads": agent.cars.db_reads,
                  "cache_hits": agent.cars.cache.hits, "cache_misses": agent.cars.cache.misses,
                  "wall_ms": round(wall * 1000, 2),
                  "processing_ms": round(max(0.0, wall - calls["n"] * AI_DELAY_SEC) * 1000, 2)}
        await agent.close()
        agent.cars.close()
        return result


def ai_benchmark(repeats: int = 3) -> dict:
    workflows = ("first_mention", "returning_one", "returning_several", "new_model",
                 "correction", "repeated_booking")
    result = {"A": {}, "B": {}}
    for arch, memory_class in (("A", legacy_vehicles.VehicleMemory), ("B", vehicles.VehicleMemory)):
        # Прогрев импортов и первого открытия SQLite, чтобы он не попал в замер.
        asyncio.run(_ai_case(memory_class, "first_mention", "ai.VehicleMemory"))
        for workflow in workflows:
            runs = [asyncio.run(_ai_case(memory_class, workflow, "ai.VehicleMemory"))
                    for _ in range(repeats)]
            item = {"runs": repeats}
            for key in runs[0]:
                values = [run[key] for run in runs]
                if isinstance(values[0], (int, float)):
                    item[key] = statistics.median(values)
                else:
                    item[key] = values[0]
            result[arch][workflow] = item
    return result


def print_table(results: dict) -> None:
    print("\n=== Архитектура базы ===")
    header = ("scale", "arch", "db_MB", "table_MB", "index_MB", "bulk_s", "lookup_p50", "lookup_p95",
              "list_p50", "list_p95", "update_p50", "dup_p50", "conc_rps", "peak_RSS_MB", "cache_KB")
    print("  ".join(f"{name:>12}" for name in header))
    for scale, pair in results["db"].items():
        for arch in ("A", "B"):
            item = pair[arch]
            reads = item["reads"]["cache_512kb"]
            row = (scale, arch, round(item["db_bytes"] / 1024 / 1024, 2),
                   round(item["table_bytes"] / 1024 / 1024, 2),
                   round(item["index_bytes"] / 1024 / 1024, 2), item["bulk_sec"],
                   reads["lookup"]["p50_ms"], reads["lookup"]["p95_ms"],
                   reads["customer_list"]["p50_ms"], reads["customer_list"]["p95_ms"],
                   item["update"]["p50_ms"], item["duplicate"]["p50_ms"],
                   item["concurrent"]["inserts_per_second"],
                   round(item["peak_rss_kb"] / 1024, 1), item["cache"]["cache_kb"])
            print("  ".join(f"{str(value):>12}" for value in row))
    print("\n=== Миграция v3/v4 -> v5 ===")
    for item in results["migration"]:
        print(f"v{item['from_version']} {item['scale']:>9}: migrate {item['migration_sec']:>7.3f}s + vacuum {item['vacuum_sec']:>7.3f}s  "
              f"rows {item['rows_before']}->{item['rows_after']}  ids={item['ids_preserved']}  "
              f"models={item['catalog_models']}  plate_keys={item['plate_filled']}  "
              f"db {item['before']['db_bytes']/1024/1024:.2f} -> "
              f"{item['after_migration']['db_bytes']/1024/1024:.2f} (migrated) -> "
              f"{item['after']['db_bytes']/1024/1024:.2f} (vacuum) MB  "
              f"fk_problems={item['foreign_key_problems']}  peak_RSS={item['peak_rss_kb']/1024:.1f} MB")
    print("\n=== AI-сценарии (мок AI, задержка 50 мс на вызов) ===")
    if not results.get("ai"):
        print("пропущено")
        return
    header = ("arch", "workflow", "ai_calls", "prompt_chars", "db_upserts", "db_lists",
              "db_reads", "cache_hit", "cache_miss", "wall_ms", "processing_ms")
    print("  ".join(f"{name:>13}" for name in header))
    for arch in ("A", "B"):
        for workflow, item in results["ai"][arch].items():
            row = (arch, workflow, item["ai_calls"], item["prompt_chars"], item["db_upserts"],
                   item["db_lists"], item["db_reads"], item["cache_hits"], item["cache_misses"],
                   item["wall_ms"], item["processing_ms"])
            print("  ".join(f"{str(value):>13}" for value in row))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scales", default="1000,10000,100000,1000000")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--skip-migration", action="store_true")
    parser.add_argument("--migration-from", default="3,4", help="исходные версии для миграции")
    parser.add_argument("--json", default="")
    parser.add_argument("--temp-dir", default="", help="каталог для БД (например, SSD или tmpfs)")
    parser.add_argument("--worker", choices=("A", "B", "migrate"))
    parser.add_argument("--scale", type=int, default=0)
    parser.add_argument("--from-version", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.worker:
        if arguments.worker == "migrate":
            print(json.dumps(migration_worker(arguments.scale, arguments.temp_dir or None,
                                              arguments.from_version), ensure_ascii=False))
        else:
            print(json.dumps(worker(arguments.worker, arguments.scale, arguments.temp_dir or None), ensure_ascii=False))
        return 0

    scales = [int(value) for value in arguments.scales.split(",") if value.strip()]
    migration_from = [int(value) for value in arguments.migration_from.split(",") if value.strip()]
    results = {"db": {}, "migration": [], "ai": {}}
    for scale in scales:
        pair = {}
        for arch in ("A", "B"):
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", arch, "--scale", str(scale)]
            if arguments.temp_dir:
                command.extend(["--temp-dir", arguments.temp_dir])
            process = subprocess.run(command, capture_output=True, text=True)
            if process.returncode != 0:
                print(process.stderr, file=sys.stderr)
                raise SystemExit(f"Бенчмарк {arch} x {scale} завершился с ошибкой")
            pair[arch] = json.loads(process.stdout)
        results["db"][scale] = pair
        print(f"готово: {scale}", flush=True)
    if not arguments.skip_migration:
        for scale in scales:
            for from_version in migration_from:
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", "migrate",
                           "--scale", str(scale), "--from-version", str(from_version)]
                if arguments.temp_dir:
                    command.extend(["--temp-dir", arguments.temp_dir])
                process = subprocess.run(command, capture_output=True, text=True)
                if process.returncode != 0:
                    print(process.stderr, file=sys.stderr)
                    raise SystemExit(f"Миграция v{from_version} x {scale} завершилась с ошибкой")
                results["migration"].append(json.loads(process.stdout))
                print(f"миграция v{from_version}: {scale}", flush=True)
    if not arguments.skip_ai:
        results["ai"] = ai_benchmark()
    print_table(results)
    if arguments.json:
        Path(arguments.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {arguments.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

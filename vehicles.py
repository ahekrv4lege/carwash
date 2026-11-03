"""Постоянная память об автомобилях клиента.

SQLite на диске — единственный источник истины. RAM-кэш хранит только записи
активных клиентов, загружает их лениво и выгружает по простою. Выгрузка из кэша
никогда не удаляет данные из базы.

Запись появляется только после подтверждённой записи (решение Python).
Владелец строки — Telegram ID; чужие строки не возвращаются и не меняются.
Удаление — смена статуса, строка не переиспользуется.
Колонка last_tariff/last_services — последний подтверждённый заказ этой машины
(«повторная запись как в прошлый раз»); цена и слоты при повторе всегда
пересчитываются заново, память даёт только состав заказа.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
ACTIVE, DELETED = "active", "deleted"
MAX_PER_CUSTOMER = 10
MAX_NAME = 120
ENTRY_OVERHEAD_BYTES = 240  # ориентировочная стоимость записи кэша без строк

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class CarMemoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SavedCar:
    id: int
    name: str
    size_class: str
    last_tariff: str = ""
    last_services: tuple = ()
    last_plate: str = ""


def car_key(name: str) -> str:
    """Ключ дубля: регистр, «ё», пунктуация и пробелы не различаются; «3,5» равно «3.5»."""
    text = (name or "").casefold().replace("ё", "е")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(r"[^\w.]+", " ", text)
    return " ".join(text.split())


def clean_name(name: str) -> str:
    return " ".join((name or "").split())[:MAX_NAME]


class VehicleDatabase:
    """Файл SQLite (WAL). Открывается при первом обращении, поэтому импорт модуля файлов не создаёт."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Два процесса могут одновременно открыть новый файл: WAL/миграция
            # требуют короткой эксклюзивной блокировки. Повторяем её, не теряя данные.
            for attempt in range(5):
                connection = None
                try:
                    connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    self._migrate(connection)
                except sqlite3.OperationalError as error:
                    if connection is not None:
                        connection.close()
                    if "locked" not in str(error).lower() or attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
                    continue
                except Exception:
                    if connection is not None:
                        connection.close()
                    raise
                self._connection = connection
                break
        return self._connection

    @staticmethod
    def _services_text(services) -> str:
        """Услуги прошлого заказа — JSON-список; None ничего не меняет."""
        if services is None:
            return ""
        try:
            return json.dumps([str(service).strip() for service in services if str(service).strip()],
                              ensure_ascii=False)
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _services_list(text: str) -> tuple:
        try:
            parsed = json.loads(text or "[]")
        except (TypeError, ValueError):
            return ()
        return tuple(str(service) for service in parsed) if isinstance(parsed, list) else ()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise CarMemoryError("База памяти создана более новой версией приложения")
        if version == SCHEMA_VERSION:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            if version <= 0:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS vehicles ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " telegram_id INTEGER NOT NULL,"
                    " car_key TEXT NOT NULL,"
                    " car_name TEXT NOT NULL,"
                    " size_class TEXT NOT NULL DEFAULT '',"
                    " status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),"
                    " created_at TEXT NOT NULL,"
                    " updated_at TEXT NOT NULL,"
                    " last_tariff TEXT NOT NULL DEFAULT '',"
                    " last_services TEXT NOT NULL DEFAULT '',"
                    " last_plate TEXT NOT NULL DEFAULT '')")
            else:
                # Миграция по шагам: v1 -> v2 (состав заказа), v2 -> v3 (госномер).
                if version < 2:
                    connection.execute("ALTER TABLE vehicles ADD COLUMN last_tariff TEXT NOT NULL DEFAULT ''")
                    connection.execute("ALTER TABLE vehicles ADD COLUMN last_services TEXT NOT NULL DEFAULT ''")
                if version < 3:
                    connection.execute("ALTER TABLE vehicles ADD COLUMN last_plate TEXT NOT NULL DEFAULT ''")
            # Уникальность активного дубля обеспечивает сама база, даже при гонке двух записей.
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS vehicles_active_key "
                               "ON vehicles (telegram_id, car_key) WHERE status = 'active'")
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def list_active(self, telegram_id: int) -> list[SavedCar]:
        with self._lock:
            rows = self._db().execute(
                "SELECT id, car_name, size_class, last_tariff, last_services, last_plate FROM vehicles "
                "WHERE telegram_id = ? AND status = ? ORDER BY updated_at DESC, id DESC",
                (int(telegram_id), ACTIVE)).fetchall()
        return [SavedCar(id=row[0], name=row[1], size_class=row[2], last_tariff=row[3] or "",
                         last_services=self._services_list(row[4]), last_plate=row[5] or "") for row in rows]

    def upsert(self, telegram_id: int, name: str, size_class: str, now: str,
               tariff: str | None = None, services=None, plate: str | None = None) -> SavedCar | None:
        key = car_key(name)
        if not key:
            return None
        services_text = self._services_text(services) if services is not None else None
        plate_text = (plate or "").strip()[:40] if plate is not None else None
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT id, car_name, last_tariff, last_services, last_plate FROM vehicles "
                    "WHERE telegram_id = ? AND car_key = ? AND status = ?",
                    (int(telegram_id), key, ACTIVE)).fetchone()
                if row:
                    # Пустые tariff/services/plate не затирают прошлый заказ: подтверждённая запись
                    # всегда несёт состав, а пустота означает пробой без подтверждения.
                    tariff_value = (tariff or "") if tariff is not None else (row[2] or "")
                    services_value = services_text if services_text is not None else (row[3] or "")
                    plate_value = plate_text if plate_text is not None else (row[4] or "")
                    connection.execute(
                        "UPDATE vehicles SET size_class = ?, updated_at = ?, last_tariff = ?, last_services = ?, "
                        "last_plate = ? WHERE id = ?",
                        (size_class, now, tariff_value, services_value, plate_value, row[0]))
                    saved = SavedCar(id=row[0], name=row[1], size_class=size_class,
                                     last_tariff=tariff_value, last_services=self._services_list(services_value),
                                     last_plate=plate_value)
                else:
                    count = connection.execute("SELECT COUNT(*) FROM vehicles WHERE telegram_id = ? AND status = ?",
                                               (int(telegram_id), ACTIVE)).fetchone()[0]
                    if count >= MAX_PER_CUSTOMER:
                        connection.execute("ROLLBACK")
                        log.warning("У клиента достигнут лимит сохранённых автомобилей")
                        return None
                    cursor = connection.execute(
                        "INSERT INTO vehicles (telegram_id, car_key, car_name, size_class, status, created_at, "
                        "updated_at, last_tariff, last_services, last_plate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (int(telegram_id), key, name, size_class, ACTIVE, now, now,
                         (tariff or "") if tariff is not None else "",
                         services_text if services_text is not None else "",
                         plate_text if plate_text is not None else ""))
                    saved = SavedCar(id=cursor.lastrowid, name=name, size_class=size_class,
                                     last_tariff=(tariff or "") if tariff is not None else "",
                                     last_services=self._services_list(services_text or ""),
                                     last_plate=plate_text if plate_text is not None else "")
                connection.execute("COMMIT")
                return saved
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def deactivate(self, telegram_id: int, vehicle_id: int, now: str) -> bool:
        with self._lock:
            cursor = self._db().execute(
                "UPDATE vehicles SET status = ?, updated_at = ? WHERE id = ? AND telegram_id = ? AND status = ?",
                (DELETED, now, int(vehicle_id), int(telegram_id), ACTIVE))
            return cursor.rowcount == 1

    def backup_to(self, destination: Path) -> None:
        """Согласованная копия через backup API SQLite: корректна при открытом WAL."""
        with self._lock:
            source = self._db()
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


@dataclass
class _Entry:
    cars: tuple
    size: int
    last_seen: float


class VehicleCache:
    """RAM-кэш клиентов с ограничением по объёму (LRU) и выгрузкой по простою."""

    def __init__(self, budget_bytes: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget_bytes = budget_bytes
        self._clock = clock
        self._entries: OrderedDict[int, _Entry] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _size(cars: tuple) -> int:
        return ENTRY_OVERHEAD_BYTES + sum(
            len(car.name) * 2 + len(car.size_class) * 2 + len(car.last_tariff) * 2 + len(car.last_plate) * 2
            + sum(len(service) * 2 for service in car.last_services) + 48 for car in cars)

    def get(self, telegram_id: int) -> tuple | None:
        entry = self._entries.get(telegram_id)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        entry.last_seen = self._clock()
        self._entries.move_to_end(telegram_id)
        return entry.cars

    def put(self, telegram_id: int, cars: tuple) -> None:
        self.drop(telegram_id)
        size = self._size(cars)
        self._entries[telegram_id] = _Entry(cars=cars, size=size, last_seen=self._clock())
        self._bytes += size
        while self._bytes > self.budget_bytes and len(self._entries) > 1:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted.size

    def drop(self, telegram_id: int) -> None:
        entry = self._entries.pop(telegram_id, None)
        if entry is not None:
            self._bytes -= entry.size

    def evict_idle(self, ttl_sec: float) -> int:
        now = self._clock()
        idle = [key for key, entry in self._entries.items() if now - entry.last_seen >= ttl_sec]
        for key in idle:
            self.drop(key)
        return len(idle)

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class RetentionPolicy:
    """Срок простоя в кэше: 30 минут по умолчанию, от 15 до 60 минут по нагрузке.

    Изменение за один шаг не больше `step_min`, чтобы срок не колебался между проверками.
    """
    base_min: float = 30.0
    low_min: float = 15.0
    high_min: float = 60.0
    step_min: float = 5.0
    adaptive: bool = True
    current_min: float = field(default=30.0)

    def __post_init__(self) -> None:
        self.current_min = self.base_min

    def update(self, *, available_mb: float | None, hit_rate: float | None, fill_ratio: float) -> float:
        if not self.adaptive:
            self.current_min = self.base_min
            return self.current_min
        if available_mb is not None and available_mb < 150:
            target = self.low_min
        elif available_mb is not None and available_mb < 300:
            target = max(self.low_min, 20.0)
        elif (hit_rate is not None and hit_rate >= 0.7 and fill_ratio < 0.5
              and (available_mb is None or available_mb >= 400)):
            target = self.high_min
        else:
            target = self.base_min
        delta = max(-self.step_min, min(self.step_min, target - self.current_min))
        self.current_min = min(self.high_min, max(self.low_min, self.current_min + delta))
        return self.current_min


def read_available_mb() -> float | None:
    """MemAvailable из /proc/meminfo; None там, где файла нет."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class VehicleMemory:
    """Фасад для агента: кэш поверх базы. Все методы потокобезопасны и выполняются не в цикле событий."""

    MAINTENANCE_PRESSURE_SEC = 300.0

    def __init__(self, path: Path, *, cache_mb: float = 4.0, ttl_min: float = 30.0, adaptive: bool = True,
                 clock: Callable[[], float] = time.monotonic,
                 memory_reader: Callable[[], float | None] = read_available_mb) -> None:
        self.db = VehicleDatabase(path)
        self.cache = VehicleCache(int(cache_mb * 1024 * 1024), clock=clock)
        self.policy = RetentionPolicy(base_min=ttl_min, low_min=min(15.0, ttl_min), high_min=max(60.0, ttl_min),
                                      adaptive=adaptive)
        self._clock = clock
        self._memory_reader = memory_reader
        self._lock = threading.RLock()
        self._available_mb: float | None = None
        self._pressure_checked_at = float("-inf")
        self._last_policy_hits = 0
        self._last_policy_misses = 0
        self.db_reads = 0

    # ---- чтение
    def for_user(self, telegram_id: int) -> list[SavedCar]:
        with self._lock:
            cached = self.cache.get(int(telegram_id))
            if cached is not None:
                return list(cached)
            cars = tuple(self.db.list_active(telegram_id))
            self.db_reads += 1
            self.cache.put(int(telegram_id), cars)
            return list(cars)

    def touch(self, telegram_id: int) -> None:
        """Входящее сообщение: обновляет активность клиента и при необходимости загружает его записи."""
        self.for_user(telegram_id)

    # ---- запись: сначала транзакция в базе, потом кэш
    def remember(self, telegram_id: int, name: str, size_class: str,
                 tariff: str | None = None, services=None, plate: str | None = None) -> SavedCar | None:
        name = clean_name(name)
        now = datetime.now().strftime(_TIME_FORMAT)
        with self._lock:
            saved = self.db.upsert(int(telegram_id), name, size_class, now,
                                   tariff=tariff, services=services, plate=plate)
            self.cache.drop(int(telegram_id))
        return saved

    def forget(self, telegram_id: int, vehicle_id: int) -> bool:
        now = datetime.now().strftime(_TIME_FORMAT)
        with self._lock:
            removed = self.db.deactivate(int(telegram_id), int(vehicle_id), now)
            self.cache.drop(int(telegram_id))
        return removed

    # ---- обслуживание кэша
    def maintenance(self) -> dict:
        """Раз в минуту: выгрузка по простою. Давление памяти читается не чаще раза в 5 минут."""
        with self._lock:
            now = self._clock()
            if now - self._pressure_checked_at >= self.MAINTENANCE_PRESSURE_SEC:
                self._available_mb = self._memory_reader()
                self._pressure_checked_at = now
            hits = self.cache.hits - self._last_policy_hits
            misses = self.cache.misses - self._last_policy_misses
            self._last_policy_hits, self._last_policy_misses = self.cache.hits, self.cache.misses
            hit_rate = hits / (hits + misses) if hits + misses else None
            fill = self.cache.bytes_used / self.cache.budget_bytes if self.cache.budget_bytes else 1.0
            ttl_min = self.policy.update(available_mb=self._available_mb, hit_rate=hit_rate, fill_ratio=fill)
            evicted = self.cache.evict_idle(ttl_min * 60)
            return {"ttl_min": ttl_min, "evicted": evicted, "entries": len(self.cache),
                    "cache_bytes": self.cache.bytes_used, "available_mb": self._available_mb}

    def backup(self, destination: Path) -> None:
        with self._lock:
            self.db.backup_to(Path(destination))

    def close(self) -> None:
        with self._lock:
            self.db.close()

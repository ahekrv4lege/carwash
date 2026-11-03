"""Постоянная память об автомобилях клиента.

SQLite на диске — единственный источник истины. RAM-кэш хранит только записи
активных клиентов, загружает их лениво и выгружает по простою. Выгрузка из кэша
никогда не удаляет данные из базы.

Названия моделей вынесены в общий справочник vehicle_models: одинаковая машина
хранится один раз, а записи клиентов ссылаются на неё по model_id. В строке
клиента остаются только его собственные данные: Telegram ID, класс, госномер,
состав последнего заказа и статус.

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

SCHEMA_VERSION = 4
ACTIVE, DELETED = "active", "deleted"
MAX_PER_CUSTOMER = 10
MAX_NAME = 120
ENTRY_OVERHEAD_BYTES = 240  # ориентировочная стоимость записи кэша без строк

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_VEHICLES_TABLE = (
    "CREATE TABLE {table} ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " telegram_id INTEGER NOT NULL,"
    " model_id INTEGER NOT NULL REFERENCES vehicle_models(model_id) ON DELETE RESTRICT,"
    " size_class TEXT NOT NULL DEFAULT '',"
    " status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL,"
    " last_tariff TEXT NOT NULL DEFAULT '',"
    " last_services TEXT NOT NULL DEFAULT '',"
    " last_plate TEXT NOT NULL DEFAULT '')"
)
_MODELS_TABLE = (
    "CREATE TABLE IF NOT EXISTS vehicle_models ("
    " model_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " model_key TEXT NOT NULL UNIQUE,"
    " canonical_name TEXT NOT NULL,"
    " created_at TEXT NOT NULL)"
)
_ACTIVE_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS vehicles_active_model "
                 "ON vehicles (telegram_id, model_id) WHERE status = 'active'")


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
                    connection.execute("PRAGMA foreign_keys=ON")
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
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}

    @classmethod
    def _create_schema(cls, connection: sqlite3.Connection) -> None:
        connection.execute(_MODELS_TABLE)
        connection.execute(_VEHICLES_TABLE.format(table="vehicles"))
        connection.execute(_ACTIVE_INDEX)

    @classmethod
    def _migrate_legacy(cls, connection: sqlite3.Connection) -> None:
        """v0–v3 -> v4: общий справочник моделей и ссылки model_id.

        Все операции выполняются в одной транзакции вызывающего кода. При любой
        ошибке (пустой ключ, активный дубль, несовпадение количества строк)
        транзакция откатывается, user_version остаётся прежним, и приложение
        не продолжает работу с частично мигрированной базой.
        """
        columns = cls._columns(connection, "vehicles")
        if not {"car_key", "car_name"}.issubset(columns):
            if "model_id" in columns:
                raise CarMemoryError(
                    "Таблица автомобилей уже содержит model_id, но версия схемы не обновлена")
            raise CarMemoryError("В таблице автомобилей нет car_key/car_name: миграция невозможна")
        # Очень старые базы (user_version 0) могли не иметь этих колонок.
        for name, definition in (("last_tariff", "TEXT NOT NULL DEFAULT ''"),
                                 ("last_services", "TEXT NOT NULL DEFAULT ''"),
                                 ("last_plate", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                connection.execute(f"ALTER TABLE vehicles ADD COLUMN {name} {definition}")

        empty = connection.execute(
            "SELECT COUNT(*) FROM vehicles WHERE car_key IS NULL OR TRIM(car_key) = '' "
            "OR car_name IS NULL OR TRIM(car_name) = ''").fetchone()[0]
        if empty:
            raise CarMemoryError(
                f"В базе памяти {empty} записей без названия или ключа автомобиля; "
                "миграция остановлена без изменения данных")
        bad_status = connection.execute(
            "SELECT COUNT(*) FROM vehicles WHERE status IS NULL OR status NOT IN ('active', 'deleted')"
        ).fetchone()[0]
        if bad_status:
            raise CarMemoryError(
                f"В базе памяти {bad_status} записей с неизвестным статусом; "
                "миграция остановлена без изменения данных")

        # Каноническое название — самое частое написание ключа. При равенстве
        # побеждает более раннее, затем алфавитное: результат детерминирован.
        connection.execute(_MODELS_TABLE)
        rows = list(connection.execute(
            "SELECT car_key, car_name, COUNT(*) AS uses, MIN(created_at) AS first_seen "
            "FROM vehicles GROUP BY car_key, car_name "
            "ORDER BY car_key, uses DESC, first_seen ASC, car_name ASC"))
        models, last_key = [], None
        for key, name, _uses, first_seen in rows:
            if key == last_key:
                continue
            last_key = key
            models.append((key, name, first_seen or ""))
        connection.executemany(
            "INSERT OR IGNORE INTO vehicle_models (model_key, canonical_name, created_at) "
            "VALUES (?, ?, ?)", models)

        duplicate = connection.execute(
            "SELECT v.telegram_id, m.model_id, COUNT(*) FROM vehicles v "
            "JOIN vehicle_models m ON m.model_key = v.car_key "
            "WHERE v.status = 'active' "
            "GROUP BY v.telegram_id, m.model_id HAVING COUNT(*) > 1 LIMIT 1").fetchone()
        if duplicate:
            raise CarMemoryError(
                "В базе памяти есть активные дубли одной модели у клиента; "
                "миграция остановлена без изменения данных")

        before = connection.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        connection.execute("DROP TABLE IF EXISTS vehicles_migrated")
        connection.execute(_VEHICLES_TABLE.format(table="vehicles_migrated"))
        connection.execute(
            "INSERT INTO vehicles_migrated "
            "(id, telegram_id, model_id, size_class, status, created_at, updated_at, "
            " last_tariff, last_services, last_plate) "
            "SELECT v.id, v.telegram_id, m.model_id, COALESCE(v.size_class, ''), v.status, "
            "       COALESCE(v.created_at, ''), COALESCE(v.updated_at, ''), "
            "       COALESCE(v.last_tariff, ''), COALESCE(v.last_services, ''), "
            "       COALESCE(v.last_plate, '') "
            "FROM vehicles v JOIN vehicle_models m ON m.model_key = v.car_key")
        after = connection.execute("SELECT COUNT(*) FROM vehicles_migrated").fetchone()[0]
        if before != after:
            raise CarMemoryError(
                f"Миграция потеряла записи ({before} -> {after}); изменения отменены")
        connection.execute("DROP TABLE vehicles")
        connection.execute("ALTER TABLE vehicles_migrated RENAME TO vehicles")
        connection.execute(_ACTIVE_INDEX)
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise CarMemoryError("Миграция нарушила внешние ключи; изменения отменены")

    @classmethod
    def _migrate(cls, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise CarMemoryError("База памяти создана более новой версией приложения")
        if version == SCHEMA_VERSION:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Пока ждали эксклюзивную блокировку, другой процесс мог уже
            # выполнить миграцию. Перечитываем версию под блокировкой: иначе
            # второй открыватель увидел бы готовую v4 и принял её за поломку.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise CarMemoryError("База памяти создана более новой версией приложения")
            if version < SCHEMA_VERSION:
                if version <= 0 and "vehicles" not in cls._tables(connection):
                    cls._create_schema(connection)
                else:
                    cls._migrate_legacy(connection)
                connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def list_active(self, telegram_id: int) -> list[SavedCar]:
        with self._lock:
            rows = self._db().execute(
                "SELECT v.id, m.canonical_name, v.size_class, v.last_tariff, "
                "       v.last_services, v.last_plate "
                "FROM vehicles v JOIN vehicle_models m ON m.model_id = v.model_id "
                "WHERE v.telegram_id = ? AND v.status = ? "
                "ORDER BY v.updated_at DESC, v.id DESC",
                (int(telegram_id), ACTIVE)).fetchall()
        return [SavedCar(id=row[0], name=row[1], size_class=row[2], last_tariff=row[3] or "",
                         last_services=self._services_list(row[4]), last_plate=row[5] or "") for row in rows]

    def upsert(self, telegram_id: int, name: str, size_class: str, now: str,
               tariff: str | None = None, services=None, plate: str | None = None) -> SavedCar | None:
        name = clean_name(name)
        key = car_key(name)
        if not key:
            return None
        services_text = self._services_text(services) if services is not None else None
        plate_text = (plate or "").strip()[:40] if plate is not None else None
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Атомарно заводим общую модель. ON CONFLICT не даёт двум
                # одновременным записям создать дубль; каноническое имя уже
                # существующей модели не перезаписывается.
                connection.execute(
                    "INSERT INTO vehicle_models (model_key, canonical_name, created_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(model_key) DO NOTHING",
                    (key, name, now))
                model = connection.execute(
                    "SELECT model_id, canonical_name FROM vehicle_models WHERE model_key = ?",
                    (key,)).fetchone()
                if model is None:  # pragma: no cover - защита от повреждённой базы
                    raise CarMemoryError("Не удалось определить модель автомобиля")
                model_id, canonical = model
                row = connection.execute(
                    "SELECT id, last_tariff, last_services, last_plate FROM vehicles "
                    "WHERE telegram_id = ? AND model_id = ? AND status = ?",
                    (int(telegram_id), model_id, ACTIVE)).fetchone()
                if row:
                    # Пустые tariff/services/plate не затирают прошлый заказ: подтверждённая запись
                    # всегда несёт состав, а пустота означает пробой без подтверждения.
                    tariff_value = (tariff or "") if tariff is not None else (row[1] or "")
                    services_value = services_text if services_text is not None else (row[2] or "")
                    plate_value = plate_text if plate_text is not None else (row[3] or "")
                    connection.execute(
                        "UPDATE vehicles SET size_class = ?, updated_at = ?, last_tariff = ?, "
                        "last_services = ?, last_plate = ? WHERE id = ?",
                        (size_class, now, tariff_value, services_value, plate_value, row[0]))
                    saved = SavedCar(id=row[0], name=canonical, size_class=size_class,
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
                        "INSERT INTO vehicles (telegram_id, model_id, size_class, status, created_at, "
                        "updated_at, last_tariff, last_services, last_plate) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (int(telegram_id), model_id, size_class, ACTIVE, now, now,
                         (tariff or "") if tariff is not None else "",
                         services_text if services_text is not None else "",
                         plate_text if plate_text is not None else ""))
                    saved = SavedCar(id=cursor.lastrowid, name=canonical, size_class=size_class,
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

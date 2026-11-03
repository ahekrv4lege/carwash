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

SCHEMA_VERSION = 5
ACTIVE, DELETED = "active", "deleted"
MAX_PER_CUSTOMER = 10
MAX_NAME = 120
MODEL_CACHE_ENTRIES = 8192  # общий bounded-кэш model_id -> canonical_name
ENTRY_OVERHEAD_BYTES = 240  # ориентировочная стоимость записи кэша без строк

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_PLATE_TRANSLATION = str.maketrans("abcehkmoptxy", "авсенкмортху")

_VEHICLES_TABLE = (
    "CREATE TABLE {table} ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " telegram_id INTEGER NOT NULL,"
    " model_id INTEGER NOT NULL REFERENCES vehicle_models(model_id) ON DELETE RESTRICT,"
    " plate_key TEXT NOT NULL DEFAULT '',"
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
_ACTIVE_MODEL_INDEX = "vehicles_active_model"
_ACTIVE_VEHICLE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS vehicles_active_vehicle "
    "ON vehicles (telegram_id, model_id, plate_key) WHERE status = 'active'"
)


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
    model_id: int = 0


@dataclass(frozen=True, slots=True)
class CachedCar:
    """Компактная запись RAM-кэша: без строки названия модели."""
    id: int
    model_id: int
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


def plate_key(plate: str) -> str:
    """Ключ госномера: регистр, пробелы, дефисы и похожие латинские буквы не различаются.

    «А 123 АА 77», «а123аа77» и «A123AA77» дают один ключ. Кириллица и латиница
    сводятся к кириллице только для букв, которые встречаются в российских номерах;
    остальные символы сохраняются, чтобы не склеить разные номера.
    """
    text = (plate or "").casefold().replace("ё", "е").translate(_PLATE_TRANSLATION)
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def plate_mentioned(plate: str, text: str) -> bool:
    """Номер действительно назван в текущем сообщении, а не остался от прошлой машины."""
    key = plate_key(plate)
    return bool(key) and key in plate_key(text)


def clean_name(name: str) -> str:
    return " ".join((name or "").split())[:MAX_NAME]


class ModelNameCache:
    """Ограниченный LRU-кэш model_id -> canonical_name.

    Названия общие для всех клиентов: тысячи записей клиентов ссылаются на один
    и тот же объект строки. При вытеснении имя дозагружается одним batch-запросом
    на клиента, а не отдельным запросом на каждую машину.
    """

    def __init__(self, max_entries: int = MODEL_CACHE_ENTRIES) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[int, str] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, model_id: int) -> str | None:
        name = self._entries.get(model_id)
        if name is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(model_id)
        return name

    def peek(self, model_id: int) -> str | None:
        return self._entries.get(model_id)

    def put(self, model_id: int, name: str) -> None:
        if not model_id or not name:
            return
        self._entries[model_id] = name
        self._entries.move_to_end(model_id)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def bytes_used(self) -> int:
        # Оценка Python-памяти: строка UTF-16-ish + запись словаря/ключа.
        return sum(len(name) * 2 + 64 for name in self._entries.values())



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
                    connection.create_function("vehicle_plate_key", 1, plate_key, deterministic=True)
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
        connection.execute(_ACTIVE_VEHICLE_INDEX)

    @staticmethod
    def _ensure_unique_vehicle_keys(connection: sqlite3.Connection) -> None:
        """Один активный номер (или одна безымянная запись) на модель у клиента."""
        duplicate = connection.execute(
            "SELECT telegram_id, model_id, plate_key, COUNT(*) FROM vehicles "
            "WHERE status = 'active' "
            "GROUP BY telegram_id, model_id, plate_key HAVING COUNT(*) > 1 LIMIT 1").fetchone()
        if duplicate:
            raise CarMemoryError(
                "В базе памяти есть активные дубли машины с одной моделью и номером; "
                "миграция остановлена без изменения данных")

    @classmethod
    def _migrate_legacy(cls, connection: sqlite3.Connection) -> None:
        """v0–v3 -> v5: общий справочник моделей, model_id и ключ госномера.

        Все операции выполняются в одной транзакции вызывающего кода. При любой
        ошибке (пустой ключ, дубль номера, несовпадение количества строк)
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
        connection.execute("UPDATE vehicles SET plate_key = vehicle_plate_key(last_plate)")
        cls._ensure_unique_vehicle_keys(connection)
        connection.execute(_ACTIVE_VEHICLE_INDEX)
        problems = connection.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise CarMemoryError("Миграция нарушила внешние ключи; изменения отменены")

    @classmethod
    def _migrate_v4_to_v5(cls, connection: sqlite3.Connection) -> None:
        """v4 -> v5: ключ госномера и уникальность (клиент, модель, номер).

        Старый индекс (telegram_id, model_id) разрешал только одну активную
        машину модели у клиента. Новый индекс разрешает две разные машины с
        разными номерами и по-прежнему блокирует дубль одного номера.
        """
        columns = cls._columns(connection, "vehicles")
        if not {"model_id", "last_plate"}.issubset(columns):
            raise CarMemoryError("Версия 4, но таблица vehicles не содержит model_id/last_plate")
        if "plate_key" not in columns:
            connection.execute("ALTER TABLE vehicles ADD COLUMN plate_key TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE vehicles SET plate_key = vehicle_plate_key(last_plate)")
        cls._ensure_unique_vehicle_keys(connection)
        connection.execute(f"DROP INDEX IF EXISTS {_ACTIVE_MODEL_INDEX}")
        connection.execute(_ACTIVE_VEHICLE_INDEX)
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
            # второй открыватель увидел бы готовую схему и принял её за поломку.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise CarMemoryError("База памяти создана более новой версией приложения")
            if version < SCHEMA_VERSION:
                if version <= 0 and "vehicles" not in cls._tables(connection):
                    cls._create_schema(connection)
                elif version <= 3:
                    cls._migrate_legacy(connection)
                else:
                    cls._migrate_v4_to_v5(connection)
                connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def list_active(self, telegram_id: int) -> list[SavedCar]:
        with self._lock:
            rows = self._db().execute(
                "SELECT v.id, v.model_id, m.canonical_name, v.size_class, v.last_tariff, "
                "       v.last_services, v.last_plate "
                "FROM vehicles v JOIN vehicle_models m ON m.model_id = v.model_id "
                "WHERE v.telegram_id = ? AND v.status = ? "
                "ORDER BY v.updated_at DESC, v.id DESC",
                (int(telegram_id), ACTIVE)).fetchall()
        return [SavedCar(id=row[0], name=row[2], size_class=row[3], last_tariff=row[4] or "",
                         last_services=self._services_list(row[5]), last_plate=row[6] or "",
                         model_id=row[1]) for row in rows]

    def list_compact_active(self, telegram_id: int) -> list[tuple[CachedCar, str]]:
        """Компактные записи для RAM-кэша плюс название модели только для прогрева."""
        with self._lock:
            rows = self._db().execute(
                "SELECT v.id, v.model_id, m.canonical_name, v.size_class, v.last_tariff, "
                "       v.last_services, v.last_plate "
                "FROM vehicles v JOIN vehicle_models m ON m.model_id = v.model_id "
                "WHERE v.telegram_id = ? AND v.status = ? "
                "ORDER BY v.updated_at DESC, v.id DESC",
                (int(telegram_id), ACTIVE)).fetchall()
        return [(CachedCar(id=row[0], model_id=row[1], size_class=row[3], last_tariff=row[4] or "",
                           last_services=self._services_list(row[5]), last_plate=row[6] or ""), row[2])
                for row in rows]

    def model_names(self, model_ids) -> dict[int, str]:
        """Один batch-запрос на все недостающие model_id клиента, без N+1."""
        ids = sorted({int(model_id) for model_id in model_ids if model_id})
        result = {}
        with self._lock:
            connection = self._db()
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT model_id, canonical_name FROM vehicle_models "
                    f"WHERE model_id IN ({placeholders})", chunk).fetchall()
                result.update(rows)
        return result

    def upsert(self, telegram_id: int, name: str, size_class: str, now: str,
               tariff: str | None = None, services=None, plate: str | None = None) -> SavedCar | None:
        name = clean_name(name)
        key = car_key(name)
        if not key:
            return None
        services_text = self._services_text(services) if services is not None else None
        plate_text = (plate or "").strip()[:40] if plate is not None else None
        wanted_plate = plate_key(plate_text or "")
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Атомарно заводим общую модель. Сначала дешёвое чтение: у
                # существующей модели не нужно выполнять даже no-op INSERT.
                model = connection.execute(
                    "SELECT model_id, canonical_name FROM vehicle_models WHERE model_key = ?",
                    (key,)).fetchone()
                if model is None:
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
                # У клиента может быть несколько машин одной модели. Номер —
                # надёжный идентификатор; без номера различать машины нельзя.
                rows = connection.execute(
                    "SELECT id, plate_key, last_tariff, last_services, last_plate FROM vehicles "
                    "WHERE telegram_id = ? AND model_id = ? AND status = ?",
                    (int(telegram_id), model_id, ACTIVE)).fetchall()
                row = None
                if wanted_plate:
                    row = next((candidate for candidate in rows
                                if (candidate[1] or plate_key(candidate[4])) == wanted_plate), None)
                    # Единственная машина модели без номера: новый номер уточняет её.
                    if row is None and len(rows) == 1 and not (rows[0][1] or plate_key(rows[0][4])):
                        row = rows[0]
                else:
                    if len(rows) == 1:
                        row = rows[0]
                    elif len(rows) > 1:
                        connection.execute("ROLLBACK")
                        log.warning("Несколько машин одной модели без номера: выбор неоднозначен")
                        return None
                if row:
                    # Пустые tariff/services не затирают прошлый заказ. Пустой номер
                    # тоже не стирает подтверждённый: он означает «не назвали сейчас».
                    tariff_value = (tariff or "") if tariff is not None else (row[2] or "")
                    services_value = services_text if services_text is not None else (row[3] or "")
                    existing_plate = row[4] or ""
                    existing_key = row[1] or plate_key(existing_plate)
                    if plate_text:
                        plate_value, plate_key_value = plate_text, wanted_plate
                    else:
                        plate_value, plate_key_value = existing_plate, existing_key
                    if plate_key_value != existing_key:
                        connection.execute(
                            "UPDATE vehicles SET size_class = ?, updated_at = ?, last_tariff = ?, "
                            "last_services = ?, last_plate = ?, plate_key = ? WHERE id = ?",
                            (size_class, now, tariff_value, services_value, plate_value,
                             plate_key_value, row[0]))
                    elif plate_value != existing_plate:
                        # Написание номера уточнилось, ключ тот же: уникальный индекс не трогаем.
                        connection.execute(
                            "UPDATE vehicles SET size_class = ?, updated_at = ?, last_tariff = ?, "
                            "last_services = ?, last_plate = ? WHERE id = ?",
                            (size_class, now, tariff_value, services_value, plate_value, row[0]))
                    else:
                        connection.execute(
                            "UPDATE vehicles SET size_class = ?, updated_at = ?, last_tariff = ?, "
                            "last_services = ? WHERE id = ?",
                            (size_class, now, tariff_value, services_value, row[0]))
                    saved = SavedCar(id=row[0], name=canonical, size_class=size_class,
                                     last_tariff=tariff_value, last_services=self._services_list(services_value),
                                     last_plate=plate_value, model_id=model_id)
                else:
                    count = connection.execute("SELECT COUNT(*) FROM vehicles WHERE telegram_id = ? AND status = ?",
                                               (int(telegram_id), ACTIVE)).fetchone()[0]
                    if count >= MAX_PER_CUSTOMER:
                        connection.execute("ROLLBACK")
                        log.warning("У клиента достигнут лимит сохранённых автомобилей")
                        return None
                    cursor = connection.execute(
                        "INSERT INTO vehicles (telegram_id, model_id, plate_key, size_class, status, "
                        "created_at, updated_at, last_tariff, last_services, last_plate) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (int(telegram_id), model_id, wanted_plate, size_class, ACTIVE, now, now,
                         (tariff or "") if tariff is not None else "",
                         services_text if services_text is not None else "",
                         plate_text or ""))
                    saved = SavedCar(id=cursor.lastrowid, name=canonical, size_class=size_class,
                                     last_tariff=(tariff or "") if tariff is not None else "",
                                     last_services=self._services_list(services_text or ""),
                                     last_plate=plate_text or "", model_id=model_id)
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
        total = ENTRY_OVERHEAD_BYTES
        for car in cars:
            name = getattr(car, "name", "")
            total += (len(name) * 2 + len(car.size_class) * 2 + len(car.last_tariff) * 2
                      + len(car.last_plate) * 2
                      + sum(len(service) * 2 for service in car.last_services) + 48)
        return total

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
        self.model_cache = ModelNameCache()
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
    def _load_compact(self, telegram_id: int) -> tuple:
        rows = self.db.list_compact_active(telegram_id)
        for car, name in rows:
            self.model_cache.put(car.model_id, name)
        cars = tuple(car for car, _name in rows)
        self.db_reads += 1
        self.cache.put(int(telegram_id), cars)
        return cars

    def _display(self, cars: tuple) -> list[SavedCar]:
        names = {}
        missing = set()
        for car in cars:
            name = self.model_cache.peek(car.model_id)
            if name is None:
                missing.add(car.model_id)
            else:
                names[car.model_id] = name
                self.model_cache.get(car.model_id)  # LRU-касание и статистика попаданий
        if missing:
            # Один запрос на все недостающие модели клиента, не N+1.
            fetched = self.db.model_names(missing)
            names.update(fetched)
            for model_id, name in fetched.items():
                self.model_cache.put(model_id, name)
        return [SavedCar(id=car.id, name=names.get(car.model_id, ""),
                         size_class=car.size_class, last_tariff=car.last_tariff,
                         last_services=car.last_services, last_plate=car.last_plate,
                         model_id=car.model_id) for car in cars]

    def for_user(self, telegram_id: int) -> list[SavedCar]:
        with self._lock:
            cached = self.cache.get(int(telegram_id))
            if cached is None:
                cached = self._load_compact(telegram_id)
            return self._display(cached)

    def touch(self, telegram_id: int) -> None:
        """Входящее сообщение: обновляет активность клиента и при необходимости загружает его записи."""
        with self._lock:
            if self.cache.get(int(telegram_id)) is None:
                self._load_compact(telegram_id)

    # ---- запись: сначала транзакция в базе, потом кэш
    def remember(self, telegram_id: int, name: str, size_class: str,
                 tariff: str | None = None, services=None, plate: str | None = None) -> SavedCar | None:
        name = clean_name(name)
        now = datetime.now().strftime(_TIME_FORMAT)
        with self._lock:
            saved = self.db.upsert(int(telegram_id), name, size_class, now,
                                   tariff=tariff, services=services, plate=plate)
            self.cache.drop(int(telegram_id))
            if saved is not None:
                self.model_cache.put(saved.model_id, saved.name)
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
                    "cache_bytes": self.cache.bytes_used, "available_mb": self._available_mb,
                    "model_cache_entries": len(self.model_cache),
                    "model_cache_bytes": self.model_cache.bytes_used}

    def backup(self, destination: Path) -> None:
        with self._lock:
            self.db.backup_to(Path(destination))

    def close(self) -> None:
        with self._lock:
            self.db.close()

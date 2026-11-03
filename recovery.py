"""Краткосрочная память диалогов и постоянный профиль клиента.

Разговоры живут в RAM, а на диск попадают только при штатной остановке: один
компактный снимок активных за последние пять минут сессий. Это не журнал
каждого сообщения и не замена SQLite автомобилей.

Профиль клиента (подтверждённые имя и телефон) хранится постоянно и не зависит
от пятиминутного окна восстановления. Автомобили, госномера и состав прошлого
заказа остаются в customer_vehicles.sqlite3: здесь только ссылка на
индивидуальный vehicles.id, без model_id и без копии названий.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SNAPSHOT_TTL_SEC = 300

_PROFILES_TABLE = (
    "CREATE TABLE IF NOT EXISTS customer_profiles ("
    " telegram_id INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL DEFAULT '',"
    " phone TEXT NOT NULL DEFAULT '',"
    " updated_at REAL NOT NULL)"
)
_SNAPSHOTS_TABLE = (
    "CREATE TABLE IF NOT EXISTS conversation_snapshots ("
    " telegram_id INTEGER PRIMARY KEY,"
    " saved_at REAL NOT NULL,"
    " payload TEXT NOT NULL)"
)
_SNAPSHOTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS conversation_snapshots_saved_at "
    "ON conversation_snapshots (saved_at)"
)


class RecoveryMemory:
    """SQLite для профилей и снимков диалогов. Открывается лениво."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False,
                                         isolation_level=None, timeout=10)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA busy_timeout=10000")
                self._migrate(connection)
            except Exception:
                connection.close()
                raise
            self._connection = connection
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return self._connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("База восстановления создана более новой версией приложения")
        if version == SCHEMA_VERSION:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_PROFILES_TABLE)
            connection.execute(_SNAPSHOTS_TABLE)
            connection.execute(_SNAPSHOTS_INDEX)
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def save_profile(self, telegram_id: int, name: str = "", phone: str = "",
                     now: float | None = None) -> bool:
        """Обновить постоянные имя/телефон. Пустое значение не стирает прежнее."""
        name, phone = " ".join((name or "").split())[:120], (phone or "").strip()[:40]
        if not name and not phone:
            return False
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT name, phone FROM customer_profiles WHERE telegram_id = ?",
                    (int(telegram_id),)).fetchone()
                previous_name, previous_phone = row if row else ("", "")
                merged_name = name or previous_name
                merged_phone = phone or previous_phone
                if (merged_name, merged_phone) == (previous_name, previous_phone):
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO customer_profiles (telegram_id, name, phone, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                    "name = excluded.name, phone = excluded.phone, updated_at = excluded.updated_at",
                    (int(telegram_id), merged_name, merged_phone,
                     time.time() if now is None else float(now)))
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def profile(self, telegram_id: int) -> dict | None:
        with self._lock:
            row = self._db().execute(
                "SELECT name, phone, updated_at FROM customer_profiles WHERE telegram_id = ?",
                (int(telegram_id),)).fetchone()
        if not row:
            return None
        return {"name": row[0], "phone": row[1], "updated_at": row[2]}

    def save_snapshot(self, entries, now: float | None = None,
                      ttl: int = SNAPSHOT_TTL_SEC) -> int:
        """Заменить снимок целиком одной транзакцией.

        entries — итерируемое из (telegram_id, payload). Старые и просроченные
        строки удаляются здесь же, поэтому файл не растёт бесконечно.
        """
        now = time.time() if now is None else float(now)
        rows = []
        for telegram_id, payload in entries:
            try:
                text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as error:
                log.error("Снимок диалога не сериализуется (%s)", type(error).__name__)
                continue
            rows.append((int(telegram_id), now, text))
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM conversation_snapshots")
                connection.executemany(
                    "INSERT INTO conversation_snapshots (telegram_id, saved_at, payload) "
                    "VALUES (?, ?, ?)", rows)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(rows)

    def load_snapshot(self, now: float | None = None,
                      ttl: int = SNAPSHOT_TTL_SEC) -> list[dict]:
        """Прочитать непустые снимки не старше ttl и вычистить просроченные."""
        now = time.time() if now is None else float(now)
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM conversation_snapshots WHERE saved_at < ?",
                    (now - ttl,))
                rows = list(connection.execute(
                    "SELECT telegram_id, saved_at, payload FROM conversation_snapshots "
                    "ORDER BY telegram_id"))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        result = []
        for telegram_id, saved_at, payload in rows:
            try:
                value = json.loads(payload)
            except (TypeError, ValueError):
                log.warning("Снимок диалога повреждён; пропущен")
                continue
            if not isinstance(value, dict):
                log.warning("Снимок диалога повреждён; пропущен")
                continue
            result.append({"telegram_id": int(telegram_id),
                           "saved_at": float(saved_at), "payload": value})
        return result

    def clear_snapshot(self, telegram_ids) -> None:
        ids = [int(value) for value in telegram_ids]
        if not ids:
            return
        with self._lock:
            connection = self._db()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "DELETE FROM conversation_snapshots WHERE telegram_id = ?",
                    [(value,) for value in ids])
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

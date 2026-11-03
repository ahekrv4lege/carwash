"""Общий справочник моделей: model_id, миграция v3 -> v4 и читаемые имена.

Проверяется разделение двух идентификаторов: vehicle_id принадлежит конкретной
записи клиента, model_id — общей модели. Миграция тестируется на реалистичной
базе v3 с сохранением id, владельцев, госномеров, классов и прошлых заказов.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import vehicle_backup
from ai import Agent, ChatUser
from sheets import BOOKING_HEADERS
from tests.fakes import fixture
from tests.test_conversation import ARGS, response
from vehicles import CarMemoryError, SCHEMA_VERSION, VehicleDatabase, VehicleMemory

LEGACY_TABLE = (
    "CREATE TABLE vehicles ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL,"
    " car_key TEXT NOT NULL, car_name TEXT NOT NULL,"
    " size_class TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
    " last_tariff TEXT NOT NULL DEFAULT '', last_services TEXT NOT NULL DEFAULT '',"
    " last_plate TEXT NOT NULL DEFAULT '')"
)
LEGACY_INDEX = ("CREATE UNIQUE INDEX vehicles_active_key "
                "ON vehicles (telegram_id, car_key) WHERE status = 'active'")


def legacy_rows():
    """Реалистичная база: общие модели, варианты, удалённая строка, разные написания."""
    return [
        (101, 111, "toyota camry 3.5", "Toyota Camry 3.5", "Легковой", "active",
         "2025-01-01 10:00:00", "2026-01-05 10:00:00", "Базовый", '["Мойка"]', "а 123 ав 77"),
        (102, 222, "toyota camry 3.5", "toyota camry 3,5", "Кроссовер", "active",
         "2025-02-01 10:00:00", "2026-01-06 10:00:00", "Стандарт", '["Воск"]', "в 456 вв 99"),
        (103, 111, "kia rio", "Kia Rio", "Легковой", "deleted",
         "2025-03-01 10:00:00", "2026-01-07 10:00:00", "Базовый", '[]', "с 789 сс 50"),
        (104, 222, "kia rio x-line", "Kia Rio X-Line", "Кроссовер", "active",
         "2025-04-01 10:00:00", "2026-01-08 10:00:00", "Стандарт", '["Мойка", "Воск"]', "е 321 ее 78"),
        (105, 333, "bmw x5", "BMW X5", "Кроссовер", "active",
         "2025-01-02 10:00:00", "2026-01-09 10:00:00", "Базовый", '["Мойка"]', "к 654 кк 77"),
        (106, 444, "bmw x5", "BMW X5", "Кроссовер", "active",
         "2025-01-03 10:00:00", "2026-01-10 10:00:00", "Стандарт", '["Воск"]', "м 987 мм 99"),
        (107, 555, "bmw x5", "bmw x5", "Кроссовер", "active",
         "2025-01-04 10:00:00", "2026-01-11 10:00:00", "Базовый", '["Мойка"]', "н 147 нн 50"),
    ]


def make_legacy(path: Path, rows=None, *, version=3, unique_index=True):
    connection = sqlite3.connect(path)
    try:
        connection.execute(LEGACY_TABLE)
        if unique_index:
            connection.execute(LEGACY_INDEX)
        connection.executemany(
            "INSERT INTO vehicles (id, telegram_id, car_key, car_name, size_class, status, "
            "created_at, updated_at, last_tariff, last_services, last_plate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows if rows is not None else legacy_rows())
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def raw(path: Path, query: str, parameters=()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "vehicles.sqlite3"

    def open_db(self):
        db = VehicleDatabase(self.path)
        self.addCleanup(db.close)
        return db

    def models(self):
        return raw(self.path, "SELECT model_id, model_key, canonical_name FROM vehicle_models ORDER BY model_id")

    def vehicles(self):
        return raw(self.path, "SELECT id, telegram_id, model_id, size_class, status, "
                              "last_tariff, last_services, last_plate FROM vehicles ORDER BY id")

    def test_same_model_is_stored_once_for_different_customers(self):
        db = self.open_db()
        first = db.upsert(111, "Toyota Camry 3.5", "Легковой", "2026-01-01 10:00:00")
        second = db.upsert(222, "toyota camry 3,5", "Легковой", "2026-01-02 10:00:00")
        self.assertNotEqual(first.id, second.id)  # vehicle_id у каждой записи свой
        self.assertEqual(len(self.models()), 1)  # model_id общий
        self.assertEqual(self.models()[0][2], "Toyota Camry 3.5")
        self.assertEqual([car.name for car in db.list_active(111)], ["Toyota Camry 3.5"])
        self.assertEqual([car.name for car in db.list_active(222)], ["Toyota Camry 3.5"])
        self.assertEqual(raw(self.path, "SELECT COUNT(DISTINCT model_id) FROM vehicles")[0][0], 1)

    def test_distinct_variants_remain_distinct_models(self):
        db = self.open_db()
        db.upsert(111, "Kia Rio", "Легковой", "2026-01-01 10:00:00")
        db.upsert(111, "Kia Rio X-Line", "Кроссовер", "2026-01-02 10:00:00")
        self.assertEqual(sorted(row[1] for row in self.models()), ["kia rio", "kia rio x line"])
        self.assertEqual(sorted(car.name for car in db.list_active(111)), ["Kia Rio", "Kia Rio X-Line"])

    def test_vehicle_ids_stay_stable_when_model_is_reused(self):
        db = self.open_db()
        first = db.upsert(111, "BMW 5 Series", "Легковой", "2026-01-01 10:00:00")
        before = self.vehicles()
        db.upsert(222, "bmw 5 series", "Легковой", "2026-01-02 10:00:00")
        self.assertEqual(self.vehicles()[0][0], first.id)
        self.assertEqual([row[0] for row in self.vehicles()[:len(before)]], [row[0] for row in before])

    def test_personal_data_and_ownership_are_not_shared(self):
        db = self.open_db()
        db.upsert(111, "Toyota Camry", "Легковой", "2026-01-01 10:00:00",
                  tariff="Базовый", services=["Мойка"], plate="а 123 ав 77")
        db.upsert(222, "toyota camry", "Кроссовер", "2026-01-02 10:00:00",
                  tariff="Стандарт", services=["Воск"], plate="в 456 вв 99")
        first, second = db.list_active(111)[0], db.list_active(222)[0]
        self.assertEqual((first.last_tariff, first.last_services, first.last_plate),
                         ("Базовый", ("Мойка",), "а 123 ав 77"))
        self.assertEqual((second.last_tariff, second.last_services, second.last_plate),
                         ("Стандарт", ("Воск",), "в 456 вв 99"))
        self.assertEqual(len(self.models()), 1)
        self.assertEqual(raw(self.path, "SELECT COUNT(*) FROM vehicles")[0][0], 2)

    def test_duplicate_normalization_updates_one_vehicle_and_keeps_canonical_name(self):
        db = self.open_db()
        first = db.upsert(111, "Toyota Camry 3.5", "Легковой", "2026-01-01 10:00:00",
                          tariff="Базовый", services=["Мойка"], plate="а 123 ав 77")
        second = db.upsert(111, "TOYOTA CAMRY 3,5!", "Кроссовер", "2026-01-02 10:00:00")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.models()), 1)
        self.assertEqual(self.models()[0][2], "Toyota Camry 3.5")
        car = db.list_active(111)[0]
        self.assertEqual(car.size_class, "Кроссовер")
        self.assertEqual((car.last_tariff, car.last_services, car.last_plate),
                         ("Базовый", ("Мойка",), "а 123 ав 77"))

    def test_new_model_is_inserted_once_and_reused(self):
        db = self.open_db()
        db.upsert(111, "Lada Vesta SW Cross", "Легковой", "2026-01-01 10:00:00")
        db.upsert(222, "lada vesta sw cross", "Легковой", "2026-01-02 10:00:00")
        self.assertEqual([row[2] for row in self.models()], ["Lada Vesta SW Cross"])
        self.assertEqual(len(db.list_active(111)), 1)
        self.assertEqual(len(db.list_active(222)), 1)

    def test_forget_keeps_shared_model_and_other_customers(self):
        db = self.open_db()
        first = db.upsert(111, "Toyota Camry", "Легковой", "2026-01-01 10:00:00")
        db.upsert(222, "toyota camry", "Легковой", "2026-01-02 10:00:00")
        self.assertTrue(db.deactivate(111, first.id, "2026-01-03 10:00:00"))
        self.assertEqual(len(self.models()), 1)
        self.assertEqual(db.list_active(111), [])
        self.assertEqual([car.name for car in db.list_active(222)], ["Toyota Camry"])

    def test_deleted_vehicle_can_be_recreated_with_same_model_but_new_id(self):
        db = self.open_db()
        first = db.upsert(111, "Toyota Camry", "Легковой", "2026-01-01 10:00:00")
        db.deactivate(111, first.id, "2026-01-02 10:00:00")
        again = db.upsert(111, "toyota camry", "Легковой", "2026-01-03 10:00:00")
        self.assertNotEqual(first.id, again.id)
        self.assertEqual(len(self.models()), 1)
        self.assertEqual(raw(self.path, "SELECT COUNT(*) FROM vehicles WHERE status = 'deleted'")[0][0], 1)

    def test_foreign_key_rejects_unknown_model_id(self):
        db = self.open_db()
        db.upsert(111, "Toyota Camry", "Легковой", "2026-01-01 10:00:00")
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys=ON")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO vehicles (telegram_id, model_id, size_class, status, created_at, updated_at) "
                "VALUES (111, 999999, '', 'active', 'x', 'x')")

    def test_application_connection_enables_foreign_keys(self):
        db = self.open_db()
        db.list_active(111)
        self.assertEqual(db._db().execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_concurrent_same_model_creates_one_catalog_entry(self):
        errors = []

        def worker(user_id):
            try:
                db = VehicleDatabase(self.path)
                try:
                    for _ in range(4):
                        db.upsert(user_id, "Toyota RAV4", "Кроссовер", "2026-01-01 10:00:00")
                finally:
                    db.close()
            except Exception as error:  # pragma: no cover - не должно происходить
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(1000 + index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.models()), 1)
        self.assertEqual(raw(self.path, "SELECT COUNT(*) FROM vehicles")[0][0], 8)
        self.assertEqual(raw(self.path, "PRAGMA integrity_check")[0][0], "ok")

    def test_concurrent_distinct_models_are_not_lost(self):
        errors = []

        def worker(index):
            try:
                db = VehicleDatabase(self.path)
                try:
                    db.upsert(2000 + index, f"Unique Model {index}", "Легковой", "2026-01-01 10:00:00")
                finally:
                    db.close()
            except Exception as error:  # pragma: no cover - не должно происходить
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.models()), 10)
        self.assertEqual(raw(self.path, "SELECT COUNT(*) FROM vehicles")[0][0], 10)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "legacy.sqlite3"

    def open_db(self):
        db = VehicleDatabase(self.path)
        self.addCleanup(db.close)
        return db

    def test_v3_migration_preserves_rows_ids_ownership_and_order_history(self):
        make_legacy(self.path)
        db = self.open_db()
        db.list_active(111)  # ленивое открытие: миграция выполняется здесь
        self.assertEqual(raw(self.path, "PRAGMA user_version")[0][0], SCHEMA_VERSION)
        self.assertEqual(raw(self.path, "PRAGMA foreign_key_check"), [])
        ids = [row[0] for row in raw(self.path, "SELECT id FROM vehicles ORDER BY id")]
        self.assertEqual(ids, [row[0] for row in legacy_rows()])
        self.assertEqual(len(raw(self.path, "SELECT * FROM vehicle_models")), 4)  # Camry, Rio, Rio X-Line, X5
        # Названия и личные данные не потеряны.
        camry = db.list_active(111)[0]
        self.assertEqual((camry.id, camry.name, camry.size_class, camry.last_tariff,
                          camry.last_services, camry.last_plate),
                         (101, "Toyota Camry 3.5", "Легковой", "Базовый", ("Мойка",), "а 123 ав 77"))
        other = db.list_active(222)
        self.assertEqual([(car.id, car.name) for car in other],
                         [(104, "Kia Rio X-Line"), (102, "Toyota Camry 3.5")])
        # Обе записи Camry ссылаются на один model_id.
        models = raw(self.path, "SELECT id, telegram_id, model_id FROM vehicles ORDER BY id")
        self.assertEqual(models[0][2], models[1][2])
        # Удалённая строка сохранила статус и тоже получила model_id.
        deleted = raw(self.path, "SELECT id, status, model_id FROM vehicles WHERE id = 103")[0]
        self.assertEqual((deleted[0], deleted[1]), (103, "deleted"))
        self.assertIsNotNone(deleted[2])
        # Частое написание выбрано каноническим без выдумывания нового имени.
        bmw = raw(self.path, "SELECT canonical_name FROM vehicle_models WHERE model_key = 'bmw x5'")[0][0]
        self.assertEqual(bmw, "BMW X5")

    def test_migration_is_idempotent_across_reopens(self):
        make_legacy(self.path)
        self.open_db().list_active(111)
        ids = raw(self.path, "SELECT id, telegram_id, model_id FROM vehicles ORDER BY id")
        models = raw(self.path, "SELECT model_id, model_key, canonical_name FROM vehicle_models ORDER BY model_id")
        second = VehicleDatabase(self.path)
        self.addCleanup(second.close)
        self.assertEqual(raw(self.path, "PRAGMA user_version")[0][0], SCHEMA_VERSION)
        self.assertEqual(raw(self.path, "SELECT id, telegram_id, model_id FROM vehicles ORDER BY id"), ids)
        self.assertEqual(raw(self.path, "SELECT model_id, model_key, canonical_name FROM vehicle_models ORDER BY model_id"), models)

    def test_upsert_after_migration_reuses_model_and_vehicle(self):
        make_legacy(self.path)
        db = self.open_db()
        saved = db.upsert(111, "toyota camry 3,5", "Кроссовер", "2026-02-01 10:00:00",
                          tariff="Стандарт", services=["Воск"], plate="А 123 АВ 77")
        self.assertEqual(saved.id, 101)
        self.assertEqual(saved.name, "Toyota Camry 3.5")
        self.assertEqual(len(raw(self.path, "SELECT * FROM vehicle_models")), 4)
        self.assertEqual(db.list_active(111)[0].last_plate, "А 123 АВ 77")

    def test_migration_refuses_empty_key_without_changing_database(self):
        rows = legacy_rows() + [(108, 666, "", "", "", "active", "x", "x", "", "[]", "")]
        make_legacy(self.path, rows)
        with self.assertRaises(CarMemoryError):
            self.open_db().list_active(111)
        self.assertEqual(raw(self.path, "PRAGMA user_version")[0][0], 3)
        self.assertEqual(raw(self.path, "SELECT COUNT(*) FROM vehicles")[0][0], len(rows))
        self.assertEqual(raw(self.path, "SELECT name FROM sqlite_master WHERE name = 'vehicle_models'"), [])

    def test_migration_refuses_active_duplicates_without_partial_schema(self):
        rows = [
            (201, 111, "bmw x5", "BMW X5", "Кроссовер", "active", "x", "x", "", "[]", ""),
            (202, 111, "bmw x5", "BMW X5", "Кроссовер", "active", "x", "x", "", "[]", ""),
        ]
        make_legacy(self.path, rows, unique_index=False)
        with self.assertRaises(CarMemoryError):
            self.open_db().list_active(111)
        self.assertEqual(raw(self.path, "PRAGMA user_version")[0][0], 3)
        self.assertEqual(raw(self.path, "SELECT COUNT(*) FROM vehicles")[0][0], 2)
        self.assertEqual(raw(self.path, "SELECT name FROM sqlite_master WHERE name = 'vehicle_models'"), [])

    def test_migration_refuses_unknown_status(self):
        rows = [(301, 111, "bmw x5", "BMW X5", "Кроссовер", "archived", "x", "x", "", "[]", "")]
        make_legacy(self.path, rows)
        with self.assertRaises(CarMemoryError):
            self.open_db().list_active(111)
        self.assertEqual(raw(self.path, "PRAGMA user_version")[0][0], 3)

    def test_corrupted_file_is_refused(self):
        self.path.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            self.open_db().list_active(111)

    def test_old_backup_verifies_restores_and_migrates(self):
        make_legacy(self.path)
        self.assertEqual(vehicle_backup.verify(self.path)["active_vehicles"], 6)
        target = Path(self.temp.name) / "restored" / "vehicles.sqlite3"
        vehicle_backup.restore(self.path, target, force=False)
        restored = VehicleDatabase(target)
        self.addCleanup(restored.close)
        self.assertEqual([(car.id, car.name) for car in restored.list_active(222)],
                         [(104, "Kia Rio X-Line"), (102, "Toyota Camry 3.5")])
        self.assertEqual(raw(target, "PRAGMA user_version")[0][0], SCHEMA_VERSION)

    def test_migrated_backup_is_consistent_and_restorable(self):
        make_legacy(self.path)
        db = self.open_db()
        db.upsert(111, "Lada Vesta", "Легковой", "2026-02-01 10:00:00")
        destination = vehicle_backup.backup(self.path, Path(self.temp.name) / "backups", keep=2)
        self.assertEqual(vehicle_backup.verify(destination)["active_vehicles"], 7)
        target = Path(self.temp.name) / "restored.sqlite3"
        vehicle_backup.restore(destination, target, force=False)
        restored = VehicleDatabase(target)
        self.addCleanup(restored.close)
        self.assertIn("Lada Vesta", [car.name for car in restored.list_active(111)])

    def test_newer_schema_is_still_refused(self):
        make_legacy(self.path)
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        with self.assertRaises(CarMemoryError):
            self.open_db().list_active(111)


class AgentCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(111, None, "Synthetic")

    async def say(self, text, *outputs):
        self.transport.side_effect = list(outputs)
        result = await self.agent.reply(self.user, text)
        self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id))
        return result

    async def test_booking_sheet_gets_human_readable_name_not_model_id(self):
        args = dict(ARGS, car="Toyota Camry 3.5")
        await self.say("Хочу записаться: " + json.dumps(args, ensure_ascii=False),
                       response(calls=[("get_price_list", {})]),
                       response(calls=[("create_booking", args)]))
        offer = self.agent._pending[self.user.id]["offer_id"]
        answer = await self.agent.proposal_action(self.user, offer, "confirm")
        self.assertIn("Записала", answer)
        rows = self.spreadsheet.worksheet("Записи").values
        headers, row = rows[0], rows[1]
        car = row[headers.index("Автомобиль")]
        self.assertEqual(car, "Toyota Camry 3.5")
        self.assertFalse(any("model_id" in str(header).lower() for header in headers))
        self.assertNotIn("model_id", str(row))

    async def test_two_customers_share_one_catalog_model_but_not_records(self):
        self.agent.cars.remember(111, "Toyota Camry 3.5", "Легковой")
        self.agent.cars.remember(222, "toyota camry 3,5", "Легковой")
        self.assertEqual(raw(Path(self.store.config.vehicle_memory_file),
                             "SELECT COUNT(*) FROM vehicle_models")[0][0], 1)
        self.assertEqual([car.name for car in self.agent.cars.for_user(111)], ["Toyota Camry 3.5"])
        self.assertEqual([car.name for car in self.agent.cars.for_user(222)], ["Toyota Camry 3.5"])
        self.assertNotEqual(self.agent.cars.for_user(111)[0].id, self.agent.cars.for_user(222)[0].id)

    async def test_restart_new_agent_resolves_name_from_catalog(self):
        self.agent.cars.remember(111, "Kia Rio X-Line", "Кроссовер",
                                 tariff="Стандарт", services=["Мойка"], plate="а 123 ав 77")
        with patch("ai.AsyncOpenAI"):
            restarted = Agent(self.store.config, self.store)
        self.addCleanup(restarted.cars.close)
        cars = restarted.cars.for_user(111)
        self.assertEqual([(car.name, car.size_class, car.last_tariff, car.last_services, car.last_plate)
                          for car in cars],
                         [("Kia Rio X-Line", "Кроссовер", "Стандарт", ("Мойка",), "а 123 ав 77")])

    async def test_ai_typo_normalization_shares_one_catalog_model(self):
        await self.say("У меня таёта камри", response(calls=[("respond", {
            "reply": "Запомнила.",
            "facts": {"car": ["таёта камри", "таёта камри"],
                      "car_model": ["Toyota Camry", "таёта камри"]},
            "tentative": {}})]))
        self.user = ChatUser(222, None, "Other")
        await self.say("у меня тойота камри", response(calls=[("respond", {
            "reply": "Запомнила.",
            "facts": {"car": ["тойота камри", "тойота камри"],
                      "car_model": ["toyota camry", "тойота камри"]},
            "tentative": {}})]))
        path = Path(self.store.config.vehicle_memory_file)
        self.assertEqual(raw(path, "SELECT COUNT(*) FROM vehicle_models")[0][0], 1)
        self.assertEqual(raw(path, "SELECT canonical_name FROM vehicle_models")[0][0], "Toyota Camry")
        self.assertEqual(raw(path, "SELECT COUNT(*) FROM vehicles")[0][0], 2)
        self.assertEqual([car.name for car in self.agent.cars.for_user(111)], ["Toyota Camry"])
        self.assertEqual([car.name for car in self.agent.cars.for_user(222)], ["Toyota Camry"])

    async def test_ambiguous_model_never_reaches_catalog(self):
        call = ("respond", {
            "reply": "Уточню модель.",
            "facts": {"car": ["королла", "королла"],
                      "car_model": ["Toyota Corolla", "королла"]},
            "tentative": {}})
        answer = await self.say("У меня королла", response(calls=[call]))
        self.assertIn("Уточните", answer)
        path = Path(self.store.config.vehicle_memory_file)
        self.assertEqual(raw(path, "SELECT COUNT(*) FROM vehicle_models")[0][0], 0)
        self.assertEqual(raw(path, "SELECT COUNT(*) FROM vehicles")[0][0], 0)


if __name__ == "__main__":
    unittest.main()

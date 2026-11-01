"""Память об автомобилях клиента: SQLite + ленивый RAM-кэш с выгрузкой по простою.

Время и давление памяти подменяются: тесты не ждут 30 минут и не читают /proc.
"""
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import vehicle_backup
from ai import Agent, ChatUser
from bot import cmd_forgetcar, cmd_mycars
from tests.fakes import fixture
from tests.test_conversation import ARGS, response
from vehicles import (CarMemoryError, RetentionPolicy, SCHEMA_VERSION, SavedCar, VehicleCache, VehicleDatabase,
                      VehicleMemory, car_key)

MINUTE = 60.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def memory(path, clock=None, reader=lambda: None, **kwargs):
    return VehicleMemory(path, clock=clock or Clock(), memory_reader=reader, **kwargs)


class KeyTests(unittest.TestCase):
    def test_case_yo_comma_and_punctuation_do_not_split_one_car(self):
        self.assertEqual(car_key("Тойота Камри 3.5"), car_key("тойота  камри 3,5"))
        self.assertEqual(car_key("Toyota Camry 3.5"), car_key("toyota camry 3.5!"))
        self.assertEqual(car_key("Ёжик 1"), car_key("ежик 1"))
        self.assertNotEqual(car_key("Toyota Camry 3.5"), car_key("Toyota Camry 2.5"))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "vehicles.sqlite3"

    def test_database_file_is_not_created_until_first_use(self):
        VehicleDatabase(self.path)
        self.assertFalse(self.path.exists())

    def test_upsert_deduplicates_and_updates_class(self):
        db = VehicleDatabase(self.path)
        first = db.upsert(111, "Toyota Camry 3.5", "Легковой", "2026-01-01 10:00:00")
        second = db.upsert(111, "toyota camry 3,5", "Кроссовер", "2026-01-02 10:00:00")
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.size_class, "Кроссовер")
        self.assertEqual(len(db.list_active(111)), 1)

    def test_unique_index_blocks_raw_duplicate_even_without_python_check(self):
        db = VehicleDatabase(self.path)
        db.upsert(111, "BMW X5", "Кроссовер", "2026-01-01 10:00:00")
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO vehicles (telegram_id, car_key, car_name, size_class, status, created_at, updated_at) "
                        "VALUES (111, 'bmw x5', 'BMW X5', '', 'active', 'x', 'x')")
        raw.close()

    def test_limit_per_customer(self):
        db = VehicleDatabase(self.path)
        for number in range(10):
            self.assertIsNotNone(db.upsert(111, f"Car {number}", "Легковой", "2026-01-01 10:00:00"))
        self.assertIsNone(db.upsert(111, "Car 10", "Легковой", "2026-01-01 10:00:00"))

    def test_deactivate_checks_owner_and_never_reuses_ids(self):
        db = VehicleDatabase(self.path)
        car = db.upsert(111, "Lada Vesta", "Легковой", "2026-01-01 10:00:00")
        self.assertFalse(db.deactivate(222, car.id, "2026-01-02 10:00:00"))
        self.assertTrue(db.deactivate(111, car.id, "2026-01-02 10:00:00"))
        self.assertEqual(db.list_active(111), [])
        again = db.upsert(111, "Lada Vesta", "Легковой", "2026-01-03 10:00:00")
        self.assertNotEqual(again.id, car.id)

    def test_persists_across_connections(self):
        VehicleDatabase(self.path).upsert(111, "Toyota Camry 3.5", "Легковой", "2026-01-01 10:00:00")
        self.assertEqual([c.name for c in VehicleDatabase(self.path).list_active(111)], ["Toyota Camry 3.5"])

    def test_newer_schema_is_refused(self):
        raw = sqlite3.connect(self.path)
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()
        with self.assertRaises(CarMemoryError):
            VehicleDatabase(self.path).list_active(111)

    def test_backup_is_consistent_while_wal_is_open(self):
        db = VehicleDatabase(self.path)
        db.upsert(111, "Toyota Camry 3.5", "Легковой", "2026-01-01 10:00:00")
        copy = Path(self.temp.name) / "copy.sqlite3"
        db.backup_to(copy)
        self.assertEqual(vehicle_backup.verify(copy)["active_vehicles"], 1)


class CacheTests(unittest.TestCase):
    def test_empty_result_is_cached(self):
        clock = Clock()
        cache = VehicleCache(1024 * 1024, clock=clock)
        cache.put(111, ())
        self.assertEqual(cache.get(111), ())
        self.assertEqual(cache.hits, 1)

    def test_idle_eviction_uses_last_activity_not_load_time(self):
        clock = Clock()
        cache = VehicleCache(1024 * 1024, clock=clock)
        cache.put(111, ())
        clock.advance(20 * MINUTE)
        cache.get(111)  # активность продлевает срок
        clock.advance(20 * MINUTE)
        self.assertEqual(cache.evict_idle(30 * MINUTE), 0)
        clock.advance(11 * MINUTE)
        self.assertEqual(cache.evict_idle(30 * MINUTE), 1)
        self.assertIsNone(cache.get(111))

    def test_lru_budget_evicts_least_recently_used_first(self):
        cache = VehicleCache(2000)
        big = (SavedCar(1, "x" * 200, "y"),)
        for user in (1, 2, 3, 4):
            cache.put(user, big)
        self.assertLessEqual(cache.bytes_used, 2000 + cache._size(big))
        self.assertIsNone(cache.get(1))
        self.assertIsNotNone(cache.get(4))


class PolicyTests(unittest.TestCase):
    def test_default_is_thirty_minutes_without_pressure_signal(self):
        policy = RetentionPolicy()
        for _ in range(5):
            self.assertEqual(policy.update(available_mb=None, hit_rate=None, fill_ratio=0.1), 30.0)

    def test_pressure_reduces_towards_fifteen_in_small_steps(self):
        policy = RetentionPolicy()
        values = [policy.update(available_mb=100, hit_rate=0.9, fill_ratio=0.1) for _ in range(5)]
        self.assertEqual(values, [25.0, 20.0, 15.0, 15.0, 15.0])

    def test_good_hit_rate_and_free_memory_allow_up_to_sixty(self):
        policy = RetentionPolicy()
        values = [policy.update(available_mb=900, hit_rate=0.95, fill_ratio=0.2) for _ in range(8)]
        self.assertEqual(values[-1], 60.0)
        self.assertTrue(all(b - a <= 5.0 for a, b in zip(values, values[1:])))

    def test_non_adaptive_mode_is_fixed(self):
        policy = RetentionPolicy(adaptive=False)
        self.assertEqual(policy.update(available_mb=50, hit_rate=0.0, fill_ratio=1.0), 30.0)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "vehicles.sqlite3"
        self.clock = Clock()

    def new(self, **kwargs):
        return memory(self.path, clock=self.clock, **kwargs)

    def test_repeated_messages_do_not_read_sqlite_again(self):
        mem = self.new()
        mem.remember(111, "Toyota Camry 3.5", "Легковой")
        mem.cache.drop(111)
        for _ in range(50):
            mem.touch(111)
        self.assertEqual(mem.db_reads, 1)
        self.assertEqual(mem.cache.hits, 49)

    def test_idle_eviction_keeps_database_and_reloads_on_return(self):
        mem = self.new()
        mem.remember(111, "Toyota Camry 3.5", "Легковой")
        mem.for_user(111)
        self.clock.advance(31 * MINUTE)
        report = mem.maintenance()
        self.assertEqual(report["evicted"], 1)
        self.assertEqual(report["entries"], 0)
        reads = mem.db_reads
        self.assertEqual([c.name for c in mem.for_user(111)], ["Toyota Camry 3.5"])
        self.assertEqual(mem.db_reads, reads + 1)

    def test_activity_refresh_keeps_active_customer_in_cache(self):
        mem = self.new()
        mem.remember(111, "Toyota Camry 3.5", "Легковой")
        mem.for_user(111)
        for _ in range(4):
            self.clock.advance(20 * MINUTE)
            mem.touch(111)
            mem.maintenance()
        self.assertEqual(len(mem.cache), 1)

    def test_other_customers_are_not_evicted_or_leaked(self):
        mem = self.new()
        mem.remember(111, "BMW X5", "Кроссовер")
        self.clock.advance(31 * MINUTE)
        mem.remember(222, "Lada Vesta", "Легковой")
        mem.maintenance()
        self.assertEqual([c.name for c in mem.for_user(222)], ["Lada Vesta"])
        self.assertEqual([c.name for c in mem.for_user(111)], ["BMW X5"])

    def test_forget_by_other_customer_is_refused(self):
        mem = self.new()
        car = mem.remember(111, "BMW X5", "Кроссовер")
        self.assertFalse(mem.forget(222, car.id))
        self.assertEqual(len(mem.for_user(111)), 1)
        self.assertTrue(mem.forget(111, car.id))
        self.assertEqual(mem.for_user(111), [])

    def test_restart_restores_records(self):
        self.new().remember(111, "Toyota Camry 3.5", "Легковой")
        self.assertEqual([c.name for c in self.new().for_user(111)], ["Toyota Camry 3.5"])

    def test_failed_database_write_leaves_database_and_cache_consistent(self):
        mem = self.new()
        mem.remember(111, "Toyota Camry 3.5", "Легковой")
        with patch.object(mem.db, "upsert", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                mem.remember(111, "BMW X5", "Кроссовер")
        self.assertEqual([c.name for c in self.new().for_user(111)], ["Toyota Camry 3.5"])
        self.assertEqual([c.name for c in mem.for_user(111)], ["Toyota Camry 3.5"])

    def test_concurrent_remember_of_same_car_creates_one_record(self):
        mem = self.new()
        errors = []

        def worker():
            try:
                mem.remember(111, "Toyota Camry 3.5", "Легковой")
            except Exception as error:  # pragma: no cover - должно не происходить
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(mem.for_user(111)), 1)

    def test_maintenance_reads_pressure_only_every_five_minutes(self):
        reader = Mock(return_value=900.0)
        mem = self.new(reader=reader)
        for _ in range(5):
            mem.maintenance()
            self.clock.advance(MINUTE)
        self.assertEqual(reader.call_count, 1)

    def test_maintenance_shortens_retention_under_pressure(self):
        mem = self.new(reader=lambda: 100.0)
        ttl = [mem.maintenance()["ttl_min"] for _ in range(4)]
        self.assertEqual(ttl[-1], 15.0)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "live.sqlite3"
        VehicleDatabase(self.source).upsert(111, "Toyota Camry 3.5", "Легковой", "2026-01-01 10:00:00")
        VehicleDatabase(self.source).upsert(222, "BMW X5", "Кроссовер", "2026-01-01 10:00:00")

    def test_backup_is_private_and_restorable(self):
        destination = vehicle_backup.backup(self.source, self.root / "backups", keep=5)
        self.assertEqual(stat.S_IMODE(os.stat(destination).st_mode), 0o600)
        self.assertEqual(vehicle_backup.verify(destination), {"ok": True, "active_vehicles": 2, "customers": 2})
        target = self.root / "restored" / "customer_vehicles.sqlite3"
        vehicle_backup.restore(destination, target, force=False)
        self.assertEqual([c.name for c in VehicleDatabase(target).list_active(111)], ["Toyota Camry 3.5"])

    def test_restore_refuses_to_overwrite_without_force_and_keeps_old_copy_with_force(self):
        destination = vehicle_backup.backup(self.source, self.root / "backups", keep=5)
        target = self.root / "target.sqlite3"
        target.write_bytes(b"old")
        with self.assertRaises(SystemExit):
            vehicle_backup.restore(destination, target, force=False)
        vehicle_backup.restore(destination, target, force=True)
        kept = list(self.root.glob("target.sqlite3.before-restore-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), b"old")

    def test_retention_keeps_only_latest_backups(self):
        directory = self.root / "backups"
        for index in range(4):
            directory.mkdir(exist_ok=True)
            (directory / f"customer_vehicles-2026010{index + 1}-000000.sqlite3").write_bytes(b"x")
        vehicle_backup.backup(self.source, directory, keep=3)
        self.assertEqual(len(list(directory.glob("customer_vehicles-*.sqlite3"))), 3)

    def test_backup_without_database_is_a_clean_noop(self):
        missing = self.root / "never-created.sqlite3"
        self.assertEqual(vehicle_backup.main(["backup", "--source", str(missing), "--dir", str(self.root / "b")]), 0)
        self.assertFalse((self.root / "b").exists())

    def test_invalid_backup_is_rejected(self):
        bad = self.root / "bad.sqlite3"
        bad.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            vehicle_backup.verify(bad)


class AgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
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

    async def book_camry(self):
        args = dict(ARGS, car="Toyota Camry 3.5")
        await self.say("Хочу обсудить запись: " + json.dumps(args, ensure_ascii=False),
                       response(calls=[("get_price_list", {})]),
                       response(calls=[("create_booking", args)]))
        offer = self.agent._pending[self.user.id]["offer_id"]
        return await self.agent.proposal_action(self.user, offer, "confirm")

    async def test_confirmed_booking_is_remembered_in_database(self):
        self.assertIn("Записала", await self.book_camry())
        self.assertEqual([(c.name, c.size_class) for c in self.agent.cars.for_user(111)], [("Toyota Camry 3.5", "Легковой")])
        self.assertEqual(len(self.spreadsheet.worksheet("Записи").writes), 1)  # бронирование записано в таблицу один раз

    async def test_memory_failure_does_not_change_booking_answer(self):
        with patch.object(VehicleMemory, "remember", side_effect=sqlite3.OperationalError("disk full")):
            self.assertIn("Записала", await self.book_camry())

    async def test_ordinary_messages_do_not_write_to_database(self):
        with patch.object(self.agent.cars.db, "upsert", wraps=self.agent.cars.db.upsert) as upsert:
            await self.say("Здравствуйте", response(text="Здравствуйте!"))
            await self.say("Какие услуги есть?", response(text="Уточню."))
        upsert.assert_not_called()

    async def test_saved_cars_hint_is_loaded_lazily_and_has_no_ids(self):
        self.agent.cars.remember(111, "Lada Vesta", "Легковой")
        self.agent.cars.cache.drop(111)
        await self.say("Здравствуйте, хочу на мойку", response(text="Какой автомобиль?"))
        hints = [m["content"] for m in self.transport.call_args.kwargs["messages"] if m["content"].startswith("SAVED_CARS ")]
        self.assertEqual(len(hints), 1)
        self.assertIn("Lada Vesta", hints[0])
        self.assertNotIn('"id"', hints[0])

    async def test_no_saved_cars_hint_when_car_is_already_known(self):
        self.agent.cars.remember(111, "Lada Vesta", "Легковой")
        self.agent._state[111]["facts"] = {"car": "Toyota Camry 3.5"}
        await self.say("Какая цена на мойку?", response(text="Уточню."))
        self.assertFalse([m for m in self.transport.call_args.kwargs["messages"] if m["content"].startswith("SAVED_CARS ")])

    async def test_customer_commands_use_owned_records(self):
        self.agent.cars.remember(111, "Toyota Camry 3.5", "Легковой")
        self.agent.cars.remember(222, "BMW X5", "Кроссовер")
        msg = SimpleNamespace(from_user=SimpleNamespace(id=111), answer=AsyncMock())
        await cmd_mycars(msg, self.agent)
        text = msg.answer.await_args.args[0]
        self.assertIn("Toyota Camry 3.5", text)
        self.assertNotIn("BMW X5", text)
        msg = SimpleNamespace(from_user=SimpleNamespace(id=111), answer=AsyncMock())
        await cmd_forgetcar(msg, SimpleNamespace(args="1"), self.agent)
        self.assertEqual(self.agent.cars.for_user(111), [])
        self.assertEqual(len(self.agent.cars.for_user(222)), 1)


if __name__ == "__main__":
    unittest.main()

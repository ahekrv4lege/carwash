"""Административная статистика: точные значения на синтетических данных и доступ только администратору.

Ожидаемые числа посчитаны вручную по синтетическим строкам ниже.
"""
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import analytics
from bot import AdminOnly, cmd_stats
from sheets import FreeCapacity
from tests.fakes import booking, fixture
import tempfile

TODAY = date(2026, 10, 20)


def row(day, time, status="Новая", car_class="Легковой", tariff="", services="", amount=""):
    return {"Дата": day, "Время": time, "Статус": status, "Класс": car_class, "Тариф": tariff,
            "Состав": services, "Сумма": amount}


SLOTS = {
    (date(2026, 10, 18), "09:00"): 2,
    (date(2026, 10, 18), "10:00"): 2,
    (date(2026, 10, 20), "09:00"): 2,
    (date(2026, 10, 21), "09:00"): 2,   # завтра: не входит в период, но предстоящая запись есть
    (date(2026, 10, 1), "09:00"): 2,    # вне 7 дней
}

ROWS = [
    row("18.10.2026", "09:00", "Архив", tariff="Экспресс", services="Мойка кузова, Чернение резины", amount="900 ₽"),
    row("18.10.2026", "09:00", "Отмена", car_class="Кроссовер", tariff="Стандарт", services="Мойка кузова", amount="1000"),
    row("20.10.2026", "09:00", amount="700 ₽", services="Мойка кузова"),
    row("20.10.2026", "09:00", services="Мойка кузова"),
    row("20.10.2026", "09:00", services="Мойка кузова"),
    row("20.10.2026", "10:00"),                        # время, которого нет в «Слоты»
    row("21.10.2026", "09:00"),                        # предстоящая
    row("01.10.2026", "09:00", "Архив", amount="500"),  # вне периода
    row("", "", "Новая"),                              # без даты и времени
]


class ComputeTests(unittest.TestCase):
    def test_seven_day_period_exact_counts(self):
        stats = analytics.compute(ROWS, SLOTS, TODAY, 7)
        self.assertEqual((stats.start, stats.end), (date(2026, 10, 14), TODAY))
        self.assertEqual(stats.total, 6)
        self.assertEqual(stats.canceled, 1)
        self.assertEqual(stats.completed, 1)
        self.assertEqual(stats.open_past, 4)
        self.assertEqual(stats.upcoming_total, 1)
        self.assertEqual(stats.skipped_rows, 1)
        self.assertAlmostEqual(stats.cancellation_rate, 1 / 6)

    def test_occupancy_and_capacity_use_hourly_slots(self):
        stats = analytics.compute(ROWS, SLOTS, TODAY, 7)
        self.assertEqual(stats.capacity_hours, 6)       # 2 + 2 + 2 по слотам в периоде
        self.assertEqual(stats.occupied_hours, 5)       # все неотменённые в периоде
        self.assertAlmostEqual(stats.utilization, 5 / 6)
        self.assertEqual(stats.over_capacity_hours, 1)  # три записи на 20.10 09:00 при двух постах
        self.assertEqual(stats.unlisted_hours, 1)

    def test_breakdowns_and_booked_value(self):
        stats = analytics.compute(ROWS, SLOTS, TODAY, 7)
        self.assertEqual(stats.by_tariff, {"Экспресс": 1, "без тарифа": 4})
        self.assertEqual(stats.by_class, {"Легковой": 5})
        self.assertEqual(stats.by_hour, {"09:00": 4, "10:00": 1})
        self.assertEqual(stats.by_service, {"Мойка кузова": 4, "Чернение резины": 1})
        self.assertEqual(stats.booked_value, 1600)      # 900 + 700; отменённая 1000 не входит
        self.assertEqual(stats.value_missing, 3)

    def test_one_day_period_detects_over_capacity(self):
        stats = analytics.compute(ROWS, SLOTS, TODAY, 1)
        self.assertEqual(stats.total, 4)
        self.assertEqual(stats.capacity_hours, 2)
        self.assertEqual(stats.over_capacity_hours, 1)  # три записи на 09:00 при двух постах
        self.assertEqual(stats.unlisted_hours, 1)

    def test_rescheduled_booking_is_one_row_counted_once(self):
        moved = [row("20.10.2026", "11:00", amount="700")]  # перенос меняет дату/время в той же строке
        stats = analytics.compute(moved, {}, TODAY, 7)
        self.assertEqual(stats.total, 1)
        self.assertEqual(stats.booked_value, 700)

    def test_free_grid_capacity_does_not_fake_utilization(self):
        slots = {(date(2026, 10, 20), "09:00"): FreeCapacity(1)}
        stats = analytics.compute([row("20.10.2026", "09:00")], slots, TODAY, 7)
        self.assertIsNone(stats.capacity_hours)
        self.assertIsNone(stats.utilization)

    def test_empty_tables_yield_zero_without_division(self):
        stats = analytics.compute([], {}, TODAY, 30)
        self.assertEqual((stats.total, stats.canceled, stats.occupied_hours), (0, 0, 0))
        self.assertIsNone(stats.cancellation_rate)
        self.assertIsNone(stats.utilization)

    def test_render_states_counts_and_non_payment_value(self):
        text = analytics.render(analytics.compute(ROWS, SLOTS, TODAY, 7))
        self.assertIn("Всего записей: 6", text)
        self.assertIn("Отменено: 1 (17%)", text)
        self.assertIn("забронировано, не оплачено", text)
        self.assertIn("Мойка кузова: 4", text)


class AdminAccessTests(unittest.IsolatedAsyncioTestCase):
    def config(self, admin, users=()):
        return SimpleNamespace(admin_chat_id=admin, admin_user_ids=tuple(users))

    def message(self, user_id, text=""):
        return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())

    async def test_only_configured_admin_passes_filter(self):
        check = AdminOnly()
        self.assertTrue(await check(self.message(111), self.config(111)))
        self.assertFalse(await check(self.message(222), self.config(111)))
        self.assertFalse(await check(self.message(111), self.config(None)))

    async def test_explicit_admin_user_ids_allow_list(self):
        check = AdminOnly()
        self.assertTrue(await check(self.message(555), self.config(None, users=(555, 666))))
        self.assertFalse(await check(self.message(777), self.config(None, users=(555, 666))))

    async def test_no_admin_configuration_denies_everyone(self):
        check = AdminOnly()
        self.assertFalse(await check(self.message(111), self.config(None)))

    async def test_customer_gets_no_statistics(self):
        self.assertFalse(await AdminOnly()(self.message(222), self.config(111)))

    async def test_stats_command_for_admin_answers_with_real_counts(self):
        store = Mock()
        store.analytics_snapshot = Mock(return_value=(ROWS, SLOTS))
        store.now = Mock(return_value=SimpleNamespace(date=lambda: TODAY))
        msg = self.message(111)
        await cmd_stats(msg, SimpleNamespace(args="7"), store)
        sent = msg.answer.await_args.args[0]
        self.assertIn("Всего записей: 6", sent)

    async def test_invalid_period_is_rejected_without_reading_sheets(self):
        store = Mock()
        msg = self.message(111)
        await cmd_stats(msg, SimpleNamespace(args="5"), store)
        store.analytics_snapshot.assert_not_called()
        self.assertIn("/stats 30", msg.answer.await_args.args[0])

    async def test_storage_failure_returns_generic_message(self):
        store = Mock()
        store.analytics_snapshot = Mock(side_effect=RuntimeError("secret detail"))
        store.now = Mock(return_value=SimpleNamespace(date=lambda: TODAY))
        msg = self.message(111)
        await cmd_stats(msg, SimpleNamespace(args=""), store)
        sent = msg.answer.await_args.args[0]
        self.assertNotIn("secret", sent)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_read_only_and_sees_new_booking(self):
        with tempfile.TemporaryDirectory() as temp:
            store, spreadsheet = fixture(temp)
            def mutations():
                return sum(len(sheet.writes) + len(sheet.updates) + sheet.format_calls for sheet in spreadsheet.sheets)
            before = mutations()
            rows, slots = store.analytics_snapshot()
            self.assertEqual(mutations(), before)  # статистика не пишет в таблицу
            self.assertEqual(rows, [])
            self.assertIn((date(2099, 1, 1), "10:00"), slots)
            self.assertTrue(store.create_booking(**booking())["ok"])
            rows, slots = store.analytics_snapshot()
            self.assertEqual(len(rows), 1)  # кэш сброшен: новая запись видна сразу
            stats = analytics.compute(rows, slots, date(2098, 12, 31), 7)
            self.assertEqual(stats.upcoming_total, 1)


if __name__ == "__main__":
    unittest.main()

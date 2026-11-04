"""Тарифы и ресурсы на разных листах и раскладках: сквозь адаптивный маппинг.

Проверяется поведение существующей системы на различных структурах таблиц,
а не конкретная фикстура: переименования, порядок колонок, английские алиасы,
скидки, отсутствующие цены, пакеты, вместимость и legacy-режим без адаптива.
"""
import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace

from adaptive import MappingError
from sheets import Store
from tests.fakes import booking, fixture


def sub(temp):
    path = os.path.join(temp, f"store-{next(_DIRS)}")
    os.makedirs(path, exist_ok=True)
    return path


_DIRS = iter(__import__("itertools").count(1))


class TariffLayoutTests(unittest.TestCase):
    """Тарифы: название листа, порядок и названия колонок, скидки, пакеты."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(sub(self.temp.name))
        self.tariffs = self.spreadsheet.worksheet("Тарифы")

    def price(self, *args, **kwargs):
        self.store._invalidate("catalog")
        return self.store.calculate_price(*args, **kwargs)

    def test_tariffs_on_renamed_sheet_with_other_column_order(self):
        self.tariffs.title = "Packages"
        self.tariffs.values[0] = ["Available", "Discount %", "Composition", "Package"]
        self.tariffs.values[1] = ["да", "10", "Мойка, Воск", "Базовый"]
        quote = self.price("Легковой", "Базовый", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 630)  # (700) * 0.9: Воск в фикстуре недоступен

    def test_discount_formats_and_math(self):
        for text, total in (("10", 630), ("10%", 630), ("0", 700), ("25", 525)):
            self.tariffs.values[1][2] = text
            self.store._invalidate("catalog")
            quote = self.store.calculate_price("Легковой", "Базовый", [])
            self.assertTrue(quote["ok"], quote)
            self.assertEqual(quote["total"], total, text)

    def test_discount_change_between_proposal_and_confirmation(self):
        quote = self.price("Легковой", "Базовый", [])
        expected = dict(quote, currency="₽")
        self.tariffs.values[1][2] = "25"
        self.store._invalidate("catalog")
        changed = self.store.create_booking(**booking(expected_quote=expected))
        self.assertFalse(changed["ok"])
        self.assertTrue(changed.get("quote_changed"), changed)
        self.assertEqual(changed["quote"]["total"], 525)

    def test_missing_price_for_one_class_rejects_only_that_class(self):
        sheet = self.spreadsheet.worksheet("Услуги")
        sheet.values.append(["Химчистка", "Салон", "3000", "", "да"])
        self.store._invalidate("catalog")
        refused = self.store.calculate_price("Кроссовер", None, ["Химчистка"])
        self.assertFalse(refused["ok"])
        self.assertIn("не оказывается для класса", refused["error"])
        fine = self.price("Легковой", None, ["Химчистка"])
        self.assertEqual(fine["total"], 3000)

    def test_service_unavailable_is_removed_from_tariff_with_note(self):
        sheet = self.spreadsheet.worksheet("Услуги")
        sheet.values[2][4] = "нет"  # Воск недоступен
        self.store._invalidate("catalog")
        quote = self.price("Легковой", "Базовый", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 630)  # скидка 10% от одной мойки
        self.assertEqual(quote["lines"][0]["not_included_now"], ["Воск"])
        price_list = self.store.get_catalog().price_list()
        note = next(entry for entry in price_list["tariffs"] if entry["name"] == "Базовый")
        self.assertEqual(note["not_included_now"], ["Воск"])

    def test_unknown_service_in_composition_is_dropped_with_warning(self):
        self.tariffs.values[1][1] = "Мойка, Несуществующая"
        self.store._invalidate("catalog")
        quote = self.price("Легковой", "Базовый", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 630)
        self.assertEqual(quote["lines"][0]["not_included_now"], ["Несуществующая"])

    def test_tariff_without_available_items_is_unavailable(self):
        self.tariffs.values[1][1] = "Несуществующая"
        self.store._invalidate("catalog")
        refused = self.price("Легковой", "Базовый", [])
        self.assertFalse(refused["ok"])
        self.assertIn("все его позиции убраны", refused["error"])

    def test_ambiguous_composition_columns_are_rejected(self):
        headers, rows = self.tariffs.values[0], self.tariffs.values[1:]
        self.tariffs.values = [headers + ["Состав"], *[row + ["Мойка"] for row in rows]]
        self.store._invalidate("catalog")
        with self.assertRaises(MappingError):
            self.price("Легковой", "Базовый", [])

    def test_renamed_columns_are_remapped_by_aliases(self):
        self.tariffs.values[0] = ["Тариф", "Composition", "Скидка %", "Доступен"]
        self.store._invalidate("catalog")
        quote = self.price("Легковой", "Базовый", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 630)

    def test_legacy_mode_without_adaptive_reads_same_business_rules(self):
        legacy = Store(replace(self.store.config, sheets_adaptive_enabled=False))
        legacy._spreadsheet = self.spreadsheet
        self.assertIsNotNone(legacy._adaptive)
        quote = legacy.calculate_price("Легковой", "Базовый", [])
        self.assertEqual(quote["total"], 630)  # те же правила, что и в адаптивном режиме
        result = legacy.create_booking(**booking())
        self.assertTrue(result["ok"], result)
        self.assertTrue(legacy.cancel_booking(result["booking_id"], 111)["ok"])

    def test_fixed_price_tariffs_without_composition(self):
        self.tariffs.values = [["Тариф", "Легковой", "Кроссовер", "Доступен"],
                               ["Экспресс", "700", "800", "да"],
                               ["Комплекс", "1500", "1800", "да"]]
        self.store._invalidate("catalog")
        quote = self.price("Кроссовер", "Экспресс", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 800)
        self.assertEqual(quote["lines"][0]["includes"], [])  # состав не выдуман
        missing = self.price("Легковой", "Комплекс", [])
        self.assertEqual(missing["total"], 1500)

    def test_fixed_price_conflicting_with_composition_is_exposed(self):
        # Воск в фикстуре недоступен: состав даёт 700*0.9 = 630, цена пакета 800 — конфликт.
        self.tariffs.values = [["Тариф", "Состав", "Скидка %", "Легковой", "Доступен"],
                               ["Базовый", "Мойка", "10", "800", "да"]]
        self.store._invalidate("catalog")
        quote = self.price("Легковой", "Базовый", [])
        self.assertFalse(quote["ok"], quote)
        self.assertIn("не совпадает с расчётом по составу", quote["error"])

    def test_fixed_price_equal_to_computation_is_accepted(self):
        self.tariffs.values = [["Тариф", "Состав", "Скидка %", "Легковой", "Доступен"],
                               ["Базовый", "Мойка", "0", "700", "да"]]
        self.store._invalidate("catalog")
        quote = self.price("Легковой", "Базовый", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 700)
        self.assertEqual(quote["lines"][0]["includes"], ["Мойка"])

    def test_malformed_fixed_price_blocks_the_tariff_read(self):
        self.tariffs.values = [["Тариф", "Легковой", "Доступен"], ["Экспресс", "по договорённости", "да"]]
        self.store._invalidate("catalog")
        with self.assertRaises(MappingError):
            self.price("Легковой", "Экспресс", [])

    def test_class_with_several_words_and_spacer_column(self):
        sheet = self.spreadsheet.worksheet("Услуги")
        values = [["Услуга", "", "Легковой", "Внедорожник/минивэн", "Доступна"],
                  ["Мойка", "", "700", "1000", "да"]]
        sheet.values = values
        self.store._invalidate("catalog")
        quote = self.price("Внедорожник/минивэн", None, ["Мойка"])
        self.assertEqual(quote["total"], 1000)


class ResourceLayoutTests(unittest.TestCase):
    """Ресурсы: суммарная вместимость, посты в графике, сетка — без смешения."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(sub(self.temp.name))
        self.slots = self.spreadsheet.worksheet("Слоты")

    def test_duplicate_capacity_rows_sum_up(self):
        self.slots.values = [["Дата", "Время", "Постов"],
                             ["01.01.2099", "10:00", "2"],
                             ["01.01.2099", "10:00", "2"]]
        self.store._invalidate("slots", "bookings")
        for number in range(4):
            result = self.store.create_booking(**booking(telegram_id=500 + number, car=f"Car {number}"))
            self.assertTrue(result["ok"], result)
        refused = self.store.create_booking(**booking(telegram_id=999, car="Extra"))
        self.assertFalse(refused["ok"])
        self.store._invalidate("slots", "bookings")
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], [])

    def test_zero_capacity_slot_is_never_offered(self):
        self.slots.values = [["Дата", "Время", "Постов"], ["01.01.2099", "10:00", "0"]]
        self.store._invalidate("slots", "bookings")
        self.assertEqual(self.store.free_slots("2099-01-01")["days"][0]["free_times"], [])
        self.assertFalse(self.store.create_booking(**booking())["ok"])

    def test_hours_mode_posts_come_from_the_table_not_assumptions(self):
        from tests.test_duration_schedule import duration_store
        store, _ = duration_store(self.temp.name, schedule=[["09:00", "19:00", "3"]])
        for number in range(3):
            result = store.create_booking(**booking(telegram_id=600 + number, car=f"Car {number}"))
            self.assertTrue(result["ok"], result)
        refused = store.create_booking(**booking(telegram_id=999, car="Extra"))
        self.assertFalse(refused["ok"])
        self.assertIn("нет свободного поста", refused["error"])

    def test_grid_and_capacity_are_distinguished_but_not_mixed(self):
        # Сетка боксов в датированном листе: свободные боксы уже учтены в клетках.
        self.slots.title = "01.01.2099"
        self.slots.values = [["Время", "Пост 1", "Пост 2"], ["10:00", "", ""]]
        self.store._invalidate("slots", "bookings")
        self.assertTrue(self.store.create_booking(**booking())["ok"])
        self.assertTrue(self.store.create_booking(**booking(telegram_id=222, car="B"))["ok"])
        self.store._invalidate("slots", "bookings")
        free = self.store.free_slots("2099-01-01")["days"][0]["free_times"]
        self.assertEqual(free, [])  # обе клетки заняты, повторного вычитания нет


class TariffBookingIntegrationTests(unittest.TestCase):
    """Тариф/услуга/класс меняются — предложение и запись используют одну проверку."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(sub(self.temp.name))

    def test_price_change_between_proposal_and_confirmation(self):
        quote = self.store.calculate_price("Легковой", "Базовый", [])
        expected = dict(quote, currency="₽")
        self.spreadsheet.worksheet("Услуги").values[1][2] = "900"
        self.store._invalidate("catalog")
        changed = self.store.create_booking(**booking(expected_quote=expected))
        self.assertFalse(changed["ok"])
        self.assertTrue(changed.get("quote_changed"), changed)
        self.assertEqual(changed["quote"]["total"], 810)

    def test_service_unavailable_kills_whole_tariff_safely(self):
        quote = self.store.calculate_price("Легковой", "Базовый", [])
        self.assertTrue(quote["ok"], quote)
        self.spreadsheet.worksheet("Услуги").values[1][4] = "нет"  # Мойка недоступна
        self.store._invalidate("catalog")
        refused = self.store.calculate_price("Легковой", "Базовый", [])
        self.assertFalse(refused["ok"])
        self.assertIn("все его позиции убраны", refused["error"])

    def test_class_change_keeps_class_rule_enforcement(self):
        from tests.fakes import Worksheet
        sheet = self.spreadsheet.worksheet("Услуги")
        sheet.values.append(["Химчистка", "Салон", "3000", "3500", "да"])
        classes = Worksheet(9, "Классы", [["Автомобиль", "Класс"], ["BMW X5", "Кроссовер"]])
        self.spreadsheet.sheets.append(classes)
        self.store._invalidate("catalog")
        enforced = self.store.create_booking(**booking(car="BMW X5", size_class="Легковой"))
        self.assertFalse(enforced["ok"])
        self.assertIn("класс «Кроссовер»", enforced["error"])
        fine = self.store.create_booking(**booking(car="BMW X5", size_class="Кроссовер",
                                                   services=["Химчистка"], tariff=None))
        self.assertTrue(fine["ok"], fine)
        self.assertEqual(fine["total"], 3500)


if __name__ == "__main__":
    unittest.main()

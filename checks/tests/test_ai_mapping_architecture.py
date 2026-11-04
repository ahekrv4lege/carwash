"""AI-first mapping: один вызов на структуру, повторное использование и 6-часовой контроль.

Модель полностью подменена записанным ответом: реальные ключи и сеть не нужны.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adaptive import MappingError, VERSION
from tests.fakes import Worksheet, booking, fixture

AI_CALLS: list[dict] = []


def workbook(plate: bool = False) -> dict:
    booking_columns = {
        "ID": "ID", "Создано": "Создано", "Дата": "Дата", "Время": "Время",
        "Имя": "Имя", "Телефон": "Телефон", "Автомобиль": "Автомобиль",
        "Класс": "Класс", "Тариф": "Тариф", "Доп. услуги": "Доп. услуги",
        "Состав": "Состав", "Сумма": "Сумма", "Комментарий": "Комментарий",
        "Telegram ID": "Telegram ID", "Username": "Username", "Статус": "Статус",
    }
    if plate:
        booking_columns["Госномер"] = "Гос. Номер"
    return {"regions": [
        {"role": "services", "sheet_title": "Услуги", "header_row": 1,
         "orientation": "matrix",
         "columns": {"Услуга": "Услуга", "Описание": "Описание", "Доступна": "Доступна"},
         "price_columns": {"Легковой": "Легковой", "Кроссовер": "Кроссовер"}},
        {"role": "tariffs", "sheet_title": "Тарифы", "header_row": 1,
         "columns": {"Тариф": "Тариф", "Состав": "Состав",
                     "Скидка %": "Скидка %", "Доступен": "Доступен"}},
        {"role": "slots", "sheet_title": "Слоты", "header_row": 1,
         "columns": {"Дата": "Дата", "Время": "Время", "Постов": "Постов"}},
        {"role": "bookings", "sheet_title": "Записи", "header_row": 1,
         "columns": booking_columns},
    ], "relationships": [], "issues": []}


class MappingArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "mapping.json")
        self.store, self.spreadsheet = fixture(self.temp.name)
        self.store.config = replace(self.store.config, sheets_mapping_file=self.path)
        self.store._adaptive.config = self.store.config
        self.store._adaptive.path = Path(self.path)
        AI_CALLS.clear()

    def fake_ai(self, document):
        def call(config, compact, hint=None):
            AI_CALLS.append(compact)
            return document
        return patch("schema_ai._request_mapping", side_effect=call)

    def build(self, document=None):
        with self.fake_ai(document or workbook()):
            return self.store.refresh_schema(force=True, explicit=True)

    def test_initial_ai_mapping_is_complete_and_persisted(self):
        document = self.build()
        self.assertEqual(len(AI_CALLS), 1)
        self.assertEqual(document["version"], VERSION)
        self.assertEqual(document["spreadsheet_id"], self.store.config.spreadsheet_id)
        self.assertEqual(document["validation_status"], "valid")
        self.assertTrue(document["writes_enabled"])
        booking_rule = next(rule for rule in document["regions"] if rule["role"] == "bookings")
        self.assertEqual(booking_rule["columns"]["Дата"], 3)
        self.assertIn("fingerprint", booking_rule)
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8"))["spreadsheet_id"],
                         self.store.config.spreadsheet_id)

    def test_restart_reuses_map_without_ai(self):
        self.build()
        AI_CALLS.clear()
        from sheets import Store
        second = Store(self.store.config)
        second._spreadsheet = self.spreadsheet
        document = second.refresh_schema(force=True)
        self.assertEqual(AI_CALLS, [])
        self.assertEqual(document["validation_status"], "valid")
        self.assertEqual(second.calculate_price("Легковой", "Базовый", [])["total"], 630)

    def test_normal_price_and_booking_changes_do_not_call_ai(self):
        self.build()
        AI_CALLS.clear()
        self.spreadsheet.worksheet("Услуги").values[1][2] = "900"  # цена
        self.store._invalidate("catalog")
        self.store.get_catalog(fresh=True)
        self.assertTrue(self.store.create_booking(**booking())["ok"])  # обычная запись
        self.store._adaptive.refresh(force=True)
        self.assertEqual(AI_CALLS, [])

    def test_structural_rename_triggers_one_remap(self):
        self.build()
        AI_CALLS.clear()
        self.spreadsheet.worksheet("Записи").values[0][3] = "Start time"
        with self.fake_ai(workbook()):
            document = self.store._adaptive.refresh(force=True)
        self.assertEqual(len(AI_CALLS), 1)
        self.assertTrue(any("Start time" in str(rule.get("last_changes")) for rule in document["regions"]
                            if rule["role"] == "bookings"))
        self.assertFalse(document["writes_enabled"])

    def test_six_hour_check_is_due_and_unchanged_structure_is_free(self):
        self.build()
        document = self.store._adaptive.document
        document["checked_at"] -= 6 * 3600 + 1
        self.store._adaptive._save()
        AI_CALLS.clear()
        with self.fake_ai(workbook()):
            again = self.store._adaptive.refresh()
        self.assertEqual(AI_CALLS, [])
        self.assertEqual(again["last_check"], "unchanged")

    def test_failed_mapping_blocks_writes_and_retries_only_explicitly(self):
        AI_CALLS.clear()
        with patch("schema_ai._request_mapping", side_effect=RuntimeError("provider down")), \
                patch.object(self.store._adaptive, "_salvage_missing_roles", return_value=None):
            document = self.store.refresh_schema(force=True, explicit=True)
        self.assertFalse(document["writes_enabled"])
        self.assertEqual(len(AI_CALLS), 0)
        with self.assertRaises(MappingError):
            self.store.create_booking(**booking())
        with patch("schema_ai._request_mapping", side_effect=RuntimeError("provider down")) as call, \
                patch.object(self.store._adaptive, "_salvage_missing_roles", return_value=None):
            self.store.refresh_schema(force=True)
            self.assertEqual(call.call_count, 0)  # тот же сбой не биллится повторно
        with self.fake_ai(workbook()):
            recovered = self.store.refresh_schema(force=True, explicit=True)
        self.assertEqual(len(AI_CALLS), 1)
        self.assertTrue(recovered["writes_enabled"])

    def test_plate_header_is_mapped_and_written(self):
        self.spreadsheet.worksheet("Записи").values[0].append("Гос. Номер")
        self.store.config = replace(self.store.config, car_plate_required=True)
        self.store._adaptive.config = self.store.config
        self.build(workbook(plate=True))
        result = self.store.create_booking(**booking(plate="а 123 ав 77"))
        self.assertTrue(result["ok"], result)
        sheet = self.spreadsheet.worksheet("Записи")
        self.assertEqual(sheet.values[1][16], "а 123 ав 77")
        self.assertEqual(self.store._adaptive.booking_fields() & {"Госномер"}, {"Госномер"})

    def test_ambiguous_required_table_blocks_writes(self):
        self.spreadsheet.sheets.append(Worksheet(9, "Ещё журнал", [
            ["ID", "Создано", "Дата", "Время", "Имя", "Телефон", "Автомобиль",
             "Класс", "Тариф", "Доп. услуги", "Состав", "Сумма", "Комментарий",
             "Telegram ID", "Username", "Статус"]]))
        document = self.build()
        self.assertEqual(document["validation_status"], "needs_review")
        self.assertFalse(document["writes_enabled"])
        self.assertIn("bookings", document["ambiguous_roles"])


if __name__ == "__main__":
    unittest.main()

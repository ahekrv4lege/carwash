"""Интерпретация структуры книги моделью: кандидаты сверяются с таблицей.

Модель здесь подменяется записанными ответами (детерминизм тестов); реальный
DeepSeek проверяется отдельно в live-прогоне. Проверяются: форма ответа,
привязка к фактическим шапкам, отказ от выдуманных колонок, кэш вызова,
поддержка фиксированных тарифов и графика работы через AI-раскладку.
"""
import tempfile
import unittest
from unittest.mock import patch

from adaptive import MappingError
from tests.fakes import fixture

AI_CALLS: list[dict] = []


def ai_response(regions, issues=None, relationships=None):
    return {"regions": regions, "issues": issues or [], "relationships": relationships or []}


class UnfamiliarWorkbookTests(unittest.TestCase):
    """Раскладки, которые алиасы не распознают: спасает интерпретация модели."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        # Лист, который алиасы не распознают: без модели роли services не будет.
        self.spreadsheet.worksheet("Услуги").values = [
            ["Работа", "Седан", "Кроссовер", "Выполняется"],
            ["Мойка кузова", "700", "800", "да"],
            ["Чернение резины", "200", "250", "да"]]
        self.calls = AI_CALLS
        AI_CALLS.clear()

    def patch_model(self, document):
        def fake_request(config, compact):
            self.calls.append(compact)
            return document
        return patch("schema_ai.request_mapping", side_effect=fake_request)

    def refresh(self, document=None):
        document = document or ai_response([])
        with self.patch_model(document):
            return self.store._adaptive.refresh(force=True)

    def test_matrix_with_unfamiliar_headers_is_understood(self):
        self.spreadsheet.worksheet("Услуги").values = [
            ["Наименование работы", "Примечание", "Стоимость седан", "Стоимость кроссовер", "Выполняется"],
            ["Мойка кузова", "Кузов", "700", "800", "да"],
            ["Чернение резины", "Резина", "200", "250", "да"]]
        self.refresh(ai_response([{"role": "services", "sheet_title": "Услуги", "header_row": 1,
                                   "columns": {"Услуга": "Наименование работы", "Легковой": "Стоимость седан",
                                               "Кроссовер": "Стоимость кроссовер", "Доступна": "Выполняется"},
                                   "notes": "матрица цен"}]))
        catalog = self.store.get_catalog(fresh=True)
        self.assertEqual(catalog.size_classes, ["Легковой", "Кроссовер"])
        self.assertEqual(catalog.services["мойка кузова"].prices, {"Легковой": 700, "Кроссовер": 800})
        self.assertEqual(self.store.calculate_price("Кроссовер", None, ["Мойка кузова"])["total"], 800)

    def test_claimed_header_that_does_not_exist_is_rejected(self):
        doc = self.refresh(ai_response([{"role": "services", "sheet_title": "Услуги", "header_row": 1,
                                         "columns": {"Услуга": "Работа", "Легковой": "Цена за седан",
                                                     "Доступна": "Выполняется"}}]))
        services = [rule for rule in doc["regions"] if rule["role"] == "services"]
        self.assertFalse(services and services[0]["validation"]["read"])
        self.assertTrue(any("не подтверждена" in issue for issue in doc["issues"]), doc["issues"])
        with self.assertRaises(MappingError):
            self.store.calculate_price("Легковой", None, ["Мойка кузова"])

    def test_fixed_price_tariffs_are_mapped_and_priced(self):
        self.spreadsheet.worksheet("Тарифы").values = [
            ["Название пакета", "Цена седан", "Цена кроссовер", "Активен"],
            ["Экспресс", "700", "800", "да"],
            ["Комплекс", "1500", "1800", "да"]]
        self.refresh(ai_response([
            {"role": "services", "sheet_title": "Услуги", "header_row": 1,
             "columns": {"Услуга": "Работа", "Легковой": "Седан", "Кроссовер": "Кроссовер", "Доступна": "Выполняется"}},
            {"role": "tariffs", "sheet_title": "Тарифы", "header_row": 1,
             "columns": {"Тариф": "Название пакета", "Легковой": "Цена седан", "Кроссовер": "Цена кроссовер",
                         "Доступен": "Активен"},
             "notes": "пакеты с фиксированной ценой без состава"}]))
        quote = self.store.calculate_price("Кроссовер", "Экспресс", [])
        self.assertTrue(quote["ok"], quote)
        self.assertEqual(quote["total"], 800)
        self.assertEqual(quote["lines"][0]["includes"], [])  # состав не выдуман
        combo = self.store.calculate_price("Легковой", "Комплекс", [])
        self.assertEqual(combo["total"], 1500)

    def test_hours_schedule_with_unfamiliar_headers_is_mapped(self):
        self.spreadsheet.worksheet("Слоты").values = [
            ["Открываемся", "Закрываемся", "Мастерых на смене"],
            ["09:00", "19:00", "2"]]
        self.refresh(ai_response([
            {"role": "slots", "sheet_title": "Слоты", "header_row": 1,
             "columns": {"Начало": "Открываемся", "Конец": "Закрываемся", "Постов": "Мастерых на смене"},
             "schedule_mode": "hours", "notes": "график работы смен"}]))
        slots = self.store._get_slots()
        self.assertEqual(slots.intervals(self.store.now().date()), ((540, 1140),))
        self.assertEqual(slots.posts(self.store.now().date()), 2)

    def test_model_issues_are_surfaced_but_do_not_block_deterministic_roles(self):
        from tests.fakes import Worksheet
        self.spreadsheet.sheets.append(Worksheet(9, "Заметки администратора", [["что-то"], ["ещё"]]))
        doc = self.refresh(ai_response([], issues=["Лист «Заметки администратора» не удалось интерпретировать"]))
        self.assertTrue(any("Заметки администратора" in issue for issue in doc["issues"]))
        # Детерминированные роли остались читаемыми; services без ответа модели — нет.
        self.assertTrue(next(rule for rule in doc["regions"] if rule["role"] == "bookings")["validation"]["read"])
        self.assertFalse(doc["writes_enabled"])

    def test_repeated_refresh_does_not_call_the_model_again(self):
        self.refresh(ai_response([{"role": "services", "sheet_title": "Услуги", "header_row": 1,
                                   "columns": {"Услуга": "Работа", "Легковой": "Седан",
                                               "Кроссовер": "Кроссовер", "Доступна": "Выполняется"}}]))
        self.assertEqual(len(self.calls), 1)
        self.store._adaptive.refresh(force=True)
        self.store._adaptive.refresh()
        self.assertEqual(len(self.calls), 1)  # структура не менялась — модель не нужна
        self.assertGreaterEqual(self.store._adaptive.ai_requests, 1)

    def test_ai_disabled_by_config_skips_the_model(self):
        from dataclasses import replace
        disabled = replace(self.store.config, sheets_ai_mapping_enabled=False)
        self.store.config = disabled
        self.store._adaptive.config = disabled
        doc = self.refresh(ai_response([{"role": "services", "sheet_title": "Услуги", "header_row": 1,
                                         "columns": {"Услуга": "Работа", "Легковой": "Седан",
                                                     "Кроссовер": "Кроссовер", "Доступна": "Выполняется"}}]))
        self.assertEqual(self.calls, [])
        self.assertFalse(any(rule["role"] == "services" for rule in doc["regions"]))

    def test_malformed_model_answer_is_rejected_safely(self):
        doc = self.refresh({"regions": [{"role": "wizard", "sheet_title": "Услуги", "columns": {"Услуга": "Работа"}}]})
        self.assertTrue(any("Неизвестная роль" in issue for issue in doc["issues"]))
        # Новый магазин: кэш структуры свой, модель отвечает снова — и снова ошибается.
        self.setUp()
        doc = self.refresh(ai_response([{"role": "services", "sheet_title": "Услуги", "columns": "не словарь"}]))
        self.assertTrue(any("некорректные колонки" in issue for issue in doc["issues"]))


class ResponseParsingTests(unittest.TestCase):
    def test_plain_fenced_and_embedded_json(self):
        from schema_ai import _extract_json
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(_extract_json('Вот результат: {"a": {"b": 2}} спасибо'), {"a": {"b": 2}})
        self.assertIsNone(_extract_json('никакого json'))
        self.assertIsNone(_extract_json('[1,2,3]'))

    def test_compact_workbook_shape_and_limits(self):
        from schema_ai import compact_workbook
        snapshot = {"title": "Лист", "values": [["a", "b"] * 20 for _ in range(30)],
                    "merges": [], "formulas": [(1, 1)]}
        compact = compact_workbook([snapshot])
        self.assertEqual(len(compact["sheets"]), 1)
        self.assertLessEqual(len(compact["sheets"][0]["sample_rows"]), 12)
        self.assertLessEqual(len(compact["sheets"][0]["sample_rows"][0]), 12)
        self.assertEqual(compact["sheets"][0]["formula_cells"], 1)

    def test_normalize_rejects_bad_roles_and_columns(self):
        from schema_ai import normalize_ai_document
        candidates, issues = normalize_ai_document({"regions": [
            {"role": "services", "sheet_title": "У", "columns": {"Услуга": "Услуга"}},
            {"role": "magic", "sheet_title": "У", "columns": {"Услуга": "Услуга"}},
            {"role": "services", "sheet_title": "У", "columns": {"Услуга": 5}},
        ]})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(issues), 2)


if __name__ == "__main__":
    unittest.main()

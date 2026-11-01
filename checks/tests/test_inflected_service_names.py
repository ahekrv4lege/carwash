"""Регрессия: услуга в именительном падеже и цитата клиента в косвенном падеже.

Найдено живым прогоном: «Хочу записаться на мойку кузова» — модель выдала значение
«мойка кузова» и цитату «мойку кузова»; подстрока не совпадала, факт отбрасывался,
и бот снова спрашивал, какую мойку выбрать. Синтетические данные.
"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from conversation import inflection_grounded, validate_patch

NOW = datetime(2026, 11, 1, 21, 0, tzinfo=ZoneInfo("Europe/Moscow"))
TEXT = "Хочу записаться на мойку кузова на завтра в 11:00"


class InflectionTests(unittest.TestCase):
    def test_declined_forms_are_grounded(self):
        self.assertTrue(inflection_grounded("мойка кузова", "записаться на мойку кузова"))
        self.assertTrue(inflection_grounded("Мойка кузова", "мойку кузова"))

    def test_different_services_are_not_grounded(self):
        self.assertFalse(inflection_grounded("полировка кузова", "мойку кузова"))
        self.assertFalse(inflection_grounded("химчистка салона", "мойку кузова"))

    def test_short_words_must_match_exactly(self):
        self.assertFalse(inflection_grounded("воск", "воски"))  # короткие слова без смягчения
        self.assertTrue(inflection_grounded("воск", "воск"))


class RequestedServicePatchTests(unittest.TestCase):
    def test_nominative_value_with_declined_quote_is_kept(self):
        result = validate_patch({"requested_service": ["мойка кузова", "мойку кузова"]}, TEXT, NOW)
        self.assertEqual(result, {"requested_service": "мойка кузова"})

    def test_value_not_in_customer_text_is_still_rejected(self):
        with self.assertRaises(ValueError):
            validate_patch({"requested_service": ["полировка фар", "полировку фар"]}, TEXT, NOW)


if __name__ == "__main__":
    unittest.main()

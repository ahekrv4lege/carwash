"""Регрессия: salvage-режим терял car_model и size_class.

Найдено живым прогоном и многотановым тестом: модель отвечает respond'ом
(факты без мутаций — главный путь сохранения), patch валидируется по полям
изолированно; у car_model/size_class grounding пересчитывался на пустом
одиночном patch и факт «машина + класс» выбрасывался. Клиент получал
повторный вопрос «Какой у вас автомобиль?». Синтетические данные.
"""
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from conversation import salvage_patch, validate_patch
from tests.fakes import fixture

CATALOG = fixture(tempfile.mkdtemp())[0].get_catalog()

NOW = datetime(2026, 11, 1, 21, 0, tzinfo=ZoneInfo("Europe/Moscow"))
TEXT = "Здравствуйте! Меня зовут Тест, машина Toyota Camry 3.5."
PATCH = {"client_name": ["Тест", "Тест"],
         "car": ["Toyota Camry 3.5", "Toyota Camry 3.5"],
         "car_model": ["Toyota Camry", "Toyota Camry 3.5"],
         "size_class": ["Легковой", "Toyota Camry 3.5"]}


class SalvageClassGroundingTests(unittest.TestCase):
    def test_pure_respond_keeps_car_model_and_class(self):
        result = salvage_patch(dict(PATCH), TEXT, NOW, CATALOG)
        self.assertEqual(result.get("car"), "Toyota Camry 3.5")
        self.assertEqual(result.get("car_model"), "Toyota Camry")
        self.assertEqual(result.get("size_class"), "Легковой")

    def test_failing_neighbor_field_does_not_drop_the_class(self):
        patch = dict(PATCH, phone=["не-телефон", "не-телефон"])  # соседнее поле ломается
        result = salvage_patch(patch, TEXT, NOW, CATALOG)
        self.assertEqual(result.get("car_model"), "Toyota Camry")
        self.assertEqual(result.get("size_class"), "Легковой")
        self.assertNotIn("phone", result)

    def test_full_validation_still_requires_car_grounding(self):
        with self.assertRaises(ValueError):
            validate_patch({"car_model": ["Toyota Camry", "просто камри"]},
                           "Здравствуйте, нужна мойка.", NOW, CATALOG)


if __name__ == "__main__":
    unittest.main()

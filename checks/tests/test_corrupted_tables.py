"""Случайно испорченные таблицы: пустые поля, дубли, нестандартные заголовки,
формулы и неверные цены (задача №4 из TODO.md).

Каждый тест запускает один сценарий из corruption_cases: портит синтетическую
таблицу и проверяет, что Store отвечает штатно — внятным отказом
(MappingError), корректным расчётом или честным «услуга недоступна», но никак
не чужим исключением и не молчаливо принятым мусором.
"""
import unittest

from adaptive import MappingError
from tests.corruption_cases import SCENARIOS, run_scenario


def make_test(scenario):
    def test(self):
        outcome = run_scenario(scenario)
        if scenario.expect == "mapping-error":
            self.assertEqual(outcome["kind"], "mapping-error",
                             f"{scenario.id}: ожидали отказ MappingError, пришло: {outcome}")
            if scenario.expect_message:
                self.assertIn(scenario.expect_message, outcome["message"],
                              f"{scenario.id}: ждём внятный текст ошибки, пришло: {outcome['message']}")
        elif scenario.expect == "ok-or-mapping-error":
            self.assertIn(outcome["kind"], ("ok", "mapping-error"),
                          f"{scenario.id}: чужой исход, пришло: {outcome}")
        else:
            self.assertEqual(outcome["kind"], scenario.expect,
                             f"{scenario.id}: ожидали «{scenario.expect}», пришло: {outcome}")
            if scenario.verify:
                scenario.verify(outcome["result"])
    test.__name__ = f"test_{scenario.id}"
    test.__doc__ = f"{scenario.title} ({scenario.id})"
    return test


class CorruptedTablesTests(unittest.TestCase):
    """Все сценарии порчи генерируются из общего описания corruption_cases."""


for scenario in SCENARIOS:
    setattr(CorruptedTablesTests, make_test(scenario).__name__, make_test(scenario))


if __name__ == "__main__":
    unittest.main()

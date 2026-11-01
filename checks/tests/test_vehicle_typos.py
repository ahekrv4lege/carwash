"""Опечатки и разговорные названия в известных неоднозначных моделях.

Список неоднозначных моделей тот же, что в ai.vehicle_clarification; меняется только
распознавание написаний. Ошибка в сторону вопроса безопасна, ошибка в сторону
молчания при неоднозначной модели влияет на класс и цену.
"""
import unittest

from ai import vehicle_clarification

COROLLA = "Corolla / Corolla Cross"
YARIS = "Yaris / Yaris Cross"


class AmbiguousTypoTests(unittest.TestCase):
    def assertAsksAbout(self, text, options):
        question = vehicle_clarification(text)
        self.assertIsNotNone(question, text)
        self.assertIn(options, question)

    def test_toyota_brand_typos_still_trigger_corolla_question(self):
        for text in ("Toyota Corolla 1.6", "тойота королла 1.6", "таета королла 1.6",
                     "тайота королла 1.6", "таёта корола 1.8", "тоета королла 1.6"):
            with self.subTest(text=text):
                self.assertAsksAbout(text, COROLLA)

    def test_corolla_misspellings_without_brand_trigger_question(self):
        for text in ("карола 1.6", "королла, мойка", "корола на завтра", "corola 1.6"):
            with self.subTest(text=text):
                self.assertAsksAbout(text, COROLLA)

    def test_yaris_typo_without_brand_triggers_question(self):
        self.assertAsksAbout("ярис 1.5", YARIS)

    def test_mercedes_short_brand_typos(self):
        self.assertAsksAbout("мерс с 200", "C-Class / GLC")
        self.assertAsksAbout("мерседс е 220", "E-Class / GLE")

    def test_bmw_brand_typos(self):
        self.assertAsksAbout("бэмв 5", "5 Series / X5")
        self.assertAsksAbout("бемв 3", "3 Series / X3")
        self.assertIsNone(vehicle_clarification("бемв 3 серия"))  # серия названа явно

    def test_explicit_model_resolves_ambiguity(self):
        for text in ("Toyota Corolla Cross 2.0", "тайота королла кросс", "карола кросс 1.8",
                     "бмв x5 2020", "бэмв х5 дизель", "мерс glc 300"):
            with self.subTest(text=text):
                self.assertIsNone(vehicle_clarification(text))

    def test_unambiguous_models_do_not_ask(self):
        for text in ("тайота камри 3.5", "камрюха 3.5", "хендай солярис", "киа рио", "лада веста"):
            with self.subTest(text=text):
                self.assertIsNone(vehicle_clarification(text))

    def test_hypothetical_wording_does_not_ask(self):
        self.assertIsNone(vehicle_clarification("если королла, то сколько?"))
        self.assertIsNone(vehicle_clarification("или карола, не знаю"))


if __name__ == "__main__":
    unittest.main()

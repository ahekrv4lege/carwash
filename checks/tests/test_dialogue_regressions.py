"""Регрессии диалога «мне на беху полировку» и общих проблем понимания.

Воспроизведённый баг (2026-11): на «пятая» бот молча признавал BMW 5 Series,
выдумывал подвид услуги («Полировка фар» вместо общей «полировки») с ценой и
показывал кнопки выбора дня до выбора услуги. Проверяются общие правила,
а не отдельные фразы: неоднозначная модель, неоднозначная услуга, преждевременные
кнопки и потеря выбора клиента из-за строгой привязки к цитате.
"""
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser, vehicle_clarification, vehicle_family
from conversation import (distinguishing_word_grounded, inflection_grounded, stem_variants,
                          validate_patch, word_matches)
from tests.fakes import fixture
from tests.test_conversation import response


def polishing_store(folder):
    """Синтетическая мойка с двумя полировками: общее слово «полировка» неоднозначно."""
    store, spreadsheet = fixture(folder)
    spreadsheet.sheets[0].values = [
        ["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
        ["Мойка", "Кузов", "700", "800", "да"],
        ["Полировка фар", "Фары", "800", "900", "да"],
        ["Полировка кузова", "Кузов", "5000", "6000", "да"],
    ]
    day = store.now().date() + timedelta(days=2)
    spreadsheet.sheets[2].values = [["Дата", "Время", "Постов"],
                                    [f"{day:%d.%m.%Y}", "10:00", "2"],
                                    [f"{day:%d.%m.%Y}", "11:00", "2"],
                                    [f"{day:%d.%m.%Y}", "17:00", "2"]]
    store._invalidate("services", "tariffs")
    return store, spreadsheet, day


class DialogueAgentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet, self.day = polishing_store(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.transport = AsyncMock()
        self.agent.client.chat.completions.create = self.transport
        self.user = ChatUser(3201, None, "BMW")

    async def say(self, text, *replies):
        self.transport.side_effect = list(replies) + ([replies[-1]] * 6 if replies else [])
        answer = await self.agent.reply(self.user, text)
        self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id, answer))
        return answer

    def facts(self):
        return self.agent._state[self.user.id].get("facts", {})


class ReportedBmwDialogueTests(DialogueAgentTestCase):
    """Полный сценарий из сообщения об ошибке: «мне на беху полировку» -> «пятая»."""

    async def test_bmw_ordinal_answer_clarifies_instead_of_assuming(self):
        # 1) «мне на беху полировку»: модель спрашивает, какая БМВ.
        answer = await self.say("мне на беху полировку",
                                response(calls=[("respond", {"reply": "Какая у вас модель БМВ?",
                                                              "facts": {"car": ["беху", "беху"],
                                                                        "requested_service": ["полировка", "полировку"]},
                                                              "tentative": {}})]))
        self.assertIn("БМВ", answer)
        self.assertEqual(self.facts().get("car"), "беху")
        self.assertEqual(self.facts().get("requested_service"), "полировка")
        # 2) «пятая»: неоднозначный ответ, Python уточняет семейство, а не угадывает.
        answer = await self.say("пятая",
                                response(calls=[("respond", {"reply": "",
                                                             "facts": {"car": ["пятая", "пятая"],
                                                                       "car_model": ["BMW 5 Series", "пятая"]},
                                                             "tentative": {}})]),
                                response(calls=[("respond", {"reply": "Полировка фар для BMW 5 Series — 800 ₽. "
                                                                    "На какой день и время вас записать?",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIn("BMW 5 Series / X5 / i5 / M5", answer)
        self.assertIsNone(self.facts().get("car_model"), "модель не должна выбираться молча")
        self.assertNotIn("800", answer, "цена несуществующего выбора не называется")
        self.assertIsNone(await self.agent.slot_keyboard(self.user),
                          "кнопки дня преждевременны: услуга не выбрана")
        self.assertTrue(self.agent._state[self.user.id].get("vehicle_ambiguous"))

    async def test_repeated_ordinal_answer_asks_again(self):
        await self.say("мне на беху полировку",
                       response(calls=[("respond", {"reply": "Какая у вас модель БМВ?",
                                                     "facts": {"car": ["беху", "беху"],
                                                               "requested_service": ["полировка", "полировку"]},
                                                     "tentative": {}})]))
        first = await self.say("пятая", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        second = await self.say("пятая", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        # Повтор не снимает неоднозначность: вопрос задаётся снова.
        self.assertIn("BMW 5 Series / X5 / i5 / M5", first)
        self.assertIn("BMW 5 Series / X5 / i5 / M5", second)

    async def test_customer_complaint_about_missing_question_still_clarifies(self):
        await self.say("мне на беху полировку",
                       response(calls=[("respond", {"reply": "Какая у вас модель БМВ?",
                                                     "facts": {"car": ["беху", "беху"],
                                                               "requested_service": ["полировка", "полировку"]},
                                                     "tentative": {}})]))
        await self.say("пятая", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        # «стоп, а где вопрос мол какая БМВ 5?» — слово «БМВ 5» само требует уточнения.
        answer = await self.say("стоп, а где вопрос мол какая БМВ 5?",
                                response(calls=[("respond", {"reply": "Секунду, уточню.", "facts": {}, "tentative": {}})]))
        self.assertIn("BMW 5 Series / X5 / i5 / M5", answer)

    async def test_specific_answer_resolves_and_booking_proceeds(self):
        await self.say("мне на беху полировку",
                       response(calls=[("respond", {"reply": "Какая у вас модель БМВ?",
                                                     "facts": {"car": ["беху", "беху"],
                                                               "requested_service": ["полировка", "полировку"]},
                                                     "tentative": {}})]))
        await self.say("пятая", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        # «икс пятый» — различающая часть названа: модель сохраняется.
        await self.say("икс пятый",
                       response(calls=[("respond", {"reply": "",
                                                    "facts": {"car": ["икс пятый", "икс пятый"],
                                                              "car_model": ["BMW X5", "икс пятый"],
                                                              "size_class": ["Кроссовер", "икс пятый"]},
                                                    "tentative": {}})]),
                       response(calls=[("respond", {"reply": "Принято.", "facts": {}, "tentative": {}})]))
        self.assertEqual(self.facts().get("car_model"), "BMW X5")
        self.assertEqual(self.facts().get("size_class"), "Кроссовер")
        # Общая «полировка» всё ещё неоднозначна: варианты предлагаются, а не выбираются.
        answer = await self.say("ну полировка",
                                response(calls=[("get_price_list", {})]),
                                response(calls=[("respond", {"reply": "Полировка кузова для BMW X5 — 6000 ₽. "
                                                                    "На какой день и время вас записать?",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIn("Полировка фар", answer)
        self.assertIn("Полировка кузова", answer)
        self.assertNotIn("6000", answer)
        # Точный выбор клиента: различающее слово «фары» обосновывает вариант.
        answer = await self.say("полировку фар",
                                response(calls=[("respond", {"reply": "",
                                                             "facts": {"requested_service": ["полировка фар", "полировку фар"],
                                                                       "services": [["Полировка фар"], "полировку фар"]},
                                                             "tentative": {}}),
                                                ("get_price_list", {})]),
                                response(calls=[("respond", {"reply": "Полировка фар для BMW X5 — 900 ₽. "
                                                                    "На какой день и время вас записать?",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIn("900", answer)
        self.assertEqual(self.facts().get("services"), ["Полировка фар"])
        # Состав известен: кнопки выбора дня появляются вовремя.
        self.assertIsNotNone(await self.agent.slot_keyboard(self.user))


class InventedServiceChoiceTests(DialogueAgentTestCase):
    """Модель не выбирает за клиента один из нескольких подходящих вариантов."""

    async def test_model_choice_with_price_is_replaced_by_options(self):
        await self.say("сколько стоит полировка?",
                       response(calls=[("get_price_list", {})]),
                       response(calls=[("respond", {"reply": "Полировка фар — 800 ₽.",
                                                     "facts": {"requested_service": ["полировка", "полировка"]},
                                                     "tentative": {}})]))
        answer = await self.agent.reply(self.user, "хочу полировку")
        self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id, answer))
        self.assertIn("Полировка фар", answer)
        self.assertIn("Полировка кузова", answer)
        self.assertNotIn("800", answer)
        self.assertIsNone(self.facts().get("services"))

    async def test_full_variant_list_with_prices_passes(self):
        answer = await self.say("сколько выходит полировка?",
                                response(calls=[("get_price_list", {})]),
                                response(calls=[("respond", {
                                    "reply": "Для легкового:\n- Полировка фар — 800 ₽\n- Полировка кузова — 5000 ₽",
                                    "facts": {"requested_service": ["полировка", "полировка"]}, "tentative": {}})]))
        self.assertIn("800", answer)
        self.assertIn("5000", answer)

    async def test_question_mentioning_variant_is_not_replaced(self):
        answer = await self.say("хочу полировку",
                                response(calls=[("get_price_list", {})]),
                                response(calls=[("respond", {"reply": "Вам полировку фар или кузова?",
                                                             "facts": {"requested_service": ["полировка", "полировку"]},
                                                             "tentative": {}})]))
        self.assertIn("или", answer)

    async def test_specific_request_is_not_blocked(self):
        answer = await self.say("сколько стоит полировка фар?",
                                response(calls=[("get_price_list", {})]),
                                response(calls=[("respond", {"reply": "Полировка фар — 800 ₽ для легкового.",
                                                             "facts": {"requested_service": ["полировка фар", "полировка фар"]},
                                                             "tentative": {}})]))
        self.assertIn("800", answer)


class ServiceOptionsAnswerTests(DialogueAgentTestCase):
    """Ответ на предложенные варианты: различающее слово, номер, «все»."""

    async def _offer_options(self):
        await self.say("сколько стоит полировка?",
                       response(calls=[("get_price_list", {})]),
                       response(calls=[("respond", {"reply": "Полировка фар — 800 ₽.",
                                                     "facts": {"requested_service": ["полировка", "полировка"]},
                                                     "tentative": {}})]))

    async def test_distinguishing_word_selects_variant(self):
        await self._offer_options()
        answer = await self.say("фары",
                                response(calls=[("respond", {"reply": "Полировка фар — 800 ₽.",
                                                             "facts": {}, "tentative": {}})]))
        self.assertEqual(self.facts().get("services"), ["Полировка фар"])

    async def test_ordinal_number_selects_variant(self):
        await self._offer_options()
        await self.say("вторую", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        self.assertEqual(self.facts().get("services"), ["Полировка кузова"])

    async def test_generic_repeat_does_not_select(self):
        await self._offer_options()
        await self.say("полировку", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        self.assertIsNone(self.facts().get("services"))

    async def test_all_choice_selects_every_variant(self):
        await self._offer_options()
        await self.say("всё", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        self.assertEqual(self.facts().get("services"), ["Полировка фар", "Полировка кузова"])

    async def test_other_service_named_is_left_to_model(self):
        await self._offer_options()
        await self.say("мойку", response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        self.assertIsNone(self.facts().get("services"))


class SlotMenuReadinessTests(DialogueAgentTestCase):
    """Кнопки выбора дня появляются, когда состав записи уже известен."""

    async def test_date_menu_suppressed_without_service(self):
        answer = await self.say("запишите меня",
                                response(calls=[("respond", {"reply": "На какой день и время вас записать?",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIn("день", answer)
        self.assertIsNone(await self.agent.slot_keyboard(self.user))

    async def test_date_menu_shown_with_service(self):
        await self.say("беха x5, полировку фар",
                       response(calls=[("respond", {"reply": "",
                                                    "facts": {"car": ["беха x5", "беха x5"],
                                                              "car_model": ["BMW X5", "беха x5"],
                                                              "size_class": ["Кроссовер", "беха x5"],
                                                              "requested_service": ["полировка фар", "полировку фар"],
                                                              "services": [["Полировка фар"], "полировку фар"]},
                                                    "tentative": {}})]),
                       response(calls=[("respond", {"reply": "", "facts": {}, "tentative": {}})]))
        answer = await self.say("а когда можно?",
                                response(calls=[("respond", {"reply": "На какой день вас записать?",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIsNotNone(await self.agent.slot_keyboard(self.user))

    async def test_availability_question_keeps_menu_without_service(self):
        answer = await self.say("что у вас свободно на этой неделе?",
                                response(calls=[("respond", {"reply": "На какой день вас записать?",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIsNotNone(await self.agent.slot_keyboard(self.user))


class VehicleClarificationContextTests(unittest.TestCase):
    """Короткий ответ номером модели проверяется по марке из разговора."""

    def test_ordinal_answer_with_known_brand_asks(self):
        for text in ("пятая", "пятая.", "ну пятая", "5", "пятёрка", "пятую"):
            with self.subTest(text=text):
                question = vehicle_clarification(text, "беху BMW", True)
                self.assertIsNotNone(question, text)
                self.assertIn("BMW 5 Series / X5 / i5 / M5", question)

    def test_no_vehicle_question_pending_does_not_ask(self):
        self.assertIsNone(vehicle_clarification("пятая", "беху BMW", False))

    def test_no_known_brand_does_not_ask(self):
        self.assertIsNone(vehicle_clarification("пятая", "", True))

    def test_specific_designators_do_not_ask(self):
        for text in ("икс пятый", "х5", "м5", "ай пятая", "эм пятая", "пятая серия", "5 серия"):
            with self.subTest(text=text):
                self.assertIsNone(vehicle_clarification(text, "беху BMW", True))

    def test_other_brands_use_their_families(self):
        question = vehicle_clarification("шестая", "моя ауди", True)
        self.assertIn("A6 / Q6", question)
        question = vehicle_clarification("тройка", "мазда", True)
        self.assertIn("Mazda3 / CX-3", question)

    def test_date_like_answers_do_not_ask(self):
        for text in ("пятого", "пятое", "1.6", "2.5"):
            with self.subTest(text=text):
                self.assertIsNone(vehicle_clarification(text, "беху BMW", True))

    def test_long_answers_are_left_to_model(self):
        self.assertIsNone(vehicle_clarification("пятая, но только если утром", "беху BMW", True))

    def test_same_message_inflected_brand_asks(self):
        for text in ("мне на беху пятую полировку", "пятая бэха", "мазду третью помыть"):
            with self.subTest(text=text):
                self.assertIsNotNone(vehicle_clarification(text))

    def test_family_detection(self):
        self.assertEqual(vehicle_family("у меня беху"), "BMW")
        self.assertEqual(vehicle_family("BMW X5"), "BMW")
        self.assertEqual(vehicle_family("ауди"), "Audi")
        self.assertIsNone(vehicle_family("камри"))
        self.assertIsNone(vehicle_family("пятая"))


class _CatalogItem:
    def __init__(self, name):
        self.name = name


class _PolishingCatalog:
    services = {name: _CatalogItem(name) for name in ("Мойка", "Полировка фар", "Полировка кузова")}
    tariffs = {}
    size_classes = ["Легковой", "Кроссовер"]


class CatalogGroundingTests(unittest.TestCase):
    """Общие слова клиента не превращаются молча в конкретный вариант каталога."""

    Catalog = _PolishingCatalog
    def test_stem_variants(self):
        names = ["Мойка", "Полировка фар", "Полировка кузова", "Воск"]
        self.assertEqual(stem_variants("полировка", names), ["Полировка фар", "Полировка кузова"])
        self.assertEqual(stem_variants("мойку", names), [])
        self.assertEqual(stem_variants("мойку кузова", names), [])

    def test_word_matches(self):
        self.assertTrue(word_matches("фар", "фары"))
        self.assertTrue(word_matches("мойка", "мойку"))
        self.assertFalse(word_matches("в", "вода"))
        self.assertFalse(word_matches("фар", "кузова"))

    def test_distinguishing_word_grounded(self):
        self.assertTrue(distinguishing_word_grounded("Полировка фар", "полировка", "только фары"))
        self.assertFalse(distinguishing_word_grounded("Полировка кузова", "полировка", "только фары"))
        self.assertFalse(distinguishing_word_grounded("Полировка фар", "", "фары"))

    def test_short_words_still_strict_in_inflection_grounded(self):
        self.assertFalse(inflection_grounded("воск", "воски"))
        self.assertTrue(inflection_grounded("воск", "воск"))

    def test_ambiguous_choice_rejected_without_distinguishing_word(self):
        known = {"requested_service": "полировка"}
        with self.assertRaises(ValueError):
            validate_patch({"services": [["Полировка фар"], "полировку"]}, "полировку",
                            None, self.Catalog(), known=known)

    def test_distinguishing_word_accepts_choice(self):
        known = {"requested_service": "полировка"}
        result = validate_patch({"services": [["Полировка фар"], "фары"]}, "фары",
                                None, self.Catalog(), known=known)
        self.assertEqual(result, {"services": ["Полировка фар"]})

    def test_full_quote_accepts_choice(self):
        known = {"requested_service": "полировка"}
        result = validate_patch({"services": [["Полировка фар"], "полировку фар"]}, "полировку фар",
                                None, self.Catalog(), known=known)
        self.assertEqual(result, {"services": ["Полировка фар"]})

    def test_unique_exact_name_still_accepted(self):
        result = validate_patch({"services": [["Мойка"], "мойку"]}, "хочу мойку",
                                None, self.Catalog(), known={})
        self.assertEqual(result, {"services": ["Мойка"]})


class AutoServiceMappingTests(DialogueAgentTestCase):
    """Авто-предложение: общие слова клиента не превращаются в выбор за него."""

    def _preset_booking_facts(self, requested):
        """Все данные записи, кроме тарифа/услуг; каталог уже прочитан."""
        self.agent._state[self.user.id]["catalog"] = {
            "checked_at": self.store.now().isoformat(timespec="seconds"),
            "size_classes": ["Легковой", "Кроссовер"], "class_rules": [],
            "tariffs": ["Базовый"],
            "services": [row[0] for row in self.spreadsheet.sheets[0].values[1:]],
            "unavailable_services": []}
        self.agent._state[self.user.id].setdefault("facts", {}).update({
            "car": "икс пятый", "car_model": "BMW X5", "size_class": "Кроссовер",
            "client_name": "Тест", "phone": "+7 900 111-22-33",
            "plate": "а 123 ав 777",
            "date": f"{self.day:%Y-%m-%d}", "time": "10:00",
            "requested_service": requested})

    async def test_exact_name_wins_over_narrower_variant(self):
        # «мойку» при «Мойка» и «Мойка двигателя» выбирает точное «Мойка».
        self.spreadsheet.sheets[0].values = [
            ["Услуга", "Описание", "Легковой", "Кроссовер", "Доступна"],
            ["Мойка", "Кузов", "700", "800", "да"],
            ["Мойка двигателя", "Двигатель", "900", "1000", "да"],
        ]
        self.store._invalidate("services", "tariffs")
        self._preset_booking_facts("мойку")
        await self.say("запишите меня",
                       response(calls=[("respond", {"reply": "Готовлю запись.", "facts": {}, "tentative": {}})]))
        self.assertEqual(self.facts().get("services"), ["Мойка"],
                         "точное название важнее узкого варианта")
        self.assertIsNotNone(self.agent._pending.get(self.user.id), "сводка должна быть показана")

    async def test_ambiguous_generic_word_asks_for_variant(self):
        # «полировку» при двух полировках: вариант спрашивается, не выбирается.
        self._preset_booking_facts("полировку")
        answer = await self.say("запишите меня",
                                response(calls=[("respond", {"reply": "Готовлю запись.", "facts": {}, "tentative": {}})]))
        self.assertIn("Полировка фар", answer)
        self.assertIn("Полировка кузова", answer)
        self.assertIsNone(self.facts().get("services"))


class LongChaoticDialogTests(DialogueAgentTestCase):
    """Длинный диалог (30+ реплик): правки, смена машины и услуги, болтовня, повторы.

    Проверяется итоговое состояние агента и таблицы, а не тексты ответов.
    """

    async def test_full_chaotic_dialog_keeps_state_consistent(self):
        facts_respond = lambda fields: response(calls=[("respond", {"reply": "", "facts": fields, "tentative": {}})])
        empty = response(calls=[("respond", {"reply": "Хорошо.", "facts": {}, "tentative": {}})])

        await self.say("привет", empty)
        # Неоднозначная модель: уточнение вместо догадки.
        await self.say("у меня беху пятую, хочу полировку",
                       facts_respond({"car": ["беху пятую", "беху пятую"],
                                      "requested_service": ["полировка", "полировку"]}))
        answer = await self.say("пятая", empty)
        self.assertIn("BMW 5 Series / X5 / i5 / M5", answer)
        # Клиент настаивает, потом называет различающую часть.
        await self.say("да, именно она", empty)
        await self.say("икс пятый, если точнее",
                       facts_respond({"car": ["икс пятый", "икс пятый"],
                                      "car_model": ["BMW X5", "икс пятый"],
                                      "size_class": ["Кроссовер", "икс пятый"]}))
        self.assertEqual(self.facts().get("car_model"), "BMW X5")
        # Неоднозначная услуга: варианты, затем выбор различающим словом.
        answer = await self.say("полировку",
                                response(calls=[("get_price_list", {})]),
                                response(calls=[("respond", {"reply": "Полировка фар — 900 ₽.",
                                                             "facts": {}, "tentative": {}})]))
        self.assertIn("Полировка фар", answer)
        await self.say("фары", empty)
        self.assertEqual(self.facts().get("services"), ["Полировка фар"])
        # Смена услуги по ходу: старая заменяется, цена не теряется.
        await self.say("а давайте вместо этого просто мойку",
                       facts_respond({"requested_service": ["мойка", "просто мойку"]}))
        # apply_patch: новое requested_service без услуг обнуляет прежний выбор.
        self.assertIsNone(self.facts().get("services"))
        # Болтовня не портит факты.
        for topic in ("какая погода?", "вы живая?", "понятно", "а адрес скажите", "спасибо"):
            await self.say(topic, empty)
        self.assertEqual(self.facts().get("car_model"), "BMW X5")
        self.assertEqual(self.facts().get("requested_service"), "мойка")
        # Запись: дата со сменой, неопределённое время, вечер, контакты.
        await self.say("запишите на завтра", empty)
        await self.say("нет, лучше послезавтра",
                       facts_respond({"date": [f"{self.day:%Y-%m-%d}", "послезавтра"]}))
        await self.say("давайте на 5", empty)
        answer = await self.say("вечером", empty)
        facts = self.facts()
        self.assertEqual(facts.get("time"), "17:00", "«на 5 вечером» - это 17:00")
        await self.say("зовут Тест, телефон +7 900 111-22-33, госномер а 123 ав 777",
                       facts_respond({"client_name": ["Тест", "Тест"],
                                      "phone": ["+7 900 111-22-33", "+7 900 111-22-33"],
                                      "plate": ["а 123 ав 777", "а 123 ав 777"]}))
        facts = self.facts()
        self.assertEqual(facts.get("client_name"), "Тест")
        self.assertIn("phone", facts)
        self.assertTrue(facts.get("date") and facts.get("time"),
                        f"дата и время должны быть сохранены: {facts}")
        if not facts.get("services") and not facts.get("tariff"):
            # Модель сохраняет выбор: «Мойка» - единственное точное совпадение.
            await self.say("ну мойку", facts_respond({"services": [["Мойка"], "мойку"]}))
            facts = self.facts()
            self.assertEqual(facts.get("services"), ["Мойка"])
        pending = self.agent._pending.get(self.user.id)
        if pending is None:
            # Модель оформляет запись: Python проверяет и показывает сводку.
            await self.say("оформляй", response(calls=[("create_booking", {})]))
            pending = self.agent._pending.get(self.user.id)
        self.assertIsNotNone(pending, "сводка должна быть показана")
        confirmed = await self.agent.proposal_action(self.user, pending["offer_id"], "confirm")
        self.agent.mark_delivered(self.user.id, self.agent.delivery_token(self.user.id, confirmed))
        bookings = self.store.my_bookings(self.user.id)["bookings"]
        self.assertEqual(len(bookings), 1)
        # Повторное нажатие старой кнопки не создаёт вторую запись.
        duplicate = await self.agent.proposal_action(self.user, pending["offer_id"], "confirm")
        self.assertEqual(len(self.store.my_bookings(self.user.id)["bookings"]), 1)
        # Правка после записи: перенос времени.
        await self.say("перенесите на другое время",
                       response(calls=[("reschedule_booking", {"booking_id": bookings[0]["booking_id"],
                                                               "date": bookings[0]["date"],
                                                               "time": "10:00"})]),
                       response(calls=[("respond", {"reply": "Перенесу.", "facts": {}, "tentative": {}})]))
        self.assertIn("reschedule", str(self.agent._pending.get(self.user.id, {}).get("action", "")))


if __name__ == "__main__":
    unittest.main()

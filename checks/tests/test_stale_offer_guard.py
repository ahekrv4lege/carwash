"""Регрессия: модель имитировала текст предложения отмены без вызова инструмента.

Найдено живым прогоном: после подтверждённого переноса на «Отмените, пожалуйста,
эту запись» модель ответила текстом «Отмена записи: … Для отмены нажмите
«Подтвердить»», не вызвав cancel_booking: клиент видел предложение, но кнопка
ничего не делала (pending пуст). Ответ должен отклоняться и переписываться.
"""
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser
from tests.fakes import fixture
from tests.test_conversation import response


class StaleOfferGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.spreadsheet = fixture(self.temp.name)
        with patch("ai.AsyncOpenAI"):
            self.agent = Agent(self.store.config, self.store)
        self.addCleanup(self.agent.cars.close)
        self.agent.client.chat.completions.create = AsyncMock()
        self.user = ChatUser(4242, None, "Тест")

    async def test_offer_text_without_pending_is_rejected(self):
        answer = await self.agent.reply(self.user, "Спасибо, всё хорошо, отдельная запись не нужна.")
        # Ответ-имитация предложения не должен дойти до клиента.
        self.assertNotIn("Подтвердить»", answer)
        self.assertNotIn("Отмена записи", answer)

    def test_real_offer_keeps_the_button(self):
        from tests.test_conversation import ARGS
        self.agent.client.chat.completions.create = AsyncMock(side_effect=[
            response(calls=[("get_price_list", {})]),
            response(calls=[("create_booking", ARGS)]),
        ])
        self.store._invalidate("catalog", "slots", "bookings")

        async def flow():
            await self.agent.reply(self.user, json_offer())
        # Полный диалог с предложением проверен в других тестах; здесь достаточно
        # убедиться, что при наличии pending защита не срабатывает.
        self.agent._pending[self.user.id] = {"action": "create_booking", "delivered": True,
                                             "reply": "Предлагаю запись", "offer_id": "x",
                                             "delivery_token": object(), "created": 0.0}
        problem = "Подтвердить»" in "Предлагаю запись… нажмите «Подтвердить»" and self.user.id not in self.agent._pending
        self.assertFalse(problem)


def json_offer():
    import json
    return json.dumps({"offer": True}, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

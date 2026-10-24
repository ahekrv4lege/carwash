import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ai import Agent, ChatUser
from tests.fakes import fixture


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_agent_tools_work_with_adaptive_store(self):
        with tempfile.TemporaryDirectory() as temp:
            store, _ = fixture(temp)
            with patch("ai.AsyncOpenAI") as provider:
                agent = Agent(store.config, store)
                user = ChatUser(id=111, username=None, full_name="Synthetic")
                prices = await agent._execute("get_price_list", {}, user)
                self.assertEqual(prices["services"][0]["prices"]["Легковой"], 700)
                result = await agent._execute("create_booking", {"date": "2099-01-01", "time": "10:00",
                    "client_name": "Synthetic", "phone": "00000000000", "car": "Fixture car",
                    "size_class": "Легковой", "tariff": "Базовый"}, user)
                self.assertTrue(result["ok"])
                mine = await agent._execute("get_my_bookings", {}, user)
                self.assertEqual(len(mine["bookings"]), 1)
                cancelled = await agent._execute("cancel_booking", {"booking_id": result["booking_id"]}, user)
                self.assertTrue(cancelled["ok"])
                provider.return_value.chat.completions.create.assert_not_called()

    async def test_application_closes_provider_and_telegram_sessions(self):
        import bot
        with tempfile.TemporaryDirectory() as temp:
            store, _ = fixture(temp)
            transport = Mock()
            transport.delete_webhook = AsyncMock()
            transport.session.close = AsyncMock()
            agent = Mock()
            agent.close = AsyncMock()
            dispatcher = Mock()
            dispatcher.start_polling = AsyncMock()
            with patch("bot.load_config", return_value=store.config), patch("bot.Bot", return_value=transport), \
                 patch("bot.Store", return_value=store), patch("bot.Agent", return_value=agent), \
                 patch("bot.Dispatcher", return_value=dispatcher):
                await bot.main()
            transport.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
            dispatcher.start_polling.assert_awaited_once_with(transport, close_bot_session=False)
            transport.session.close.assert_awaited_once()
            agent.close.assert_awaited_once()

    async def test_minimum_reply_delay_includes_model_time_without_extra_ai_calls(self):
        import bot
        from types import SimpleNamespace as N
        for minimum, elapsed, expected in ((1.5, 0.2, 1.3), (1.5, 3, 0), (0, 0.2, 0)):
            with self.subTest(minimum=minimum, elapsed=elapsed):
                agent = Mock(config=N(reply_min_delay_sec=minimum), reply=AsyncMock(return_value="Подскажу цены."))
                message = Mock(from_user=N(id=111, username=None, full_name="Тест"), chat=N(id=111), answer=AsyncMock())
                reporter = Mock(dialog=AsyncMock())
                sender = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
                timer = N(monotonic=Mock(side_effect=[100, 100 + elapsed]))
                with patch("bot.time", timer), patch("bot.ChatActionSender.typing", return_value=sender), \
                     patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    await bot.ask_agent(message, agent, reporter, "Прайс?")
                self.assertAlmostEqual(sleep.await_args.args[0], expected)
                agent.reply.assert_awaited_once()
                message.answer.assert_awaited_once_with("Подскажу цены.")
                agent.mark_delivered.assert_called_once()

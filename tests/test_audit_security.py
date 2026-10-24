import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from ai import Agent, ChatUser
from tests.fakes import fixture


class AuditSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_tool_arguments_and_error_bodies_are_not_logged(self):
        with tempfile.TemporaryDirectory() as temp:
            store, _ = fixture(temp)
            with patch("ai.AsyncOpenAI"):
                agent = Agent(store.cfg, store)
            user = ChatUser(111, None, "SYNTHETIC_PRIVATE_NAME")
            args = json.dumps({"size_class": "SYNTHETIC_PRIVATE_CLASS"})
            with patch.object(agent, "_execute", new=AsyncMock(side_effect=RuntimeError("SYNTHETIC_PROVIDER_SECRET"))), \
                 self.assertLogs("ai", level="INFO") as captured:
                result = await agent._call_tool("calculate_price", args, user)
            self.assertFalse(result["ok"])
            self.assertNotIn("SYNTHETIC_", "\n".join(captured.output))
            self.assertIn("RuntimeError", "\n".join(captured.output))

    async def test_provider_and_proofreader_errors_are_sanitized_in_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            store, _ = fixture(temp)
            with patch("ai.AsyncOpenAI"):
                agent = Agent(store.cfg, store)
            with patch.object(agent, "_run", new=AsyncMock(side_effect=RuntimeError("PRIVATE_SENTINEL"))), \
                 self.assertLogs("ai", level="ERROR") as captured:
                await agent.reply(ChatUser(111, None, "Synthetic"), "synthetic")
            self.assertNotIn("PRIVATE_SENTINEL", "\n".join(captured.output))
            agent.client.chat.completions.create = AsyncMock(side_effect=RuntimeError("PRIVATE_SENTINEL"))
            with self.assertLogs("ai", level="ERROR") as captured:
                self.assertEqual(await agent._proofread("synthetic"), "synthetic")
            self.assertNotIn("PRIVATE_SENTINEL", "\n".join(captured.output))

    async def test_admin_delivery_errors_do_not_log_message_or_exception_body(self):
        from admin import AdminReporter
        from unittest.mock import Mock
        bot = Mock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("PRIVATE_SENTINEL"))
        reporter = AdminReporter(bot, 111)
        with self.assertLogs("admin", level="ERROR") as captured:
            await reporter.send("PRIVATE_SENTINEL")
        self.assertNotIn("PRIVATE_SENTINEL", "\n".join(captured.output))

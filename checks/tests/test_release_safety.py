import asyncio
from dataclasses import replace
import os
import signal
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ai import Agent
from config import ProviderConfig, load_config
from gspread.exceptions import APIError
from sheets import SheetsHTTPClient
import run
from tests.fakes import fixture


def google_error(status):
    response = Mock()
    response.json.return_value = {"error": {"code": status, "message": "synthetic", "status": "UNAVAILABLE"}}
    return APIError(response)


class ReleaseSafety(unittest.TestCase):
    def test_google_read_retries_only_temporary_failures(self):
        client = SheetsHTTPClient(None, session=Mock())
        with patch("gspread.http_client.HTTPClient.request", side_effect=[google_error(503), "synthetic-result"]) as request, \
             patch("sheets.time.sleep"):
            self.assertEqual(client.request("get", "https://synthetic.invalid"), "synthetic-result")
            self.assertEqual(request.call_count, 2)

    def test_google_read_retries_are_bounded(self):
        client = SheetsHTTPClient(None, session=Mock())
        with patch("gspread.http_client.HTTPClient.request", side_effect=google_error(503)) as request, \
             patch("sheets.time.sleep"), self.assertRaises(APIError):
            client.request("get", "https://synthetic.invalid")
        self.assertEqual(request.call_count, 3)

    def test_google_writes_are_never_repeated_on_http_failure(self):
        client = SheetsHTTPClient(None, session=Mock())
        for method in ("post", "put", "patch", "delete"):
            for status in (408, 429, 500, 503):
                with self.subTest(method=method, status=status), \
                     patch("gspread.http_client.HTTPClient.request", side_effect=google_error(status)) as request, \
                     patch("sheets.time.sleep") as sleep, self.assertRaises(APIError):
                    client.request(method, "https://synthetic.invalid")
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()

    def test_windows_ctrl_c_does_not_send_unsupported_sigint(self):
        child = Mock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        child.poll.return_value = None
        with patch("run.subprocess.Popen", return_value=child), patch("run.signal.signal"), patch("run.os.name", "nt"):
            run.run_forever()
        child.send_signal.assert_not_called()
        self.assertEqual(child.wait.call_count, 2)

    def test_linux_ctrl_c_is_forwarded_to_child(self):
        child = Mock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        child.poll.return_value = None
        with patch("run.subprocess.Popen", return_value=child), patch("run.signal.signal"), patch("run.os.name", "posix"):
            run.run_forever()
        child.send_signal.assert_called_once_with(signal.SIGINT)

    def test_ai_timeouts_are_separate_and_validated(self):
        env = {"BOT_TOKEN": "123456:synthetic_token_never_used_12345", "SPREADSHEET_ID": "synthetic", "LLM_API_KEY": "synthetic"}
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
            self.assertEqual(config.ai_timeout_sec, 60)
            self.assertEqual(config.ai_request_budget_sec, 120)
        for value in ("0", "nan", "-1", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {**env, "AI_REQUEST_BUDGET_SEC": value}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "AI_REQUEST_BUDGET_SEC"):
                    load_config()


class DeliveryAndTimeouts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, _ = fixture(self.temp.name)
        self.client = Mock()
        self.client.chat.completions.create = AsyncMock(return_value="synthetic-result")
        self.client.close = AsyncMock()
        with patch("ai.AsyncOpenAI", return_value=self.client):
            self.agent = Agent(replace(self.store.config,
                llm_fallbacks=(ProviderConfig("https://backup.invalid/v1", "synthetic-backup", "backup-model"),)), self.store)
        self.addAsyncCleanup(self.agent.close)

    async def test_healthy_slow_primary_keeps_its_timeout_with_backups(self):
        self.agent.config = replace(self.agent.config, ai_timeout_sec=0.12, ai_request_budget_sec=0.18)
        async def slow(**arguments):
            await asyncio.sleep(0.08)
            return "synthetic-result"
        self.client.chat.completions.create.side_effect = slow
        with patch("ai.AsyncOpenAI") as factory:
            self.assertEqual(await self.agent._request(messages=[]), "synthetic-result")
        factory.assert_not_called()
        self.assertEqual(self.agent._provider_retry_at[0], 0)

    async def test_exhausted_budget_does_not_cool_untried_backups(self):
        self.agent.config = replace(self.agent.config, ai_timeout_sec=0.12, ai_request_budget_sec=0.02)
        async def hang(**arguments):
            await asyncio.Event().wait()
        self.client.chat.completions.create.side_effect = hang
        with patch("ai.AsyncOpenAI") as factory, self.assertRaises(asyncio.TimeoutError):
            await self.agent._request(messages=[])
        factory.assert_not_called()
        self.assertGreater(self.agent._provider_retry_at[0], 0)
        self.assertEqual(self.agent._provider_retry_at[1], 0)

    async def test_old_answer_cannot_mark_new_proposal_delivered(self):
        self.agent._proposal_text = Mock(side_effect=["old proposal", "new proposal"])
        self.agent._offer(111, {"action": "cancel_booking"})
        old_token = self.agent.delivery_token(111, "old proposal")
        self.agent._offer(111, {"action": "cancel_booking"})
        self.assertIsNone(self.agent.delivery_token(111, "old proposal"))
        self.agent.mark_delivered(111, old_token)
        self.assertFalse(self.agent._pending[111]["delivered"])
        self.agent.mark_delivered(111, self.agent.delivery_token(111, "new proposal"))
        self.assertTrue(self.agent._pending[111]["delivered"])

    async def test_telegram_binds_delivery_to_answer_text(self):
        from bot import ask_agent
        agent = Mock()
        agent.reply = AsyncMock(return_value="synthetic-answer")
        agent.delivery_token.return_value = "synthetic-token"
        message = Mock()
        message.from_user.id = 111
        message.from_user.username = None
        message.from_user.full_name = "Synthetic"
        message.answer = AsyncMock()
        reporter = Mock()
        reporter.dialog = AsyncMock()
        context = Mock()
        context.__aenter__ = AsyncMock()
        context.__aexit__ = AsyncMock()
        with patch("bot.ChatActionSender.typing", return_value=context):
            await ask_agent(message, agent, reporter, "synthetic-question")
        agent.delivery_token.assert_called_once_with(111, "synthetic-answer")
        agent.mark_delivered.assert_called_once_with(111, "synthetic-token")


class AdminMessageLimits(unittest.IsolatedAsyncioTestCase):
    async def test_long_question_and_answer_fit_telegram_without_losing_html_escaping(self):
        import html
        from admin import AdminReporter
        reporter = AdminReporter(Mock(), 111)
        reporter.send = AsyncMock()
        await reporter.dialog(111, "Synthetic", None, "<question>" * 500, "😀<answer>" * 500)
        message = reporter.send.call_args.args[0]
        self.assertLessEqual(len(html.unescape(message).encode("utf-16-le")) // 2, 4096)
        self.assertIn("&lt;question&gt;", message)
        self.assertIn("&lt;answer&gt;", message)
        self.assertEqual(reporter.send.call_args.kwargs["parse_mode"], "HTML")

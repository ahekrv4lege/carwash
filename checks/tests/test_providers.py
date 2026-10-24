"""Параметры совместимых API, старые переменные и независимый mapping-клиент."""
import json
import os
import tempfile
import unittest
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from adaptive import MappingError
from config import ProviderConfig, load_config
from ai import Agent
from openai import APIConnectionError, APIStatusError
from tests.fakes import suggest_mappings
from providers import client_options, completion_options
from tests.fakes import config, fixture


ENV = {"BOT_TOKEN": "123456:synthetic_token_never_used_12345", "SPREADSHEET_ID": "synthetic",
       "LLM_API_KEY": "synthetic-primary-key"}


class ProviderTests(unittest.TestCase):
    def load(self, **env):
        with patch.dict(os.environ, dict(ENV, **env), clear=True):
            return load_config()

    def test_fallbacks_require_complete_explicit_configuration(self):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            values = {"LLM_FALLBACK_1_BASE_URL": "https://backup.invalid/v1",
                      "LLM_FALLBACK_1_API_KEY": "synthetic-backup", "LLM_FALLBACK_1_MODEL": "backup-model"}
            values.pop("LLM_FALLBACK_1_" + field)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "LLM_FALLBACK_1_" + field):
                self.load(**values)

    def test_fallback_order_and_separate_credentials(self):
        cfg = self.load(LLM_FALLBACK_1_BASE_URL="https://first.invalid/v1/",
                        LLM_FALLBACK_1_API_KEY="synthetic-first", LLM_FALLBACK_1_MODEL="first-model",
                        LLM_FALLBACK_2_BASE_URL="https://second.invalid/v1",
                        LLM_FALLBACK_2_API_KEY="synthetic-second", LLM_FALLBACK_2_MODEL="second-model")
        self.assertEqual([p.model for p in cfg.llm_fallbacks], ["first-model", "second-model"])
        self.assertEqual(cfg.llm_fallbacks[0].base_url, "https://first.invalid/v1")
        self.assertNotIn("synthetic-first", repr(cfg.llm_fallbacks))
        self.assertEqual(client_options(cfg)["api_key"], ENV["LLM_API_KEY"])

    def test_existing_xkiro_defaults_are_preserved(self):
        cfg = self.load()
        self.assertEqual(cfg.ai_model, "qwen/qwen3.8-max:free")
        options = client_options(cfg)
        self.assertEqual(options["base_url"], "https://api.xkiro.com/v1")
        self.assertEqual(options["api_key"], ENV["LLM_API_KEY"])
        self.assertEqual(options["max_retries"], 0)
        request = completion_options(cfg)
        self.assertNotIn("reasoning_effort", request)
        self.assertEqual(request["temperature"], 0.3)
        self.assertEqual(request["max_tokens"], 1024)

    def test_generic_provider_overrides_without_legacy_key(self):
        cfg = self.load(LLM_API_KEY="", AI_API_KEY="new-key", AI_MODEL="selected-later",
                        AI_BASE_URL="https://provider.invalid/v1", AI_TIMEOUT_SEC="12.5")
        options = client_options(cfg)
        self.assertEqual(options["api_key"], "new-key")
        self.assertEqual(options["timeout"], 12.5)
        self.assertNotIn("default_headers", options)
        self.assertEqual(completion_options(cfg)["model"], "selected-later")

    def test_custom_endpoint_never_receives_inherited_legacy_key(self):
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(AI_BASE_URL="https://provider.invalid/v1", AI_MODEL="different")
        cfg = config("unused", ai_base_url="https://provider.invalid/v1", ai_api_key="")
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            client_options(cfg)

    def test_custom_endpoint_requires_explicit_model(self):
        with self.assertRaisesRegex(RuntimeError, "AI_MODEL"):
            self.load(AI_BASE_URL="https://provider.invalid/v1", AI_API_KEY="new-key")

    def test_key_precedence_and_model_fallback(self):
        cfg = self.load(AI_API_KEY="new-key", LLM_MODEL="legacy-model")
        self.assertEqual(client_options(cfg)["api_key"], "new-key")
        self.assertEqual(completion_options(cfg)["model"], "legacy-model")

    def test_reasoning_is_omitted_unless_explicitly_supported(self):
        for effort in ("none", "low", "high"):
            with self.subTest(effort=effort):
                with self.assertRaisesRegex(RuntimeError, "AI_REASONING_SUPPORTED"):
                    self.load(AI_REASONING_EFFORT=effort)
                cfg = self.load(AI_REASONING_EFFORT=effort, AI_REASONING_SUPPORTED="true")
                self.assertEqual(completion_options(cfg)["reasoning_effort"], effort)
        with self.assertRaisesRegex(RuntimeError, "AI_REASONING_EFFORT"):
            self.load(AI_REASONING_EFFORT="invented")

    def test_output_limit_parameter_is_explicit_and_singular(self):
        cfg = self.load(AI_TOKEN_LIMIT_PARAMETER="max_completion_tokens", AI_MAX_OUTPUT_TOKENS="2048")
        options = completion_options(cfg)
        self.assertEqual(options["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", options)
        with self.assertRaisesRegex(RuntimeError, "AI_TOKEN_LIMIT_PARAMETER"):
            self.load(AI_TOKEN_LIMIT_PARAMETER="max_output_tokens")
        with self.assertRaisesRegex(RuntimeError, "AI_MAX_OUTPUT_TOKENS"):
            self.load(AI_MAX_OUTPUT_TOKENS="300")

    def test_temperature_can_be_omitted_for_unsupported_models(self):
        cfg = self.load(AI_TEMPERATURE="")
        self.assertNotIn("temperature", completion_options(cfg))
        self.assertNotIn("temperature", completion_options(cfg, mapping=True))
        with self.assertRaisesRegex(RuntimeError, "AI_TEMPERATURE"):
            self.load(AI_TEMPERATURE="3")

    def test_invalid_limits_timeouts_and_flags_fail_without_values(self):
        for key, value in (("AI_TIMEOUT_SEC", "nan"), ("AI_TIMEOUT_SEC", "0"),
                           ("AI_REQUEST_RETRIES", "3"), ("AI_MAX_TOOL_ITERATIONS", "0"),
                           ("AI_MAX_TOOL_CALLS", "1.5"), ("AI_HISTORY_CHARS", "20"),
                           ("AI_RECENT_TURNS", "0"), ("AI_ENABLED", "perhaps")):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, key):
                self.load(**{key: value})

    def test_disabled_conversation_needs_no_api_key(self):
        cfg = self.load(AI_ENABLED="false", LLM_API_KEY="")
        self.assertFalse(cfg.ai_enabled)
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(LLM_API_KEY="")

    def test_sheets_only_does_not_require_conversation_credentials(self):
        with patch.dict(os.environ, {"SPREADSHEET_ID": "synthetic"}, clear=True):
            cfg = load_config(sheets_only=True)
        self.assertEqual(cfg.bot_token, "")
        self.assertEqual(cfg.ai_api_key, "")

    def test_base_url_validation_and_local_http(self):
        for url in ("garbage", "https://user:SECRET@provider.invalid/v1", "https://provider.invalid/v1?key=SECRET",
                    "http://provider.invalid/v1", "https://provider.invalid/v1#SECRET", ""):
            with self.subTest(url=url), self.assertRaises(RuntimeError) as raised:
                self.load(AI_BASE_URL=url)
            self.assertNotIn("SECRET", str(raised.exception))
        cfg = self.load(AI_BASE_URL="http://127.0.0.1:7777/v1", AI_API_KEY="local", AI_MODEL="local")
        self.assertEqual(client_options(cfg)["base_url"], "http://127.0.0.1:7777/v1")

    def test_mapping_model_and_endpoint_can_be_independent(self):
        cfg = self.load(AI_MODEL="conversation", SHEETS_AI_MODEL="mapping",
                        SHEETS_AI_BASE_URL="https://mapping.invalid/v1", SHEETS_AI_API_KEY="mapping-key")
        self.assertEqual(completion_options(cfg, mapping=True)["model"], "mapping")
        self.assertEqual(client_options(cfg, mapping=True)["api_key"], "mapping-key")
        self.assertEqual(completion_options(cfg)["model"], "conversation")
        self.assertNotIn("reasoning_effort", completion_options(cfg, mapping=True))
        self.assertEqual(completion_options(cfg, mapping=True)["max_tokens"], 1500)

    def test_separate_mapping_host_requires_its_own_key(self):
        cfg = self.load(SHEETS_AI_BASE_URL="https://mapping.invalid/v1")
        with self.assertRaisesRegex(RuntimeError, "SHEETS_AI_API_KEY"):
            client_options(cfg, mapping=True)

    def test_owned_mapping_transport_uses_configured_options_and_no_write_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            store, spreadsheet = fixture(temp, sheets_ai_mapping_enabled=True, sheets_ai_model="mapping-model",
                sheets_ai_base_url="https://mapping.invalid/v1", sheets_ai_api_key="mapping-key",
                sheets_ai_json_mode=False, ai_token_limit_parameter="max_completion_tokens")
            a = store._adaptive
            a.refresh()
            spreadsheet.worksheet("Записи").values[0][3] = "Arrival window"
            a.refresh(force=True)
            client = MagicMock()
            client.chat.completions.create.return_value = N(choices=[N(finish_reason="stop", message=N(content='{"suggestions":[]}'))])
            with patch("openai.OpenAI", return_value=client) as factory:
                suggest_mappings(a)
                suggest_mappings(a)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(factory.call_args.kwargs["base_url"], "https://mapping.invalid/v1")
            request = client.chat.completions.create.call_args.kwargs
            self.assertEqual(request["model"], "mapping-model")
            self.assertNotIn("tools", request)
            self.assertNotIn("response_format", request)
            self.assertEqual(request["max_completion_tokens"], 1500)
            self.assertNotIn("00000000000", json.dumps(request, ensure_ascii=False))
            client.close.assert_called_once()
            self.assertFalse(any(worksheet.writes for worksheet in spreadsheet.sheets))

    def test_truncated_mapping_result_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            store, spreadsheet = fixture(temp, sheets_ai_mapping_enabled=True)
            a = store._adaptive
            a.refresh()
            spreadsheet.worksheet("Записи").values[0][3] = "Arrival window"
            a.refresh(force=True)
            client = MagicMock()
            client.chat.completions.create.return_value = N(choices=[N(finish_reason="length", message=N(content='{"suggestions":[]}'))])
            with self.assertRaises(MappingError):
                suggest_mappings(a, client)
            self.assertFalse(a.path.with_name("mapping-suggestions.json").exists())


class Fallback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, _ = fixture(self.temp.name)
        self.clients = []
        self.factory = Mock(side_effect=self.new_client)
        self.patch = patch('ai.AsyncOpenAI', self.factory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        config = replace(self.store.config, ai_api_key='synthetic-primary', ai_timeout_sec=1,
                         ai_request_retries=1, llm_fallbacks=(
                             ProviderConfig(self.store.config.ai_base_url, 'synthetic-second', 'second-model'),
                             ProviderConfig('https://third.invalid/v1', 'synthetic-third', 'third-model')))
        self.agent = Agent(config, self.store)
        self.addAsyncCleanup(self.agent.close)

    def new_client(self, **options):
        client = Mock()
        client.chat.completions.create = AsyncMock(return_value=N(choices=[]))
        client.close = AsyncMock()
        self.clients.append(client)
        return client

    def error(self, status):
        return APIStatusError('synthetic', response=N(status_code=status, headers={},
                              request=N(method='POST', url='https://synthetic.invalid')), body=None)

    async def fail_primary(self, status=503):
        self.clients[0].chat.completions.create.side_effect = self.error(status)
        return await self.agent._request(messages=[])

    async def test_success_does_not_create_backup(self):
        await self.agent._request(messages=[])
        self.assertEqual(self.factory.call_count, 1)

    async def test_backup_connection_and_client_are_reused(self):
        await self.fail_primary()
        await self.agent._request(messages=[])
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.clients[0].chat.completions.create.await_count, 1)
        self.assertEqual(self.clients[1].chat.completions.create.await_count, 2)
        self.clients[1].close.assert_not_awaited()

    async def test_second_key_then_other_provider_and_key_isolation(self):
        self.clients[0].chat.completions.create.side_effect = self.error(401)
        original = self.factory.side_effect
        def factory(**options):
            client = original(**options)
            if options['api_key'] == 'synthetic-second':
                client.chat.completions.create.side_effect = self.error(429)
            return client
        self.factory.side_effect = factory
        await self.agent._request(messages=[])
        self.assertEqual([call.kwargs['api_key'] for call in self.factory.call_args_list],
                         ['synthetic-primary', 'synthetic-second', 'synthetic-third'])
        self.assertEqual(self.clients[2].chat.completions.create.call_args.kwargs['model'], 'third-model')
        self.assertEqual([client.chat.completions.create.await_count for client in self.clients], [1, 1, 1])

    async def test_cooldown_is_60_seconds(self):
        start = time.monotonic()
        await self.fail_primary()
        self.assertGreaterEqual(self.agent._provider_retry_at[0], start + 60)
        self.assertLess(self.agent._provider_retry_at[0], time.monotonic() + 60.1)

    async def test_primary_returns_after_cooldown(self):
        await self.fail_primary()
        self.agent._provider_retry_at[0] = time.monotonic() - 1
        self.clients[0].chat.completions.create.side_effect = None
        await self.agent._request(messages=[])
        self.assertEqual(self.clients[0].chat.completions.create.await_count, 2)
        self.assertEqual(self.agent._provider_retry_at[0], 0)
        self.assertEqual(self.clients[1].chat.completions.create.await_count, 1)

    async def test_all_cooling_fail_immediately_without_more_requests(self):
        self.agent._provider_retry_at = [time.monotonic() + 60] * 3
        with self.assertRaises(RuntimeError):
            await self.agent._request(messages=[])
        self.clients[0].chat.completions.create.assert_not_awaited()
        self.assertEqual(self.factory.call_count, 1)

    async def test_bad_request_does_not_failover_or_mark_unhealthy(self):
        self.clients[0].chat.completions.create.side_effect = self.error(400)
        with self.assertRaises(APIStatusError):
            await self.agent._request(messages=[])
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.agent._provider_retry_at, [0, 0, 0])

    async def test_cancellation_does_not_failover_or_mark_unhealthy(self):
        self.clients[0].chat.completions.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.agent._request(messages=[])
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.agent._provider_retry_at, [0, 0, 0])

    async def test_connection_error_uses_backup(self):
        self.clients[0].chat.completions.create.side_effect = APIConnectionError(
            request=N(method='POST', url='https://synthetic.invalid'))
        await self.agent._request(messages=[])
        self.assertEqual(len(self.clients), 2)

    async def test_empty_success_is_not_retried_elsewhere(self):
        response = await self.agent._request(messages=[])
        self.assertEqual(response.choices, [])
        self.assertEqual(self.factory.call_count, 1)

    async def test_one_available_provider_retains_bounded_retries(self):
        self.agent._provider_retry_at[1:] = [time.monotonic() + 60] * 2
        self.clients[0].chat.completions.create.side_effect = [self.error(503), N(choices=[])]
        with patch('ai.asyncio.sleep', new_callable=AsyncMock):
            await self.agent._request(messages=[])
        self.assertEqual(self.clients[0].chat.completions.create.await_count, 2)

    async def test_total_time_budget_includes_all_providers(self):
        self.agent.config = replace(self.agent.config, ai_timeout_sec=0.04, ai_request_budget_sec=0.12)
        async def hang(**arguments):
            await asyncio.Event().wait()
        self.clients[0].chat.completions.create.side_effect = hang
        original = self.factory.side_effect
        def factory(**options):
            client = original(**options)
            client.chat.completions.create.side_effect = hang
            return client
        self.factory.side_effect = factory
        start = time.monotonic()
        with self.assertRaises(asyncio.TimeoutError):
            await self.agent._request(messages=[])
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(len(self.clients), 3)
        self.assertTrue(all(client.chat.completions.create.await_count == 1 for client in self.clients))

    async def test_only_one_recovery_probe_while_other_users_use_backup(self):
        await self.fail_primary()
        self.agent._provider_retry_at[0] = time.monotonic() - 1
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(**arguments):
            entered.set()
            await release.wait()
            return N(choices=[])
        self.clients[0].chat.completions.create.side_effect = probe
        recovering = asyncio.create_task(self.agent._request(messages=[]))
        await entered.wait()
        await self.agent._request(messages=[])
        release.set()
        await recovering
        self.assertEqual(self.clients[0].chat.completions.create.await_count, 2)
        self.assertEqual(self.clients[1].chat.completions.create.await_count, 2)
        self.assertFalse(self.agent._provider_probes)

    async def test_cancelled_probe_releases_recovery_flag(self):
        self.agent._provider_retry_at[0] = time.monotonic() - 1
        self.clients[0].chat.completions.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.agent._request(messages=[])
        self.assertFalse(self.agent._provider_probes)

    async def test_all_clients_closed_once_at_shutdown_even_if_primary_close_fails(self):
        await self.fail_primary()
        self.clients[0].close.side_effect = OSError('synthetic')
        await self.agent.close()
        await self.agent.close()
        for client in self.clients:
            client.close.assert_awaited_once()
        with self.assertRaises(RuntimeError):
            await self.agent._request(messages=[])

    async def test_telegram_shutdown_closes_agent_even_if_polling_fails(self):
        import bot
        transport = Mock()
        transport.delete_webhook = AsyncMock()
        transport.session.close = AsyncMock()
        agent = Mock()
        agent.close = AsyncMock()
        dispatcher = Mock()
        dispatcher.start_polling = AsyncMock(side_effect=RuntimeError('synthetic'))
        with patch('bot.load_config', return_value=self.store.config), patch('bot.Bot', return_value=transport), \
             patch('bot.Store', return_value=self.store), patch('bot.Agent', return_value=agent), \
             patch('bot.Dispatcher', return_value=dispatcher):
            with self.assertRaises(RuntimeError):
                await bot.main()
        transport.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        transport.session.close.assert_awaited_once()
        agent.close.assert_awaited_once()


    async def test_every_supported_http_failure_uses_backup_without_primary_retry(self):
        for status in (401, 402, 403, 404, 408, 429, 500, 503):
            with self.subTest(status=status):
                self.agent._provider_retry_at = [0.0] * 3
                previous = self.clients[0].chat.completions.create.await_count
                await self.fail_primary(status)
                self.assertEqual(self.clients[0].chat.completions.create.await_count, previous + 1)
        self.assertEqual(len(self.clients), 2)

    async def test_all_failed_providers_are_cooled_without_repeating_requests(self):
        self.clients[0].chat.completions.create.side_effect = self.error(503)
        original = self.factory.side_effect
        def factory(**options):
            client = original(**options)
            client.chat.completions.create.side_effect = self.error(503)
            return client
        self.factory.side_effect = factory
        with self.assertRaises(APIStatusError):
            await self.agent._request(messages=[])
        with self.assertRaises(RuntimeError):
            await self.agent._request(messages=[])
        self.assertEqual(len(self.clients), 3)
        self.assertTrue(all(client.chat.completions.create.await_count == 1 for client in self.clients))

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
from providers import client_options, completion_options, invalid_tool_output, request_arguments, retry_after
from tests.fakes import config, fixture


ENV = {"BOT_TOKEN": "123456:synthetic_token_never_used_12345", "SPREADSHEET_ID": "synthetic",
       "LLM_API_KEY": "synthetic-primary-key"}


class ProviderTests(unittest.TestCase):
    def test_message_batch_window_is_optional_and_rejects_invalid_values(self):
        self.assertEqual(self.load().reply_batch_sec, 0.4)
        self.assertEqual(self.load(REPLY_BATCH_SEC="0").reply_batch_sec, 0)
        for value in ("-1", "nan", "inf", "invalid"):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "REPLY_BATCH_SEC"):
                self.load(REPLY_BATCH_SEC=value)

    def test_retry_after_accepts_seconds_and_http_date_but_not_invalid_values(self):
        for value, expected in (("120", 120), ("0.5", 1), ("999999999", 86400),
                                ("nan", None), ("inf", None), ("nonsense", None)):
            with self.subTest(value=value):
                self.assertEqual(
                    retry_after(N(status_code=429, response=N(headers={"retry-after": value}))), expected)
        with patch("providers.time.time", return_value=0):
            self.assertEqual(retry_after(N(
                status_code=503,
                response=N(headers={"retry-after": "Thu, 01 Jan 1970 00:02:00 GMT"}),
            )), 120)
        self.assertIsNone(retry_after(N(status_code=400, response=N(headers={"retry-after": "120"}))))

    def test_cloudflare_combines_system_messages_without_mutating_history(self):
        cfg = self.load(AI_BASE_URL="https://api.cloudflare.com/client/v4/accounts/synthetic/ai/v1",
                        AI_API_KEY="synthetic", AI_MODEL="@cf/zai-org/glm-4.7-flash")
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"},
                    {"role": "system", "content": "repair"},
                    {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "{}"}]
        arguments = {"messages": messages, "tool_choice": "auto"}
        converted = request_arguments(cfg, arguments)
        self.assertEqual(converted["messages"], [{"role": "system", "content": "rules\nrepair"},
                                               messages[1], *messages[3:]])
        self.assertEqual(converted["tool_choice"], "auto")
        self.assertEqual(len(arguments["messages"]), 5)
        self.assertIs(request_arguments(self.load(), arguments), arguments)

    def test_cloudflare_thinking_is_explicit_and_token_limit_is_singular(self):
        cfg = self.load(AI_BASE_URL="https://api.cloudflare.com/client/v4/accounts/synthetic/ai/v1",
                        AI_API_KEY="synthetic", AI_MODEL="@cf/zai-org/glm-4.7-flash")
        options = completion_options(cfg)
        self.assertEqual(options["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(options["max_completion_tokens"], 1024)
        self.assertNotIn("max_tokens", options)
        self.assertNotIn("extra_body", completion_options(self.load()))
        self.assertTrue(completion_options(replace(cfg, ai_reasoning_effort="low"))["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        self.assertFalse(completion_options(replace(cfg, ai_reasoning_effort="low"), mapping=True)["extra_body"]["chat_template_kwargs"]["enable_thinking"])

    def test_cloudflare_thinking_controls_do_not_leak_to_other_endpoints_or_models(self):
        cfg = self.load(AI_BASE_URL="https://api.cloudflare.com/client/v4/accounts/synthetic/ai/v1",
                        AI_API_KEY="synthetic", AI_MODEL="@cf/qwen/qwen3.8-27b",
                        AI_REASONING_SUPPORTED="true", AI_REASONING_EFFORT="low")
        self.assertEqual(completion_options(cfg)["reasoning_effort"], "low")
        self.assertNotIn("extra_body", completion_options(replace(cfg, ai_model="@cf/openai/gpt-oss-120b")))
        self.assertNotIn("extra_body", completion_options(replace(cfg, ai_base_url="https://other.invalid/v1")))

    def test_only_cloudflare_invalid_generated_tool_json_is_recoverable(self):
        body = {"errors": [{"code": 3030, "message": "AiError: Invalid JSON: expected value"}]}
        self.assertTrue(invalid_tool_output(N(status_code=400, body=body)))
        for status, detail in ((429, body), (400, None), (400, {"errors": None}),
                               (400, {"errors": [{"code": 3030, "message": "Invalid input"}]})):
            self.assertFalse(invalid_tool_output(N(status_code=status, body=detail)))

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
        self.assertEqual(cfg.ai_model, "minimax/minimax-m3.1-flash")
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
    async def test_quota_retry_after_skips_the_key_until_its_own_deadline(self):
        error = self.error(429)
        error.response.headers["retry-after"] = "7200"
        self.clients[0].chat.completions.create.side_effect = error
        start = time.monotonic()
        await self.agent._request(messages=[])
        await self.agent._request(messages=[])
        self.assertGreaterEqual(self.agent._provider_retry_at[0], start + 7200)
        self.assertEqual(self.clients[0].chat.completions.create.await_count, 1)
        self.assertEqual(self.clients[1].chat.completions.create.await_count, 2)

    async def test_single_available_key_does_not_retry_before_server_deadline(self):
        self.agent._provider_retry_at[1:] = [time.monotonic() + 60] * 2
        error = self.error(429)
        error.response.headers["retry-after"] = "120"
        self.clients[0].chat.completions.create.side_effect = error
        with patch("ai.asyncio.sleep", new_callable=AsyncMock) as sleep, self.assertRaises(APIStatusError):
            await self.agent._request(messages=[])
        self.assertEqual(self.clients[0].chat.completions.create.await_count, 1)
        sleep.assert_not_awaited()

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

    async def test_cloudflare_fallback_prepares_its_own_request(self):
        cfg = replace(self.agent._provider_configs[1],
                      ai_base_url="https://api.cloudflare.com/client/v4/accounts/synthetic/ai/v1",
                      ai_model="@cf/zai-org/glm-4.7-flash")
        self.agent._provider_configs = (self.agent.config, cfg)
        self.clients[0].chat.completions.create.side_effect = self.error(429)
        messages = [{"role": "user", "content": "hello"}, {"role": "system", "content": "rules"}]
        await self.agent._request(messages=messages)
        primary = self.clients[0].chat.completions.create.call_args.kwargs
        backup = self.clients[1].chat.completions.create.call_args.kwargs
        self.assertEqual(primary["messages"], messages)
        self.assertNotIn("extra_body", primary)
        self.assertEqual(backup["messages"][0]["role"], "system")
        self.assertEqual(backup["model"], cfg.ai_model)
        self.assertEqual(backup["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)

    async def test_cloudflare_tool_output_uses_one_named_response_retry(self):
        error = self.error(400)
        error.body = {"errors": [{"code": 3030, "message": "Invalid JSON: expected value"}]}
        client = self.clients[0].chat.completions.create
        client.side_effect = [error, "synthetic-result"]
        result = await self.agent._request(messages=[], tools=[{"function": {"name": "respond"}}], tool_choice="required")
        self.assertEqual(result, "synthetic-result")
        self.assertEqual(client.await_count, 2)
        self.assertEqual(client.call_args.kwargs["tool_choice"], {"type": "function", "function": {"name": "respond"}})
        self.assertEqual(self.factory.call_count, 1)

    async def test_invalid_tool_json_retry_omits_unusable_tools(self):
        error = self.error(400)
        error.body = {"errors": [{"code": 3030, "message": "Invalid JSON"}]}
        client = self.clients[0].chat.completions.create
        client.side_effect = [error, "synthetic-result"]
        tools = [{"function": {"name": name}} for name in ("respond", "get_price_list", "create_booking")]
        await self.agent._request(messages=[], tools=tools)
        self.assertEqual(client.call_args.kwargs["tools"], tools[:1])
        self.assertEqual(len(tools), 3)
        self.assertEqual(client.await_count, 2)

    async def test_cloudflare_output_recovery_is_not_repeated_or_used_for_input_errors(self):
        client = self.clients[0].chat.completions.create
        for message, expected in (("Invalid JSON", 2), ("Invalid input", 1)):
            error = self.error(400)
            error.body = {"errors": [{"code": 3030, "message": message}]}
            client.reset_mock()
            client.side_effect = error
            with self.assertRaises(APIStatusError):
                await self.agent._request(messages=[], tools=[{"function": {"name": "respond"}}])
            self.assertEqual(client.await_count, expected)
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
        # Значения кратны масштабу real-конфигурации (~12 с / 45 с), но
        # растянуты под разрешение таймера Windows (~16 мс), чтобы доли
        # бюджета всегда превышали тик часов и тест не зависел от квантования.
        self.agent.config = replace(self.agent.config, ai_timeout_sec=0.12, ai_request_budget_sec=0.45)
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

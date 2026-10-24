"""Параметры совместимых API, старые переменные и независимый mapping-клиент."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace as N
from unittest.mock import MagicMock, patch

from adaptive import MappingError
from config import load_config
from mapping_ai import suggest_mappings
from providers import client_options, completion_options
from tests.fakes import config, fixture


ENV = {"BOT_TOKEN": "123456:synthetic_token_never_used_12345", "SPREADSHEET_ID": "synthetic",
       "OPENROUTER_API_KEY": "synthetic-old-key"}


class ProviderTests(unittest.TestCase):
    def load(self, **env):
        with patch.dict(os.environ, dict(ENV, **env), clear=True):
            return load_config()

    def test_existing_openrouter_defaults_are_preserved(self):
        cfg = self.load()
        self.assertEqual(cfg.conversation_model, "google/gemma-4-31b-it:free")
        options = client_options(cfg)
        self.assertEqual(options["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(options["api_key"], ENV["OPENROUTER_API_KEY"])
        self.assertEqual(options["max_retries"], 0)
        request = completion_options(cfg)
        self.assertNotIn("reasoning_effort", request)
        self.assertEqual(request["temperature"], 0.3)
        self.assertEqual(request["max_tokens"], 1024)

    def test_generic_provider_overrides_without_legacy_key(self):
        cfg = self.load(OPENROUTER_API_KEY="", AI_API_KEY="new-key", AI_MODEL="selected-later",
                        AI_BASE_URL="https://provider.invalid/v1", AI_TIMEOUT_SEC="12.5")
        options = client_options(cfg)
        self.assertEqual(options["api_key"], "new-key")
        self.assertEqual(options["timeout"], 12.5)
        self.assertNotIn("default_headers", options)
        self.assertEqual(completion_options(cfg)["model"], "selected-later")

    def test_custom_endpoint_never_receives_inherited_legacy_key(self):
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(AI_BASE_URL="https://provider.invalid/v1", AI_MODEL="different")
        cfg = config("unused", ai_base_url="https://provider.invalid/v1")
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            client_options(cfg)

    def test_custom_endpoint_requires_explicit_model(self):
        with self.assertRaisesRegex(RuntimeError, "AI_MODEL"):
            self.load(AI_BASE_URL="https://provider.invalid/v1", AI_API_KEY="new-key")

    def test_key_precedence_and_model_fallback(self):
        cfg = self.load(AI_API_KEY="new-key", OPENROUTER_MODEL="legacy-model")
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
        cfg = self.load(AI_ENABLED="false", OPENROUTER_API_KEY="")
        self.assertFalse(cfg.ai_enabled)
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(OPENROUTER_API_KEY="")

    def test_sheets_only_does_not_require_conversation_credentials(self):
        with patch.dict(os.environ, {"SPREADSHEET_ID": "synthetic"}, clear=True):
            cfg = load_config(sheets_only=True)
        self.assertEqual(cfg.bot_token, "")
        self.assertEqual(cfg.openrouter_api_key, "")

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
            store, ss = fixture(temp, sheets_ai_mapping_enabled=True, sheets_ai_model="mapping-model",
                sheets_ai_base_url="https://mapping.invalid/v1", sheets_ai_api_key="mapping-key",
                sheets_ai_json_mode=False, ai_token_limit_parameter="max_completion_tokens")
            a = store._adaptive
            a.refresh()
            ss.worksheet("Записи").values[0][3] = "Arrival window"
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
            self.assertFalse(any(ws.writes for ws in ss.sheets))

    def test_truncated_mapping_result_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            store, ss = fixture(temp, sheets_ai_mapping_enabled=True)
            a = store._adaptive
            a.refresh()
            ss.worksheet("Записи").values[0][3] = "Arrival window"
            a.refresh(force=True)
            client = MagicMock()
            client.chat.completions.create.return_value = N(choices=[N(finish_reason="length", message=N(content='{"suggestions":[]}'))])
            with self.assertRaises(MappingError):
                suggest_mappings(a, client)
            self.assertFalse(a.path.with_name("mapping-suggestions.json").exists())

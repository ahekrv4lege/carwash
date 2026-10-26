"""Регрессии слияния: прежние конфиги, штатный запуск и реальные SDK без сети."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteWebhook, GetMe, SendChatAction, SendMessage
from aiogram.types import Message, Update, User

import config
import run
from ai import Agent
from providers import client_options, completion_options
from tests.fakes import config as fake_config

ROOT = Path(__file__).resolve().parents[2]
ENV = {"BOT_TOKEN": "123456:synthetic_token_never_used_12345", "SPREADSHEET_ID": "synthetic"}


class MergeConfigTests(unittest.TestCase):
    def load(self, **values):
        with patch.dict(os.environ, dict(ENV, **values), clear=True):
            return config.load_config()

    def test_1451f6d_llm_configuration_keeps_xkiro_defaults(self):
        cfg = self.load(LLM_API_KEY="synthetic-xkiro")
        self.assertEqual(client_options(cfg)["base_url"], "https://api.xkiro.com/v1")
        self.assertEqual(client_options(cfg)["api_key"], "synthetic-xkiro")
        self.assertEqual(completion_options(cfg)["model"], "qwen/qwen3.8-max:free")

    def test_llm_custom_configuration_keeps_its_key_url_and_model(self):
        cfg = self.load(LLM_API_KEY="synthetic-llm", LLM_BASE_URL="https://llm.invalid/v1/",
                        LLM_MODEL="team-model")
        self.assertEqual(client_options(cfg)["base_url"], "https://llm.invalid/v1")
        self.assertEqual(client_options(cfg)["api_key"], "synthetic-llm")
        self.assertEqual(completion_options(cfg)["model"], "team-model")

    def test_llm_custom_url_preserves_original_default_model(self):
        cfg = self.load(LLM_API_KEY="synthetic-llm", LLM_BASE_URL="https://llm.invalid/v1")
        self.assertEqual(completion_options(cfg)["model"], "qwen/qwen3.8-max:free")

    def test_second_llm_key_does_not_override_primary(self):
        cfg = self.load(LLM_API_KEY="synthetic-primary", LLM_MODEL="team-model", LLM_API_KEY_2="synthetic-second")
        self.assertEqual(client_options(cfg)["base_url"], "https://api.xkiro.com/v1")
        self.assertEqual(client_options(cfg)["api_key"], "synthetic-primary")
        self.assertEqual(completion_options(cfg)["model"], "team-model")
        self.assertEqual(cfg.llm_fallbacks[0].api_key, "synthetic-second")

    def test_explicit_ai_configuration_overrides_llm_as_a_unit(self):
        cfg = self.load(LLM_API_KEY="synthetic-llm", LLM_MODEL="team-model",
                        AI_BASE_URL="https://ai.invalid/v1", AI_API_KEY="synthetic-ai", AI_MODEL="ai-model")
        self.assertEqual(client_options(cfg)["base_url"], "https://ai.invalid/v1")
        self.assertEqual(client_options(cfg)["api_key"], "synthetic-ai")
        self.assertEqual(completion_options(cfg)["model"], "ai-model")

    def test_other_provider_cannot_inherit_unrelated_keys(self):
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(LLM_API_KEY="synthetic-primary", LLM_MODEL="team-model",
                      UNRELATED_API_KEY="synthetic-unrelated", AI_BASE_URL="https://other.invalid/v1")

    def test_changed_endpoint_cannot_inherit_llm_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(LLM_API_KEY="synthetic-llm", AI_BASE_URL="https://other.invalid/v1", AI_MODEL="m")

    def test_changed_endpoint_cannot_inherit_llm_model(self):
        with self.assertRaisesRegex(RuntimeError, "AI_MODEL"):
            self.load(LLM_API_KEY="synthetic-llm", LLM_MODEL="team-model",
                      AI_BASE_URL="https://other.invalid/v1", AI_API_KEY="synthetic-ai")

    def test_changed_port_cannot_inherit_llm_key(self):
        with self.assertRaisesRegex(RuntimeError, "AI_API_KEY"):
            self.load(LLM_API_KEY="synthetic-llm", AI_BASE_URL="https://api.xkiro.com:444/v1", AI_MODEL="m")

    def test_original_config_constructor_does_not_require_new_llm_fields(self):
        cfg = fake_config("unused")
        self.assertEqual(cfg.ai_model, "existing-model")
        self.assertEqual(client_options(cfg)["api_key"], "synthetic")

    def test_llm_url_validation_does_not_expose_secrets(self):
        for url in ("broken", "https://user:SECRET@llm.invalid/v1", "http://llm.invalid/v1"):
            with self.subTest(url=url), self.assertRaises(RuntimeError) as error:
                self.load(LLM_API_KEY="synthetic-llm", LLM_BASE_URL=url)
            self.assertNotIn("SECRET", str(error.exception))

    def test_environment_without_env_or_template_is_sufficient(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credentials = root / "google.json"
            credentials.write_text(json.dumps({"type": "service_account", "client_email": "test@example.invalid",
                                               "private_key": "synthetic", "token_uri": "https://example.invalid/token"}))
            values = dict(ENV, LLM_API_KEY="synthetic-llm", GOOGLE_CREDENTIALS_FILE=str(credentials))
            with patch.dict(os.environ, values, clear=True), patch.object(run, "ROOT", root):
                cfg = run.validate_config()
            self.assertEqual(cfg.bot_token, ENV["BOT_TOKEN"])
            self.assertFalse((root / ".env").exists())
            self.assertFalse((root / ".env.example").exists())

    def test_existing_env_is_loaded_without_template_and_environment_has_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.py").write_bytes((ROOT / "config.py").read_bytes())
            env_file = root / ".env"
            content = "\n".join(f"{key}={value}" for key, value in dict(ENV,
                LLM_API_KEY="synthetic-file-key", LLM_MODEL="file-model").items()) + "\n"
            env_file.write_text(content)
            code = "from config import load_config; c=load_config(); assert c.ai_api_key=='synthetic-file-key'; assert c.ai_model=='env-model'"
            env = dict(os.environ, LLM_MODEL="env-model")
            env.pop("PYTHON_DOTENV_DISABLED", None)
            proc = subprocess.run([sys.executable, "-c", code], cwd=root,
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(env_file.read_text(), content)
            self.assertFalse((root / ".env.example").exists())

    def test_standard_launch_uses_existing_token_without_local_flags(self):
        cfg = fake_config("unused")
        launch = Mock()
        with patch.object(sys, "argv", ["run.py"]), patch.object(run, "prepare_environment"), \
             patch.object(run, "validate_config", return_value=cfg), \
             patch.object(run, "InstanceLock"), patch.object(run, "run_forever", launch):
            self.assertEqual(run.main(), 0)
        launch.assert_called_once()

    def test_check_does_not_initialize_cloud_clients(self):
        cfg = fake_config("unused")
        with patch.object(sys, "argv", ["run.py", "--check"]), patch.object(run, "prepare_environment"), \
             patch.object(run, "validate_config", return_value=cfg), patch.object(run, "InstanceLock"), \
             patch("bot.Bot") as telegram, patch("ai.AsyncOpenAI") as ai, patch("sheets.Store") as sheets:
            self.assertEqual(run.main(), 0)
        telegram.assert_not_called()
        ai.assert_not_called()
        sheets.assert_not_called()

    def test_real_ai_client_initialization_uses_restored_provider_without_inference(self):
        cfg = self.load(LLM_API_KEY="synthetic-llm", LLM_MODEL="team-model")
        # Инициализация настоящего SDK не должна отправлять запросы.
        # Жёстко блокируем только внешние адреса: локальную петлю использует
        # self-pipe asyncio на Windows, полностью блокировать её нельзя.
        def denied(self_source, address, *rest):
            host = address[0] if isinstance(address, (tuple, list)) and address else address
            if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.endswith(".localhost"):
                raise AssertionError("Network disabled")
            return connect_original(self_source, address, *rest)
        connect_original = socket.socket.connect
        with patch("socket.socket.connect", new=denied):
            agent = Agent(cfg, Mock())
            self.assertEqual(str(agent.client.base_url), "https://api.xkiro.com/v1/")
            self.assertEqual(agent.client.api_key, "synthetic-llm")
            import asyncio
            asyncio.run(agent.close())


class OfflineTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Synthetic", username="synthetic_bot")
        if isinstance(method, (DeleteWebhook, SendChatAction)):
            return True
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.methods), date=0,
                           chat={"id": method.chat_id, "type": "private"}, text=method.text)
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")

    async def stream_content(self, url, **kwargs):
        raise AssertionError("Network disabled")
        yield b""


class MergeTelegramTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location("merge_test_bot", ROOT / "bot.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_real_aiogram_startup_routes_commands_and_private_text(self):
        module = self.module()
        cfg = fake_config("unused", ai_enabled=False)
        session = OfflineTelegram()

        async def polling(dispatcher, telegram, **kwargs):
            self.assertEqual(telegram.token, cfg.bot_token)
            self.assertEqual(kwargs, {"close_bot_session": False})
            agent = dispatcher.workflow_data["agent"]
            async def feed(index, text=None, group=False):
                message = {"message_id": index, "date": 0, "chat": {"id": 111, "type": "group" if group else "private"},
                           "from": {"id": 111, "is_bot": False, "first_name": "Synthetic"}}
                if text is not None:
                    message["text"] = text
                    if text.startswith("/"):
                        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
                await dispatcher.feed_update(telegram, Update.model_validate({"update_id": index, "message": message}))
            agent._state[111] = {"old": True}
            await feed(1, "/start")
            self.assertNotIn(111, agent._state)
            agent._state[111] = {"old": True}
            await feed(2, "/reset")
            self.assertNotIn(111, agent._state)
            with patch.object(agent, "reply", wraps=agent.reply) as reply:
                await feed(3, "Здравствуйте")
                self.assertEqual(reply.await_args.args[1], "Здравствуйте")
                await feed(4)
                await feed(5, "group message", group=True)
                self.assertEqual(reply.await_count, 1)

        with patch.object(module, "load_config", return_value=cfg), \
             patch.object(module, "Bot", side_effect=lambda token: Bot(token, session=session)), \
             patch.object(module.Dispatcher, "start_polling", new=polling):
            await module.main()
        self.assertTrue(session.closed)
        webhooks = [method for method in session.methods if isinstance(method, DeleteWebhook)]
        self.assertEqual(len(webhooks), 1)
        self.assertFalse(webhooks[0].drop_pending_updates)
        self.assertEqual(len([method for method in session.methods if isinstance(method, SendMessage)]), 4)

    async def test_telegram_session_closes_when_agent_initialization_fails(self):
        module = self.module()
        session = OfflineTelegram()
        with patch.object(module, "load_config", return_value=fake_config("unused")), \
             patch.object(module, "Bot", side_effect=lambda token: Bot(token, session=session)), \
             patch.object(module, "Agent", side_effect=RuntimeError("synthetic startup failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic startup failure"):
                await module.main()
        self.assertTrue(session.closed)
        self.assertEqual(session.methods, [])

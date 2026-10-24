"""Общие параметры OpenAI-совместимого API; без выбора провайдера или модели."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit


def private_sdk_logging() -> None:
    # SDK может печатать тела запросов при OPENAI_LOG=debug.
    prefixes = ("openai", "httpx", "httpx2", "httpcore")
    for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)


def client_options(cfg, *, mapping: bool = False) -> dict:
    private_sdk_logging()
    url = (cfg.sheets_ai_base_url if mapping else "") or cfg.ai_base_url
    host = urlsplit(url).hostname
    if mapping and cfg.sheets_ai_base_url and host != urlsplit(cfg.ai_base_url).hostname:
        key = cfg.sheets_ai_api_key
    else:
        key = (cfg.sheets_ai_api_key if mapping else "") or cfg.ai_api_key
        if not key and host == "openrouter.ai":
            key = cfg.openrouter_api_key
    if not key:
        raise RuntimeError("Для выбранного адреса AI нужен отдельный AI_API_KEY или SHEETS_AI_API_KEY")
    options = dict(api_key=key, base_url=url,
                   timeout=cfg.ai_timeout_sec, max_retries=0)
    if host == "openrouter.ai":
        options["default_headers"] = {"X-Title": "CarWash Telegram Bot"}
    return options


def completion_options(cfg, *, mapping: bool = False) -> dict:
    model = (cfg.sheets_ai_model if mapping else "") or cfg.conversation_model
    options = {"model": model, cfg.ai_token_limit_parameter: 1500 if mapping else cfg.ai_max_output_tokens}
    if cfg.ai_temperature is not None:
        options["temperature"] = 0 if mapping else cfg.ai_temperature
    if cfg.ai_reasoning_effort and not mapping:
        options["reasoning_effort"] = cfg.ai_reasoning_effort
    return options

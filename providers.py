"""Общие параметры OpenAI-совместимого API; без выбора провайдера или модели."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit


def invalid_tool_output(error) -> bool:
    body = getattr(error, "body", None)
    return (getattr(error, "status_code", None) == 400 and isinstance(body, dict)
            and isinstance(body.get("errors"), list) and any(
                isinstance(item, dict) and item.get("code") == 3030
                and "Invalid JSON" in str(item.get("message", "")) for item in body["errors"]))


def private_sdk_logging() -> None:
    # SDK может печатать тела запросов при OPENAI_LOG=debug.
    prefixes = ("openai", "httpx", "httpx2", "httpcore")
    for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)


def client_options(config, *, mapping: bool = False) -> dict:
    private_sdk_logging()
    url = (config.sheets_ai_base_url if mapping else "") or config.ai_base_url
    if mapping and config.sheets_ai_base_url and urlsplit(url)[:2] != urlsplit(config.ai_base_url)[:2]:
        key = config.sheets_ai_api_key
    else:
        key = (config.sheets_ai_api_key if mapping else "") or config.ai_api_key
    if not key:
        raise RuntimeError("Для выбранного адреса AI нужен отдельный AI_API_KEY или SHEETS_AI_API_KEY")
    options = dict(api_key=key, base_url=url,
                   timeout=config.ai_timeout_sec, max_retries=0)
    return options


def completion_options(config, *, mapping: bool = False) -> dict:
    model = (config.sheets_ai_model if mapping else "") or config.ai_model
    options = {"model": model, config.ai_token_limit_parameter: 1500 if mapping else config.ai_max_output_tokens}
    if config.ai_temperature is not None:
        options["temperature"] = 0 if mapping else config.ai_temperature
    if config.ai_reasoning_effort and not mapping:
        options["reasoning_effort"] = config.ai_reasoning_effort
    return options

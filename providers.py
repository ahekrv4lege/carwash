"""Общие параметры OpenAI-совместимого API; без выбора провайдера или модели."""
from __future__ import annotations

import logging
import math
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit


def retry_after(error) -> float | None:
    if getattr(error, "status_code", None) not in {429, 503}:
        return None
    value = getattr(getattr(error, "response", None), "headers", {}).get("retry-after")
    if value is None:
        return None
    try:
        try:
            seconds = float(value)
        except ValueError:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        return min(max(seconds, 1), 86400) if math.isfinite(seconds) else None
    except (ValueError, TypeError, OverflowError):
        return None


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


def client_options(config) -> dict:
    private_sdk_logging()
    if not config.ai_api_key:
        raise RuntimeError("Для выбранного адреса AI нужен AI_API_KEY")
    return dict(api_key=config.ai_api_key, base_url=config.ai_base_url, timeout=config.ai_timeout_sec, max_retries=0)


_COMPLETION_CACHE: dict[tuple, dict] = {}


def completion_options(config) -> dict:
    cache_key = (config.ai_model, config.ai_base_url, config.ai_max_output_tokens,
                 config.ai_token_limit_parameter, config.ai_temperature, config.ai_reasoning_effort)
    cached = _COMPLETION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    options = _build_completion_options(config)
    if len(_COMPLETION_CACHE) >= 32:
        _COMPLETION_CACHE.clear()
    _COMPLETION_CACHE[cache_key] = options
    return options


def _build_completion_options(config) -> dict:
    model = config.ai_model
    hostname = urlsplit(config.ai_base_url).hostname
    options = {"model": model}
    if config.ai_max_output_tokens:
        options[config.ai_token_limit_parameter] = config.ai_max_output_tokens
    if config.ai_temperature is not None:
        options["temperature"] = config.ai_temperature
    if config.ai_reasoning_effort:
        options["reasoning_effort"] = config.ai_reasoning_effort
    if hostname in {"polza.ai", "api.polza.ai"}:
        polza = {}
        effort = options.pop("reasoning_effort", None)
        canonical, marker, aliases = model.partition("@")
        if marker:
            seen = set()
            for key, value in parse_qsl(aliases, strict_parsing=True, keep_blank_values=True):
                if not value.strip() or key in seen or key not in {"provider", "allow_fallbacks", "reasoning_effort"}:
                    raise RuntimeError("LLM_MODEL: пустой, неизвестный или повторный параметр Polza")
                seen.add(key)
                if key == "reasoning_effort":
                    if value not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
                        raise RuntimeError("LLM_MODEL: неизвестный уровень рассуждений Polza")
                    if effort is None:
                        effort = value
                elif key == "provider":
                    polza.setdefault("provider", {})["only"] = [value]
                else:
                    if value not in {"true", "false"}:
                        raise RuntimeError("LLM_MODEL: allow_fallbacks должен быть true или false")
                    polza.setdefault("provider", {})[key] = value == "true"
            options["model"] = canonical
        if effort == "none":
            polza["reasoning"] = {"enabled": False}
        elif effort:
            polza["reasoning"] = {"effort": effort}
        if polza:
            options["extra_body"] = polza
    if hostname == "api.cloudflare.com":
        if config.ai_token_limit_parameter in options:
            options["max_completion_tokens"] = options.pop(config.ai_token_limit_parameter)
        if model in {"@cf/zai-org/glm-4.7-flash", "@cf/google/gemma-4-26b-a4b-it", "@cf/qwen/qwen3.8-27b"}:
            effort = options.pop("reasoning_effort", "")
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": effort not in {"", "none"}}}
            if effort and effort != "none" and model == "@cf/qwen/qwen3.8-27b":
                options["reasoning_effort"] = effort
    return options


def api_error_label(error) -> str:
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    nested = body.get("error", body) if isinstance(body, dict) else {}
    code = str(nested.get("code", "")) if isinstance(nested, dict) else ""
    code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", code) else ""
    param = str(nested.get("param", "")) if isinstance(nested, dict) else ""
    message = str(nested.get("message", "")) if isinstance(nested, dict) else ""
    if not param and status == 400:
        fields = re.findall(r"\b(?:max_price|reasoning_effort|reasoning|provider|tool_choice|tools|messages|max_tokens|temperature)\b", message)
        param = ",".join(dict.fromkeys(fields))
    param = param if re.fullmatch(r"[A-Za-z0-9_.,\[\]-]{1,100}", param) else ""
    return " ".join(filter(None, (type(error).__name__, f"HTTP {status}" if isinstance(status, int) else "",
                                  f"code={code}" if code else "", f"param={param}" if param else "")))


def request_arguments(config, arguments) -> dict:
    if urlsplit(config.ai_base_url).hostname != "api.cloudflare.com":
        return arguments
    messages = arguments.get("messages", [])
    system = [message["content"] for message in messages if message["role"] == "system"]
    return dict(arguments, messages=([{"role": "system", "content": "\n".join(system)}] if system else [])
                + [message for message in messages if message["role"] != "system"])

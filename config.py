"""Настройки бота. Все значения берутся из переменных окружения / файла .env"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
import math
import re

from dotenv import load_dotenv
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
# Явные переменные окружения сервера важнее .env, как в актуальной dev.
load_dotenv(ROOT / ".env", override=False)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise RuntimeError(f"{name}: нужно true или false")
    return value in {"true", "1", "yes"}


def _number(name: str, default: str, minimum: float = 0) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        raise RuntimeError(f"{name}: нужно число") from None
    if not math.isfinite(value) or value < minimum:
        raise RuntimeError(f"{name}: недопустимое значение")
    return value


def _integer(name: str, default: str, minimum: int = 0) -> int:
    raw = os.getenv(name, default).strip()
    if not re.fullmatch(r"\d+", raw):
        raise RuntimeError(f"{name}: нужно целое число")
    value = int(raw)
    if value < minimum:
        raise RuntimeError(f"{name}: недопустимое значение")
    return value


def _path(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip()
    if not raw:
        raise RuntimeError(f"{name}: нужен путь к файлу")
    return str(ROOT / Path(raw).expanduser())


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная {name}. Укажите её в .env или окружении"
        )
    return value


def _api_url(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name}: нужен HTTP(S) URL без секретов, параметров и фрагмента")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(f"{name}: для удалённого сервиса нужен HTTPS")
    return value.rstrip("/")


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str


@dataclass(frozen=True)
class Config:
    # Telegram / LLM-провайдер (OpenAI-совместимый API).
    # LLM_* — основной xKiro; AI_* — явные переопределения совместимого API.
    bot_token: str
    # Google Sheets
    spreadsheet_id: str
    google_credentials_file: str
    # Автомойка
    timezone: str
    carwash_name: str
    carwash_address: str
    carwash_phone: str
    currency: str
    admin_chat_id: int | None
    # Поведение
    min_lead_minutes: int      # за сколько минут до начала слота запись уже закрыта
    catalog_cache_sec: int     # кэш прайса и тарифов
    slots_cache_sec: int       # кэш слотов и записей (для просмотра свободного времени)
    max_history: int           # сколько последних сообщений диалога помнить
    # Названия листов в таблице
    sheet_services: str
    sheet_tariffs: str
    sheet_slots: str
    sheet_bookings: str

    sheets_adaptive_enabled: bool = False
    sheets_discover_on_start: bool = False
    sheets_schema_check_interval_hours: float = 24.0
    sheets_ai_mapping_enabled: bool = False
    sheets_force_schema_refresh: bool = False
    sheets_mapping_file: str = str(ROOT / ".runtime/sheets-mapping.json")
    ai_enabled: bool = True
    ai_base_url: str = "https://api.xkiro.com/v1"
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout_sec: float = 60
    ai_request_budget_sec: float = 120
    ai_reasoning_effort: str = ""
    ai_temperature: float | None = 0.3
    ai_max_output_tokens: int = 1024
    ai_token_limit_parameter: str = "max_tokens"
    ai_max_tool_iterations: int = 6
    ai_max_tool_calls: int = 12
    ai_request_retries: int = 1
    ai_history_chars: int = 6000
    ai_recent_turns: int = 4
    ai_proofread_enabled: bool = False
    ai_session_ttl_sec: int = 3600
    ai_max_sessions: int = 1000
    sheets_ai_base_url: str = ""
    sheets_ai_api_key: str = ""
    sheets_ai_model: str = ""
    sheets_ai_json_mode: bool = True
    llm_fallbacks: tuple[ProviderConfig, ...] = ()

def _fallbacks(primary_url: str, primary_model: str) -> tuple[ProviderConfig, ...]:
    providers = []
    second_key = os.getenv("LLM_API_KEY_2", "").strip()
    if second_key:
        providers.append(ProviderConfig(primary_url, second_key, primary_model))
    for index in range(1, 4):
        prefix = f"LLM_FALLBACK_{index}_"
        values = {name: os.getenv(prefix + name, "").strip() for name in ("BASE_URL", "API_KEY", "MODEL")}
        if not any(values.values()):
            continue
        for name, value in values.items():
            if not value:
                raise RuntimeError(f"Не задана переменная {prefix + name}")
        providers.append(
            ProviderConfig(
                _api_url(prefix + "BASE_URL", values["BASE_URL"]), values["API_KEY"], values["MODEL"]
            )
        )
    return tuple(providers)


def _generation_settings() -> dict:
    effort = os.getenv("AI_REASONING_EFFORT", "").strip()
    if effort not in {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise RuntimeError("AI_REASONING_EFFORT: неизвестный уровень")
    if effort and not _bool("AI_REASONING_SUPPORTED"):
        raise RuntimeError("AI_REASONING_SUPPORTED=true нужен после проверки поддержки выбранной моделью")
    token_parameter = os.getenv("AI_TOKEN_LIMIT_PARAMETER", "max_tokens").strip()
    if token_parameter not in {"max_tokens", "max_completion_tokens"}:
        raise RuntimeError("AI_TOKEN_LIMIT_PARAMETER: нужен max_tokens или max_completion_tokens")
    retries = _integer("AI_REQUEST_RETRIES", "1")
    if retries > 2:
        raise RuntimeError("AI_REQUEST_RETRIES: допустимо от 0 до 2")
    temperature = _number("AI_TEMPERATURE", "0.3") if os.getenv("AI_TEMPERATURE", "0.3").strip() else None
    if temperature is not None and temperature > 2:
        raise RuntimeError("AI_TEMPERATURE: допустимо от 0 до 2 или пустое значение")
    timeout = _number("AI_TIMEOUT_SEC", "60", 0.1)
    return dict(
        ai_timeout_sec=timeout,
        ai_request_budget_sec=_number("AI_REQUEST_BUDGET_SEC", str(timeout * 2), 0.1),
        ai_reasoning_effort=effort,
        ai_temperature=temperature,
        ai_max_output_tokens=_integer("AI_MAX_OUTPUT_TOKENS", "1024", 512),
        ai_token_limit_parameter=token_parameter,
        ai_max_tool_iterations=_integer("AI_MAX_TOOL_ITERATIONS", "6", 1),
        ai_max_tool_calls=_integer("AI_MAX_TOOL_CALLS", "12", 1),
        ai_request_retries=retries,
        ai_history_chars=_integer("AI_HISTORY_CHARS", "6000", 1000),
        ai_recent_turns=_integer("AI_RECENT_TURNS", "4", 1),
        ai_proofread_enabled=_bool("AI_PROOFREAD_ENABLED"),
        ai_session_ttl_sec=_integer("AI_SESSION_TTL_SEC", "3600", 600),
        ai_max_sessions=_integer("AI_MAX_SESSIONS", "1000", 1),
    )


def _ai_settings(sheets_only: bool) -> dict:
    enabled = _bool("AI_ENABLED", True)
    primary_key = os.getenv("LLM_API_KEY", "").strip()
    primary_model = os.getenv("LLM_MODEL", "qwen/qwen3.8-max:free").strip()
    primary_url = _api_url("LLM_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.xkiro.com/v1").strip())
    base_url = _api_url("AI_BASE_URL", os.getenv("AI_BASE_URL", primary_url).strip())
    same_provider = urlsplit(base_url)[:2] == urlsplit(primary_url)[:2]
    api_key = os.getenv("AI_API_KEY", "").strip() or (primary_key if same_provider else "")
    model = os.getenv("AI_MODEL", "").strip() or (primary_model if same_provider else "")
    if enabled and not sheets_only:
        if not api_key:
            raise RuntimeError(
                "Для выбранного адреса нужен LLM_API_KEY или AI_API_KEY; настройте .env или окружение"
            )
        if not model:
            raise RuntimeError("Для выбранного адреса явно задайте AI_MODEL или LLM_MODEL")
    return dict(
        ai_enabled=enabled,
        ai_base_url=base_url,
        ai_api_key=api_key,
        ai_model=model,
        llm_fallbacks=_fallbacks(primary_url, model if same_provider else primary_model),
        **_generation_settings(),
    )


def _sheet_settings() -> dict:
    defaults = {"services": "Услуги", "tariffs": "Тарифы", "slots": "Слоты", "bookings": "Записи"}
    names = {
        f"sheet_{role}": os.getenv(f"SHEET_{role.upper()}", default).strip()
        for role, default in defaults.items()
    }
    if any(not name for name in names.values()) or len(set(names.values())) != len(names):
        raise RuntimeError("SHEET_*: нужны непустые разные имена ролей")
    mapping_url = os.getenv("SHEETS_AI_BASE_URL", "").strip()
    if mapping_url:
        mapping_url = _api_url("SHEETS_AI_BASE_URL", mapping_url)
    return dict(
        **names,
        sheets_adaptive_enabled=_bool("SHEETS_ADAPTIVE_ENABLED"),
        sheets_discover_on_start=_bool("SHEETS_DISCOVER_ON_START"),
        sheets_schema_check_interval_hours=_number("SHEETS_SCHEMA_CHECK_INTERVAL_HOURS", "24", 0.001),
        sheets_ai_mapping_enabled=_bool("SHEETS_AI_MAPPING_ENABLED"),
        sheets_force_schema_refresh=_bool("SHEETS_FORCE_SCHEMA_REFRESH"),
        sheets_mapping_file=_path("SHEETS_MAPPING_FILE", ".runtime/sheets-mapping.json"),
        sheets_ai_base_url=mapping_url,
        sheets_ai_api_key=os.getenv("SHEETS_AI_API_KEY", "").strip(),
        sheets_ai_model=os.getenv("SHEETS_AI_MODEL", "").strip(),
        sheets_ai_json_mode=_bool("SHEETS_AI_JSON_MODE", True),
    )


def load_config(sheets_only: bool = False) -> Config:
    """sheets_only=True — для настройки Sheets без запуска Telegram и AI."""
    bot_token = os.getenv("BOT_TOKEN", "").strip() if sheets_only else _required("BOT_TOKEN")
    admin = os.getenv("ADMIN_CHAT_ID", "").strip()
    if admin and not re.fullmatch(r"-?\d+", admin):
        raise RuntimeError("ADMIN_CHAT_ID: нужен целочисленный Telegram ID")
    return Config(
        bot_token=bot_token,
        spreadsheet_id=_required("SPREADSHEET_ID"),
        google_credentials_file=_path("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        carwash_name=os.getenv("CARWASH_NAME", "АвтоМойка").strip(),
        carwash_address=os.getenv("CARWASH_ADDRESS", "").strip(),
        carwash_phone=os.getenv("CARWASH_PHONE", "").strip(),
        currency=os.getenv("CURRENCY", "₽").strip(),
        admin_chat_id=int(admin) if admin else None,
        min_lead_minutes=_integer("MIN_LEAD_MINUTES", "30"),
        catalog_cache_sec=_integer("CATALOG_CACHE_SEC", "60"),
        slots_cache_sec=_integer("SLOTS_CACHE_SEC", "15"),
        max_history=_integer("MAX_HISTORY", "24", 1),
        **_sheet_settings(),
        **_ai_settings(sheets_only),
    )

"""Настройки бота. Все значения берутся из переменных окружения / файла .env"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import math
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
# .env — единственный источник настроек: значения из файла сильнее
# любых переменных окружения (системных, заданных в терминале и т.п.).
load_dotenv(ROOT / ".env", override=True)


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
            f"Не задана переменная окружения {name}. Заполните файл .env (см. .env.example)"
        )
    return value


@dataclass(frozen=True)
class Config:
    # Telegram / LLM-провайдер (OpenAI-совместимый API).
    # LLM_* — схема команды (по умолчанию xKiro); AI_*/OPENROUTER_* — совместимость.
    bot_token: str
    openrouter_api_key: str
    openrouter_model: str
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
    local_bot_id: str = ""
    bot_start_confirmed: bool = False
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    ai_enabled: bool = True
    ai_base_url: str = "https://openrouter.ai/api/v1"
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout_sec: float = 60
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

    @property
    def conversation_model(self) -> str:
        return self.ai_model or self.llm_model or self.openrouter_model


def load_config(sheets_only: bool = False) -> Config:
    """sheets_only=True — для setup_sheet.py, когда токены бота и LLM ещё не нужны."""
    need = (lambda name: os.getenv(name, "").strip()) if sheets_only else _required
    bot_token = need("BOT_TOKEN")
    admin = os.getenv("ADMIN_CHAT_ID", "").strip()

    if admin and not re.fullmatch(r"-?\d+", admin):
        raise RuntimeError("ADMIN_CHAT_ID: нужен целочисленный Telegram ID")
    sheet_names = [os.getenv("SHEET_" + role, default).strip() for role, default in
                   (("SERVICES", "Услуги"), ("TARIFFS", "Тарифы"), ("SLOTS", "Слоты"), ("BOOKINGS", "Записи"))]
    if any(not name for name in sheet_names) or len(set(sheet_names)) != len(sheet_names):
        raise RuntimeError("SHEET_*: нужны непустые разные имена ролей")
    enabled = _bool("AI_ENABLED", True)
    legacy_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    legacy_model = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free").strip()
    ai_key_explicit = os.getenv("AI_API_KEY", "").strip()
    llm_key = os.getenv("LLM_API_KEY", "").strip()
    llm_base_explicit = os.getenv("LLM_BASE_URL", "").strip()
    llm_base = (llm_base_explicit or "https://api.xkiro.com/v1").rstrip("/") or "https://api.xkiro.com/v1"
    llm_model_explicit = os.getenv("LLM_MODEL", "").strip()
    # Схема LLM_* активна, если явно задан хотя бы один её параметр.
    llm_scheme = bool(llm_key or llm_base_explicit or llm_model_explicit)
    # Дефолт модели xKiro применяется только внутри схемы LLM_* и не должен
    # затенять явный OPENROUTER_MODEL (легаси-контракт команды).
    llm_model = (llm_model_explicit or "qwen/qwen3.8-max:free") if llm_scheme else ""
    ai_model_raw = os.getenv("AI_MODEL", "").strip()
    ai_model = ai_model_raw or llm_model_explicit
    if enabled and not sheets_only and not (ai_key_explicit or llm_key or legacy_key):
        raise RuntimeError("Не задан AI_API_KEY, LLM_API_KEY или OPENROUTER_API_KEY; заполните .env")
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
    temperature = None if not os.getenv("AI_TEMPERATURE", "0.3").strip() else _number("AI_TEMPERATURE", "0.3")
    if temperature is not None and temperature > 2:
        raise RuntimeError("AI_TEMPERATURE: допустимо от 0 до 2 или пустое значение")
    ai_base_raw = os.getenv("AI_BASE_URL")
    base_url_explicit = (ai_base_raw or "").strip()
    if ai_base_raw is not None and not base_url_explicit:
        raise RuntimeError("AI_BASE_URL: пустое значение — удалите строку или укажите URL")
    if base_url_explicit:
        base_url = base_url_explicit
    elif llm_base_explicit or llm_key:
        # Активна схема LLM_* (адрес указан или задан её ключ): дефолт этой
        # схемы — xKiro. Раньше при заданном LLM_API_KEY без LLM_BASE_URL
        # запросы молча уходили на openrouter.ai с чужим для него ключом.
        base_url = llm_base
    else:
        base_url = "https://openrouter.ai/api/v1"
    # Ключ OPENROUTER_* подставляется только для адреса openrouter.ai:
    # кастомный провайдер не должен получать чужой для него легаси-ключ.
    if urlsplit(base_url).hostname == "openrouter.ai":
        api_key = ai_key_explicit or llm_key or legacy_key
    else:
        api_key = ai_key_explicit or llm_key
    mapping_url = os.getenv("SHEETS_AI_BASE_URL", "").strip()
    for name, url in (("AI_BASE_URL", base_url), ("SHEETS_AI_BASE_URL", mapping_url)):
        if name == "SHEETS_AI_BASE_URL" and not url:
            continue
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(f"{name}: нужен HTTP(S) URL без секретов, параметров и фрагмента")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError(f"{name}: для удалённого сервиса нужен HTTPS")
    if enabled and not sheets_only and urlsplit(base_url).hostname != "openrouter.ai":
        if not api_key or not (ai_model or llm_model):
            raise RuntimeError("Для другого адреса AI явно задайте AI_API_KEY (или LLM_API_KEY) и AI_MODEL (или LLM_MODEL)")
    return Config(
        bot_token=bot_token,
        llm_api_key=llm_key,
        llm_model=llm_model,
        llm_base_url=llm_base,
        openrouter_api_key=legacy_key,
        openrouter_model=legacy_model,
        spreadsheet_id=_required("SPREADSHEET_ID"),
        google_credentials_file=_path("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        carwash_name=os.getenv("CARWASH_NAME", "АвтоМойка").strip(),
        carwash_address=os.getenv("CARWASH_ADDRESS", "").strip(),
        carwash_phone=os.getenv("CARWASH_PHONE", "").strip(),
        currency=os.getenv("CURRENCY", "₽").strip(),
        admin_chat_id=int(admin) if admin.lstrip("-").isdigit() else None,
        min_lead_minutes=_integer("MIN_LEAD_MINUTES", "30"),
        catalog_cache_sec=_integer("CATALOG_CACHE_SEC", "60"),
        slots_cache_sec=_integer("SLOTS_CACHE_SEC", "15"),
        max_history=_integer("MAX_HISTORY", "24", 1),
        sheet_services=sheet_names[0],
        sheet_tariffs=sheet_names[1],
        sheet_slots=sheet_names[2],
        sheet_bookings=sheet_names[3],
        sheets_adaptive_enabled=_bool("SHEETS_ADAPTIVE_ENABLED"),
        sheets_discover_on_start=_bool("SHEETS_DISCOVER_ON_START"),
        sheets_schema_check_interval_hours=_number("SHEETS_SCHEMA_CHECK_INTERVAL_HOURS", "24", 0.001),
        sheets_ai_mapping_enabled=_bool("SHEETS_AI_MAPPING_ENABLED"),
        sheets_force_schema_refresh=_bool("SHEETS_FORCE_SCHEMA_REFRESH"),
        sheets_mapping_file=_path("SHEETS_MAPPING_FILE", ".runtime/sheets-mapping.json"),
        local_bot_id=os.getenv("LOCAL_BOT_ID", "").strip(),
        bot_start_confirmed=_bool("BOT_START_CONFIRMED"),
        ai_enabled=enabled,
        ai_base_url=base_url,
        ai_api_key=api_key,
        ai_model=ai_model_raw,
        ai_timeout_sec=_number("AI_TIMEOUT_SEC", "60", 0.1),
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
        sheets_ai_base_url=mapping_url,
        sheets_ai_api_key=os.getenv("SHEETS_AI_API_KEY", "").strip(),
        sheets_ai_model=os.getenv("SHEETS_AI_MODEL", "").strip(),
        sheets_ai_json_mode=_bool("SHEETS_AI_JSON_MODE", True),
    )

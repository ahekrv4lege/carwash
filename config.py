"""Настройки бота. Все значения берутся из переменных окружения / файла .env"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. Заполните файл .env (см. .env.example)"
        )
    return value


@dataclass(frozen=True)
class Config:
    # Telegram / OpenRouter
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


def load_config(sheets_only: bool = False) -> Config:
    """sheets_only=True — для setup_sheet.py, когда токены бота и OpenRouter ещё не нужны."""
    need = (lambda name: os.getenv(name, "").strip()) if sheets_only else _required
    admin = os.getenv("ADMIN_CHAT_ID", "").strip()

    return Config(
        bot_token=need("BOT_TOKEN"),
        openrouter_api_key=need("OPENROUTER_API_KEY"),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
        spreadsheet_id=_required("SPREADSHEET_ID"),
        google_credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json").strip(),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        carwash_name=os.getenv("CARWASH_NAME", "АвтоМойка").strip(),
        carwash_address=os.getenv("CARWASH_ADDRESS", "").strip(),
        carwash_phone=os.getenv("CARWASH_PHONE", "").strip(),
        currency=os.getenv("CURRENCY", "₽").strip(),
        admin_chat_id=int(admin) if admin.lstrip("-").isdigit() else None,
        min_lead_minutes=int(os.getenv("MIN_LEAD_MINUTES", "30")),
        catalog_cache_sec=int(os.getenv("CATALOG_CACHE_SEC", "60")),
        slots_cache_sec=int(os.getenv("SLOTS_CACHE_SEC", "15")),
        max_history=int(os.getenv("MAX_HISTORY", "24")),
        sheet_services=os.getenv("SHEET_SERVICES", "Услуги").strip(),
        sheet_tariffs=os.getenv("SHEET_TARIFFS", "Тарифы").strip(),
        sheet_slots=os.getenv("SHEET_SLOTS", "Слоты").strip(),
        sheet_bookings=os.getenv("SHEET_BOOKINGS", "Записи").strip(),
    )

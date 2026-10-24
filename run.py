"""Единая точка запуска Windows/Linux; проверка без подключения: --check."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import venv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = ("aiogram", "gspread", "google.auth", "openai", "dotenv", "tzdata")


class StartupError(RuntimeError):
    pass


class InstanceLock:
    """Блокировка ОС остаётся захваченной до завершения процесса."""
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, os.SEEK_END)
                if self.file.tell() == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise StartupError("Другой локальный экземпляр уже запущен") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def prepare_environment(no_install: bool = False) -> None:
    if sys.version_info < (3, 10):
        raise StartupError("Нужен Python 3.10 или новее")
    for name in ("bot.py", "config.py", "requirements.txt"):
        if not (ROOT / name).is_file():
            raise StartupError(f"Не найден обязательный файл: {name}")
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if Path(sys.prefix).resolve() != env.resolve():
        if not python.is_file():
            if no_install:
                raise StartupError("Нет .venv; запустите без --no-install")
            print("Создаю .venv...", flush=True)
            try:
                with InstanceLock(ROOT / ".runtime/bootstrap.lock"):
                    if not python.is_file():
                        venv.EnvBuilder(with_pip=True).create(env)
            except (OSError, subprocess.SubprocessError) as exc:
                raise StartupError("Не удалось создать .venv. Проверьте поддержку Python venv/ensurepip") from exc
        try:
            os.execv(str(python), [str(python), str(ROOT / "run.py"), *sys.argv[1:]])
        except OSError:
            raise StartupError("Не удалось запустить Python из .venv; проверьте совместимость и права окружения") from None
    signature = hashlib.sha256((ROOT / "requirements.txt").read_bytes() + sys.version.encode()).hexdigest()
    stamp = env / ".requirements.sha256"
    missing = [name for name in REQUIRED_PACKAGES if not has_module(name)]
    if missing or not stamp.is_file() or stamp.read_text().strip() != signature:
        if no_install:
            raise StartupError("Зависимости отсутствуют или изменились; запустите без --no-install")
        print("Устанавливаю зависимости requirements.txt...", flush=True)
        with InstanceLock(ROOT / ".runtime/bootstrap.lock"):
            subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                            "-r", str(ROOT / "requirements.txt")], check=True)
            subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
            if any(not has_module(name) for name in REQUIRED_PACKAGES):
                raise StartupError("После установки отсутствуют обязательные пакеты; проверьте requirements.txt")
            stamp.write_text(signature, encoding="utf-8")


def validate_config(sheets_only: bool = False):
    from config import load_config
    if not (ROOT / ".env").is_file():
        raise StartupError("Нет .env. Скопируйте .env.example в .env и заполните настройки")
    try:
        cfg = load_config(sheets_only=sheets_only)
    except (RuntimeError, ValueError) as exc:
        raise StartupError(str(exc)) from None
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise StartupError("TIMEZONE: неизвестный часовой пояс") from None
    if not sheets_only and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", cfg.bot_token):
        raise StartupError("BOT_TOKEN имеет неверный формат; значение скрыто")
    path = Path(cfg.google_credentials_file)
    if not path.is_file():
        raise StartupError("Не найден GOOGLE_CREDENTIALS_FILE; настройте ключ сервисного аккаунта")
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
        required = ("client_email", "private_key", "token_uri")
        if not isinstance(credentials, dict) or credentials.get("type") != "service_account" or not all(credentials.get(k) for k in required):
            raise ValueError
    except (ValueError, OSError):
        raise StartupError("GOOGLE_CREDENTIALS_FILE: нужен корректный JSON ключ сервисного аккаунта") from None
    if not sheets_only and cfg.ai_enabled and not cfg.conversation_model:
        raise StartupError("Не задан AI_MODEL или OPENROUTER_MODEL")
    return cfg


def validate_local_bot(cfg) -> None:
    if not cfg.local_bot_id or cfg.local_bot_id != cfg.bot_token.split(":", 1)[0]:
        raise StartupError("Для локального запуска создайте отдельного бота и задайте LOCAL_BOT_ID, "
                           "равный числовой части его BOT_TOKEN")
    if not cfg.bot_start_confirmed:
        raise StartupError("Подтвердите отдельный локальный токен: BOT_START_CONFIRMED=true. "
                           "Не используйте общий токен команды")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Проверить настройки без сетевых запросов")
    modes.add_argument("--setup-only", action="store_true", help="Только подготовить .venv и зависимости")
    parser.add_argument("--no-install", action="store_true", help="Запретить установку зависимостей")
    modes.add_argument("--refresh-schema", action="store_true", help="Принудительно перечитать структуру без запуска бота")
    modes.add_argument("--validate-mappings", action="store_true", help="Проверить вручную исправленную конфигурацию без записи в Sheets")
    modes.add_argument("--suggest-mappings", action="store_true", help="Сохранить AI-подсказки по заголовкам; без применения")
    args = parser.parse_args()
    try:
        os.chdir(ROOT)
        prepare_environment(args.no_install)
        if args.setup_only:
            print("Виртуальное окружение и зависимости готовы")
            return 0
        sheets_only = args.refresh_schema or args.validate_mappings or args.suggest_mappings
        cfg = validate_config(sheets_only=sheets_only)
        if not sheets_only:
            validate_local_bot(cfg)
        # Общая блокировка по bot id защищает также копии проекта на этом компьютере.
        lock_key = cfg.bot_token.split(":", 1)[0] if not sheets_only else hashlib.sha256(cfg.spreadsheet_id.encode()).hexdigest()[:24]
        import tempfile
        lock_path = Path(tempfile.gettempdir()) / "carwash-locks" / f"{lock_key}.lock"
        with InstanceLock(lock_path):
            if sheets_only:
                from sheets import Store
                store = Store(cfg)
                document = store.refresh_schema(force=args.refresh_schema, validate=args.validate_mappings)
                if args.suggest_mappings:
                    from mapping_ai import suggest_mappings
                    if not store._adaptive:
                        raise StartupError("Включите SHEETS_ADAPTIVE_ENABLED")
                    suggest_mappings(store._adaptive)
                    print("Разбор подсказок завершён; результат применяется только вручную")
                    return 0
                print(f"Структура: {document['validation_status']}; запись разрешена: {document['writes_enabled']}")
                print("Конфигурация сохранена локально; Google Sheets не изменялась")
                return 0 if document["validation_status"] == "valid" else 2
            if args.check:
                print("Локальная конфигурация корректна; сетевые подключения не выполнялись")
                return 0
            import asyncio
            from bot import main as bot_main
            asyncio.run(bot_main())
        return 0
    except KeyboardInterrupt:
        print("Остановлено пользователем")
        return 0
    except Exception as exc:
        # Не печатаем содержимое исключений поставщиков: они могут содержать секреты.
        from adaptive import MappingError
        text = str(exc) if isinstance(exc, (StartupError, MappingError)) else f"{type(exc).__name__}: проверьте настройки, зависимости и доступ к сервисам"
        print(f"Ошибка запуска: {text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

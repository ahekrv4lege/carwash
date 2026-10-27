"""Единая точка запуска Windows/Linux; проверка без подключения: --check."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import venv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = ("aiogram", "aiohttp_socks", "gspread", "google.auth", "openai", "dotenv", "tzdata")
RESTART_DELAY_SEC = 5
WIN_CTRL_C_EXIT_CODE = 0xC000013A


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
    environment_path = ROOT / ".venv"
    python_path = environment_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if Path(sys.prefix).resolve() != environment_path.resolve():
        if not python_path.is_file():
            if no_install:
                raise StartupError("Нет .venv; запустите без --no-install")
            print("Создаю .venv...", flush=True)
            try:
                with InstanceLock(ROOT / ".runtime/bootstrap.lock"):
                    if not python_path.is_file():
                        venv.EnvBuilder(with_pip=True).create(environment_path)
            except (OSError, subprocess.SubprocessError) as error:
                raise StartupError("Не удалось создать .venv. Проверьте поддержку Python venv/ensurepip") from error
        try:
            os.execv(str(python_path), [str(python_path), str(ROOT / "run.py"), *sys.argv[1:]])
        except OSError:
            raise StartupError("Не удалось запустить Python из .venv; проверьте совместимость и права окружения") from None
    signature = hashlib.sha256((ROOT / "requirements.txt").read_bytes() + sys.version.encode()).hexdigest()
    stamp = environment_path / ".requirements.sha256"
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
    try:
        config = load_config(sheets_only=sheets_only)
    except (RuntimeError, ValueError) as error:
        prefix = "Нет .env; настройте переменные окружения. " if not (ROOT / ".env").is_file() else ""
        raise StartupError(prefix + str(error)) from None
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise StartupError("TIMEZONE: неизвестный часовой пояс") from None
    if not sheets_only and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", config.bot_token):
        raise StartupError("BOT_TOKEN имеет неверный формат; значение скрыто")
    path = Path(config.google_credentials_file)
    if not path.is_file():
        raise StartupError("Не найден GOOGLE_CREDENTIALS_FILE; настройте ключ сервисного аккаунта")
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
        required = ("client_email", "private_key", "token_uri")
        if not isinstance(credentials, dict) or credentials.get("type") != "service_account" or not all(credentials.get(field_name) for field_name in required):
            raise ValueError
    except (ValueError, OSError):
        raise StartupError("GOOGLE_CREDENTIALS_FILE: нужен корректный JSON ключ сервисного аккаунта") from None
    if not sheets_only and config.ai_enabled and not config.ai_model:
        raise StartupError("Не задан LLM_MODEL или AI_MODEL")
    return config


def run_forever(output=None) -> None:
    print("Запускаю бота. Остановка — Ctrl+C.", flush=True)
    process = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if process is not None and process.poll() is None:
            try:
                # Ctrl+C уже доставлен общей Windows-консоли; Popen не принимает SIGINT.
                if os.name == "nt" and signum == signal.SIGINT:
                    return
                process.send_signal(signum)
            except ProcessLookupError:
                pass

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping:
            streams = {"stdout": output, "stderr": output} if output is not None else {}
            process = subprocess.Popen([sys.executable, str(ROOT / "bot.py")], cwd=ROOT, **streams)
            try:
                code = process.wait()
            except KeyboardInterrupt:
                stop(signal.SIGINT, None)
                process.wait()
                return
            if stopping or code in (0, WIN_CTRL_C_EXIT_CODE):
                return
            print(f"Бот завершился с кодом {code}. Перезапуск через {RESTART_DELAY_SEC} с.", flush=True)
            time.sleep(RESTART_DELAY_SEC)
    except KeyboardInterrupt:
        return
    finally:
        signal.signal(signal.SIGTERM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Проверить настройки без сетевых запросов")
    modes.add_argument("--setup-only", action="store_true", help="Только подготовить .venv и зависимости")
    modes.add_argument("--doctor", action="store_true", help="Проверить Telegram и Sheets; без сообщений, записей и платного AI")
    for mode, description in (("host", "Фоновый хостинг с автозапуском"), ("stop", "Остановить и выключить автозапуск"),
                              ("status", "Состояние фонового бота"), ("logs", "Последние сообщения журнала")):
        modes.add_argument("--" + mode, action="store_true", help=description)
    parser.add_argument("--host-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--profile", choices=("cheap", "demo"), help="Профиль модели; ключи остаются в .env")
    parser.add_argument("--no-install", action="store_true", help="Запретить установку зависимостей")
    modes.add_argument("--refresh-schema", action="store_true", help="Принудительно перечитать структуру без запуска бота")
    modes.add_argument("--validate-mappings", action="store_true", help="Проверить вручную исправленную конфигурацию без записи в Sheets")
    modes.add_argument("--suggest-mappings", action="store_true", help="Сохранить AI-подсказки по заголовкам; без применения")
    args = parser.parse_args()
    try:
        os.chdir(ROOT)
        if args.host_worker:
            folder = ROOT / ".runtime"
            folder.mkdir(exist_ok=True)
            path = folder / "host.log"
            if path.exists() and path.stat().st_size > 5_000_000:
                path.replace(folder / "host.previous.log")
            sys.stdout = sys.stderr = path.open("a", encoding="utf-8", buffering=1)
        prepare_environment(args.no_install)
        if args.profile:
            from dotenv import dotenv_values
            path = ROOT / "profiles" / (args.profile + ".env")
            if not path.is_file():
                raise StartupError("Профиль модели не найден")
            allowed = {"LLM_BASE_URL", "LLM_MODEL", "LLM_MAX_INPUT_RUB", "LLM_MAX_OUTPUT_RUB", "AI_TIMEOUT_SEC",
                       "AI_REQUEST_BUDGET_SEC", "AI_REQUEST_RETRIES", "AI_MAX_OUTPUT_TOKENS", "AI_DIRECT_ACTIONS"}
            values = dotenv_values(path)
            if values.keys() - allowed or any(value is None for value in values.values()):
                raise StartupError("В профиле есть недопустимые настройки")
            os.environ.update(values)
        if args.setup_only:
            print("Виртуальное окружение и зависимости готовы")
            return 0
        sheets_only = args.refresh_schema or args.validate_mappings or args.suggest_mappings
        config = validate_config(sheets_only=sheets_only)
        if args.doctor or args.host:
            from healthcheck import diagnose
            if not asyncio.run(diagnose(config)):
                raise StartupError("Подключения не готовы; исправьте указанную выше ошибку")
            if args.doctor:
                return 0
        if any(getattr(args, action) for action in ("host", "stop", "status", "logs")):
            from hosting import manage
            action = next(action for action in ("host", "stop", "status", "logs") if getattr(args, action))
            return manage(action, ROOT, config.bot_token.split(":", 1)[0], profile=args.profile)
        # Общая блокировка по bot id защищает также копии проекта на этом компьютере.
        lock_key = config.bot_token.split(":", 1)[0] if not sheets_only else hashlib.sha256(config.spreadsheet_id.encode()).hexdigest()[:24]
        import tempfile
        lock_path = Path(tempfile.gettempdir()) / "carwash-locks" / f"{lock_key}.lock"
        with InstanceLock(lock_path):
            if sheets_only:
                from sheets import Store
                store = Store(config)
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
            run_forever(sys.stdout if args.host_worker else None)
        return 0
    except KeyboardInterrupt:
        print("Остановлено пользователем")
        return 0
    except Exception as error:
        # Не печатаем содержимое исключений поставщиков: они могут содержать секреты.
        from adaptive import MappingError
        text = str(error) if isinstance(error, (StartupError, MappingError)) else f"{type(error).__name__}: проверьте настройки, зависимости и доступ к сервисам"
        print(f"Ошибка запуска: {text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

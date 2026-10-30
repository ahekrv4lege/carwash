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
import shutil
import subprocess
import sys
import time
import venv
import uuid
from dataclasses import replace
import xml.etree.ElementTree as ET
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


def prepare_new_table(config, store_factory=None):
    from config import table_mapping_path
    from sheets import Store
    target = Path(table_mapping_path(config.spreadsheet_id))
    old_path = Path(config.sheets_mapping_file)
    old_journal = old_path.with_name(old_path.stem + "-operations.json")
    if not target.exists() and old_path != target and old_path.exists():
        try:
            old_document = json.loads(old_path.read_text(encoding="utf-8"))
            old_id = old_document["spreadsheet_id"]
        except (OSError, ValueError, KeyError, TypeError):
            if old_journal.exists():
                raise StartupError("Нельзя определить таблицу старой карты с журналом операций; проверьте её вручную") from None
            old_id = None
        if old_id == config.spreadsheet_id:
            target = old_path
    existing = target.exists()
    original = None
    if existing:
        try:
            original = target.read_bytes()
            if json.loads(original)["spreadsheet_id"] != config.spreadsheet_id:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise StartupError("Существующая карта повреждена или принадлежит другой таблице; проверьте её вручную") from None
    candidate = target.with_name(f"{target.stem}-candidate-{uuid.uuid4().hex}{target.suffix}")
    temporary = replace(config, sheets_adaptive_enabled=True, sheets_mapping_file=str(candidate))
    store = (store_factory or Store)(temporary)
    try:
        document = store.refresh_schema(force=True)
        if document["validation_status"] != "valid" or not document["writes_enabled"]:
            raise StartupError(f"Таблица требует настройки; карта: {candidate}")
        with store.fresh_tables():
            store.get_catalog(fresh=True)
            store._get_slots()
            store._get_bookings()
        if existing:
            if target.read_bytes() != original:
                raise StartupError("Карта изменилась во время проверки; повторите запуск")
            os.replace(candidate, target)
        else:
            # Не заменяем карту, если её успел создать другой процесс.
            try:
                os.link(candidate, target)
            except FileExistsError:
                raise StartupError(f"Карта уже появилась; проверьте её: {target}") from None
            candidate.unlink()
    except Exception:
        if candidate.exists():
            print(f"Карта для проверки сохранена: {candidate}", file=sys.stderr)
        raise
    return str(target)


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


def systemd_quote(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise RuntimeError("Путь или настройка автозапуска содержит перевод строки")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def linux_unit(root: Path, proxy_environment: dict) -> str:
    directory = str(root).replace("%", "%%")
    if "\n" in directory or "\r" in directory:
        raise RuntimeError("Путь автозапуска содержит перевод строки")
    environment = "\n".join("Environment=" + systemd_quote(key + "=" + value)
                            for key, value in proxy_environment.items())
    return f"""[Unit]
Description=Carwash Telegram bot
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={directory}
ExecStart={systemd_quote(root / '.venv/bin/python')} {systemd_quote(root / 'run.py')} --no-install
Environment=PYTHONUNBUFFERED=1
{environment}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
"""


def windows_task(root: Path, username: str) -> str:
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def add(parent, tag, value=None):
        element = ET.SubElement(parent, "{" + namespace + "}" + tag)
        element.text = value
        return element
    task = ET.Element("{" + namespace + "}Task", version="1.2")
    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "UserId", username)
    add(trigger, "Enabled", "true")
    principal = add(add(task, "Principals"), "Principal")
    principal.set("id", "Author")
    add(principal, "UserId", username)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "true"),
                       ("ExecutionTimeLimit", "PT0S")):
        add(settings, key, value)
    restart = add(settings, "RestartOnFailure")
    add(restart, "Interval", "PT1M")
    add(restart, "Count", "999")
    actions = add(task, "Actions")
    actions.set("Context", "Author")
    action = add(actions, "Exec")
    add(action, "Command", str(root / ".venv/Scripts/pythonw.exe"))
    add(action, "Arguments", subprocess.list2cmdline([str(root / "run.py"), "--no-install", "--host-worker"]))
    add(action, "WorkingDirectory", str(root))
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def manage_host(action: str, root: Path, bot_id: str) -> int:
    if not re.fullmatch(r"\d+", bot_id):
        raise RuntimeError("Для автозапуска нужен корректный BOT_TOKEN")
    name = "carwash-" + bot_id
    if sys.platform.startswith("linux"):
        if not shutil.which("systemctl"):
            raise RuntimeError("Для фонового хостинга нужен systemd; обычный запуск: ./start.sh")
        unit = name + ".service"
        if action == "host":
            folder = Path.home() / ".config/systemd/user"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / unit
            accepted = {"WorkingDirectory=" + str(root).replace("%", "%%"), "WorkingDirectory=" + systemd_quote(root)}
            if path.exists() and not accepted.intersection(path.read_text().splitlines()):
                raise RuntimeError("Автозапуск этого бота уже привязан к другой папке; сначала остановите прежнюю копию")
            variables = {key: value for key, value in os.environ.items()
                         if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
            path.write_text(linux_unit(root, variables), encoding="utf-8")
            path.chmod(0o600)
            if shutil.which("systemd-analyze"):
                subprocess.run(["systemd-analyze", "--user", "verify", str(path)], check=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", unit], check=True)
            subprocess.run(["systemctl", "--user", "restart", unit], check=True)
            subprocess.run(["systemctl", "--user", "is-active", unit], check=True)
            print("Фоновая служба включена. Автозапуск — при входе в систему.")
            print("Состояние: ./start.sh --status; журнал: ./start.sh --logs; остановка: ./start.sh --stop")
            return 0
        if action == "stop":
            return subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False).returncode
        if action == "logs":
            return subprocess.run(["journalctl", "--user", "-u", unit, "-n", "60", "--no-pager"], check=False).returncode
        return subprocess.run(["systemctl", "--user", "status", unit, "--no-pager"], check=False).returncode
    if os.name == "nt":
        if action == "host":
            username = subprocess.check_output(["whoami"], text=True).strip()
            existing = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"],
                                      capture_output=True, text=True, check=False)
            replace = []
            if existing.returncode == 0:
                try:
                    task = ET.fromstring(existing.stdout)
                    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                    directory = task.findtext("./t:Actions/t:Exec/t:WorkingDirectory", namespaces=namespace)
                    command = task.findtext("./t:Actions/t:Exec/t:Command", namespaces=namespace)
                    if not directory or not command or Path(directory).resolve() != root.resolve() or Path(command).resolve() != (root / ".venv/Scripts/pythonw.exe").resolve():
                        raise ValueError
                except (ET.ParseError, ValueError, OSError):
                    raise RuntimeError("Автозапуск этого бота уже привязан к другой папке или его нельзя проверить") from None
                replace = ["/F"]
            folder = root / ".runtime"
            folder.mkdir(exist_ok=True)
            path = folder / "host-task.xml"
            path.write_text(windows_task(root, username), encoding="utf-16")
            subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(path), *replace], check=True)
            subprocess.run(["schtasks", "/Run", "/TN", name], check=True)
            print("Задача запущена в фоне и будет запускаться при входе в Windows.")
            print("Журнал: start.bat --logs; остановка: start.bat --stop")
            return 0
        if action == "stop":
            subprocess.run(["schtasks", "/End", "/TN", name], check=False)
            return subprocess.run(["schtasks", "/Change", "/TN", name, "/DISABLE"], check=False).returncode
        if action == "logs":
            path = root / ".runtime/host.log"
            print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]) if path.exists() else "Журнал ещё не создан")
            return 0
        return subprocess.run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"], check=False).returncode
    raise RuntimeError("Фоновый хостинг поддержан для Linux с systemd и Windows; обычный запуск: python run.py")


async def diagnose(config) -> bool:
    from aiogram import Bot
    from bot import telegram_session
    from providers import api_error_label
    from sheets import Store

    ok = True
    bot = Bot(config.bot_token, session=telegram_session())
    try:
        identity = await bot.get_me(request_timeout=12)
        webhook = await bot.get_webhook_info(request_timeout=12)
        print(f"Telegram: @{identity.username}; авторизация работает; ожидает сообщений: {webhook.pending_update_count}")
        if webhook.url:
            print("При запуске polling бот переключит существующий webhook.")
    except Exception as error:
        ok = False
        print("Telegram недоступен: " + api_error_label(error) + ". Проверьте токен, сеть и TELEGRAM_PROXY.")
    finally:
        await bot.session.close()
    try:
        store = Store(config)
        catalog = await asyncio.wait_for(asyncio.to_thread(store.price_list), timeout=30)
        slots = await asyncio.wait_for(asyncio.to_thread(store.free_slots), timeout=30)
        print(f"Google Sheets: прайс прочитан; услуг: {len(catalog.get('services', []))}; тарифов: {len(catalog.get('tariffs', []))}; дней в ответе слотов: {len(slots.get('days', []))}")
    except Exception as error:
        ok = False
        print("Google Sheets недоступна: " + api_error_label(error) + ". Проверьте credentials.json, доступ и названия листов.")
    print("AI: " + config.ai_model + "; платный запрос не выполнялся.")
    return ok


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
    # Совместимость с уже установленными службами; настройки теперь берутся из .env.
    parser.add_argument("--profile", choices=("cheap", "demo"), help=argparse.SUPPRESS)
    parser.add_argument("--no-install", action="store_true", help="Запретить установку зависимостей")
    parser.add_argument("--new-table", action="store_true", help="Проверить новую таблицу и запустить бота с её отдельной картой")
    modes.add_argument("--refresh-schema", action="store_true", help="Принудительно перечитать структуру без запуска бота")
    modes.add_argument("--validate-mappings", action="store_true", help="Проверить вручную исправленную конфигурацию без записи в Sheets")
    args = parser.parse_args()
    try:
        if args.new_table and (args.check or args.setup_only or args.doctor or args.host or args.stop or args.status or args.logs or args.refresh_schema or args.validate_mappings or args.host_worker):
            raise StartupError("--new-table используется только для обычного запуска бота")
        os.chdir(ROOT)
        if args.host_worker:
            folder = ROOT / ".runtime"
            folder.mkdir(exist_ok=True)
            path = folder / "host.log"
            if path.exists() and path.stat().st_size > 5_000_000:
                path.replace(folder / "host.previous.log")
            sys.stdout = sys.stderr = path.open("a", encoding="utf-8", buffering=1)
        prepare_environment(args.no_install)
        if args.setup_only:
            print("Виртуальное окружение и зависимости готовы")
            return 0
        sheets_only = args.refresh_schema or args.validate_mappings
        config = validate_config(sheets_only=sheets_only)
        if args.doctor or args.host:
            if not asyncio.run(diagnose(config)):
                raise StartupError("Подключения не готовы; исправьте указанную выше ошибку")
            if args.doctor:
                return 0
        if any(getattr(args, action) for action in ("host", "stop", "status", "logs")):
            action = next(action for action in ("host", "stop", "status", "logs") if getattr(args, action))
            return manage_host(action, ROOT, config.bot_token.split(":", 1)[0])
        # Общая блокировка по bot id защищает также копии проекта на этом компьютере.
        lock_key = config.bot_token.split(":", 1)[0] if not sheets_only else hashlib.sha256(config.spreadsheet_id.encode()).hexdigest()[:24]
        import tempfile
        lock_path = Path(tempfile.gettempdir()) / "carwash-locks" / f"{lock_key}.lock"
        with InstanceLock(lock_path):
            if sheets_only:
                from sheets import Store
                store = Store(config)
                document = store.refresh_schema(force=args.refresh_schema, validate=args.validate_mappings)
                print(f"Структура: {document['validation_status']}; запись разрешена: {document['writes_enabled']}")
                print("Конфигурация сохранена локально; Google Sheets не изменялась")
                return 0 if document["validation_status"] == "valid" else 2
            if args.check:
                print("Локальная конфигурация корректна; сетевые подключения не выполнялись")
                return 0
            if args.new_table:
                mapping = prepare_new_table(config)
                os.environ["SHEETS_ADAPTIVE_ENABLED"] = "true"
                os.environ["SHEETS_MAPPING_FILE"] = mapping
                print(f"Таблица проверена; карта: {mapping}", flush=True)
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

"""Запуск бота с проверкой окружения и автоперезапуском при сбоях.

Проверяет версию Python, файлы .env и credentials.json, установленные пакеты
и обязательные настройки, затем запускает bot.py; если тот упал — перезапускает
его через несколько секунд. Остановка — Ctrl+C. Запуск: python run.py (или start.bat).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOT_SCRIPT = ROOT / "bot.py"
RESTART_DELAY_SEC = 5
WIN_CTRL_C_EXIT_CODE = 0xC000013A
REQUIRED_PACKAGES = ("aiogram", "gspread", "google.auth", "openai", "dotenv")


def fail(text: str) -> None:
    print(f"\n✖ {text}")
    sys.exit(1)


def check_python() -> None:
    if sys.version_info < (3, 10):
        fail(f"Нужен Python 3.10+, у вас {sys.version.split()[0]}")


def check_files() -> None:
    if not BOT_SCRIPT.is_file():
        fail(f"Не найден {BOT_SCRIPT.name} — запускайте скрипт из папки проекта")
    if not (ROOT / ".env").is_file():
        fail("Нет файла .env. Скопируйте .env.example в .env и заполните значения")


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def check_packages() -> None:
    missing = [p for p in REQUIRED_PACKAGES if not _has_module(p)]
    if missing:
        fail(
            "Не установлены зависимости. Выполните:\n"
            "  python3 -m venv .venv\n"
            "  .venv/bin/pip install -r requirements.txt   (Windows: .venv\\Scripts\\pip)"
        )


def _venv_python() -> Path | None:
    """Путь до интерпретатора из .venv, если окружение создано."""
    subdir = "Scripts" if os.name == "nt" else "bin"
    exe = ROOT / ".venv" / subdir / ("python.exe" if os.name == "nt" else "python")
    return exe if exe.is_file() else None


def use_venv() -> None:
    """Если рядом есть .venv — перезапускаемся его интерпретатором."""
    vpy = _venv_python()
    if vpy is None or Path(sys.prefix).resolve() == (ROOT / ".venv").resolve():
        return
    try:
        code = subprocess.call([str(vpy), str(Path(__file__).resolve())], cwd=ROOT)
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


def check_config() -> None:
    from config import load_config

    try:
        cfg = load_config()
    except RuntimeError as e:
        fail(str(e))
    cred = ROOT / cfg.google_credentials_file
    if not cred.is_file():
        fail(
            f"Не найден файл {cfg.google_credentials_file} с ключами Google "
            "(как его получить — README, раздел «Google Sheets API»)"
        )


def run_forever() -> None:
    print("\nЗапускаю бота. Остановка — Ctrl+C.\n")
    while True:
        try:
            code = subprocess.call([sys.executable, str(BOT_SCRIPT)], cwd=ROOT)
        except KeyboardInterrupt:
            return
        if code in (0, WIN_CTRL_C_EXIT_CODE):
            print("\nБот остановлен.")
            return
        print(f"\n⚠ Бот завершился с кодом {code}. "
              f"Перезапуск через {RESTART_DELAY_SEC} с (Ctrl+C — выход)…")
        try:
            time.sleep(RESTART_DELAY_SEC)
        except KeyboardInterrupt:
            return


def main() -> None:
    print("── Бот-администратор автомойки ──")
    use_venv()
    check_python()
    check_files()
    check_packages()
    check_config()
    try:
        run_forever()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
    print("Готово.")


if __name__ == "__main__":
    main()

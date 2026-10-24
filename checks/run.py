"""Проверки Carwash: без флагов — изолированные, --live — настоящие API."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import sys
import traceback
import unittest
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parent
PROJECT = FOLDER.parent


class Results(unittest.TextTestResult):
    def __init__(self, *arguments, **options):
        super().__init__(*arguments, **options)
        self.diagnostics = []

    def record_error(self, test, error, label):
        frames = traceback.extract_tb(error[2])
        frame = next((frame for frame in reversed(frames) if Path(frame.filename).is_relative_to(PROJECT)), frames[-1])
        message = " ".join(str(error[1]).splitlines())[:350]
        self.diagnostics.append(f"{label} {test.id()} — {frame.filename}:{frame.lineno} — {error[0].__name__}: {message}")

    def addFailure(self, test, error):
        self.record_error(test, error, "FAIL")
        super().addFailure(test, error)

    def addError(self, test, error):
        self.record_error(test, error, "ERROR")
        super().addError(test, error)

    def addSubTest(self, test, subtest, error):
        if error is not None:
            label = "FAIL" if issubclass(error[0], test.failureException) else "ERROR"
            self.record_error(subtest, error, label)
        super().addSubTest(test, subtest, error)

    def printErrors(self):
        if self.dots or self.showAll:
            self.stream.writeln()
        if self.diagnostics:
            self.stream.writeln("\nНе прошли проверки:")
            for line in self.diagnostics:
                self.stream.writeln(line)


def block_network(*arguments, **options):
    raise AssertionError("Сеть запрещена в изолированном режиме")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Настоящие API: чтение Telegram/Sheets и AI-запросы с расходом квоты")
    parser.add_argument("-v", "--verbose", action="store_true", help="Показать каждую проверку")
    parser.add_argument("names", nargs="*", help="Необязательно: полное имя конкретного теста")
    args = parser.parse_args()
    if args.live and args.names:
        parser.error("Имена отдельных тестов поддерживаются только в изолированном режиме")
    python = PROJECT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file() and Path(sys.prefix).resolve() != python.parent.parent.resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(PROJECT), str(FOLDER)]
    with ExitStack() as context:
        if args.live:
            import live
            try:
                suite = live.suite()
            except (RuntimeError, ValueError, OSError) as error:
                parser.exit(2, f"Ошибка настроек live: {error}\n")
            print("LIVE: реальные Telegram/Sheets; один AI-запрос на каждый настроенный ключ. Без сообщений и записи в Sheets.", flush=True)
        else:
            safe = {name: value for name, value in os.environ.items() if name in {
                "PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL",
                "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PYTHONPATH",
            }}
            safe.update(PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
            context.enter_context(patch.dict(os.environ, safe, clear=True))
            context.enter_context(patch("dotenv.load_dotenv", return_value=False))
            context.enter_context(patch.object(socket.socket, "connect", block_network))
            context.enter_context(patch.object(socket.socket, "connect_ex", block_network))
            context.enter_context(patch.object(socket, "getaddrinfo", block_network))
            context.enter_context(patch.object(socket.socket, "sendto", block_network))
            loader = unittest.defaultTestLoader
            suite = loader.loadTestsFromNames(args.names) if args.names else loader.discover(str(FOLDER / "tests"), top_level_dir=str(FOLDER))
            print("ISOLATED: настоящие .env и ключи не загружаются; сеть запрещена.", flush=True)
        if suite.countTestCases() == 0:
            parser.error("Не найдено ни одной проверки")
        result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, resultclass=Results, buffer=True).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

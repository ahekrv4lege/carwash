"""Резервные копии памяти об автомобилях клиентов (SQLite).

Примеры (из папки проекта):
  .venv/bin/python vehicle_backup.py backup --dir /root/carwash-backups --keep 14
  .venv/bin/python vehicle_backup.py verify /root/carwash-backups/customer_vehicles-....sqlite3
  .venv/bin/python vehicle_backup.py restore FILE --target .runtime/customer_vehicles.sqlite3

Копия делается через backup API SQLite: корректна, даже пока бот держит WAL открытым.
Восстановление нужно выполнять при остановленном боте. Существующий файл сохраняется рядом
с именем *.before-restore-<время>, перезаписи без --force не происходит.
Файлы не печатают содержимого клиентов: только количество записей.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from vehicles import SCHEMA_VERSION, VehicleDatabase

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / ".runtime" / "customer_vehicles.sqlite3"
BACKUP_PREFIX = "customer_vehicles-"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _restrict(path: Path) -> None:
    """0o600 на POSIX; на Windows chmod умеет только read-only (и он сломал бы перезапись копии)."""
    if os.name != "nt":
        os.chmod(path, 0o600)


def backup(source: Path, directory: Path, keep: int) -> Path:
    if not Path(source).is_file():
        raise SystemExit(f"Нет базы памяти: {source}")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{BACKUP_PREFIX}{_stamp()}.sqlite3"
    partial = destination.with_suffix(".partial")
    db = VehicleDatabase(Path(source))
    try:
        db.backup_to(partial)
    finally:
        db.close()  # иначе на Windows база остаётся заблокированной открытым WAL
    _restrict(partial)
    problems = _integrity_problems(partial)
    if problems:
        partial.unlink(missing_ok=True)
        raise SystemExit("Копия не прошла проверку: " + "; ".join(problems))
    os.replace(partial, destination)
    _prune(directory, keep)
    return destination


def _prune(directory: Path, keep: int) -> None:
    if keep < 1:
        raise SystemExit("--keep должен быть не меньше 1")
    backups = sorted(directory.glob(f"{BACKUP_PREFIX}*.sqlite3"))
    for old in backups[:-keep]:
        old.unlink()


def _integrity_problems(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        problems = []
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            problems.append("integrity_check: " + str(result))
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            problems.append(f"версия схемы {version}, ожидается {SCHEMA_VERSION}")
        return problems
    finally:
        connection.close()


def verify(path: Path) -> dict:
    if not Path(path).is_file():
        raise SystemExit(f"Нет файла: {path}")
    problems = _integrity_problems(Path(path))
    if problems:
        raise SystemExit("Проверка не пройдена: " + "; ".join(problems))
    connection = sqlite3.connect(path)
    try:
        active = connection.execute("SELECT COUNT(*) FROM vehicles WHERE status = 'active'").fetchone()[0]
        customers = connection.execute("SELECT COUNT(DISTINCT telegram_id) FROM vehicles WHERE status = 'active'").fetchone()[0]
    finally:
        connection.close()
    return {"ok": True, "active_vehicles": active, "customers": customers}


def restore(source: Path, target: Path, force: bool) -> None:
    verify(source)
    if target.exists() and not force:
        raise SystemExit(f"Файл уже существует: {target}. Для замены используйте --force (бот должен быть остановлен).")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.rename(target.with_name(f"{target.name}.before-restore-{_stamp()}"))
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    partial = target.with_suffix(".restoring")
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(partial)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    _restrict(partial)
    os.replace(partial, target)
    print(f"Восстановлено: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    back = commands.add_parser("backup", help="Создать согласованную копию")
    back.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    back.add_argument("--dir", type=Path, required=True)
    back.add_argument("--keep", type=int, default=14)
    check = commands.add_parser("verify", help="Проверить копию без вывода данных клиентов")
    check.add_argument("file", type=Path)
    back_restore = commands.add_parser("restore", help="Восстановить из копии")
    back_restore.add_argument("file", type=Path)
    back_restore.add_argument("--target", type=Path, default=DEFAULT_SOURCE)
    back_restore.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "backup":
        if not args.source.is_file():
            print(f"Базы памяти пока нет, копировать нечего: {args.source}")
            return 0
        print(f"Копия: {backup(args.source, args.dir, args.keep)}")
    elif args.command == "verify":
        print(verify(args.file))
    else:
        restore(args.file, args.target, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())

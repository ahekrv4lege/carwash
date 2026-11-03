"""Один критический участок записи для процессов с общей SQLite базой."""
from pathlib import Path
import sqlite3
import threading

_states = {}
_guard = threading.Lock()


class BookingLock:
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        with _guard:
            self.lock, self.local = _states.setdefault(self.path, (threading.RLock(), threading.local()))

    def __enter__(self):
        self.lock.acquire()
        try:
            depth = getattr(self.local, "depth", 0)
            if not depth:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
                try:
                    db.execute("CREATE TABLE IF NOT EXISTS coordinator (id INTEGER PRIMARY KEY)")
                    db.execute("BEGIN IMMEDIATE")
                except BaseException:
                    db.close()
                    raise
                self.local.db = db
            self.local.depth = depth + 1
            return self
        except BaseException:
            self.lock.release()
            raise

    def __exit__(self, *error):
        try:
            self.local.depth -= 1
            if not self.local.depth:
                try:
                    self.local.db.rollback()
                finally:
                    self.local.db.close()
                    del self.local.db
        finally:
            self.lock.release()

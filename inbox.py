"""Долговечная очередь обычного текста и ответов с подтверждением Telegram."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import threading


class Inbox:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        self.lock.acquire()
        db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists() or not self.path.stat().st_size
            db = sqlite3.connect(self.path, timeout=10)
            if new:db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, chat INTEGER NOT NULL, "
                       "message INTEGER NOT NULL, received REAL NOT NULL, user TEXT NOT NULL, text TEXT NOT NULL, "
                       "batch TEXT, answer TEXT, payload TEXT, delivered INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE INDEX IF NOT EXISTS inbox_delivery ON inbox(delivered,received)")
            os.chmod(self.path, 0o600)
            yield db
            db.commit()
        finally:
            if db is not None:db.close()
            self.lock.release()

    def accept(self, chat, message, user, text):
        key = f"{chat}:{message}"
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO inbox(key,chat,message,received,user,text) VALUES (?,?,?,?,?,?)",
                       (key, chat, message, time.time(), json.dumps(user, ensure_ascii=False), text))
            db.execute("DELETE FROM inbox WHERE delivered=1 AND received < ?", (time.time()-7*86400,))
        return key

    def delivered(self, keys):
        with self.db() as db:
            return all(db.execute("SELECT delivered FROM inbox WHERE key=?", (key,)).fetchone() == (1,) for key in keys)

    def prepare(self, keys, answer, payload):
        with self.db() as db:
            db.executemany("UPDATE inbox SET batch=?,answer=?,payload=? WHERE key=? AND delivered=0",
                           [(keys[-1], answer, json.dumps(payload, ensure_ascii=False), key) for key in keys])

    def acknowledge(self, keys):
        with self.db() as db:db.executemany("UPDATE inbox SET delivered=1 WHERE key=?", [(key,) for key in keys])

    def pending(self, processed_only=False):
        with self.db() as db:
            rows = db.execute("SELECT key,chat,received,user,text,batch,answer,payload FROM inbox "
                              "WHERE delivered=0" + (" AND answer IS NOT NULL" if processed_only else "") +
                              " ORDER BY received,message").fetchall()
        groups = {}
        for key, chat, received, user, text, batch, answer, payload in rows:
            entry = groups.setdefault(batch or key, dict(keys=[], chat=chat, received=received,
                user=json.loads(user), texts=[], answer=answer, payload=json.loads(payload) if payload else None))
            entry["keys"].append(key);entry["texts"].append(text)
        return list(groups.values())

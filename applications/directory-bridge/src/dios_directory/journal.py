"""Application-owned durable outbox and local synchronization checkpoint."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat

from .mapping import canonical, require
from .scim import SyncError


class Journal:
    def __init__(self, directory):
        self.root = Path(directory)
        require(self.root.is_absolute() and not self.root.is_symlink(), "INVALID_STATE_DIRECTORY")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "PRIVATE_STATE_DIRECTORY_REQUIRED")
        self.lock = os.open(self.root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.db = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.open(self.root / "sync.sqlite", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                        "PRIVATE_STATE_FILE_REQUIRED")
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.root / "sync.sqlite")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, plan TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS patches (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle INTEGER NOT NULL,
                    principal TEXT NOT NULL, patch TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT);
            ''')
        except BaseException as exc:
            self.close()
            if isinstance(exc, BlockingIOError): raise SyncError("SYNC_ALREADY_RUNNING") from None
            raise

    def close(self):
        if self.db is not None: self.db.close()
        os.close(self.lock)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, canonical(value)))

    def cached(self, key):
        row = self.db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self):
        return [(row[0], json.loads(row[1])) for row in self.db.execute(
            "SELECT id,patch FROM patches WHERE state='pending' ORDER BY id")]

    def prepare(self, document, cache, chosen, superseded):
        # Snapshot observations and the publish outbox commit before any external mutation.
        with self.db:
            cycle = self.db.execute("INSERT INTO cycles(plan,state) VALUES (?,'pending')", (canonical(document),)).lastrowid
            for key, value in cache.items():
                self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (key, canonical(value)))
            for pid in superseded:
                self.db.execute("UPDATE patches SET state='superseded' WHERE id=?", (pid,))
            for old_id, patch in chosen:
                if old_id is None:
                    self.db.execute("INSERT INTO patches(cycle,principal,patch,state) VALUES (?,?,?,'pending')",
                                    (cycle, patch["principal_id"], canonical(patch)))
        return cycle

    def receipt(self, patch_id, value):
        with self.db:
            self.db.execute("UPDATE patches SET state='applied',receipt=? WHERE id=?", (canonical(value), patch_id))

    def complete(self, cycle, now):
        require(not self.pending(), "SYNC_PENDING_WRITES")
        with self.db:
            self.db.execute("UPDATE cycles SET state='complete' WHERE id=?", (cycle,))
            self.db.execute("UPDATE cycles SET state='superseded' WHERE state='pending' AND id<>?", (cycle,))
            for key, value in (("checkpoint", cycle), ("last_success_at", now), ("failures", 0),
                               ("next_retry_at", 0), ("last_error", None)):
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, canonical(value)))

    def status(self):
        return {key: self.get(key, default) for key, default in (
            ("checkpoint", 0), ("last_success_at", None), ("last_error", None),
            ("next_retry_at", 0), ("failures", 0))} | {"pending": len(self.pending())}

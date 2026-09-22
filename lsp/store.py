import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, platform_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT 'unknown', topic_id TEXT NOT NULL DEFAULT '',
 mode TEXT NOT NULL DEFAULT 'off', auto_since TEXT NOT NULL DEFAULT '', last_customer TEXT NOT NULL DEFAULT '',
 sync_complete INTEGER NOT NULL DEFAULT 0, sync_error TEXT, synced_at REAL, sync_cursor TEXT NOT NULL DEFAULT '',
 assigned_agent_id TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, UNIQUE(tenant,platform_id));
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, conversation INTEGER NOT NULL REFERENCES conversations(id),
 platform_id TEXT NOT NULL, actor TEXT NOT NULL, actor_id TEXT NOT NULL, created TEXT NOT NULL,
 private INTEGER NOT NULL, interaction TEXT, parts TEXT NOT NULL, cached_at REAL NOT NULL,
 UNIQUE(tenant,conversation,platform_id));
CREATE INDEX IF NOT EXISTS message_order ON messages(conversation,created,id);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, conversation INTEGER, platform_id TEXT, action TEXT NOT NULL,
 version TEXT, retries TEXT, payload TEXT, created REAL NOT NULL, UNIQUE(tenant,conversation,platform_id,action));
CREATE TABLE IF NOT EXISTS assets (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, logical_id TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, name TEXT NOT NULL, purpose TEXT NOT NULL, tags TEXT NOT NULL, filename TEXT NOT NULL,
 mime TEXT NOT NULL, size INTEGER NOT NULL, path TEXT, remote_url TEXT, ref TEXT,
 state TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, channels TEXT NOT NULL,
 error TEXT, created REAL NOT NULL, UNIQUE(tenant,logical_id,version));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, conversation INTEGER, kind TEXT NOT NULL, origin TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL, trigger_id TEXT, batch TEXT, seq INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
 result TEXT, platform_id TEXT, error_code TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(batch,seq));
CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(state,due,created);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, conversation INTEGER NOT NULL, matter TEXT NOT NULL,
 job_id TEXT, ticket_id INTEGER, status INTEGER, url TEXT, state TEXT NOT NULL,
 UNIQUE(tenant,conversation,matter));
CREATE TABLE IF NOT EXISTS checks (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, revision INTEGER NOT NULL, channel TEXT NOT NULL,
 capability TEXT NOT NULL, status TEXT NOT NULL, target TEXT NOT NULL, evidence TEXT NOT NULL,
 job_id TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS logs (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, conversation INTEGER, event TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
 id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, created REAL NOT NULL, reserved INTEGER NOT NULL,
 tokens INTEGER, request_id TEXT, elapsed_ms INTEGER);
"""


class Store:
    def __init__(self, path, key):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.cipher, self.lock = path, Fernet(key.encode()), threading.RLock()
        self.local = threading.local()
        with self.connect() as db:
            db.executescript(SCHEMA)
            if "sync_cursor" not in {r[1] for r in db.execute("PRAGMA table_info(conversations)")}:
                db.execute("ALTER TABLE conversations ADD COLUMN sync_cursor TEXT NOT NULL DEFAULT ''")
            if "payload" not in {r[1] for r in db.execute("PRAGMA table_info(events)")}:
                db.execute("ALTER TABLE events ADD COLUMN payload TEXT")
            if "assigned_agent_id" not in {r[1] for r in db.execute("PRAGMA table_info(conversations)")}:
                db.execute("ALTER TABLE conversations ADD COLUMN assigned_agent_id TEXT NOT NULL DEFAULT ''")
        Path(path).chmod(0o600)

    @contextmanager
    def connect(self):
        with self.lock:
            if getattr(self.local, "connection", None) is not None:
                yield self.local.connection
                return
            db = sqlite3.connect(self.path, timeout=10)
            self.local.connection = db
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                self.local.connection = None
                db.close()

    def all(self, query, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args).fetchall()]

    def one(self, query, args=()):
        rows = self.all(query, args)
        return rows[0] if rows else None

    def run(self, query, args=()):
        with self.connect() as db:
            return db.execute(query, args).lastrowid

    def seal(self, value):
        return self.cipher.encrypt(dump(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def log(self, tenant, conversation, event, detail=""):
        self.run("INSERT INTO logs(tenant,conversation,event,detail,created) VALUES(?,?,?,?,?)",
                 (tenant, conversation, event, detail, time.time()))

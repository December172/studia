"""Studia accounts for the hosted mode: users, invites, sessions and login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ITERATIONS = 600_000  # PBKDF2-SHA256, OWASP 2023; scrypt is missing from LibreSSL builds
USERNAME = re.compile(r"^[\w.\-]{2,32}$")
MIN_PASSWORD = 8
SESSION_TTL = 7 * 86400
INVITE_TTL = 7 * 86400


class AccountError(Exception):
    pass


def hash_password(password, salt=None, iterations=None):
    salt = salt or secrets.token_bytes(16)
    iterations = iterations or ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def check_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# Verifying against a dummy hash keeps unknown usernames as slow as wrong passwords.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def _validate(username, password):
    if not USERNAME.fullmatch(username or ""):
        raise AccountError("用户名需 2–32 位，只能包含字母、数字、汉字、下划线、点或减号")
    if len(password or "") < MIN_PASSWORD:
        raise AccountError("密码至少 %d 位" % MIN_PASSWORD)


class Accounts:
    """Users, invites and sessions in one SQLite file (mode 600) inside the data directory."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password TEXT NOT NULL,
            role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, created REAL, last_login REAL);
        CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY, created REAL, created_by TEXT, expires REAL, used_by TEXT);
        CREATE TABLE IF NOT EXISTS sessions (
            key TEXT PRIMARY KEY, uid TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "studia.db"
        # Create the file 600 first: SQLite gives its journal the same mode as the database.
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        # ponytail: one connection behind one lock; fine for a handful of users, use a pool if it ever isn't.
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(self.SCHEMA)
        self._import_json()

    def _import_json(self):
        """One-time move from the accounts.json used before the database."""
        legacy = self.directory / "accounts.json"
        if not legacy.exists():
            return
        data = json.loads(legacy.read_text(encoding="utf-8"))
        with self.transaction():
            for uid, u in (data.get("users") or {}).items():
                self.db.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?,?,?,?)",
                                (uid, u["username"], u["password"], u["role"], int(bool(u.get("disabled"))),
                                 u.get("created"), u.get("last_login")))
            for code, i in (data.get("invites") or {}).items():
                self.db.execute("INSERT OR IGNORE INTO invites VALUES (?,?,?,?,?)",
                                (code, i.get("created"), i.get("created_by"), i.get("expires"), i.get("used_by")))
        legacy.replace(legacy.with_name("accounts.json.migrated"))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _one(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    @staticmethod
    def public(row):
        return {"id": row["id"], "username": row["username"], "role": row["role"], "disabled": bool(row["disabled"]),
                "created": row["created"], "last_login": row["last_login"]}

    # ------------------------------------------------------------ users

    def create_user(self, username, password, role="user"):
        _validate(username, password)
        uid = secrets.token_hex(8)
        try:
            with self.transaction():
                self.db.execute("INSERT INTO users VALUES (?,?,?,?,0,?,NULL)",
                                (uid, username, hash_password(password), role, time.time()))
        except sqlite3.IntegrityError:
            raise AccountError("用户名已被使用")
        return uid

    def setup_admin(self, username, password):
        """Create the first administrator; refused once any administrator exists."""
        _validate(username, password)
        uid = secrets.token_hex(8)
        with self.transaction():
            if self.db.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
                raise AccountError("管理员已经设置过了，请直接登录")
            self.db.execute("INSERT INTO users VALUES (?,?,?,'admin',0,?,NULL)",
                            (uid, username, hash_password(password), time.time()))
        return uid

    def user(self, uid):
        row = self._one("SELECT * FROM users WHERE id = ?", uid)
        return self.public(row) if row else None

    def users(self):
        with self.lock:
            return [self.public(row) for row in self.db.execute("SELECT * FROM users ORDER BY created")]

    def has_admin(self):
        return self._one("SELECT 1 FROM users WHERE role = 'admin'") is not None

    def authenticate(self, username, password):
        row = self._one("SELECT * FROM users WHERE username = ?", username or "")
        if not check_password(password or "", row["password"] if row else _DUMMY) or row is None:
            return None
        if row["disabled"]:
            raise AccountError("这个账号已被停用，请联系管理员")
        with self.transaction():
            self.db.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), row["id"]))
        return self.user(row["id"])

    def set_password(self, uid, password, old_password=None):
        if len(password or "") < MIN_PASSWORD:
            raise AccountError("密码至少 %d 位" % MIN_PASSWORD)
        row = self._one("SELECT password FROM users WHERE id = ?", uid)
        if row is None:
            raise AccountError("用户不存在")
        if old_password is not None and not check_password(old_password, row["password"]):
            raise AccountError("原密码不正确")
        with self.transaction():
            self.db.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), uid))

    def set_disabled(self, uid, disabled):
        with self.transaction():
            row = self.db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
            if row is None:
                raise AccountError("用户不存在")
            admins = self.db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0").fetchone()[0]
            if disabled and row["role"] == "admin" and admins <= 1:
                raise AccountError("不能停用最后一个管理员")
            self.db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(bool(disabled)), uid))

    # ------------------------------------------------------------ invites

    def create_invite(self, created_by, ttl=INVITE_TTL):
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        with self.transaction():
            self.db.execute("INSERT INTO invites VALUES (?,?,?,?,NULL)", (code, time.time(), created_by, time.time() + ttl))
        return code

    def invites(self):
        now = time.time()
        with self.lock:
            rows = self.db.execute("SELECT * FROM invites ORDER BY created DESC").fetchall()
        return [{"code": r["code"], "created": r["created"], "expires": r["expires"], "used_by": r["used_by"],
                 "state": "used" if r["used_by"] else "expired" if r["expires"] < now else "open"} for r in rows]

    def revoke_invite(self, code):
        with self.transaction():
            if self.db.execute("DELETE FROM invites WHERE code = ?", (code,)).rowcount == 0:
                raise AccountError("邀请码不存在")

    def register(self, code, username, password):
        _validate(username, password)
        code = (code or "").strip().upper()
        uid = secrets.token_hex(8)
        with self.transaction():
            invite = self.db.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
            if invite is None or invite["used_by"] or invite["expires"] < time.time():
                raise AccountError("邀请码无效或已过期")
            try:
                self.db.execute("INSERT INTO users VALUES (?,?,?,'user',0,?,NULL)",
                                (uid, username, hash_password(password), time.time()))
            except sqlite3.IntegrityError:
                raise AccountError("用户名已被使用")
            self.db.execute("UPDATE invites SET used_by = ? WHERE code = ?", (uid, code))
        return uid


class Sessions:
    """Sessions stored in the accounts database, keyed by the SHA-256 of the cookie value,
    so a restart (or a container update) does not sign everyone out."""

    def __init__(self, accounts, ttl=SESSION_TTL):
        self.accounts = accounts
        self.ttl = ttl

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, uid):
        token = secrets.token_urlsafe(32)
        with self.accounts.transaction():
            self.accounts.db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            self.accounts.db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                                     (self._key(token), uid, secrets.token_urlsafe(24), time.time() + self.ttl))
        return token

    def get(self, token):
        if not token:
            return None
        row = self.accounts._one("SELECT uid, csrf, expires FROM sessions WHERE key = ?", self._key(token))
        if row is None or row["expires"] < time.time():
            return None
        return {"uid": row["uid"], "csrf": row["csrf"], "expires": row["expires"]}

    def drop(self, token):
        with self.accounts.transaction():
            self.accounts.db.execute("DELETE FROM sessions WHERE key = ?", (self._key(token or ""),))

    def drop_user(self, uid):
        with self.accounts.transaction():
            self.accounts.db.execute("DELETE FROM sessions WHERE uid = ?", (uid,))


class Throttle:
    """Lock a key (IP or username) after repeated failures within a window."""

    def __init__(self, limit=5, window=900):
        self.limit, self.window = limit, window
        self.failures = {}
        self.lock = threading.Lock()

    def blocked(self, *keys):
        now = time.time()
        with self.lock:
            for key in keys:
                recent = [t for t in self.failures.get(key, []) if now - t < self.window]
                self.failures[key] = recent
                if len(recent) >= self.limit:
                    return int(self.window - (now - recent[0])) + 1
        return 0

    def fail(self, *keys):
        with self.lock:
            for key in keys:
                self.failures.setdefault(key, []).append(time.time())

    def clear(self, *keys):
        with self.lock:
            for key in keys:
                self.failures.pop(key, None)

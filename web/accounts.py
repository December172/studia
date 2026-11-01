"""Studia accounts for the hosted mode: users, invites, sessions and login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
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
    """Users and invites persisted in one JSON file (mode 600, atomic replace)."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "accounts.json"
        self.lock = threading.RLock()

    # ------------------------------------------------------------ storage

    def _read(self):
        if not self.path.exists():
            return {"users": {}, "invites": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("users", {})
        data.setdefault("invites", {})
        return data

    def _write(self, data):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".accounts-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def public(uid, user):
        return {"id": uid, "username": user["username"], "role": user["role"], "disabled": user.get("disabled", False),
                "created": user.get("created"), "last_login": user.get("last_login")}

    # ------------------------------------------------------------ users

    def create_user(self, username, password, role="user"):
        _validate(username, password)
        with self.lock:
            data = self._read()
            if any(u["username"].lower() == username.lower() for u in data["users"].values()):
                raise AccountError("用户名已被使用")
            uid = secrets.token_hex(8)
            data["users"][uid] = {"username": username, "password": hash_password(password), "role": role,
                                  "disabled": False, "created": time.time(), "last_login": None}
            self._write(data)
            return uid

    def user(self, uid):
        user = self._read()["users"].get(uid)
        return self.public(uid, user) if user else None

    def users(self):
        return [self.public(uid, user) for uid, user in self._read()["users"].items()]

    def has_admin(self):
        return any(u["role"] == "admin" for u in self._read()["users"].values())

    def authenticate(self, username, password):
        with self.lock:
            data = self._read()
            uid, user = next(((k, v) for k, v in data["users"].items()
                              if v["username"].lower() == (username or "").lower()), (None, None))
            if not check_password(password or "", user["password"] if user else _DUMMY) or user is None:
                return None
            if user.get("disabled"):
                raise AccountError("这个账号已被停用，请联系管理员")
            user["last_login"] = time.time()
            self._write(data)
            return self.public(uid, user)

    def set_password(self, uid, password, old_password=None):
        if len(password or "") < MIN_PASSWORD:
            raise AccountError("密码至少 %d 位" % MIN_PASSWORD)
        with self.lock:
            data = self._read()
            user = data["users"].get(uid)
            if user is None:
                raise AccountError("用户不存在")
            if old_password is not None and not check_password(old_password, user["password"]):
                raise AccountError("原密码不正确")
            user["password"] = hash_password(password)
            self._write(data)

    def set_disabled(self, uid, disabled):
        with self.lock:
            data = self._read()
            user = data["users"].get(uid)
            if user is None:
                raise AccountError("用户不存在")
            if disabled and user["role"] == "admin" and sum(
                    1 for u in data["users"].values() if u["role"] == "admin" and not u.get("disabled")) <= 1:
                raise AccountError("不能停用最后一个管理员")
            user["disabled"] = bool(disabled)
            self._write(data)

    # ------------------------------------------------------------ invites

    def create_invite(self, created_by, ttl=INVITE_TTL):
        with self.lock:
            data = self._read()
            code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
            data["invites"][code] = {"created": time.time(), "created_by": created_by,
                                     "expires": time.time() + ttl, "used_by": None}
            self._write(data)
            return code

    def invites(self):
        now = time.time()
        return [{"code": code, "created": i["created"], "expires": i["expires"], "used_by": i["used_by"],
                 "state": "used" if i["used_by"] else "expired" if i["expires"] < now else "open"}
                for code, i in sorted(self._read()["invites"].items(), key=lambda kv: -kv[1]["created"])]

    def revoke_invite(self, code):
        with self.lock:
            data = self._read()
            if data["invites"].pop(code, None) is None:
                raise AccountError("邀请码不存在")
            self._write(data)

    def register(self, code, username, password):
        _validate(username, password)
        with self.lock:
            data = self._read()
            invite = data["invites"].get((code or "").strip().upper())
            if invite is None or invite["used_by"] or invite["expires"] < time.time():
                raise AccountError("邀请码无效或已过期")
            uid = self.create_user(username, password)
            data = self._read()
            data["invites"][code.strip().upper()]["used_by"] = uid
            self._write(data)
            return uid


class Sessions:
    """In-memory sessions keyed by the SHA-256 of the cookie value; a restart signs everyone out."""

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self.items = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, uid):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.items[self._key(token)] = {"uid": uid, "csrf": secrets.token_urlsafe(24), "expires": time.time() + self.ttl}
        return token

    def get(self, token):
        if not token:
            return None
        with self.lock:
            session = self.items.get(self._key(token))
            if session and session["expires"] < time.time():
                del self.items[self._key(token)]
                return None
            return session

    def drop(self, token):
        with self.lock:
            self.items.pop(self._key(token or ""), None)

    def drop_user(self, uid):
        with self.lock:
            for key in [k for k, s in self.items.items() if s["uid"] == uid]:
                del self.items[key]


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

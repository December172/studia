"""Hosted Studia: several people share one server, each with their own isolated data."""
from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import hmac
import html
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from Crypto.Cipher import AES

from NewMoocMain.oauth_login import SSO_AUTH_URL, SSO_SOURCE
from ZHSMain.memory import DatabaseAnswerMemory
from ai.memory import MoocDatabaseAnswerMemory
from settings import SettingsError, SettingsStore
from web.accounts import AccountError, Accounts, Sessions, Throttle
from web.server import MAX_JOBS, MAX_PENDING, NOT_HANDLED, HttpError, Reply, Studia, _EventLogHandler, make_handler

COOKIE = "studia_session"
OAUTH_TTL = 600
KEY_ENV = "STUDIA_SECRET_KEY"
logger = logging.getLogger(__name__)


def load_key(value):
    """32-byte AES key from the base64url text printed by --gen-key."""
    try:
        key = base64.urlsafe_b64decode((value or "").strip() + "==")
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32:
        raise ValueError("%s 无效：请用 python StartWork.py --serve --gen-key 生成" % KEY_ENV)
    return key


class EncryptedSettingsStore(SettingsStore):
    """A user's settings (platform tokens, API keys) sealed with AES-256-GCM.

    The user id is the associated data, so a file copied into another user's
    directory fails authentication instead of handing over that account.
    """

    def __init__(self, directory, key, owner):
        super().__init__(directory)
        self.key, self.owner = key, owner.encode()
        self.plain_path = self.path
        self.path = self.directory / "settings.enc"

    def _read(self):
        if not self.path.exists():
            if not self.plain_path.exists():
                return {}
            return self._migrate()
        try:
            sealed = json.loads(self.path.read_text(encoding="utf-8"))
            cipher = AES.new(self.key, AES.MODE_GCM, nonce=base64.b64decode(sealed["nonce"]))
            cipher.update(self.owner)
            plain = cipher.decrypt_and_verify(base64.b64decode(sealed["data"]), base64.b64decode(sealed["tag"]))
            data = json.loads(plain)
        except (OSError, ValueError, KeyError, TypeError):
            raise SettingsError("无法解密用户配置：密钥不对或文件已损坏（原文件未覆盖）") from None
        if not isinstance(data, dict):
            raise SettingsError("用户配置格式不正确（原文件未覆盖）")
        return data

    def _migrate(self):
        data = json.loads(self.plain_path.read_text(encoding="utf-8"))
        self._write(data)
        self.plain_path.unlink()
        return data

    def _write(self, data):
        data["version"] = 1
        cipher = AES.new(self.key, AES.MODE_GCM)
        cipher.update(self.owner)
        body, tag = cipher.encrypt_and_digest(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        sealed = {"v": 1, "nonce": base64.b64encode(cipher.nonce).decode(), "tag": base64.b64encode(tag).decode(),
                  "data": base64.b64encode(body).decode()}
        temporary = self.directory / (".settings-%s.tmp" % secrets.token_hex(4))
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(sealed, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise SettingsError("无法保存用户配置，请检查数据目录权限") from None
        finally:
            if temporary.exists():
                temporary.unlink()


class Hub:
    """Accounts plus one Studia app per user, each rooted in data/users/<id>/."""

    def __init__(self, data_dir, public_url, *, key, trust_proxy=False):
        self.key = key
        self.data = Path(data_dir).expanduser()
        (self.data / "users").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data.chmod(0o700)  # mkdir(parents=True) leaves the root at the umask default
        self.accounts = Accounts(self.data)
        self.sessions = Sessions(self.accounts)
        # Answers the platform has judged, shared by every user: one user's graded paper
        # spares everyone else the AI call (and the wrong answer) next time.
        self.memory = DatabaseAnswerMemory(self.accounts.path)
        # MOOC answers are only shared within the same course offering and paper (ai.memory).
        self.mooc_memory = MoocDatabaseAnswerMemory(self.accounts.path)
        for legacy in sorted((self.data / "users").glob("*/zhs-answers.json")):
            try:
                count = self.memory.merge_file(legacy)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("合并 %s 失败，保留原文件：%s", legacy, exc)
                continue
            legacy.replace(legacy.with_name("zhs-answers.json.merged"))
            logger.info("已把 %s 的 %d 题并入共享答案记忆", legacy.parent.name, count)
        # Until an administrator exists, whoever reaches the site first could claim it; the
        # first-run page therefore also asks for this code, printed only in the server log.
        self.setup_code = None if self.accounts.has_admin() else "-".join(
            secrets.token_hex(2).upper() for _ in range(3))
        self.throttle = Throttle()
        self.public_url = public_url.rstrip("/")
        self.trust_proxy = trust_proxy
        # The program writes its daily log files to the working directory (/data in Docker).
        self.log_dir = Path.cwd()
        self.lock = threading.RLock()
        self.apps = {}
        self.pending_oauth = {}

    def app(self, uid):
        with self.lock:
            app = self.apps.get(uid)
            if app is None:
                directory = self.data / "users" / uid
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                store = EncryptedSettingsStore(directory, self.key, uid)
                store.ensure_default_model()
                app = Studia(store, reports_dir=directory / "ai_results", memory=self.memory, mooc_memory=self.mooc_memory,
                             oauth_url=lambda pid, uid=uid: self.oauth_url(uid, pid),
                             job_limit=lambda uid=uid: self.job_limit(uid),
                             zhs_parallel=lambda: bool(self.accounts.setting("zhs_parallel_study", False)))
                app.owner = (self.accounts.user(uid) or {}).get("username", uid)
                self.apps[uid] = app
            return app

    def audit(self, action, *, actor="", target="", ip="", ok=True):
        """Append-only record of sign-ins and admin actions; never passwords or tokens."""
        line = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "ok": ok,
                           "actor": actor, "target": target, "ip": ip}, ensure_ascii=False)
        path = self.data / "audit.log"
        with self.lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    # ------------------------------------------------------------ MOOC browser login

    def oauth_url(self, uid, pid):
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.pending_oauth = {k: v for k, v in self.pending_oauth.items() if v[2] > now}
            self.pending_oauth[nonce] = (uid, pid, now + OAUTH_TTL)
        redirect = "%s/oauth/callback/%s" % (self.public_url, nonce)
        return SSO_AUTH_URL + "?" + urlencode({"mode": "simple", "source": SSO_SOURCE, "redirect": redirect})

    def oauth_callback(self, nonce, token, session_uid):
        # The SSO accepts any redirect, so only a fresh single-use nonce we issued
        # (and, when a Studia session is present, the same user) may bind a token.
        with self.lock:
            entry = self.pending_oauth.pop(nonce, None)
        if entry is None or entry[2] < time.time():
            return False, "这个登录链接已失效，请回到 Studia 重新点击登录。"
        uid, pid, _ = entry
        if session_uid is not None and session_uid != uid:
            return False, "当前浏览器登录的 Studia 账号和发起登录的账号不一致，请换回原账号重试。"
        if not token:
            return False, "没有收到职教云的登录结果，请重试。"
        threading.Thread(target=self.app(uid).finish_browser_login, args=(pid, token), daemon=True).start()
        return True, "职教云登录成功，正在读取课程。可以关闭这个页面，回到 Studia。"

    # ------------------------------------------------------------ admin helpers

    def site_settings(self):
        return {"max_jobs": self.default_job_limit(),
                "zhs_parallel_study": bool(self.accounts.setting("zhs_parallel_study", False))}

    def default_job_limit(self):
        return self.accounts.setting("max_jobs", MAX_JOBS)

    def job_limit(self, uid):
        """Jobs this person may run at once: their own limit if an admin set one, else the site default."""
        user = self.accounts.user(uid)
        return (user or {}).get("max_jobs") or self.default_job_limit()

    def overview(self):
        users = self.accounts.users()
        for user in users:
            app = self.apps.get(user["id"])
            user["jobs"] = app._running() if app else []
            user["job_limit"] = user["max_jobs"] or self.default_job_limit()
        return users

    def all_jobs(self):
        """Every running or waiting job on the site, oldest first, with whose it is."""
        names = {user["id"]: user["username"] for user in self.accounts.users()}
        jobs = [dict(job, user_id=uid, username=names.get(uid, uid))
                for uid, app in list(self.apps.items()) for job in app._running()]
        return sorted(jobs, key=lambda job: job["started"])

    def disable(self, uid):
        self.accounts.set_disabled(uid, True)
        self.sessions.drop_user(uid)
        app = self.apps.get(uid)
        if app:
            app.stop_job()


def _page(title, message, ok):
    color = "#237a3e" if ok else "#c8285a"
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><body style=\"margin:0;display:grid;place-items:center;min-height:100vh;"
            "font-family:-apple-system,'PingFang SC',sans-serif;background:#f4f5f7\"><div style=\"max-width:420px;"
            "padding:28px;border-radius:12px;background:#fff;box-shadow:0 8px 30px rgba(0,0,0,.08)\">"
            "<h1 style=\"margin:0 0 10px;font-size:20px;color:%s\">%s</h1><p style=\"margin:0;line-height:1.6;"
            "color:#3c3e41\">%s</p></div>") % (html.escape(title), color, html.escape(title), html.escape(message))


class HostedAccess:
    """Cookie sessions with CSRF headers, invite-only sign-up and an admin role."""

    mode = "hosted"

    def __init__(self, hub, port):
        self.hub = hub
        url = urlsplit(hub.public_url)
        self.secure = url.scheme == "https"
        self.hosts = {url.netloc, f"127.0.0.1:{port}", f"localhost:{port}"}

    def host_ok(self, host):
        return host in self.hosts

    def headers(self):
        headers = {"X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}
        if self.secure:
            headers["Strict-Transport-Security"] = "max-age=31536000"
        return headers

    def _ip(self, request):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if self.hub.trust_proxy and forwarded:
            return forwarded.split(",")[-1].strip()
        return request.client_address[0]

    def _token(self, request):
        cookie = SimpleCookie()
        try:
            cookie.load(request.headers.get("Cookie", ""))
        except Exception:
            return ""
        return cookie[COOKIE].value if COOKIE in cookie else ""

    def _cookie(self, value, max_age):
        return {"Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                              % (COOKIE, value, max_age, "; Secure" if self.secure else "")}

    def _signed_in(self, uid):
        token = self.hub.sessions.create(uid)
        return Reply({"user": self.hub.accounts.user(uid)}, headers=self._cookie(token, self.hub.sessions.ttl))

    # ------------------------------------------------------------ no session needed

    def public(self, request, method, path, query, body):
        hub = self.hub
        if method == "GET" and path == "/api/mode":
            return {"mode": self.mode, "setup": not hub.accounts.has_admin()}
        if method == "POST" and path == "/api/auth/setup":
            ip = self._ip(request)
            key = "ip:" + ip
            if hub.accounts.has_admin():
                raise HttpError(409, "管理员已经设置过了，请直接登录")
            if hub.throttle.blocked(key):
                raise HttpError(429, "尝试次数太多，请稍后再试")
            code = str(body.get("code", "")).strip().upper()
            if not hub.setup_code or not hmac.compare_digest(code.encode(), hub.setup_code.encode()):
                hub.throttle.fail(key)
                hub.audit("setup", ip=ip, ok=False)
                raise HttpError(403, "设置码不正确，请在服务器日志里查看")
            try:
                uid = hub.accounts.setup_admin(str(body.get("username", "")).strip(), str(body.get("password", "")))
            except AccountError as exc:
                raise HttpError(400, str(exc))
            hub.setup_code = None
            hub.audit("setup", actor=hub.accounts.user(uid)["username"], ip=ip)
            return self._signed_in(uid)
        if method == "POST" and path == "/api/auth/login":
            username = str(body.get("username", "")).strip()
            keys = ("ip:" + self._ip(request), "user:" + username.lower())
            ip = self._ip(request)
            wait = hub.throttle.blocked(*keys)
            if wait:
                hub.audit("login", actor=username, ip=ip, ok=False)
                raise HttpError(429, "尝试次数太多，请 %d 分钟后再试" % max(1, wait // 60))
            try:
                user = hub.accounts.authenticate(username, str(body.get("password", "")))
            except AccountError as exc:
                hub.audit("login", actor=username, ip=ip, ok=False)
                raise HttpError(403, str(exc))
            if user is None:
                hub.throttle.fail(*keys)
                hub.audit("login", actor=username, ip=ip, ok=False)
                raise HttpError(401, "用户名或密码不正确")
            hub.throttle.clear(*keys)
            hub.audit("login", actor=user["username"], ip=ip)
            return self._signed_in(user["id"])
        if method == "POST" and path == "/api/auth/register":
            key = "ip:" + self._ip(request)
            if hub.throttle.blocked(key):
                raise HttpError(429, "尝试次数太多，请稍后再试")
            try:
                uid = hub.accounts.register(str(body.get("invite", "")), str(body.get("username", "")).strip(),
                                            str(body.get("password", "")))
            except AccountError as exc:
                hub.throttle.fail(key)
                hub.audit("register", actor=str(body.get("username", "")), ip=self._ip(request), ok=False)
                raise HttpError(400, str(exc))
            hub.audit("register", actor=hub.accounts.user(uid)["username"], ip=self._ip(request))
            return self._signed_in(uid)
        match = re.fullmatch(r"/oauth/callback/([\w-]+)", path)
        if method == "GET" and match:
            session = hub.sessions.get(self._token(request))
            ok, message = hub.oauth_callback(match[1], (query.get("token") or [""])[0].strip(),
                                             session["uid"] if session else None)
            return Reply(_page("登录成功" if ok else "登录没有完成", message, ok), status=200 if ok else 400,
                         content_type="text/html; charset=utf-8",
                         headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        return NOT_HANDLED

    # ------------------------------------------------------------ session required

    def authorize(self, request, method, query):
        token = self._token(request)
        session = self.hub.sessions.get(token)
        if session is None:
            raise HttpError(401, "请先登录 Studia")
        user = self.hub.accounts.user(session["uid"])
        if user is None or user["disabled"]:
            self.hub.sessions.drop(token)
            raise HttpError(401, "账号已停用或不存在")
        if method == "POST" and not hmac.compare_digest(request.headers.get("X-Studia-CSRF", ""), session["csrf"]):
            raise HttpError(403, "请求校验失败，请刷新页面后重试")
        return {"app": self.hub.app(user["id"]), "user": user, "session": session, "token": token}

    def private(self, request, method, path, query, body, ctx):
        hub, user = self.hub, ctx["user"]
        if method == "GET" and path == "/api/auth/me":
            return {"user": user, "csrf": ctx["session"]["csrf"]}
        if method == "POST" and path == "/api/auth/logout":
            hub.sessions.drop(ctx["token"])
            return Reply({}, headers=self._cookie("", 0))
        if method == "POST" and path == "/api/auth/password":
            try:
                hub.accounts.set_password(user["id"], str(body.get("new", "")), old_password=str(body.get("old", "")))
            except AccountError as exc:
                raise HttpError(400, str(exc))
            hub.sessions.drop_user(user["id"])
            hub.audit("password", actor=user["username"], ip=self._ip(request))
            return self._signed_in(user["id"])
        if not path.startswith("/api/admin/"):
            return NOT_HANDLED
        if user["role"] != "admin":
            raise HttpError(403, "只有管理员可以访问")
        try:
            result = self._admin(method, path, body, user, query)
        except AccountError as exc:
            raise HttpError(400, str(exc))
        if method == "POST":
            target = path.split("/")[4] if path.count("/") >= 4 else ""
            name = (hub.accounts.user(target) or {}).get("username", target)
            hub.audit("admin:" + path.rsplit("/", 1)[-1], actor=user["username"], target=name, ip=self._ip(request))
        return result

    def _admin(self, method, path, body, me, query=None):
        hub = self.hub
        if method == "GET" and path == "/api/admin/logs":
            arg = lambda name: (query or {}).get(name, [""])[0]
            kind = "audit" if arg("kind") == "audit" else "app"
            try:
                start, end = float(arg("from")), float(arg("to"))
            except ValueError:
                raise HttpError(400, "请选择导出的时间段") from None
            if not 0 < start < end or end - start > 92 * 86400:
                raise HttpError(400, "时间段无效，最长 92 天")
            hub.audit("admin:export_logs", actor=me["username"], target=kind)
            name = "studia-%s-%s.log" % (kind, time.strftime("%Y%m%d-%H%M", time.localtime(start)))
            return Reply(export_logs(hub.data if kind == "audit" else hub.log_dir, kind, start, end).encode("utf-8"),
                         content_type="text/plain; charset=utf-8",
                         headers={"Content-Disposition": 'attachment; filename="%s"' % name})
        if method == "GET" and path == "/api/admin/users":
            return {"users": hub.overview()}
        if method == "GET" and path == "/api/admin/invites":
            return {"invites": hub.accounts.invites()}
        if method == "GET" and path == "/api/admin/memory":
            return dict(hub.memory.stats(), mooc=hub.mooc_memory.stats())
        if method == "GET" and path == "/api/admin/jobs":
            return {"jobs": hub.all_jobs()}
        if method == "GET" and path == "/api/admin/settings":
            return hub.site_settings()
        if method == "POST" and path == "/api/admin/settings":
            if "max_jobs" in body:
                hub.accounts.set_setting("max_jobs", _job_limit(body.get("max_jobs")))
            if "zhs_parallel_study" in body:
                hub.accounts.set_setting("zhs_parallel_study", body.get("zhs_parallel_study") is True)
            return hub.site_settings()
        match = re.fullmatch(r"/api/admin/users/(\w+)/jobs/(\w+)/stop", path)
        if method == "POST" and match:
            app = hub.apps.get(match[1])
            if app is None or match[2] not in app.jobs:
                raise HttpError(404, "任务已经结束")
            app.stop_job(match[2])
            return {}
        if method == "POST" and path == "/api/admin/invites":
            days = min(max(int(body.get("days") or 7), 1), 30)
            return {"code": hub.accounts.create_invite(me["id"], ttl=days * 86400)}
        match = re.fullmatch(r"/api/admin/invites/([\w-]+)/revoke", path)
        if method == "POST" and match:
            hub.accounts.revoke_invite(match[1])
            return {}
        match = re.fullmatch(r"/api/admin/users/(\w+)/(disable|enable|password|stop|limit)", path)
        if method == "POST" and match:
            uid, action = match[1], match[2]
            if hub.accounts.user(uid) is None:
                raise HttpError(404, "用户不存在")
            if action == "disable":
                if uid == me["id"]:
                    raise HttpError(400, "不能停用自己")
                hub.disable(uid)
            elif action == "enable":
                hub.accounts.set_disabled(uid, False)
            elif action == "password":
                hub.accounts.set_password(uid, str(body.get("password", "")))
                hub.sessions.drop_user(uid)
            elif action == "limit":
                # Empty returns the user to the site default.
                limit = body.get("max_jobs")
                hub.accounts.set_job_limit(uid, None if limit in (None, "") else _job_limit(limit))
            else:
                app = hub.apps.get(uid)
                if app:
                    app.stop_job()
            return {}
        raise HttpError(404, "not found")


LOG_TIME = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
EXPORT_LIMIT = 20 * 1024 * 1024


def export_logs(directory, kind, start, end):
    """Log lines written between two epoch times. A line without its own time (a traceback)
    goes with the line before it. Runtime logs are one file per day; audit is one file."""
    if kind == "audit":
        files = [Path(directory) / "audit.log"]
    else:
        # From the day before: older servers kept writing into the file of the day they started.
        day = datetime.date.fromtimestamp(start) - datetime.timedelta(days=1)
        last = datetime.date.fromtimestamp(end)
        files = []
        while day <= last:
            files.append(Path(directory) / ("icve-ai-assistant-log_%s.log" % day.isoformat()))
            day += datetime.timedelta(days=1)
    lines, size, keep = [], 0, False
    for path in files:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if kind == "audit":
                    try:
                        stamp = json.loads(line)["time"]
                    except (ValueError, KeyError, TypeError):
                        continue
                else:
                    match = LOG_TIME.match(line)
                    stamp = match[1] if match else None
                if stamp:
                    keep = start <= time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) <= end
                if keep:
                    lines.append(line)
                    size += len(line)
                    if size > EXPORT_LIMIT:
                        lines.append("……超过 20MB，后面的省略了，请缩短时间段\n")
                        return "".join(lines)
    return "".join(lines) or "这段时间没有日志。\n"


def _job_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise HttpError(400, "请填写 1 到 %d 之间的整数" % MAX_PENDING) from None
    if not 1 <= limit <= MAX_PENDING:
        raise HttpError(400, "请填写 1 到 %d 之间的整数" % MAX_PENDING)
    return limit


def serve_hosted(hub, host, port):
    logging.getLogger().addHandler(_EventLogHandler())
    server = ThreadingHTTPServer((host, port), None)
    server.daemon_threads = True
    server.RequestHandlerClass = make_handler(HostedAccess(hub, server.server_address[1]))
    print("Studia 服务器模式已启动：监听 %s:%d，对外地址 %s" % (host, server.server_address[1], hub.public_url))
    print("数据目录：%s；按 Ctrl+C 退出。" % hub.data)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for app in list(hub.apps.values()):
            app.stop_job()
        server.server_close()


def _interrupt(*_):
    raise KeyboardInterrupt


def _key_file(path):
    """The key text from a file, generated on first start so a fresh deployment needs no setup."""
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=") + "\n")
        print("已生成主密钥：%s 。请把它另外备份一份；丢了它，用户保存的平台登录和 API Key 就解不开了。" % path)
    return path.read_text().strip()


def main(argv):
    env = os.environ.get
    parser = argparse.ArgumentParser(prog="StartWork.py --serve", description="Studia 服务器模式（多用户）。"
                                     "每个选项也可以用括号里的环境变量设置，Docker 部署时就是这样用的。")
    parser.add_argument("--host", default=env("STUDIA_HOST", "127.0.0.1"),
                        help="监听地址，放在反向代理后面时保持 127.0.0.1（STUDIA_HOST）")
    parser.add_argument("--port", type=int, default=int(env("STUDIA_PORT", "8080")), help="（STUDIA_PORT）")
    parser.add_argument("--data", default=env("STUDIA_DATA", "~/.studia-hosted"),
                        help="数据目录：数据库和每个用户的子目录（STUDIA_DATA）")
    parser.add_argument("--public-url", default=env("STUDIA_PUBLIC_URL"),
                        help="浏览器访问用的地址，例如 https://studia.example.com（STUDIA_PUBLIC_URL）")
    parser.add_argument("--trust-proxy", action="store_true", default=env("STUDIA_TRUST_PROXY", "") in ("1", "true", "yes"),
                        help="信任反向代理传来的 X-Forwarded-For（STUDIA_TRUST_PROXY=1）")
    parser.add_argument("--key-file", default=env("STUDIA_KEY_FILE"),
                        help="没有设置 %s 时，从这个文件读主密钥，文件不存在就生成；默认是数据目录下的 secret.key（STUDIA_KEY_FILE）" % KEY_ENV)
    parser.add_argument("--create-admin", metavar="USERNAME", help="创建管理员账号后退出")
    parser.add_argument("--gen-key", action="store_true", help="生成加密用户数据的主密钥后退出")
    args = parser.parse_args(argv)
    if args.gen_key:
        print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))
        print("把它设成环境变量 %s 后再启动；丢失这个密钥，用户保存的平台登录和 API Key 将无法解密。" % KEY_ENV)
        return 0
    try:
        key = load_key(os.environ.get(KEY_ENV) or _key_file(Path(args.key_file or Path(args.data).expanduser() / "secret.key")))
    except ValueError as exc:
        if not args.create_admin:
            print("缺少或无效的加密密钥：%s" % exc)
            return 1
        key = None
    hub = Hub(args.data, args.public_url or "http://127.0.0.1:%d" % args.port, key=key,
              trust_proxy=args.trust_proxy)
    if args.create_admin:
        password = getpass.getpass("设置 %s 的密码：" % args.create_admin)
        if password != getpass.getpass("再输入一次："):
            print("两次输入的密码不一致。")
            return 1
        try:
            hub.accounts.create_user(args.create_admin, password, role="admin")
        except AccountError as exc:
            print(exc)
            return 1
        print("管理员 %s 已创建。" % args.create_admin)
        return 0
    if hub.setup_code:
        print("=" * 48)
        print("还没有管理员。打开 %s ，用下面的设置码创建管理员账号：" % hub.public_url)
        print("    设置码：%s" % hub.setup_code)
        print("（每次启动都会换一个，创建完管理员后失效）")
        print("=" * 48, flush=True)
    # `docker stop` sends SIGTERM: shut down like Ctrl+C so running jobs get their stop signal.
    signal.signal(signal.SIGTERM, _interrupt)
    serve_hosted(hub, args.host, args.port)
    return 0

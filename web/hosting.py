"""Hosted Studia: several people share one server, each with their own isolated data."""
from __future__ import annotations

import argparse
import getpass
import hmac
import html
import logging
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from NewMoocMain.oauth_login import SSO_AUTH_URL, SSO_SOURCE
from settings import SettingsStore
from web.accounts import AccountError, Accounts, Sessions, Throttle
from web.server import NOT_HANDLED, HttpError, Reply, Studia, _EventLogHandler, _job_public, make_handler

COOKIE = "studia_session"
OAUTH_TTL = 600
logger = logging.getLogger(__name__)


class Hub:
    """Accounts plus one Studia app per user, each rooted in data/users/<id>/."""

    def __init__(self, data_dir, public_url, *, max_jobs=4, trust_proxy=False):
        self.data = Path(data_dir).expanduser()
        (self.data / "users").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data.chmod(0o700)  # mkdir(parents=True) leaves the root at the umask default
        self.accounts = Accounts(self.data)
        self.sessions = Sessions()
        self.throttle = Throttle()
        self.public_url = public_url.rstrip("/")
        self.trust_proxy = trust_proxy
        self.slots = threading.BoundedSemaphore(max_jobs)
        self.lock = threading.RLock()
        self.apps = {}
        self.pending_oauth = {}

    def app(self, uid):
        with self.lock:
            app = self.apps.get(uid)
            if app is None:
                directory = self.data / "users" / uid
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                store = SettingsStore(directory)
                store.ensure_default_model()
                app = Studia(store, reports_dir=directory / "ai_results", slots=self.slots,
                             oauth_url=lambda pid, uid=uid: self.oauth_url(uid, pid))
                self.apps[uid] = app
            return app

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

    def overview(self):
        users = self.accounts.users()
        for user in users:
            app = self.apps.get(user["id"])
            user["job"] = _job_public(app.job) if app and app.job else None
        return users

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
        return {"Strict-Transport-Security": "max-age=31536000"} if self.secure else {}

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
            return {"mode": self.mode}
        if method == "POST" and path == "/api/auth/login":
            username = str(body.get("username", "")).strip()
            keys = ("ip:" + self._ip(request), "user:" + username.lower())
            wait = hub.throttle.blocked(*keys)
            if wait:
                raise HttpError(429, "尝试次数太多，请 %d 分钟后再试" % max(1, wait // 60))
            try:
                user = hub.accounts.authenticate(username, str(body.get("password", "")))
            except AccountError as exc:
                raise HttpError(403, str(exc))
            if user is None:
                hub.throttle.fail(*keys)
                raise HttpError(401, "用户名或密码不正确")
            hub.throttle.clear(*keys)
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
                raise HttpError(400, str(exc))
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
            return self._signed_in(user["id"])
        if not path.startswith("/api/admin/"):
            return NOT_HANDLED
        if user["role"] != "admin":
            raise HttpError(403, "只有管理员可以访问")
        try:
            return self._admin(method, path, body, user)
        except AccountError as exc:
            raise HttpError(400, str(exc))

    def _admin(self, method, path, body, me):
        hub = self.hub
        if method == "GET" and path == "/api/admin/users":
            return {"users": hub.overview()}
        if method == "GET" and path == "/api/admin/invites":
            return {"invites": hub.accounts.invites()}
        if method == "POST" and path == "/api/admin/invites":
            days = min(max(int(body.get("days") or 7), 1), 30)
            return {"code": hub.accounts.create_invite(me["id"], ttl=days * 86400)}
        match = re.fullmatch(r"/api/admin/invites/([\w-]+)/revoke", path)
        if method == "POST" and match:
            hub.accounts.revoke_invite(match[1])
            return {}
        match = re.fullmatch(r"/api/admin/users/(\w+)/(disable|enable|password|stop)", path)
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
            else:
                app = hub.apps.get(uid)
                if app:
                    app.stop_job()
            return {}
        raise HttpError(404, "not found")


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


def main(argv):
    parser = argparse.ArgumentParser(prog="StartWork.py --serve", description="Studia 服务器模式（多用户）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，放在反向代理后面时保持 127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default="~/.studia-hosted", help="数据目录，每个用户一个子目录")
    parser.add_argument("--public-url", help="浏览器访问用的地址，例如 https://studia.example.com")
    parser.add_argument("--max-jobs", type=int, default=4, help="全站同时运行的任务数")
    parser.add_argument("--trust-proxy", action="store_true", help="信任反向代理传来的 X-Forwarded-For")
    parser.add_argument("--create-admin", metavar="USERNAME", help="创建管理员账号后退出")
    args = parser.parse_args(argv)
    hub = Hub(args.data, args.public_url or "http://127.0.0.1:%d" % args.port,
              max_jobs=max(1, args.max_jobs), trust_proxy=args.trust_proxy)
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
    if not hub.accounts.has_admin():
        print("还没有管理员账号，请先运行：python StartWork.py --serve --create-admin 你的用户名")
        return 1
    serve_hosted(hub, args.host, args.port)
    return 0

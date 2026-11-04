"""超星扫码登录：本地页面展示学习通二维码并轮询手机确认。

和智慧树一样把二维码写成本地 HTML 并在浏览器里打开；平台接口见
``ChaoxingApi.login_qr`` / ``login_qr_status`` / ``refresh_login_qr``。
"""
import base64
import os
import tempfile
import time
import webbrowser
from pathlib import Path

from base.auth import PlatformUnavailable
from ChaoxingMain.api import ChaoxingError

_HEAD = """<!doctype html>
<meta charset="utf-8">
{refresh}<title>超星扫码登录</title>
<style>
  html, body {{ height: 100%; margin: 0; }}
  body {{ display: flex; flex-direction: column; align-items: center; justify-content: center;
         font-family: sans-serif; background: #fff; color: #222; }}
  img {{ width: 260px; height: 260px; }}
  p {{ max-width: 320px; text-align: center; line-height: 1.6; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1b1b1b; color: #eee; }}
  }}
</style>
"""
_QR = ('<img src="data:image/png;base64,{png}" alt="学习通登录二维码">\n'
       '<p>请用学习通 App 扫码，并在手机上确认登录；完成后回到终端。</p>\n')
MAX_REFRESHES = 5


def _write_page(path, png):
    # The page reloads itself so a replaced (expired) code shows up without user action.
    body = _QR.format(png=base64.b64encode(png).decode("ascii"))
    with open(path, "w", encoding="utf-8") as file:
        file.write(_HEAD.format(refresh='<meta http-equiv="refresh" content="3">\n') + body)


def _write_final(path, message):
    # Replace the QR with a static page instead of deleting it: the tab's next
    # reload then shows the outcome rather than a missing-file error.
    with open(path, "w", encoding="utf-8") as file:
        file.write(_HEAD.format(refresh="") + "<p>%s</p>\n" % message)


def qr_login(client, *, open_page=webbrowser.open, sleep=time.sleep, timeout=300, on_status=None):
    """Show the 学习通 QR code in a local page and poll until the phone confirms."""
    token, png = client.login_qr()
    fd, path = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    message = "扫码已结束，请回到终端查看结果。"
    try:
        _write_page(path, png)
        open_page(Path(path).as_uri())
        scanned = False
        refreshes = 0
        for _ in range(timeout):
            sleep(1)
            try:
                result = client.login_qr_status(token)
            except PlatformUnavailable:
                continue
            status = result.get("status")
            if status == -1:
                continue
            if status == 0:
                if not scanned:
                    scanned = True
                    if on_status:
                        nickname = result.get("nickname")
                        on_status("已扫码，请在手机上确认登录" + ("（%s）" % nickname if nickname else ""))
                continue
            if status == 1:
                client.finish_qr_login(result)
                message = "登录成功，可以关闭此页面并回到终端。"
                return True
            if status == 2:
                refreshes += 1
                if refreshes > MAX_REFRESHES:
                    message = "二维码多次过期，请重新登录。"
                    return False
                token, png = client.refresh_login_qr(token)
                _write_page(path, png)
                scanned = False
                continue
            if status == 3:
                message = result.get("message") or "已在手机上取消登录。"
                if on_status:
                    on_status(message)
                return False
            raise ChaoxingError("未知的扫码状态")
        return False
    finally:
        _write_final(path, message)

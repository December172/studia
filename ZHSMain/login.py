"""智慧树扫码登录：本地页面展示二维码并轮询手机确认。"""
import base64
import os
import tempfile
import time
import webbrowser
from pathlib import Path

from base.auth import PlatformUnavailable
from ZHSMain.api import ZHSError

_HEAD = """<!doctype html>
<meta charset="utf-8">
{refresh}<title>智慧树扫码登录</title>
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
_QR = ('<img src="data:image/png;base64,{png}" alt="智慧树登录二维码">\n'
       '<p>请用智慧树 App 扫码，并在手机上确认登录；完成后回到终端。</p>\n')


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
    """Show the QR code in a local page and poll until the phone confirms. Returns True on success."""
    qr_token, png = client.login_qr()
    fd, path = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    try:
        _write_page(path, png)
        open_page(Path(path).as_uri())
        scanned = False
        refreshes = 0
        message = "扫码已结束，请回到终端查看结果。"
        for _ in range(timeout):
            sleep(1)
            try:
                result = client.login_qr_status(qr_token)
            except PlatformUnavailable:
                continue
            status = result.get("status")
            if status == -1:
                continue
            if status == 0:
                if not scanned:
                    scanned = True
                    if on_status:
                        on_status("已扫码，请在手机上确认登录")
                continue
            if status == 1:
                client.finish_qr_login(result.get("oncePassword", ""))
                message = "登录成功，可以关闭此页面并回到终端。"
                return True
            if status == 2:
                refreshes += 1
                if refreshes > 5:
                    return False
                qr_token, png = client.login_qr()
                _write_page(path, png)
                scanned = False
                continue
            if status == 3:
                return False
            raise ZHSError("未知的扫码状态")
        return False
    finally:
        _write_final(path, message)

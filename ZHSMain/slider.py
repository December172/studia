"""智慧树人机验证自动通过：网易易盾滑块。

平台在请求过于频繁时由 ableCaptcha 弹出易盾滑块（captchaId 见下）。
本模块用真实浏览器加载组件、定位缺口、模拟真人拖动，返回 validate 令牌。

注意：智慧树的风控主因是同一账号多实例并发，验证只是结果。
并发不降下来，通过验证后下一个请求仍会被拦。
"""
from __future__ import annotations

import os
import random
import tempfile

from MoocMain.log import Logger

logger = Logger(__name__).get_log()

CAPTCHA_ID = "75f9f716460a422f89a628f50fd8cc2b"
LOADER = "https://cstaticdun.126.net/load.min.js"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

_INIT = """
window.__state = 'init'; window.__validate = ''; window.__err = '';
function boot() {
  try {
    initNECaptcha({
      element: '#cap', captchaId: '%s', width: '320px',
      mode: 'popup', lang: 'zh-CN', protocol: 'https',
      onReady: function(){ if (window.__state === 'init') window.__state = 'ready'; },
      onVerify: function(err, data){
        if (data && data.validate) { window.__validate = data.validate; window.__state = 'verify'; }
        else { window.__err = JSON.stringify(err || {}); window.__state = 'fail'; }
      },
      onError: function(e){ window.__err = JSON.stringify(e || {}); window.__state = 'error'; }
    }, function(ins){ window.__ins = ins; if (window.__state === 'init') window.__state = 'ok'; },
       function(e){ window.__err = String(e); window.__state = 'loadfail'; });
  } catch (e) { window.__err = e.message; window.__state = 'throw'; }
}
""" % CAPTCHA_ID


def find_gap(bg, piece):
    """返回 (缺口 x, 置信度)。bg 为 BGR，piece 为 BGRA。三路投票定位。"""
    import cv2
    import numpy as np

    if piece is None or piece.ndim != 3 or piece.shape[2] < 4:
        return 0, 0.0
    alpha = piece[:, :, 3]
    ys, xs = np.where(alpha > 20)
    if len(xs) == 0:
        return 0, 0.0
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    crop = piece[y0:y1, x0:x1]
    mask = (crop[:, :, 3] > 20).astype(np.uint8) * 255
    h, w = mask.shape
    if h < 6 or w < 6:
        return 0, 0.0

    gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    edge = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 80, 160)
    sil = np.zeros((h, w), np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cv2.drawContours(sil, contours, -1, 255, 1)

    y = max(0, min(int(round(y0 * bg.shape[0] / piece.shape[0])), bg.shape[0] - h))
    band = edge[y:y + h]
    if band.shape[0] < h or band.shape[1] < w:
        return 0, 0.0

    # xe：拼图轮廓对背景边缘；xs：拼图内部对细节图（取最低）；xh：边像度减内部像度
    _, xe_score, _, xe_loc = cv2.minMaxLoc(cv2.matchTemplate(band, sil, cv2.TM_CCOEFF_NORMED))
    detail = cv2.absdiff(gray, cv2.GaussianBlur(gray, (0, 0), 2.2))
    inner = cv2.erode(mask, np.ones((3, 3), np.uint8))
    xs_low, _, xs_loc, _ = cv2.minMaxLoc(
        cv2.matchTemplate(detail[y:y + h], np.zeros((h, w), np.uint8), cv2.TM_SQDIFF, mask=inner))
    color = bg[y:y + h]
    border = cv2.subtract(mask, inner)
    like_border = cv2.matchTemplate(color, crop[:, :, :3], cv2.TM_CCORR_NORMED, mask=border)
    like_inner = cv2.matchTemplate(color, crop[:, :, :3], cv2.TM_CCORR_NORMED, mask=inner)
    # 平坦区域会让归一化相关出现 NaN，先清零再取峰。
    xh_map = np.nan_to_num(like_border - like_inner, nan=0.0, posinf=0.0, neginf=0.0)
    _, xh_score, _, xh_loc = cv2.minMaxLoc(xh_map)
    xe_score = float(np.nan_to_num(xe_score))
    xs_low = float(np.nan_to_num(xs_low))

    xe, xs, xh = xe_loc[0], xs_loc[0], xh_loc[0]
    if abs(xe - xs) <= 20:
        best, margin = round((xe + xs) / 2), max(xe_score, 1 - xs_low)
    elif w * h >= 4500:
        # 大块的洞被涂平，颜色会认成旁边真物体，改信糊的那一路。
        best, margin = xs, 1 - xs_low
    else:
        best, margin = xh, xh_score
    if max(xe, xs, xh) - min(xe, xs, xh) > 48:
        peaks = sorted([(xe_score, xe), (1 - xs_low, xs), (xh_score, xh)], reverse=True)
        if peaks[0][0] >= 0.055 and peaks[0][0] - peaks[1][0] >= 0.012:
            best, margin = peaks[0][1], peaks[0][0]
    return int(best - x0), float(min(0.99, max(0.0, margin)))


def solve(cookies, *, page_url, attempts=3, headless=True, timeout=25):
    """在 page_url 上通过滑块，返回 (validate, cookies)。失败返回 ("", cookies)。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-proxy-server"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="zh-CN", user_agent=UA)
        context.add_cookies(cookies)
        page = context.new_page()
        images = {}
        page.on("response", lambda r: images.setdefault(r.url, r.body())
                if "nosdn" in r.url and r.url.endswith((".png", ".jpg")) else None)
        try:
            for _ in range(attempts):
                validate = _one_round(page, images, page_url, timeout)
                if validate:
                    return validate, _dump(context.cookies())
            logger.warning("滑块未通过：%s", _safe(page.evaluate("window.__err")))
            return "", _dump(context.cookies())
        finally:
            browser.close()


def _one_round(page, images, page_url, timeout):
    page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
    page.evaluate("() => { document.body.innerHTML = '<div id=\"cap\"></div>'; }")
    page.add_script_tag(url=LOADER)
    page.add_script_tag(content=_INIT + "boot();")
    page.wait_for_function("() => window.__state !== 'init'", timeout=timeout * 1000)
    if page.evaluate("window.__state") in ("loadfail", "throw", "error"):
        logger.warning("滑块组件加载失败：%s", _safe(page.evaluate("window.__err")))
        return ""
    page.evaluate("() => window.__ins.popUp()")
    try:
        page.wait_for_selector(".yidun_bg-img", timeout=timeout * 1000)
    except Exception:
        logger.warning("滑块面板未出现：%s", _safe(page.evaluate("window.__err")))
        return ""
    page.wait_for_timeout(600)

    box = page.evaluate("""() => {
      const g = e => e.getBoundingClientRect().toJSON();
      return {bg: document.querySelector('.yidun_bg-img').src,
              piece: document.querySelector('.yidun_jigsaw').src,
              slider: g(document.querySelector('.yidun_slider'))};
    }""")
    bg_path = _save(images.get(box["bg"]), ".jpg")
    piece_path = _save(images.get(box["piece"]), ".png")
    try:
        if not bg_path or not piece_path:
            return ""
        left, confidence = _gap_files(bg_path, piece_path)
    finally:
        for path in (bg_path, piece_path):
            if path:
                os.unlink(path)
    if not left:
        return ""
    logger.info("滑块缺口 x=%s（置信度 %.2f）", left, confidence)
    _drag(page, box["slider"], float(left))
    try:
        page.wait_for_function("() => ['verify','fail','error'].includes(window.__state)",
                               timeout=timeout * 1000)
    except Exception:
        return ""
    if page.evaluate("window.__state") != "verify":
        return ""
    return page.evaluate("window.__validate") or ""


def _gap_files(bg_path, piece_path):
    import cv2
    bg = cv2.imread(bg_path, cv2.IMREAD_COLOR)
    piece = cv2.imread(piece_path, cv2.IMREAD_UNCHANGED)
    if bg is None or piece is None or piece.ndim != 3 or piece.shape[2] < 4:
        return 0, 0.0
    return find_gap(bg, piece)


def _save(raw, suffix):
    if not raw:
        return ""
    handle, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(handle, "wb") as file:
        file.write(raw)
    return path


def _drag(page, slider, dest):
    """滑条起点有约 10px 死区，越过后再按读回的拼图位置闭环补齐。"""
    cx = slider["x"] + slider["width"] / 2
    cy = slider["y"] + slider["height"] / 2
    page.mouse.move(cx, cy, steps=3)
    page.mouse.down()
    page.wait_for_timeout(60)
    page.mouse.move(cx + 18, cy, steps=5)
    page.wait_for_timeout(120)
    offset = 18.0
    for _ in range(12):
        current = page.evaluate(
            "() => { const e = document.querySelector('.yidun_jigsaw');"
            " return e && e.style.left ? parseFloat(e.style.left) : 0 }") or 0.0
        remain = dest - current
        if abs(remain) < 0.6:
            break
        offset += remain
        page.mouse.move(cx + offset, cy + random.uniform(-0.3, 0.3), steps=3)
        page.wait_for_timeout(110)
    page.wait_for_timeout(120)
    page.mouse.up()


def _dump(cookies):
    return [{"name": c["name"], "value": c["value"],
             "domain": c.get("domain") or "", "path": c.get("path") or "/"} for c in cookies]


def _safe(value):
    return str(value)[:120] if value else ""

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from lxml import html
from playwright.sync_api import Error, expect, sync_playwright

from web.hosting import HostedAccess, Hub
from web.notice import notice_fields, render_notice
from web.server import make_handler


class NoticeMarkdownTest(unittest.TestCase):
    def test_basic_markdown_and_safe_links(self):
        result = html.fromstring("<div>" + render_notice(
            "# 提醒\n\n**粗体**和*斜体*与`代码`\n\n- 第一项\n- 第二项\n\n"
            "1. 步骤\n\n> 引用\n\n```python\nprint('<tag>')\n```\n\n"
            "[指南](https://example.org/help) [邮件](mailto:support@example.org)") + "</div>")
        for tag in ("h1", "strong", "em", "code", "ul", "ol", "blockquote", "pre"):
            self.assertTrue(result.xpath(".//" + tag), tag)
        self.assertEqual(result.xpath(".//pre/code")[0].text, "print('<tag>')\n")
        for link in result.xpath(".//a"):
            self.assertEqual(link.get("target"), "_blank")
            self.assertEqual(link.get("rel"), "noopener noreferrer")

    def test_raw_html_images_and_executable_links_are_not_rendered(self):
        text = ("<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n\n"
                "![图片](https://example.org/image.png)\n\n"
                "[脚本](javascript:alert%281%29) [数据](data:text/html,test) "
                "[文件](file:///tmp/test) [自定义](custom:run) [坏地址](https://[)")
        result = html.fromstring("<div>" + render_notice(text) + "</div>")
        self.assertFalse(result.xpath(".//script|.//img|.//iframe"))
        self.assertIn("<script>", result.text_content())
        self.assertTrue(all(not link.get("href", "").startswith(("javascript:", "data:", "file:", "custom:"))
                            for link in result.xpath(".//a")))

    def test_notice_validation_preserves_markdown_whitespace(self):
        text = "    代码块\n\n正文\n"
        self.assertEqual(notice_fields({"title": " ", "body": text, "enabled": True}),
                         {"title": "站点公告", "body": text, "enabled": True})
        for fields in ({"body": " ", "enabled": True}, {"title": "x" * 101}, {"body": "x" * 10001},
                       {"enabled": "false"}, {"body": []}, {"title": None}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    notice_fields(fields)


class NoticeLoginBrowserTest(unittest.TestCase):
    def test_confirm_reload_relogin_and_updated_notice(self):
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Error as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("Chromium is not installed")
                raise
            with tempfile.TemporaryDirectory() as directory, patch("web.accounts.ITERATIONS", 1000):
                server = ThreadingHTTPServer(("127.0.0.1", 0), None)
                server.daemon_threads = True
                port = server.server_address[1]
                base = "http://127.0.0.1:%d" % port
                hub = Hub(directory, base, key=os.urandom(32))
                uid = hub.accounts.create_user("notice-test", "notice-test-password", role="admin")
                hub.save_notice({"title": "公告", "body": "**消息**", "enabled": True})
                server.RequestHandlerClass = make_handler(HostedAccess(hub, port))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    page = browser.new_page()
                    state = hub.app(uid).state()
                    next(p for p in state["platforms"] if p["id"] == "zhs")["logged_in"] = True
                    blocked = []
                    page.route("**/api/state", lambda route: route.fulfill(json=state))
                    page.route("**/api/platforms/zhs/courses*", lambda route: blocked.append(route))

                    def sign_in():
                        form = page.locator("[data-auth]")
                        form.locator('[name="username"]').fill("notice-test")
                        form.locator('[name="password"]').fill("notice-test-password")
                        form.locator(".auth_submit").click()

                    page.goto(base + "/#/settings/study")
                    with page.expect_request("**/api/platforms/zhs/courses*"):
                        sign_in()
                    dialog = page.get_by_role("dialog", name="公告", exact=True)
                    expect(dialog).to_be_visible()  # A blocked course request must not delay the notice.
                    page.unroute("**/api/state")
                    for request in blocked:
                        request.fulfill(json={"courses": []})
                    expect(dialog.locator("strong")).to_have_text("消息")
                    dialog.get_by_role("button", name="我知道了").click()
                    # The close event is queued; acknowledgement must already be saved before an immediate reload.
                    self.assertTrue(page.evaluate("sessionStorage.getItem('studia-notice-seen')"))
                    page.reload()
                    expect(page.locator("[data-step]").first).to_be_visible()
                    expect(dialog).to_have_count(0)
                    page.goto(base + "/#/me")
                    page.get_by_role("button", name="退出 Studia", exact=True).click()
                    sign_in()
                    expect(dialog).to_be_visible()
                    page.keyboard.press("Escape")
                    self.assertTrue(page.evaluate("sessionStorage.getItem('studia-notice-seen')"))
                    page.reload()
                    expect(page.get_by_role("button", name="退出 Studia", exact=True)).to_be_visible()
                    expect(dialog).to_have_count(0)
                    hub.save_notice({"title": "新公告", "body": "更新", "enabled": True})
                    page.reload()
                    expect(page.get_by_role("dialog", name="新公告", exact=True)).to_be_visible()
                finally:
                    browser.close()
                    server.shutdown()
                    server.server_close()

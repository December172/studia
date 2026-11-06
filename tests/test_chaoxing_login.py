import unittest

from ChaoxingMain.login import qr_login

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class FakeClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.finished = None
        self.refreshes = 0

    def login_qr(self):
        return ({"uuid": "u1", "enc": "e1"}, PNG)

    def login_qr_status(self, token):
        return self.statuses.pop(0)

    def refresh_login_qr(self, token):
        self.refreshes += 1
        return ({"uuid": "u2", "enc": "e2"}, PNG)

    def finish_qr_login(self, result=None):
        self.finished = result


class QrLoginTest(unittest.TestCase):
    def login(self, client, **kwargs):
        return qr_login(client, open_page=lambda _: None, sleep=lambda _: None, **kwargs)

    def test_scan_then_phone_confirmation(self):
        notes = []
        client = FakeClient([{"status": -1}, {"status": 0, "nickname": "张三"}, {"status": 1}])
        self.assertTrue(self.login(client, on_status=notes.append))
        self.assertIn("已扫码", notes[0])
        self.assertIn("张三", notes[0])
        self.assertIsNotNone(client.finished)

    def test_expired_code_is_refreshed(self):
        client = FakeClient([{"status": 2}, {"status": 1}])
        self.assertTrue(self.login(client))
        self.assertEqual(client.refreshes, 1)

    def test_cancel_stops_with_the_platform_message(self):
        notes = []
        client = FakeClient([{"status": 3, "message": "这个账号需要手机二次验证"}])
        self.assertFalse(self.login(client, on_status=notes.append))
        self.assertIn("二次验证", notes[-1])

    def test_gives_up_after_repeated_expiry(self):
        client = FakeClient([{"status": 2}] * 10)
        self.assertFalse(self.login(client))
        self.assertEqual(client.refreshes, 5)


if __name__ == "__main__":
    unittest.main()

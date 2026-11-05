"""见面课接口、断点续看和两个入口；不播放真实课程。"""
import io
import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from rich.console import Console

from base.auth import AuthenticationExpired, PlatformUnavailable
from settings import SettingsStore
from ui import TerminalUI
from web.server import HttpError, NeedsRetry, Studia
from ZHSMain.api import CaptchaRequired, ZHSApi, ZHSError
from ZHSMain.meeting import MeetingHandler, MeetingPlayer, replay_url, resume_target

ROW = {"courseName": "基因与健康", "zhs": {"kind": "shared", "recruitId": 7, "courseId": 9}}
MEETING = {"taskId": "10", "taskName": "见面课一", "taskType": "1", "taskStatus": "3",
           "liveCourseId": "11", "courseId": 9, "recruitId": 7, "videoId": "20,21",
           "replayCourseUrl": "//lc.zhihuishu.com/live/vod_room.html?liveId=11&recruitId=7"}
OPEN = {"signInStatus": "1", "courseEndStatus": "1", "openStatus": "2", "courseLeaveStatus": "2"}
DONE = dict(OPEN, signInStatus="2")


class MeetingApiTest(unittest.TestCase):
    def test_sso_then_zero_based_pagination_and_empty_courses(self):
        client = ZHSApi()
        client._request = Mock(side_effect=[Mock(url="https://stuonline.zhihuishu.com/stuonline/teachMeeting/stuListV2"),
                                           {"mapCount": 11, "maps": [MEETING] * 10},
                                           {"mapCount": 11, "maps": [dict(MEETING, taskId="22")]}])
        self.assertEqual(len(client.meeting_list(7)), 11)
        calls = client._request.call_args_list
        self.assertEqual([c.kwargs["data"]["page.pageNo"] for c in calls[1:]], [0, 1])
        self.assertEqual(calls[1].kwargs["data"]["recruitId"], 7)
        client._request.side_effect = [Mock(url="https://stuonline.zhihuishu.com/"), {"mapCount": 0, "maps": None}]
        self.assertEqual(client.meeting_list(7), [])

    def test_expired_session_and_truncated_list_are_errors(self):
        client = ZHSApi()
        client._request = Mock(return_value=Mock(url="https://passport.zhihuishu.com/login"))
        with self.assertRaises(AuthenticationExpired):
            client.meeting_list(7)
        client._request.side_effect = [Mock(url="https://stuonline.zhihuishu.com/"), {"maps": [], "mapCount": 2}]
        with self.assertRaises(PlatformUnavailable):
            client.meeting_list(7)

    def test_status_parses_jsonp_but_never_executes_it(self):
        client = ZHSApi()
        client.session.cookies.set("CASLOGC", json.dumps({"userId": "u"}))
        client._request = Mock(return_value=Mock(text="studia(" + json.dumps(DONE) + ");"))
        self.assertEqual(client.meeting_status(MEETING), DONE)
        self.assertEqual(client._request.call_args.kwargs["params"]["liveCourseId"], "11")
        for bad, error in (("studia({}); alert(1)", PlatformUnavailable),
                           ('studia({"unlogin":"unlogin"})', AuthenticationExpired),
                           ('studia({"status":500})', PlatformUnavailable)):
            client._request.return_value.text = bad
            with self.assertRaises(error):
                client.meeting_status(MEETING)


class ReplayTest(unittest.TestCase):
    def test_resume_rounds_each_video_to_minutes_and_preserves_holes(self):
        videos = [{"id": 20, "duration": 61}, {"id": 21, "duration": 125}]
        self.assertEqual(resume_target(videos, ["1", "2", "3", "5"]),
                         {"id": 21, "index": 1, "position": 60})
        self.assertEqual(resume_target(videos, ["2", "3"])["position"], 0)
        self.assertIsNone(resume_target(videos, range(1, 6)))
        with self.assertRaises(ZHSError):
            resume_target([{"id": 1, "duration": 0}], [])

    def test_only_the_supported_official_replay_page_is_opened(self):
        self.assertTrue(replay_url(MEETING).startswith("https://lc.zhihuishu.com/"))
        with self.assertRaises(ZHSError):
            replay_url(dict(MEETING, replayCourseUrl="https://example.com/live/vod_room.html"))

    def test_failed_browser_entry_releases_playwright(self):
        player = MeetingPlayer(Mock(access_token="[]"), MEETING)
        with patch("playwright.sync_api.sync_playwright") as start:
            pw = start.return_value.start.return_value
            pw.chromium.launch.side_effect = RuntimeError("launch failed")
            with self.assertRaises(RuntimeError):
                player.__enter__()
            pw.stop.assert_called_once()

    def test_loading_legacy_player_avoids_dom_wait_helpers(self):
        player = MeetingPlayer(Mock(access_token="[]"), MEETING)
        with patch("playwright.sync_api.sync_playwright") as start:
            pw = start.return_value.start.return_value
            page = pw.chromium.launch.return_value.new_context.return_value.new_page.return_value
            page.url = player.url
            page.evaluate.return_value = True
            page.wait_for_timeout.side_effect = lambda _: setattr(player, "history_loaded", True)
            with player:
                self.assertTrue(player.history_loaded)
            page.wait_for_function.assert_not_called()
            pw.chromium.launch.return_value.close.assert_called_once()
            pw.stop.assert_called_once()

    def test_rejected_progress_is_not_treated_as_playback_success(self):
        for code, expected in (("-12", CaptchaRequired), ("401", AuthenticationExpired), ("-1", PlatformUnavailable)):
            player = MeetingPlayer(Mock(access_token="[]"), MEETING)
            player.page = Mock()
            player._response(Mock(url="https://appcomm-live-api.zhihuishu.com/pc/live/setWatchHistorySecret",
                                  status=200, json=Mock(return_value={"status": code})))
            with self.assertRaises(expected):
                player.wait()
        player = MeetingPlayer(Mock(access_token="[]"), MEETING)
        player._response(Mock(url="https://appcomm-live-api.zhihuishu.com/pc/live/getWatchHistory",
                              status=401, json=Mock(side_effect=ValueError())))
        self.assertIsInstance(player.error, AuthenticationExpired)


class MeetingStudyTest(unittest.TestCase):
    def client(self, meetings=None, states=None):
        return Mock(access_token="[]", meeting_list=Mock(return_value=[MEETING] if meetings is None else meetings),
                    meeting_status=Mock(side_effect=[OPEN, DONE] if states is None else states))

    def test_done_and_future_meetings_never_open_a_player(self):
        client = self.client([MEETING, dict(MEETING, taskStatus="1")], [DONE, OPEN])
        events = []
        with patch("ZHSMain.meeting.MeetingPlayer") as player:
            handler = MeetingHandler(client=client, course_rows=[ROW], on_progress=events.append)
        player.assert_not_called()
        self.assertEqual((handler.confirmed, handler.pending, handler.failed), (1, 1, 0))
        self.assertEqual([e.status for e in events if e.kind == "result"], ["waiting"])

    def test_only_server_confirmation_completes_a_replay(self):
        client = self.client(states=[OPEN, OPEN, DONE])
        player = MagicMock()
        tick = iter(range(100))
        player.snapshot.side_effect = lambda: {"key": "20", "position": next(tick), "percent": "100"}
        with patch("ZHSMain.meeting.MeetingPlayer") as factory:
            factory.return_value.__enter__.return_value = player
            handler = MeetingHandler(client=client, course_rows=[ROW])
        self.assertEqual(player.wait.call_count, 60)  # displayed 100% is not authoritative
        self.assertEqual((handler.confirmed, handler.failed), (1, 0))
        factory.return_value.__exit__.assert_called_once()

    def test_offline_closed_and_missing_replays_remain_pending(self):
        for item, state in ((MEETING, dict(OPEN, openStatus="1")),
                            (MEETING, dict(OPEN, courseEndStatus="0")),
                            (dict(MEETING, videoId=""), OPEN),
                            (dict(MEETING, taskType="2"), OPEN)):
            with patch("ZHSMain.meeting.MeetingPlayer") as player:
                handler = MeetingHandler(client=self.client([item], [state]), course_rows=[ROW])
            player.assert_not_called()
            self.assertEqual((handler.confirmed, handler.pending), (0, 1))

    def test_cancellation_and_captcha_stop_the_queue_and_close_player(self):
        cancel = threading.Event()
        player = MagicMock()
        player.wait.side_effect = lambda: cancel.set()
        player.snapshot.return_value = {"key": "20", "position": 1}
        with patch("ZHSMain.meeting.MeetingPlayer") as factory:
            factory.return_value.__enter__.return_value = player
            with self.assertRaises(KeyboardInterrupt):
                MeetingHandler(client=self.client(), course_rows=[ROW], cancel_event=cancel)
            factory.return_value.__exit__.assert_called_once()
        client = self.client(states=[CaptchaRequired("需要验证")])
        handler = MeetingHandler(client=client, course_rows=[ROW, ROW])
        self.assertIsInstance(handler.blocked, CaptchaRequired)
        self.assertEqual(client.meeting_list.call_count, 1)

    def test_a_stalled_player_fails_without_claiming_attendance(self):
        client = self.client(states=None)
        client.meeting_status.side_effect = None
        client.meeting_status.return_value = OPEN
        player = MagicMock()
        player.snapshot.return_value = {"key": "20", "position": 10}
        with patch("ZHSMain.meeting.MeetingPlayer") as factory:
            factory.return_value.__enter__.return_value = player
            handler = MeetingHandler(client=client, course_rows=[ROW])
        self.assertEqual((handler.confirmed, handler.failed), (0, 1))


class EntryTest(unittest.TestCase):
    def test_menu_is_shared_course_only(self):
        view = TerminalUI(console=Console(file=io.StringIO()), input_fn=lambda: "q")
        for row, expected in ((ROW, True), ({"courseName": "AI", "zhs": {"kind": "ai"}}, False)):
            with patch.object(view, "choice", return_value=1) as choice:
                view.course_menu([row], 2)
            self.assertEqual(any(c[1] == "见面课" for c in choice.call_args.args[1]), expected)
            ids = [c[0] for c in choice.call_args.args[1]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_terminal_dispatches_only_shared_courses_to_meeting_handler(self):
        from app import Application
        from course_flow import CourseWorkspace
        from ui import Back

        app = MagicMock()
        app.view.course_menu.side_effect = [6, Back()]
        app.view.operation_result.return_value = 1
        app.study.return_value = None
        workspace = CourseWorkspace(app)
        workspace.platform, workspace.client = 2, Mock()
        workspace.client.score_shares.return_value = {}
        workspace.course_loop([ROW, {"zhs": {"kind": "ai"}}])
        app.study.assert_called_once_with(2, workspace.client, [ROW], meeting=True)
        with tempfile.TemporaryDirectory() as directory:
            real = Application(MagicMock(), store=SettingsStore(directory))
            with patch("ZHSMain.meeting.MeetingHandler") as handler:
                real.study(2, workspace.client, [ROW], meeting=True)
            handler.assert_called_once()

    def test_web_dispatches_meetings_and_reports_pending_instead_of_done(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Studia(SettingsStore(directory), oauth=Mock())
            platform = app.platforms["zhs"]
            platform.client = Mock()
            job = {"id": "a", "kind": "meeting", "cancel": threading.Event()}
            handler = Mock(blocked=None, failed=0, pending=1, summary="未开放")
            with patch("web.server.MeetingHandler", return_value=handler) as factory:
                with self.assertRaises(NeedsRetry):
                    app._study(platform, ROW, 0, job)
                self.assertIs(factory.call_args.kwargs["cancel_event"], job["cancel"])
                handler.pending = 0
                self.assertEqual(app._study(platform, ROW, 0, job), handler.summary)
            platform.rows = [{"courseName": "AI", "zhs": {"kind": "ai", "courseId": 1}}]
            with self.assertRaises(HttpError):
                app.start_job({"platform": "zhs", "type": "meeting", "course": "ai-1"})


if __name__ == "__main__":
    unittest.main()

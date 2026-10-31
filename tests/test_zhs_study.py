import random
import threading
import unittest
from unittest.mock import patch

from base.auth import AuthenticationExpired
from reporting import ProgressEvent
from ZHSMain.api import CaptchaRequired, StudyTimeLimit, ZHSApiError
from ZHSMain.main import ZHSHandler

EV_KEY = "zzpttjd"


def decode_ev(text):
    key = "zzpttjd"
    chars = []
    for index in range(0, len(text), 2):
        chars.append(chr(int(text[index:index + 2], 16) ^ ord(key[(index // 2) % len(key)])))
    return "".join(chars)


class FixedRng(random.Random):
    def random(self):
        return 0.5

    def uniform(self, a, b):
        return (a + b) / 2

    def randint(self, a, b):
        return 0


class FakeClient:
    def __init__(self, uuid="U-1"):
        self.uuid = uuid
        self.calls = []
        self.course_info = {"recruitId": 5, "courseInfo": {"courseId": 42}}
        self.videos = {"courseId": 9, "videoChapterDtos": []}
        self.lv = {}
        self.lesson = {}
        self.study_info_queue = None
        self.prelearning_queue = []
        self.prelearning_default = {"studiedLessonDto": {"id": 100, "studyTotalTime": 0}}
        self.question_points = []
        self.questions = {}
        self.save_progress_errors = []
        self.fail_gologin = set()
        self.basic = {}
        self.ai_resources_queue = {}
        self.ai_lengths = {}
        self.ai_report_fail_file = None
        self.ai_report_fail_times = 0

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def names(self):
        return [name for name, _ in self.calls]

    def args_of(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]

    def shared_gologin(self, secret):
        self._record("shared_gologin", secret=secret)
        if secret in self.fail_gologin:
            raise RuntimeError("gologin failed")

    def shared_course(self, secret):
        self._record("shared_course", secret=secret)
        return self.course_info

    def shared_videos(self, secret):
        self._record("shared_videos", secret=secret)
        return self.videos

    def shared_study_info(self, lesson_ids, video_ids, recruit_id):
        self._record("shared_study_info", lesson_ids=lesson_ids, video_ids=video_ids)
        if self.study_info_queue is not None:
            return self.study_info_queue.pop(0) if len(self.study_info_queue) > 1 else self.study_info_queue[0]
        return {"lv": self.lv, "lesson": self.lesson}

    def shared_last_video(self, recruit_id):
        self._record("shared_last_video", recruit_id=recruit_id)

    def shared_prelearning(self, payload):
        self._record("shared_prelearning", payload=payload)
        if self.prelearning_queue:
            return self.prelearning_queue.pop(0) if len(self.prelearning_queue) > 1 else self.prelearning_queue[0]
        return self.prelearning_default

    def shared_question_points(self, payload):
        self._record("shared_question_points", payload=payload)
        return {"questionPoint": self.question_points}

    def shared_popup_question(self, payload):
        self._record("shared_popup_question", payload=payload)
        question = self.questions[payload["questionIds"][0]]
        return {"lessonTestQuestionUseInterfaceDtos": [{"testQuestion": question}]}

    def shared_save_popup(self, payload):
        self._record("shared_save_popup", payload=payload)

    def shared_courseware(self, video_id):
        self._record("shared_courseware", video_id=video_id)

    def shared_save_cache(self, payload):
        self._record("shared_save_cache", payload=payload)

    def shared_save_progress(self, payload):
        self._record("shared_save_progress", payload=dict(payload))
        if self.save_progress_errors:
            raise self.save_progress_errors.pop(0)

    def ai_course_basic(self, course_id, class_id):
        self._record("ai_course_basic", course_id=course_id, class_id=class_id)
        return self.basic

    def ai_resources(self, course_id, class_id, knowledge_id):
        self._record("ai_resources", knowledge_id=knowledge_id)
        queue = self.ai_resources_queue[knowledge_id]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def ai_complete(self, course_id, class_id, knowledge_id, resources_uid):
        self._record("ai_complete", resources_uid=resources_uid)

    def ai_video_length(self, course_id, class_id, file_id):
        self._record("ai_video_length", file_id=file_id)
        return self.ai_lengths[file_id]

    def ai_report(self, course_id, class_id, file_id, knowledge_id, position):
        self._record("ai_report", file_id=file_id, position=position)
        if file_id == self.ai_report_fail_file and self.ai_report_fail_times > 0:
            self.ai_report_fail_times -= 1
            raise RuntimeError("report failed")

    def open_video(self, video_id=None, file_id=None):
        self._record("open_video", video_id=video_id, file_id=file_id)


def shared_row(name="共享课", secret="s1"):
    return {"courseName": name, "courseInfoName": "共享课",
            "zhs": {"kind": "shared", "secret": secret, "recruitId": 5, "courseId": 42}}


def ai_row(name="智慧课"):
    return {"courseName": name, "courseInfoName": "智慧课程",
            "zhs": {"kind": "ai", "courseId": 7, "classId": 8}}


def run(client, rows, events=None):
    return ZHSHandler(
        client=client, course_rows=rows,
        on_progress=events.append if events is not None else None,
        sleep=lambda seconds: None, rng=FixedRng(),
    )


def two_video_course():
    return {
        "courseId": 9,
        "videoChapterDtos": [{
            "id": 7, "name": "第一章",
            "videoLessons": [
                {"id": 100, "name": "已完成", "videoId": 555, "videoSec": 30,
                 "videoSmallLessons": []},
                {"id": 101, "name": "待学", "videoId": 666, "videoSec": 999,
                 "videoSmallLessons": [
                     {"id": 11, "videoId": 777, "name": "待学-1", "videoSec": 10,
                      "lessonId": 101},
                 ]},
            ],
        }],
    }


class SharedStudyTest(unittest.TestCase):
    def test_skip_done_and_final_report(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.lesson = {"100": {"watchState": 1, "studyTotalTime": 30}}
        client.lv = {"11": {"watchState": 0, "studyTotalTime": 0}}
        client.study_info_queue = [
            {"lv": {"11": {"watchState": 0, "studyTotalTime": 0}},
             "lesson": {"100": {"watchState": 1, "studyTotalTime": 30}}},
            {"lv": {"11": {"watchState": 1, "studyTotalTime": 10}},
             "lesson": {"100": {"watchState": 1, "studyTotalTime": 30}}},
        ]
        events = []
        run(client, [shared_row()], events)

        progress = client.args_of("shared_save_progress")
        self.assertTrue(progress)
        last = progress[-1]["payload"]
        self.assertEqual(last["courseId"], 42)
        fields = decode_ev(last["sdsew"]).split(";")
        self.assertEqual(fields, ["5", "101", "11", "777", "7", "0", "10", "10",
                                  "0:00:10", "U-1zhs"])
        names = client.names()
        cache_index = max(i for i, name in enumerate(names) if name == "shared_save_cache")
        last_progress = max(i for i, name in enumerate(names) if name == "shared_save_progress")
        self.assertLess(cache_index, last_progress)
        results = [event for event in events if event.kind == "result"]
        self.assertEqual([(event.name, event.status) for event in results],
                         [("共享课", "success")])

    def test_popup_question_answered(self):
        client = FakeClient()
        client.videos = {
            "courseId": 9,
            "videoChapterDtos": [{
                "id": 7, "name": "ch",
                "videoLessons": [{"id": 100, "name": "v", "videoId": 555,
                                  "videoSec": 10, "videoSmallLessons": []}],
            }],
        }
        client.lesson = {"100": {"watchState": 0, "studyTotalTime": 0}}
        client.question_points = [
            {"timeSec": 0, "questionIds": [1]},
            {"timeSec": 5, "questionIds": [2]},
        ]
        client.questions = {2: {
            "questionId": 88,
            "questionOptions": [{"id": 11, "result": 1}, {"id": 12, "result": "1"},
                                {"id": 13, "result": 0}],
        }}
        run(client, [shared_row()], [])

        asked = client.args_of("shared_popup_question")
        self.assertEqual([args["payload"]["questionIds"] for args in asked], [[2]])
        saved = client.args_of("shared_save_popup")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["payload"]["answer"], "11,12")
        self.assertEqual(saved[0]["payload"]["testQuestionId"], 88)
        self.assertEqual(saved[0]["payload"]["courseId"], 9)


class SharedReviewTest(unittest.TestCase):
    def test_review_rewatches_finished_videos_from_start_until_budget(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.lesson = {"100": {"watchState": 1, "studyTotalTime": 30}}
        client.lv = {"11": {"watchState": 1, "studyTotalTime": 12}}
        client.question_points = [{"timeSec": 5}]
        events = []
        ZHSHandler(client=client, course_rows=[shared_row(), ai_row()],
                   on_progress=events.append, sleep=lambda seconds: None,
                   rng=FixedRng(), review_minutes=1)

        fields = [decode_ev(call["payload"]["sdsew"]).split(";")
                  for call in client.args_of("shared_save_progress")]
        self.assertEqual(sum(int(f[6]) for f in fields), 60)
        for f in fields:
            start, length = (30, "0:00:30") if f[2] == "0" else (12, "0:00:10")
            self.assertGreaterEqual(int(f[7]), start)
            self.assertLessEqual(f[8], length)
        self.assertIn("0:00:00", [f[8] for f in fields])
        self.assertNotIn("shared_popup_question", client.names())
        self.assertNotIn("ai_resources", client.names())
        results = [(event.name, event.status) for event in events if event.kind == "result"]
        self.assertEqual(results, [("共享课", "success"), ("智慧课", "skipped")])

    def test_review_without_finished_videos_is_skipped(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.lesson = {"100": {"watchState": 0, "studyTotalTime": 0}}
        client.lv = {"11": {"watchState": 0, "studyTotalTime": 0}}
        ZHSHandler(client=client, course_rows=[shared_row()], sleep=lambda seconds: None,
                   rng=FixedRng(), review_minutes=1)
        self.assertEqual(client.args_of("shared_save_progress"), [])


class StudyLogTest(unittest.TestCase):
    def test_shared_play_logs(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.lesson = {"100": {"watchState": 1, "studyTotalTime": 30}}
        client.lv = {"11": {"watchState": 0, "studyTotalTime": 0}}
        with self.assertLogs("ZHSMain.main", "INFO") as captured:
            run(client, [shared_row()], [])
        text = "\n".join(captured.output)
        self.assertIn("共 2 个视频（已完成 1）", text)
        self.assertIn("开始播放：待学-1", text)
        self.assertIn("播放完成：待学-1", text)


class StopTest(unittest.TestCase):
    def test_study_time_limit_stops_later_courses(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.save_progress_errors = [ZHSApiError(-9, "limit")]
        events = []
        run(client, [shared_row("A", "a"), shared_row("B", "b")], events)
        self.assertEqual(client.args_of("shared_gologin"), [{"secret": "a"}])
        results = [(event.name, event.status) for event in events if event.kind == "result"]
        self.assertEqual(results, [("A", "error")])

    def test_captcha_at_precheck_stops_later_courses(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.save_progress_errors = [CaptchaRequired("captcha")]
        events = []
        run(client, [shared_row("A", "a"), shared_row("B", "b")], events)
        self.assertEqual(len(client.args_of("shared_gologin")), 1)
        self.assertEqual(events[-1].status, "error")

    def test_authentication_expired_propagates(self):
        client = FakeClient()
        client.shared_gologin = lambda secret: (_ for _ in ()).throw(AuthenticationExpired("x"))
        with self.assertRaises(AuthenticationExpired):
            run(client, [shared_row()], [])

    def test_ordinary_error_keeps_next_course(self):
        client = FakeClient()
        client.fail_gologin = {"bad"}
        client.basic = {"cakeThemeList": [{"knowledgeList": [{"knowledgeId": 1}]}]}
        done = [{"studyStatus": 1, "resourcesDetail": {
            "resourcesType": 3, "resourcesDistributeType": 1,
            "resourcesUid": "u1", "resourcesName": "doc"}}]
        todo = [dict(done[0], studyStatus=0)]
        client.ai_resources_queue = {1: [todo, done]}
        events = []
        run(client, [shared_row("A", "bad"), ai_row("B")], events)
        results = [(event.name, event.status) for event in events if event.kind == "result"]
        self.assertEqual(results, [("A", "error"), ("B", "success")])
        self.assertEqual(len(client.args_of("ai_complete")), 1)


class RetryTest(unittest.TestCase):
    def test_multi_window_refreshes_token(self):
        client = FakeClient()
        client.videos = {
            "courseId": 9,
            "videoChapterDtos": [{
                "id": 7, "name": "ch",
                "videoLessons": [{"id": 100, "name": "v", "videoId": 555,
                                  "videoSec": 3, "videoSmallLessons": []}],
            }],
        }
        client.lesson = {"100": {"watchState": 0, "studyTotalTime": 0}}
        client.save_progress_errors = [ZHSApiError(-10, "multi window")]
        client.prelearning_queue = [
            {"studiedLessonDto": {"id": 100, "studyTotalTime": 0}},
            {"studiedLessonDto": {"id": 200, "studyTotalTime": 0}},
        ]
        run(client, [shared_row()], [])

        self.assertEqual(len(client.args_of("shared_prelearning")), 2)
        tokens = [args["payload"]["zwsds"] for args in client.args_of("shared_save_progress")]
        self.assertIn("MjAw", tokens)
        last = client.args_of("shared_save_progress")[-1]["payload"]
        self.assertEqual(decode_ev(last["sdsew"]).split(";")[7], "3")

    def test_network_error_during_report_is_retried(self):
        from base.auth import PlatformUnavailable
        client = FakeClient()
        client.videos = {
            "courseId": 9,
            "videoChapterDtos": [{
                "id": 7, "name": "ch",
                "videoLessons": [{"id": 100, "name": "v", "videoId": 555,
                                  "videoSec": 3, "videoSmallLessons": []}],
            }],
        }
        client.lesson = {"100": {"watchState": 0, "studyTotalTime": 0}}
        client.save_progress_errors = [PlatformUnavailable("network")]
        events = []
        run(client, [shared_row()], events)

        last = client.args_of("shared_save_progress")[-1]["payload"]
        self.assertEqual(decode_ev(last["sdsew"]).split(";")[7], "3")
        last_resources = [event.name for event in events if event.kind == "resources"][-1]
        self.assertIn("失败 0", last_resources)

    def test_study_time_backwards_uses_server_time(self):
        client = FakeClient()
        client.videos = {
            "courseId": 9,
            "videoChapterDtos": [{
                "id": 7, "name": "ch",
                "videoLessons": [{"id": 100, "name": "v", "videoId": 555,
                                  "videoSec": 10, "videoSmallLessons": []}],
            }],
        }
        client.lesson = {"100": {"watchState": 0, "studyTotalTime": 0}}
        client.save_progress_errors = [ZHSApiError(-8, "backwards")]
        client.prelearning_queue = [
            {"studiedLessonDto": {"id": 100, "studyTotalTime": 0}},
            {"studiedLessonDto": {"id": 101, "studyTotalTime": 20}},
        ]
        run(client, [shared_row()], [])

        last = client.args_of("shared_save_progress")[-1]["payload"]
        fields = decode_ev(last["sdsew"]).split(";")
        self.assertEqual(fields[6], "20")
        self.assertEqual(fields[7], "20")
        self.assertEqual(last["zwsds"], "MTAx")


class AiStudyTest(unittest.TestCase):
    def course(self):
        return {"cakeThemeList": [{"knowledgeList": [{"knowledgeId": 1, "knowledgeName": "k"}]}]}

    def test_cancel_stops_inside_an_ai_video(self):
        client = FakeClient()
        client.basic = self.course()
        client.ai_lengths = {"f1": 300}
        video = [{"studyStatus": 0, "schedule": 0, "resourcesDetail": {
            "resourcesType": 1, "resourcesDistributeType": 3,
            "resourcesFileId": "f1", "resourcesName": "video"}}]
        client.ai_resources_queue = {1: [video, video]}
        cancel = threading.Event()
        reports = []
        original = client.ai_report

        def report(*args):
            reports.append(args)
            if len(reports) == 3:
                cancel.set()
            return original(*args)

        client.ai_report = report
        with self.assertRaises(KeyboardInterrupt):
            ZHSHandler(client=client, course_rows=[ai_row()], sleep=lambda seconds: None,
                       rng=FixedRng(), cancel_event=cancel)
        self.assertEqual(len(reports), 3)

    def test_resource_dispatch_and_video_positions(self):
        client = FakeClient()
        client.basic = self.course()
        client.ai_lengths = {"f1": 20}
        initial = [
            {"studyStatus": 0, "schedule": 50, "resourcesDetail": {
                "resourcesType": 1, "resourcesDistributeType": 3,
                "resourcesFileId": "f1", "resourcesName": "video"}},
            {"studyStatus": 0, "resourcesDetail": {
                "resourcesType": 3, "resourcesDistributeType": 1,
                "resourcesUid": "u1", "resourcesName": "doc"}},
            {"studyStatus": 1, "resourcesDetail": {
                "resourcesType": 1, "resourcesDistributeType": 3,
                "resourcesFileId": "f2", "resourcesName": "done"}},
        ]
        completed = [dict(item, studyStatus=1) for item in initial]
        client.ai_resources_queue = {1: [initial, completed]}
        events = []
        run(client, [ai_row()], events)

        self.assertEqual(client.args_of("ai_complete"), [{"resources_uid": "u1"}])
        self.assertNotIn("ai_video_length", [name for name in client.names()
                                             if client.args_of("ai_video_length") and
                                             client.args_of("ai_video_length")[0]["file_id"] == "f2"])
        positions = [args["position"] for args in client.args_of("ai_report")]
        self.assertEqual(positions, [13, 16, 19, 20])
        names = client.names()
        self.assertLess(names.index("ai_complete"), names.index("ai_report"))
        results = [(event.name, event.status) for event in events if event.kind == "result"]
        self.assertEqual(results, [("智慧课", "success")])

    def test_ai_video_progress_follows_real_time_with_stall_cap(self):
        client = FakeClient()
        client.basic = self.course()
        client.ai_lengths = {"f1": 20}
        item = {"studyStatus": 0, "schedule": 0, "resourcesDetail": {
            "resourcesType": 1, "resourcesDistributeType": 3,
            "resourcesFileId": "f1", "resourcesName": "video"}}
        client.ai_resources_queue = {1: [[item], [dict(item, studyStatus=1)]]}
        clock = iter([0, 2.5, 102.5, 104.5, 106.5, 108.5])
        with patch("ZHSMain.main.time.monotonic", lambda: next(clock)):
            run(client, [ai_row()], [])
        positions = [args["position"] for args in client.args_of("ai_report")]
        self.assertEqual(positions, [3, 11, 14, 17, 20])

    def test_video_failure_continues_course(self):
        client = FakeClient()
        client.basic = self.course()
        client.ai_lengths = {"bad": 6}
        client.ai_report_fail_file = "bad"
        client.ai_report_fail_times = 3
        initial = [
            {"studyStatus": 0, "schedule": 0, "resourcesDetail": {
                "resourcesType": 2, "resourcesDistributeType": 2,
                "resourcesFileId": "bad", "resourcesName": "bad video"}},
            {"studyStatus": 0, "resourcesDetail": {
                "resourcesType": 3, "resourcesDistributeType": 1,
                "resourcesUid": "u1", "resourcesName": "doc"}},
        ]
        client.ai_resources_queue = {1: [initial, initial]}
        events = []
        run(client, [ai_row()], events)

        self.assertEqual(len(client.args_of("ai_report")), 3)
        self.assertEqual(client.args_of("ai_complete"), [{"resources_uid": "u1"}])
        results = [(event.name, event.status) for event in events if event.kind == "result"]
        self.assertEqual(results, [("智慧课", "error")])


class ReporterTest(unittest.TestCase):
    def test_event_order(self):
        client = FakeClient()
        client.videos = two_video_course()
        client.lesson = {"100": {"watchState": 1, "studyTotalTime": 30}}
        client.lv = {"11": {"watchState": 1, "studyTotalTime": 10}}
        events = []
        run(client, [shared_row()], events)

        self.assertEqual(events[0].kind, "begin")
        self.assertEqual(events[0].total, 1)
        self.assertEqual(events[0].unit, "门课程")
        kinds = [event.kind for event in events]
        self.assertLess(kinds.index("begin"), kinds.index("resources"))
        resources = [event for event in events if event.kind == "resources"]
        self.assertEqual(resources[0].completed, 0)
        self.assertEqual(resources[-1].completed, resources[-1].total)
        self.assertEqual(events[-1].kind, "result")
        self.assertEqual(events[-1].status, "success")
        self.assertIsInstance(events[-1], ProgressEvent)


if __name__ == "__main__":
    unittest.main()

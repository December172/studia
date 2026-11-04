import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from settings import SettingsStore
from web.server import HttpError, Studia, course_features
from ZHSMain.api import ZHSApi

SHARED = {"courseName": "新一代信息技术导论", "zhs": {"kind": "shared", "courseId": 1000091138, "recruitId": 444872, "secret": "s1"}}
MOOC_LIKE = {"courseName": "通信基础与计算机网络", "zhs": {"kind": "shared", "courseId": 1000153421, "recruitId": 476565, "secret": "s2"}}
AI = {"courseName": "通信基础与计算机网络2026年秋冬学期", "zhs": {"kind": "ai", "courseId": 3, "classId": 9}}
# The two rules read from the live 成绩分析 page on 2026-11-03.
EARNS = {"progress": 20, "habit": 20, "interactive": 10, "knowledge": 0, "test": 10, "meet": 0, "final": 40}
NEITHER = {"progress": 40, "habit": 0, "interactive": 0, "knowledge": 0, "test": 20, "meet": 0, "final": 40}


class CourseFeaturesTest(unittest.TestCase):
    def test_review_and_qa_follow_the_assessment_rule(self):
        self.assertEqual(course_features("zhs", SHARED, EARNS),
                         {"study": True, "answer": True, "review": True, "qa": True})
        self.assertEqual(course_features("zhs", MOOC_LIKE, NEITHER),
                         {"study": True, "answer": True, "review": False, "qa": False})
        # An unreadable rule narrows nothing beyond the course kind.
        self.assertEqual(course_features("zhs", SHARED, None),
                         {"study": True, "answer": True, "review": True, "qa": True})
        self.assertEqual(course_features("zhs", AI, None),
                         {"study": True, "answer": True, "review": False, "qa": False})
        self.assertEqual(course_features("mooc", {"courseName": "英语"}, None),
                         {"study": True, "answer": True, "review": False, "qa": False})
        # 超星没有自动学习，界面上也不显示学习标签页。
        self.assertEqual(course_features("cx", {"title": "高等数学"}, None),
                         {"study": True, "answer": True, "review": False, "qa": False})

    def test_score_shares_reads_the_rule(self):
        api = ZHSApi()
        api._request = Mock(return_value={"scoreAssessRuleDto": {
            "progressScoreShare": 20, "learnedHabitScoreShare": 20, "learnedInteractiveScoreShare": 10,
            "learnedKnowledgeScoreShare": 0, "examScoreShare": 10, "meetCourseScoreShare": None, "finalExamScoreShare": 40}})
        self.assertEqual(api.score_shares(1000091138, 444872), EARNS)
        self.assertEqual(api._request.call_args.kwargs["data"]["recruitId"], 444872)

    def test_score_earned_gathers_each_share(self):
        api = ZHSApi()
        replies = {"loadStuLearnReport/": {"ssd": {"courseScore": "20", "examScore": "9", "bbsScore": "0", "meetScore": "0",
                                                    "finalScore": "0", "usualScore": "29", "totalScore": "38"}},
                   "loadStuHabit/": {"learnedHabitScore": 9, "learnedHabitTotalScore": 20},
                   "loadCourseForum/": {"learnedInteractiveScore": None, "learnedInteractiveTotalScore": 10}}
        api._request = Mock(side_effect=lambda method, url, **kw: replies[url.rsplit("/stuLearnReportNew/", 1)[1]])
        self.assertEqual(api.score_earned(1000091138, 444872), {"progress": 20, "habit": 9, "interactive": 0, "test": 9,
                                                                "meet": 0, "final": 0, "usual": 29, "total": 38,
                                                                "qa": {"answers": 0, "questions": 0, "received": 0}})

    def test_course_list_carries_features_and_jobs_outside_them_are_refused(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = Studia(SettingsStore(directory.name), oauth=Mock(return_value=None))
        platform = app.platforms["zhs"]
        platform.client = Mock()
        platform.client.my_course_list.return_value = {"rows": [SHARED, MOOC_LIKE, AI]}
        platform.client.score_shares.side_effect = lambda cid, rid: EARNS if cid == 1000091138 else NEITHER
        courses = {c["id"]: c for c in app.courses("zhs")["courses"]}
        self.assertEqual(courses["shared-1000091138"]["features"]["qa"], True)
        self.assertEqual(courses["shared-1000153421"]["shares"], NEITHER)
        self.assertEqual(courses["shared-1000153421"]["features"]["review"], False)
        self.assertIsNone(courses["ai-3"]["shares"])
        with self.assertRaises(HttpError) as refused:
            app.start_job({"platform": "zhs", "type": "review", "course": "shared-1000153421"})
        self.assertIn("学习习惯分", str(refused.exception))
        with self.assertRaises(HttpError):
            app.start_job({"platform": "zhs", "type": "qa", "course": "shared-1000153421"})

    def test_a_rule_that_cannot_be_read_keeps_the_course_usable(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = Studia(SettingsStore(directory.name), oauth=Mock(return_value=None))
        platform = app.platforms["zhs"]
        platform.client = Mock()
        platform.client.my_course_list.return_value = {"rows": [SHARED]}
        platform.client.score_shares.side_effect = RuntimeError("timeout")
        (course,) = app.courses("zhs")["courses"]
        self.assertEqual((course["shares"], course["features"]["review"]), (None, True))


class TerminalMenuTest(unittest.TestCase):
    def menu(self, courses, offers):
        from tests.test_zhs_tasks import make_view
        view, _ = make_view(["q"])
        captured = []
        with patch.object(view, "choice", side_effect=lambda title, choices, **kw: captured.extend(choices) or 1):
            view.course_menu(courses, 2, offers)
        return {name: note for _, name, note in captured}

    def test_items_follow_the_courses_they_apply_to(self):
        both = [SHARED, MOOC_LIKE]
        menu = self.menu(both, {"review": [SHARED], "qa": [SHARED]})
        self.assertIn("只做新一代信息技术导论", menu["每日复习"])
        self.assertIn("只做新一代信息技术导论", menu["问答区"])
        menu = self.menu([MOOC_LIKE], {"review": [], "qa": []})
        self.assertEqual(set(menu), {"自动学习", "AI 答题"})
        menu = self.menu([SHARED], {"review": [SHARED], "qa": [SHARED]})
        self.assertNotIn("只做", menu["每日复习"])

    def test_review_runs_only_on_courses_with_a_habit_share(self):
        from course_flow import CourseWorkspace
        from ui import Back
        view = MagicMock()
        view.course_menu.side_effect = [3, Back()]
        app = MagicMock()
        workspace = CourseWorkspace(app)
        workspace.view, workspace.store, workspace.platform = view, app.store, 2
        workspace.client = Mock()
        workspace.client.score_shares.side_effect = lambda cid, rid: EARNS if cid == 1000091138 else NEITHER
        workspace.course_loop([SHARED, MOOC_LIKE])
        self.assertEqual(view.course_menu.call_args.args[2], {"review": [SHARED], "qa": [SHARED]})
        self.assertEqual(app.study.call_args.args[2], [SHARED])


if __name__ == "__main__":
    unittest.main()

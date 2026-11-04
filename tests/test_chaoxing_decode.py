import unittest

from ChaoxingMain.decode import (
    decode_course_folder,
    decode_course_list,
    decode_course_point,
    decode_questions_info,
    decode_work_jobs,
)
from ChaoxingMain.font import FontDecodeError


COURSE_LIST = """
<div class="course" id="course_1" info="info" roleid="2">
  <a href="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentcourse?courseId=111&clazzid=222&cpi=333&ut=s">
    <span class="course-name" title="高等数学"></span>
  </a>
  <input class="clazzId" value="222">
  <input class="courseId" value="111">
  <p class="margint10" title="2025 秋"></p>
  <p class="color3" title="张老师"></p>
</div>
<div class="course" id="course_2">
  <a class="not-open-tip" href="#">未开放</a>
</div>
<div class="course" id="course_3">
  <a href="https://mooc1.chaoxing.com/x?courseId=1">没有 cpi</a>
</div>
"""

FOLDERS = """
<ul class="file-list">
  <li fileid="777"><input class="rename-input" value="我的课程"></li>
  <li><input class="rename-input" value="没有 fileid"></li>
</ul>
"""

CHAPTERS = """
<div class="chapter_unit">
  <ul>
    <li><div id="cur123"><a class="clicktitle">1.1 函数</a><input class="knowledgeJobCount" value="3"></div></li>
    <li><div id="cur456"><a class="clicktitle">1.2 极限</a><span class="bntHoverTips">需要完成任务点解锁</span></div></li>
    <li><div id="cur789"><a class="clicktitle">1.3 连续</a><span class="bntHoverTips">已完成</span></div></li>
    <li><div>没有点位 id</div></li>
  </ul>
</div>
"""

CARDS = """
<script>
mArg={"defaults":{"ktoken":"kt","cpi":"333","knowledgeid":"123","reportTimeInterval":60,"defenc":"d"},"attachments":[{"type":"workid","jobid":"work-abc","otherInfo":"nodeId_123-rt_1&extra=1","property":{"title":"1.1 章节测验"},"mid":"m","enc":"e","aid":"a","isPassed":false},{"type":"video","jobid":"v1","otherInfo":"nodeId_123"},{"type":"workid","jobid":"work-def","property":{"title":"1.1 作业"},"enc":"e2"}]};
</script>
"""


def question_block(question_id, type_code, title, options=()):
    items = "".join('<li aria-label="%s"></li>' % text for text in options)
    return ('<div class="singleQuesId" data="%s"><div class="TiMu" data="%s">'
            '<div class="Zy_TItle"><span>%s</span></div>%s</div></div>'
            % (question_id, type_code, title, "<ul>%s</ul>" % items if items else ""))


def work_page(*blocks):
    return (
        '<form id="form1">'
        '<input type="hidden" name="courseId" value="111">'
        '<input type="hidden" name="workId" value="abc">'
        '<input type="hidden" name="answerwqbid" value="旧值">'
        + "".join(blocks) +
        '</form>'
        + question_block("outside", "0", "表单外的题", ["A. 1", "B. 2"])
    )


class CourseListTest(unittest.TestCase):
    def test_skips_unopened_and_incomplete_cards(self):
        courses = decode_course_list(COURSE_LIST)
        self.assertEqual([course["courseId"] for course in courses], ["111"])
        course = courses[0]
        self.assertEqual(course["title"], "高等数学")
        self.assertEqual(course["teacher"], "张老师")
        self.assertEqual(course["desc"], "2025 秋")
        self.assertEqual((course["clazzId"], course["cpi"]), ("222", "333"))

    def test_garbage_returns_empty(self):
        self.assertEqual(decode_course_list(""), [])
        self.assertEqual(decode_course_list(None), [])

    def test_folders_need_fileid(self):
        self.assertEqual(decode_course_folder(FOLDERS), [{"id": "777", "rename": "我的课程"}])


class ChapterTest(unittest.TestCase):
    def test_points_and_unlock_state(self):
        result = decode_course_point(CHAPTERS)
        self.assertTrue(result["hasLocked"])
        points = {point["id"]: point for point in result["points"]}
        self.assertEqual(set(points), {"123", "456", "789"})
        self.assertEqual(points["123"]["jobCount"], 3)
        self.assertFalse(points["123"]["need_unlock"])
        self.assertTrue(points["456"]["need_unlock"])
        self.assertFalse(points["456"]["has_finished"])
        self.assertTrue(points["789"]["has_finished"])


class WorkJobTest(unittest.TestCase):
    def test_only_work_cards_are_returned(self):
        jobs, info = decode_work_jobs(CARDS)
        self.assertEqual([job["jobid"] for job in jobs], ["work-abc", "work-def"])
        self.assertEqual(jobs[0]["name"], "1.1 章节测验")
        # otherInfo 只保留第一段；enc 用于打开答题页。
        self.assertEqual(jobs[0]["otherinfo"], "nodeId_123-rt_1")
        self.assertEqual(jobs[0]["enc"], "e")
        self.assertEqual(info["knowledgeid"], "123")
        self.assertEqual(info["ktoken"], "kt")

    def test_not_open_chapter(self):
        jobs, info = decode_work_jobs("<html>章节未开放</html>")
        self.assertEqual(jobs, [])
        self.assertEqual(info, {"notOpen": True})

    def test_page_without_cards(self):
        self.assertEqual(decode_work_jobs("<html></html>"), ([], {}))


class QuestionTest(unittest.TestCase):
    def setUp(self):
        self.page = work_page(
            question_block("q1", "0", "1.（2.0分）1+1=?", ["B. 2", "A. 1"]),
            question_block("q2", "1", "多选", ["C. 3", "A. 1", "B. 2"]),
            question_block("q3", "3", "判断：地球是圆的"),
            question_block("q4", "2", "填空：中国的首都是____"),
            question_block("q5", "4", "简答：谈谈你的理解"),
            question_block("q6", "9", "未知题型"),
        )
        self.sheet = decode_questions_info(self.page)

    def test_form_fields_exclude_answer_inputs(self):
        self.assertEqual(self.sheet["courseId"], "111")
        self.assertEqual(self.sheet["workId"], "abc")
        # 表单里的旧 answerwqbid 被忽略，按题目 id 重新生成。
        self.assertEqual(self.sheet["answerwqbid"], "q1,q2,q3,q4,q5,q6,")

    def test_question_types_and_ids(self):
        questions = self.sheet["questions"]
        self.assertEqual([q["id"] for q in questions], ["q1", "q2", "q3", "q4", "q5", "q6"])
        self.assertEqual([q["type"] for q in questions],
                         ["single", "multiple", "judgement", "completion", "shortanswer", "unknown"])
        self.assertEqual([q["type_code"] for q in questions], ["0", "1", "3", "2", "4", "9"])

    def test_options_are_sorted_with_labels_and_values(self):
        first, second = self.sheet["questions"][:2]
        self.assertEqual([option["label"] for option in first["options"]], ["A", "B"])
        self.assertEqual([option["value"] for option in first["options"]], ["A", "B"])
        self.assertEqual([option["text"] for option in first["options"]], ["A. 1", "B. 2"])
        self.assertEqual([option["label"] for option in second["options"]], ["A", "B", "C"])
        self.assertEqual(self.sheet["questions"][2]["options"], [])

    def test_title_keeps_image_marker(self):
        page = work_page(question_block("q1", "0", "看图做题<img src='https://x/1.png'>", ["A. 对", "B. 错"]))
        question = decode_questions_info(page)["questions"][0]
        self.assertIn("<img", question["title"])

    def test_choice_text_strips_platform_noise(self):
        page = work_page('<div class="singleQuesId" data="q1"><div class="TiMu" data="0">'
                         '<div class="Zy_TItle"><span>题</span></div><ul>'
                         '<li aria-label="A. 甲选择"></li>'
                         '<li aria-label="B. 乙"></li></ul></div></div>')
        options = decode_questions_info(page)["questions"][0]["options"]
        self.assertEqual([option["text"] for option in options], ["A. 甲", "B. 乙"])

    def test_no_options_label_falls_back_to_position(self):
        page = work_page(question_block("q1", "0", "题", ["正确", "错误"]))
        options = decode_questions_info(page)["questions"][0]["options"]
        self.assertEqual([option["label"] for option in options], ["A", "B"])
        self.assertEqual([option["value"] for option in options], ["正", "错"])

    def test_encrypted_font_fails_closed(self):
        page = work_page(question_block("q1", "0", "题", ["A. 1", "B. 2"])).replace(
            "<form", "<style id=\"cxSecretStyle\">@font-face{src:url('data:application/font-ttf;"
                      "charset=utf-8;base64,bm90LWEtZm9udA==')}</style><form")
        with self.assertRaises(FontDecodeError):
            decode_questions_info(page)

    def test_page_without_form(self):
        with self.assertRaises(ValueError):
            decode_questions_info("<html><body>空页面</body></html>")


if __name__ == "__main__":
    unittest.main()

"""超星章节作业自动作答：读题 → AI 作答 → 按覆盖率提交或保存。

迁移自 chaoxing 项目的 ``Chaoxing.study_work``，保留其中的关键策略：

- 交给答题服务的题干先去掉题号和「（1.0 分）」后缀；
- 覆盖率达到阈值才真正提交（``pyFlag`` 为空），否则只保存（``pyFlag=1``），
  避免拿没答完的卷子消耗作业机会；
- 一次交卷请求失败就是失败，不自动重发同一份答案。

与源码的差异（有意为之）：源码在拿不到答案时随机选一个来凑覆盖率。这里不猜：
AI 没给出的题留空并计入未覆盖，覆盖率不够就只保存，留给人工补答。
"""
import re
import threading
import time

from ai.client import AIError, AIServiceError
from ChaoxingMain.api import ChaoxingError, WorkUnavailable
from ChaoxingMain.decode import decode_questions_info
from ChaoxingMain.font import FontDecodeError
from MoocMain.log import Logger
from reporting import Reporter

logger = Logger(__name__).get_log()

# 交给 AI 的题型名称；与 ai/client.py 的系统提示词一一对应。
TYPE_NAMES = {
    "single": "单选题",
    "multiple": "多选题",
    "judgement": "判断题",
    "completion": "填空题",
    "shortanswer": "问答题",
}

TRUE_WORDS = {"true", "正确", "对", "是", "√", "t", "yes", "y"}
FALSE_WORDS = {"false", "错误", "错", "否", "×", "不对", "不正确", "f", "no", "n"}
MEDIA = re.compile(r"<(?:img|audio|video|iframe|svg)\b", re.I)
_OPTION_LABEL_PREFIX = re.compile(r"^[A-Za-z][.、,，:：)）]\s*")
_TITLE_NUMBER = re.compile(r"^\d+(?:[.、．)）]|\s)\s*")
_TITLE_SCORE = re.compile(r"（\d+(?:\.\d+)?分）$")
_TITLE_SCORE_LEADING = re.compile(r"^（\d+(?:\.\d+)?分）")


def prompt_title(title):
    """题号和分值对答题没有帮助，还会让同题匹配变差，先去掉。"""
    text = _TITLE_NUMBER.sub("", (title or "").strip())
    text = _TITLE_SCORE_LEADING.sub("", text)
    return _TITLE_SCORE.sub("", text).strip()


def prompt_body(text):
    """选项正文去掉开头的「A.」，标签已经单独给了模型。"""
    return _OPTION_LABEL_PREFIX.sub("", text or "").strip()


def model_question(question):
    return {
        "type": TYPE_NAMES.get(question["type"], "题目"),
        "title": prompt_title(question["title"]),
        "options": [{"label": option["label"], "text": prompt_body(option["text"])}
                    for option in question["options"]],
    }


def unsupported_reason(question):
    """这道题能不能安全地自动作答；返回空字符串表示可以。"""
    if question["type"] not in TYPE_NAMES:
        return "暂不支持题型 %s" % (question["type_code"] or "?")
    if not (question["title"] or "").strip():
        return "题干为空"
    if MEDIA.search(question["title"]) or any(MEDIA.search(option["text"]) for option in question["options"]):
        return "包含图片或音视频，需要人工处理"
    if question["type"] in ("single", "multiple"):
        labels = [option["label"] for option in question["options"]]
        if len(labels) < 2:
            return "选项不足"
        if len(set(labels)) != len(labels):
            return "选项标签重复"
    return ""


def encode_answer(question, answer):
    """把 AI 的结构化答案换成超星表单里的值。

    单选是选项首字母，多选是按字母排序后拼在一起的字符串，判断题是 true/false，
    填空题是各空内容直接相连（与源码一致；平台没有给出多空的其它分隔方式）。
    """
    kind = question["type"]
    if answer is None:
        raise ValueError("AI 未提供答案")
    if kind in ("single", "multiple"):
        options = {option["label"]: option["value"] for option in question["options"]}
        if kind == "single":
            label = _option_label(answer)
            if label not in options:
                raise ValueError("AI 返回了未知选项")
            return options[label]
        if isinstance(answer, str):
            answer = [part for part in re.split(r"[,，、\s]+", answer) if part]
        if not isinstance(answer, list) or not answer:
            raise ValueError("AI 未给出多选答案")
        labels = [_option_label(item) for item in answer]
        if len(set(labels)) != len(labels):
            raise ValueError("AI 返回了重复选项")
        unknown = [label for label in labels if label not in options]
        if unknown:
            raise ValueError("AI 返回了未知选项：%s" % "、".join(unknown))
        return "".join(sorted(options[label] for label in labels))
    if kind == "judgement":
        if isinstance(answer, bool):
            return "true" if answer else "false"
        if isinstance(answer, str):
            word = answer.strip().lower()
            if word in TRUE_WORDS:
                return "true"
            if word in FALSE_WORDS:
                return "false"
        raise ValueError("判断题答案不是 true/false")
    if kind == "completion":
        parts = [answer] if isinstance(answer, str) else answer
        if not isinstance(parts, list) or not parts:
            raise ValueError("填空题答案为空")
        if any(not isinstance(part, str) or not part.strip() for part in parts):
            raise ValueError("填空题答案有空项")
        return "".join(part.strip() for part in parts)
    if kind == "shortanswer":
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("问答题答案为空")
        return answer.strip()
    raise ValueError("暂不支持题型 %s" % (question["type_code"] or "?"))


def _option_label(value):
    if not isinstance(value, str):
        raise ValueError("答案不是选项标签")
    letters = re.sub(r"[^A-Za-z]", "", value).upper()
    if len(letters) != 1:
        raise ValueError("答案不是单个选项标签：%r" % value)
    return letters


class ChaoxingExerciseRunner:
    def __init__(self, client, ai_client, *, auto_submit=False, cover_rate=0.8, on_progress=None,
                 delay=0.0, sleep=None, cancel_event=None):
        self.client = client
        self.ai_client = ai_client
        self.auto_submit = auto_submit
        self.cover_rate = float(cover_rate)
        self.delay = max(0.0, float(delay))
        self.reporter = Reporter(on_progress)
        self.sleep = sleep if sleep is not None else time.sleep
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()

    def run(self, course, point, job, job_info):
        """作答一份作业，返回 ``{"paper", "status", "message"}``。

        status 取 submitted / saved / skipped / error / cancelled /
        submission_unknown；AI 服务整体不可用时向上抛 AIServiceError 停止队列。
        """
        name = job.get("name") or "章节作业"
        try:
            page = self.client.work_page(course, job, job_info)
        except WorkUnavailable as error:
            return self._result(name, "skipped", str(error))
        try:
            sheet = decode_questions_info(page)
        except FontDecodeError as error:
            return self._result(name, "error", "题干字体无法解码，未作答：%s" % error)
        except ValueError as error:
            return self._result(name, "error", str(error))
        questions = sheet["questions"]
        if not questions:
            return self._result(name, "error", "页面里没有读到题目")

        values, problems = self._answer_all(name, questions)
        if self.cancel_event.is_set():
            return self._result(name, "cancelled", "已取消，未交卷")
        if not values:
            return self._result(name, "error", "没有生成任何答案，未提交")

        coverage = len(values) / len(questions)
        submit = bool(self.auto_submit and coverage >= self.cover_rate)
        payload = self._payload(sheet, questions, values, submit)
        try:
            confirmation = self.client.submit_work(payload)
        except ChaoxingError as error:
            # 请求已经发出，平台是否收下未知；同一份答案不能自动重发。
            return self._result(name, "submission_unknown", "交卷结果未知：%s" % error)
        if not confirmation.get("status"):
            action = "提交" if submit else "保存"
            return self._result(name, "error", "%s失败：%s" % (action, confirmation.get("msg") or "平台未确认"))
        message = self._summary(len(values), len(questions), submit, len(problems))
        logger.info("%s：%s", name, message)
        return self._result(name, "submitted" if submit else "saved", message)

    def _answer_all(self, name, questions):
        values, problems = {}, {}
        for index, question in enumerate(questions):
            if self.cancel_event.is_set():
                return values, problems
            self.reporter.questions(name, index, len(questions))
            problem = unsupported_reason(question)
            if problem:
                problems[question["id"]] = problem
                logger.warning("%s：第 %d 题未作答（%s）", name, index + 1, problem)
                continue
            try:
                if self.delay:
                    self.sleep(self.delay)
                response = self.ai_client.answer(model_question(question))
                values[question["id"]] = encode_answer(question, response["answer"])
            except AIServiceError:
                raise
            except AIError as error:
                # 单题失败不影响其他题：这一题留空，交给覆盖率判断。
                problems[question["id"]] = str(error)
                logger.warning("%s：第 %d 题作答失败：%s", name, index + 1, error)
            except ValueError as error:
                problems[question["id"]] = str(error)
                logger.warning("%s：第 %d 题答案无效：%s", name, index + 1, error)
        self.reporter.questions(name, len(questions), len(questions))
        return values, problems

    @staticmethod
    def _payload(sheet, questions, values, submit):
        # 表单隐藏字段和题目都在同一层，题目列表单独放回 answer{id} 字段。
        payload = {name: value for name, value in sheet.items() if name != "questions"}
        payload["answerwqbid"] = sheet["answerwqbid"]
        for question in questions:
            # 保存模式下只写已答出的题，其余留空给人工补；提交模式下未答的题也留空。
            payload["answer" + question["id"]] = values.get(question["id"], "")
            payload["answertype" + question["id"]] = question["type_code"]
        payload["pyFlag"] = "" if submit else "1"
        return payload

    def _summary(self, answered, total, submit, unanswered):
        coverage = answered / total * 100
        if submit:
            action = "已交卷"
        elif self.auto_submit and coverage < self.cover_rate * 100:
            action = "已保存（覆盖率 %.0f%% 低于 %.0f%%，未交卷）" % (coverage, self.cover_rate * 100)
        else:
            action = "已保存，未交卷"
        message = "%s · 作答 %d/%d 题（覆盖率 %.0f%%）" % (action, answered, total, coverage)
        if unanswered:
            message += "；%d 题未作答" % unanswered
        return message

    @staticmethod
    def _result(name, status, message):
        if status not in ("submitted", "saved"):
            logger.warning("%s：%s（%s）", name, message, status)
        return {"paper": name, "status": status, "message": message}

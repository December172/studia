"""智慧课程 AI 答题：知识点作业与考试。"""
from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from AIMoocMain.export_questions import clean_html_text
from ai.client import AIError, AITransientServiceError
from base.auth import AuthenticationExpired, PlatformUnavailable
from MoocMain.log import Logger
from reporting import Reporter, child_thread_name
from task_metrics import nonnegative_number
from ZHSMain.api import CaptchaRequired, StudyTimeLimit

logger = Logger(__name__).get_log()

JOIN = "#@#"
FILL_HOMEWORK = "#@#"
FILL_EXAM = "/"
OPTION_TYPES = {1, 2, 14}
MEDIA = re.compile(r"<(img|audio|video|iframe)\b", re.I)
IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*['\"]?([^'\" >]+)[^>]*>", re.I)


def _kind(raw):
    kind = raw.get("questionType")
    if isinstance(kind, dict):
        kind = kind.get("id")
    try:
        return int(kind)
    except (TypeError, ValueError):
        raise ValueError("暂不支持该题型") from None


def _text(raw, allow_images=False, images=None):
    value = str(raw.get("content") or "")
    if allow_images:
        # A stable named placeholder keeps memory keys distinct per image; the
        # picture itself is sent alongside under the same name when it downloads.
        def placeholder(match):
            url = match.group(1)
            name = url.rsplit("/", 1)[-1].split("?")[0]
            if images is not None and url.startswith("https://") and name not in images:
                images[name] = url
            return " [图片:%s] " % name
        value = IMAGE.sub(placeholder, value)
    if MEDIA.search(value):
        raise ValueError("包含图片或音视频，需要人工处理")
    return clean_html_text(value)


def prepare(raw, allow_images=False):
    """Turn a platform question into the public AI payload plus answer mappings."""
    if not isinstance(raw, dict):
        raise ValueError("题目数据格式异常")
    kind = _kind(raw)
    if kind not in OPTION_TYPES and kind != 3:
        raise ValueError("暂不支持该题型")
    images = {}
    title = _text(raw, allow_images, images)
    if not title:
        raise ValueError("题干为空")
    options = [option for option in (raw.get("optionVos") or []) if isinstance(option, dict)]
    ids = {}
    public = {"title": title}
    if kind == 3:
        blank_count = max(1, len(options))
        public.update({"type": "填空题", "blank_count": blank_count})
        return {"qid": raw.get("id"), "kind": kind, "public": public,
                "ids": ids, "blank_count": blank_count, "images": images}

    if len(options) < 2:
        raise ValueError("题目选项不足")
    texts = []
    for index, option in enumerate(options):
        label = chr(ord("A") + index)
        if option.get("id") in (None, ""):
            raise ValueError("选项缺少平台 ID")
        ids[label] = option.get("id")
        texts.append({"label": label, "text": _text(option, allow_images, images)})
    public["type"] = "多选题" if kind == 2 else "单选题"
    public["options"] = texts
    return {"qid": raw.get("id"), "kind": kind, "public": public,
            "ids": ids, "blank_count": 0, "images": images}


IMAGE_LIMIT = 2 * 1024 * 1024


def download_images(images, get=requests.get):
    """[(name, mime, bytes)] for a question's pictures, or None if any is unusable."""
    result = []
    for name, url in images.items():
        try:
            response = get(url, timeout=(5, 15), allow_redirects=False)
        except requests.RequestException as exc:
            logger.warning("下载题目图片失败 %s：%s", name, exc)
            return None
        mime = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        if (response.status_code != 200 or not mime.startswith("image/")
                or len(response.content) > IMAGE_LIMIT):
            logger.warning("题目图片不可用 %s：HTTP %s %s", name, response.status_code, mime)
            return None
        result.append((name, mime, response.content))
    return result


def prepare_shared(raw):
    """Shared-course questions use eid / name / questionOptions instead of the AI shape."""
    if not isinstance(raw, dict):
        raise ValueError("题目数据格式异常")
    if raw.get("eid") in (None, ""):
        raise ValueError("题目缺少平台 ID")
    return prepare({"id": raw["eid"], "content": raw.get("name") or "",
                    "questionType": raw.get("questionType"),
                    "optionVos": raw.get("questionOptions")})


def encode(prepared, answer, *, fill_sep):
    """Map the model answer back to platform option ids or blank text."""
    invalid = ValueError("AI 返回的答案格式不符")
    kind = prepared["kind"]
    ids = prepared["ids"]
    if kind in (1, 14):
        if not isinstance(answer, str) or answer not in ids:
            raise invalid
        return [ids[answer]]
    if kind == 2:
        if (not isinstance(answer, list) or not answer
                or any(not isinstance(label, str) or label not in ids for label in answer)
                or len(set(answer)) != len(answer)):
            raise invalid
        return [ids[label] for label in answer]
    if kind == 3:
        blanks = [answer] if isinstance(answer, str) else answer
        if (not isinstance(blanks, list) or len(blanks) != prepared["blank_count"]
                or any(not isinstance(text, str) or not text.strip() for text in blanks)):
            raise invalid
        return [fill_sep.join(text.strip() for text in blanks)]
    raise invalid


def _answer_texts(prepared, answer):
    """Option labels or blank texts from an AI answer, or None when malformed."""
    kind = prepared["kind"]
    options = {option["label"]: option["text"]
               for option in prepared["public"].get("options") or []}
    if kind in (1, 14):
        if not isinstance(answer, str) or answer not in options:
            return None
        return [options[answer]]
    if kind == 2:
        if not isinstance(answer, list):
            return None
        texts = []
        for label in answer:
            if not isinstance(label, str) or label not in options:
                return None
            texts.append(options[label])
        return texts
    if kind == 3:
        blanks = [answer] if isinstance(answer, str) else answer
        if not isinstance(blanks, list) or any(not isinstance(text, str) for text in blanks):
            return None
        return [text.strip() for text in blanks]
    return None


def _labels_for_texts(prepared, texts):
    """Map stored answer texts back to option labels or blank texts."""
    kind = prepared["kind"]
    texts = [str(text).strip() for text in texts or []]
    if kind == 3:
        return texts if len(texts) == prepared["blank_count"] else None
    by_text = {}
    for option in prepared["public"].get("options") or []:
        by_text.setdefault(str(option["text"]).strip(), []).append(option["label"])
    labels = []
    for text in texts:
        matches = by_text.get(text)
        if not matches:
            return None
        labels.append(matches[0])
    if len(set(labels)) != len(labels):
        return None
    if kind in (1, 14) and len(labels) != 1:
        return None
    return labels


def _same_answer(prepared, first, second):
    if prepared["kind"] == 3:
        return ([text.strip() for text in first]
                == [text.strip() for text in second])
    return {text.strip() for text in first} == {text.strip() for text in second}


def _reuse(prepared, texts, fill_sep):
    labels = _labels_for_texts(prepared, texts)
    if labels is None:
        return None
    answer = labels[0] if prepared["kind"] in (1, 14) else labels
    try:
        return encode(prepared, answer, fill_sep=fill_sep)
    except ValueError:
        return None


def _untried_single(prepared, wrong):
    """Single choice / judgement options not yet known to be wrong."""
    excluded = set()
    for combo in wrong:
        labels = _labels_for_texts(prepared, combo)
        if labels and len(labels) == 1:
            excluded.add(labels[0])
    return [option for option in prepared["public"].get("options") or []
            if option["label"] not in excluded]


def _exclude_single(prepared, wrong, fill_sep):
    """For single choice / judgement, pick the only option not known wrong."""
    if prepared["kind"] not in (1, 14):
        return None
    remaining = _untried_single(prepared, wrong)
    if len(remaining) != 1:
        return None
    return _reuse(prepared, [remaining[0]["text"]], fill_sep)


def _image_only(prepared):
    """Whether the model cannot read the question: images stand in for its content."""
    public = prepared["public"]
    return "[图片:" in public.get("title", "") or any(
        "[图片:" in str(option["text"]) for option in public.get("options") or [])


def _wrong_hint(prepared, wrong):
    parts = []
    for combo in wrong:
        labels = _labels_for_texts(prepared, combo)
        parts.append("、".join(labels if labels is not None else [str(text) for text in combo]))
    return "以下答案已被平台判定为错误，不要再选：" + "；".join(parts)


def _submitted_texts(prepared, encoded, fill_sep):
    if not encoded:
        return None
    if prepared["kind"] == 3:
        return [text for text in str(encoded[0]).split(fill_sep)]
    by_id = {str(pid): label for label, pid in prepared["ids"].items()}
    labels = [by_id.get(str(part)) for part in encoded]
    if any(label is None for label in labels):
        return None
    texts = {option["label"]: option["text"]
             for option in prepared["public"].get("options") or []}
    return [texts[label] for label in labels]


def _shared_texts(prepared, entry, options):
    """Answer texts from a shared-course answer-info entry."""
    answer = entry.get("answer")
    if answer in (None, ""):
        return None
    if prepared["kind"] == 3:
        return [text.strip() for text in str(answer).split("/") if text.strip()]
    by_id = {}
    for option in options:
        if isinstance(option, dict) and option.get("id") not in (None, ""):
            by_id[str(option["id"])] = clean_html_text(option.get("content") or "")
    texts = [by_id.get(str(part).strip()) for part in str(answer).split(",")]
    if any(not text for text in texts):
        return None
    return texts


def _positive_int(value):
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


class _Skipped(Exception):
    """A queued question skipped after another one already failed."""


class ZHSAnswerRunner:
    def __init__(self, client, ai_client, *, auto_submit, on_progress=None, sleep=time.sleep,
                 rng=None, heartbeat_interval=10, batch_size=5, max_workers=8,
                 cancel_event=None, memory=None):
        self.client = client
        self.ai_client = ai_client
        self.auto_submit = auto_submit
        self.memory = memory
        self.reused = 0
        self.reporter = Reporter(on_progress)
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.heartbeat_interval = heartbeat_interval
        self.batch_size = max(1, int(batch_size))
        self.max_workers = max(1, int(max_workers))
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        self._heartbeat_error = None

    # ------------------------------------------------------------------ 入口

    def run_paper(self, course, item, category):
        z = item.get("zhs") or {}
        cz = course.get("zhs") or {}
        name = item.get("name") or "未命名试卷"
        kind = z.get("kind")
        try:
            if kind == "ai_homework":
                return self._homework(z, cz, name)
            if kind == "ai_exam":
                return self._exam(z, cz, name)
            if kind in ("shared_homework", "shared_exam"):
                return self._shared(course, item, z, name, kind == "shared_exam")
        except KeyError as exc:
            return self._result(name, "error", "试卷缺少必要字段：%s" % exc)
        except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
            raise
        except Exception as exc:
            # Opening the paper or reading its sheet failed: nothing was saved, so
            # this paper fails alone and the queue can continue.
            return self._result(name, "error", "打开试卷失败：%s" % exc)
        return self._result(name, "error", "不支持的智慧树试卷类型")

    def _homework(self, z, cz, name):
        course_id = z.get("courseId", cz.get("courseId"))
        knowledge_id = z["knowledgeId"]
        test_id = int(z["examTestId"])
        paper_id = int(z["paperId"])
        self.client.exam_open({
            "examTestId": test_id,
            "examPaperId": paper_id,
            "courseId": course_id,
        })
        sheet = self.client.exam_sheet(test_id, paper_id, all_parts=False)

        def save_one(entry, prepared, encoded):
            self.client.exam_save_answer({
                "recruitId": course_id,
                "examTestId": test_id,
                "examPaperId": paper_id,
                "questionId": entry["questionId"],
                "dataVos": None,
                "answer": JOIN.join(str(part) for part in encoded),
            })

        def submit():
            self.client.exam_submit({
                "examTestId": test_id,
                "courseId": course_id,
                "courseType": 8,
                "examPaperId": paper_id,
                "aiKnlowledgeId": knowledge_id,
            })

        def after_submit(questions, answers, outcome):
            correct = self._count_correct(test_id, paper_id, sheet, questions, answers,
                                          fill_sep=FILL_HOMEWORK, name=name)
            return {"paper": name, "status": "submitted", "correct": correct,
                    "total": len(sheet), "message": "答对 %d/%d 题" % (correct, len(sheet))}

        # Mastery keeps the best score and the platform reveals correct options after
        # submission, so image questions are attempted here and learned for next time.
        return self._run(name, test_id, paper_id, sheet, fill_sep=FILL_HOMEWORK,
                         save_one=save_one, submit=submit, after_submit=after_submit,
                         allow_images=True)

    def _exam(self, z, cz, name):
        course_id = z.get("courseId", cz.get("courseId"))
        class_id = z.get("classId", cz.get("classId"))
        test_id = int(z["examTestId"])
        paper_id = int(z["examPaperId"])
        task_id = z.get("taskId")
        user_id = z.get("userId")
        self.client.ai_exam_pull_paper(course_id, class_id, test_id)
        self.client.exam_open({"examTestId": test_id, "courseId": course_id})
        sheet = self.client.exam_sheet(test_id, paper_id, all_parts=True)

        def item_of(entry, prepared, encoded):
            return {
                "recruitId": course_id,
                "examTestId": test_id,
                "examPaperId": paper_id,
                "questionId": entry["questionId"],
                "answer": JOIN.join(str(part) for part in encoded),
                "questionType": prepared["kind"],
                "dataVos": None,
            }

        def flush(batch):
            self.client.exam_save_batch({
                "examTestId": test_id,
                "examPaperId": paper_id,
                "recruitId": course_id,
                "answerList": batch,
            })

        def submit():
            self.client.exam_submit(
                {"examTestId": test_id, "recruitId": course_id, "examPaperId": paper_id},
                expect_body=False,
            )

        def after_submit(questions, answers, outcome):
            try:
                detail = self.client.ai_exam_detail({
                    "classId": class_id,
                    "courseId": course_id,
                    "examTestId": test_id,
                    "examPaperId": paper_id,
                    "examId": paper_id,
                    "studentId": user_id,
                    "taskId": task_id,
                    "taskType": None,
                })
            except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                raise
            except Exception as exc:
                logger.warning("%s：读取考试详情失败：%s", name, exc)
                detail = {}
            if detail.get("isLookAnswer") == 1 or detail.get("isAllowShowDetail") == 1:
                correct = self._count_correct(test_id, paper_id, sheet, questions, answers,
                                              fill_sep=FILL_EXAM, name=name)
                return {"paper": name, "status": "submitted", "correct": correct,
                        "total": len(sheet),
                        "message": "答对 %d/%d 题" % (correct, len(sheet))}
            return {"paper": name, "status": "submitted",
                    "message": "已交卷；平台未开放答案，成绩以平台为准"}

        return self._run(name, test_id, paper_id, sheet, fill_sep=FILL_EXAM,
                         save_one=None, item_of=item_of, flush=flush,
                         submit=submit, after_submit=after_submit)

    # ------------------------------------------------------------ 共享课试卷

    def _shared(self, course, item, z, name, is_exam):
        zc = course.get("zhs") or {}
        recruit_id = z.get("recruitId", zc.get("recruitId"))
        course_id = z.get("courseId", zc.get("courseId"))
        exam_id = z["examId"]
        stu_exam_id = z["stuExamId"]
        # The catalog may be minutes old; re-check before opening the paper.
        entries = catalog_shared(self.client, course)
        current = next((entry for _, entry, _ in entries if entry.get("id") == item.get("id")), None)
        if current is None:
            return self._result(name, "error", "试卷已不在列表中，请刷新后重试")
        if current.get("_unavailable"):
            return self._result(name, "error", current["_unavailable"][1])

        self.client.shared_exam_sso()
        school = self.client.shared_school_id()
        redo = bool(current.get("_redo")) and not is_exam
        if redo:
            try:
                self.client.shared_redo_homework(recruit_id, exam_id, course_id)
            except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                raise
            except Exception as exc:
                return self._result(name, "error", "申请重做失败：%s" % exc)
            logger.info("%s：已申请重做（剩余 %s 次）", name, current.get("answerCount"))
        opener = self.client.shared_do_exam if is_exam else self.client.shared_do_homework
        rt = opener(recruit_id, exam_id, stu_exam_id, school, course_id)
        try:
            parts = rt["examBase"]["workExamParts"]
        except (KeyError, TypeError):
            return self._result(name, "error", "试卷题目格式异常")
        sheet = []
        for part in parts or []:
            if isinstance(part, dict):
                sheet.extend(q for q in (part.get("questionDtos") or []) if isinstance(q, dict))
        total = len(sheet)
        if total == 0:
            return self._result(name, "error", "试卷题目为空")

        def save_one(entry, prepared, encoded):
            answer = (encoded[0] if prepared["kind"] == 3
                      else ",".join(str(part) for part in encoded))
            self.client.shared_save_answer({
                "examId": exam_id,
                "recruitId": recruit_id,
                "stuExamId": stu_exam_id,
                "eid": prepared["qid"],
                "schoolId": school,
                "deviceId": "",
                "examType": 1 if is_exam else "",
                "fromType": 3,
                "answer": answer,
                "dataIds": "",
                "questionType": prepared["kind"],
            }, recruit_id, exam=is_exam)

        def submit():
            if is_exam:
                self.client.shared_submit_exam(recruit_id, exam_id, stu_exam_id, total)
                return {"paper": name, "status": "submitted", "message": "已交卷，成绩以平台为准"}
            rt = self.client.shared_submit_homework(recruit_id, exam_id, stu_exam_id, total) or {}
            score = rt.get("score")
            total_score = rt.get("totalScore")
            if score is None:
                message = "已交卷，成绩以平台为准"
            elif total_score is None:
                message = "已交卷，得分 %s" % score
            else:
                message = "已交卷，得分 %s/%s" % (score, total_score)
            return {"paper": name, "status": "submitted", "message": message}

        def after_submit(questions, answers, outcome):
            if not is_exam:
                self._remember_shared(recruit_id, stu_exam_id, exam_id, school, course_id,
                                      questions, answers, name)
            return outcome

        result = self._run(name, exam_id, stu_exam_id, sheet, fill_sep="/",
                           save_one=save_one, submit=submit, heartbeat=False,
                           fetch=lambda entry: entry, after_submit=after_submit)
        if redo and result.get("status") in ("error", "ai_error"):
            result["message"] = "%s（已消耗一次重做机会，未交卷；该作业已重置，再次选择它作答不会再消耗重做次数）" % result.get("message", "")
        return result

    # ------------------------------------------------------------------ 流程

    def _run(self, name, test_id, paper_id, sheet, *, fill_sep, save_one=None,
             item_of=None, flush=None, submit=None, after_submit=None,
             heartbeat=True, fetch=None, allow_images=False):
        if not isinstance(sheet, list):
            return self._result(name, "error", "答题卡格式异常")
        total = len(sheet)
        if total == 0:
            return self._result(name, "error", "答题卡为空")
        self.reporter.questions(name, 0, total)
        logger.info("开始：%s（%d 题）", name, total)
        if heartbeat:
            self._start_heartbeat(test_id, paper_id)
        try:
            # 1. 拿题：顺序请求平台，题目全部就绪后才开始作答与保存。
            questions = []
            fetched = False
            for index, entry in enumerate(sheet, 1):
                if self.cancel_event.is_set():
                    raise KeyboardInterrupt
                self._raise_heartbeat_error()
                try:
                    if fetch is not None:
                        raw = fetch(entry)
                    else:
                        if fetched:
                            self.sleep(self.rng.uniform(0.5, 1))
                        raw = self.client.exam_question(
                            test_id, paper_id, entry["questionId"], entry["version"])
                        fetched = True
                    prepared = prepare_shared(raw) if fetch is not None else prepare(raw, allow_images)
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    return self._result(name, "error", "第 %d 题：%s" % (index, exc))
                questions.append((entry, prepared))

            # 2. AI 作答：不碰平台，可并发；任何一题失败都不保存、不提交。
            answers, failure = self._ask_all(name, questions, fill_sep)
            if failure is not None:
                return failure

            # 3. 保存：顺序执行平台请求。
            batch = []
            saved_any = False
            for index, (entry, prepared) in enumerate(questions, 1):
                if self.cancel_event.is_set():
                    raise KeyboardInterrupt
                self._raise_heartbeat_error()
                self.reporter.detail("%s · 保存答案 %d/%d" % (name, index, total))
                try:
                    if save_one is not None:
                        if saved_any:
                            self.sleep(self.rng.uniform(1, 2))
                        save_one(entry, prepared, answers[index - 1])
                        saved_any = True
                    else:
                        batch.append(item_of(entry, prepared, answers[index - 1]))
                        if len(batch) >= self.batch_size:
                            if saved_any:
                                self.sleep(self.rng.uniform(1, 2))
                            flush(batch)
                            batch = []
                            saved_any = True
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    return self._result(name, "error", "第 %d 题保存失败：%s" % (index, exc))
            if batch:
                try:
                    if saved_any:
                        self.sleep(self.rng.uniform(1, 2))
                    flush(batch)
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    return self._result(name, "error", "保存答案失败：%s" % exc)
            logger.info("%s：已保存 %d 题答案", name, total)

            if not self.auto_submit:
                result = self._result(name, "preview", "答案已保存，未交卷")
            else:
                try:
                    outcome = submit()
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                    raise
                except Exception:
                    # Never resubmit: a duplicate attempt is worse than an unknown result.
                    self.reporter.detail("%s · 提交请求失败" % name)
                    return self._result(name, "submission_unknown",
                                        "提交结果未知，请到平台核对；未重试")
                if after_submit is not None:
                    result = after_submit(questions, answers, outcome)
                else:
                    result = outcome or self._result(name, "submitted", "已交卷")
            if self.reused and result.get("status") in ("submitted", "preview"):
                result["message"] = "%s（复用已确认答案 %d 题）" % (
                    result.get("message", ""), self.reused)
            logger.info("%s：%s", name, result.get("message", ""))
            return result
        finally:
            self._stop_heartbeat()

    def _ask_all(self, name, questions, fill_sep):
        """Answer prepared questions concurrently; keep results in question order."""
        total = len(questions)
        answers = [None] * total
        failures = {}
        reused = 0

        def solve(index, prepared, stop):
            if self.cancel_event.is_set():
                raise KeyboardInterrupt
            if stop.is_set():
                raise _Skipped()
            encoded, from_memory = self._recall(prepared, fill_sep)
            if encoded is not None:
                logger.info("%s · 复用已确认答案 %d/%d", name, index + 1, total)
                return encoded, from_memory
            return self._solve(prepared, fill_sep, stop), False

        # Queued work is skipped through this flag rather than Future.cancel():
        # as_completed never wakes for futures cancelled by executor.shutdown.
        stop = threading.Event()
        control_error = None
        with ThreadPoolExecutor(max_workers=min(self.max_workers, total), thread_name_prefix=child_thread_name('zhs-answer')) as executor:
            futures = {
                executor.submit(solve, index, prepared, stop): index
                for index, (_, prepared) in enumerate(questions)
            }
            completed = 0
            for future in as_completed(futures):
                index = futures[future]
                try:
                    encoded, from_memory = future.result()
                except _Skipped:
                    continue
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt) as exc:
                    control_error = control_error or exc
                    stop.set()
                    continue
                except Exception as exc:
                    failures[index] = exc
                    stop.set()
                    continue
                if self.cancel_event.is_set() or self._heartbeat_error is not None:
                    stop.set()
                answers[index] = encoded
                completed += 1
                if from_memory:
                    reused += 1
                self.reporter.questions(name, completed, total)
                if not from_memory:
                    logger.info("%s · AI 作答 %d/%d：%s", name, index + 1, total,
                                questions[index][1]["public"]["title"][:40])
        self.reused = reused
        if self._heartbeat_error is not None:
            control_error, self._heartbeat_error = self._heartbeat_error, None
        if control_error is None and self.cancel_event.is_set():
            control_error = KeyboardInterrupt()
        if control_error is not None:
            raise control_error
        if failures:
            index = min(failures)
            exc = failures[index]
            status = "ai_error" if isinstance(exc, AIError) else "error"
            return answers, self._result(name, status, "第 %d 题：%s" % (index + 1, exc))
        return answers, None

    def _recall(self, prepared, fill_sep):
        """(encoded answer, whether it came from a confirmed-correct entry)."""
        if self.memory is None:
            return None, False
        try:
            remembered = self.memory.lookup(prepared)
        except Exception as exc:
            logger.warning("读取本机答案记忆失败：%s", exc)
            return None, False
        if remembered.get("correct"):
            encoded = _reuse(prepared, remembered["correct"], fill_sep)
            if encoded is not None:
                return encoded, True
        encoded = _exclude_single(prepared, remembered.get("wrong") or [], fill_sep)
        return encoded, False

    def _solve(self, prepared, fill_sep, stop):
        if self.cancel_event.is_set():
            raise KeyboardInterrupt
        if stop.is_set():
            raise _Skipped()
        public = prepared["public"]
        wrong = self._wrong(prepared)
        hint = _wrong_hint(prepared, wrong) if wrong else ""
        if hint:
            public = dict(public)
            public["context"] = (public["context"] + "\n" + hint
                                 if public.get("context") else hint)
        images = download_images(prepared["images"]) if prepared.get("images") else None
        for _ in range(3):
            if self.cancel_event.is_set():
                raise KeyboardInterrupt
            if stop.is_set():
                raise _Skipped()
            answer = self._ask(public, images).get("answer")
            try:
                encoded = encode(prepared, answer, fill_sep=fill_sep)
            except ValueError:
                continue
            texts = _answer_texts(prepared, answer)
            if wrong and texts and any(_same_answer(prepared, texts, combo) for combo in wrong):
                continue
            return encoded
        # The model cannot see image-only choices; a random untried option plus the
        # memory of wrong ones converges over redos instead of blocking the paper.
        if prepared["kind"] in (1, 14) and _image_only(prepared):
            remaining = _untried_single(prepared, wrong)
            if remaining:
                choice = self.rng.choice(remaining)
                logger.info("图片题 AI 无法作答，随机选择未试过的选项 %s：%s",
                            choice["label"], prepared["public"]["title"][:40])
                return encode(prepared, choice["label"], fill_sep=fill_sep)
        raise ValueError("AI 返回的答案格式不符")

    def _wrong(self, prepared):
        if self.memory is None:
            return []
        try:
            return self.memory.lookup(prepared).get("wrong") or []
        except Exception as exc:
            logger.warning("读取本机答案记忆失败：%s", exc)
            return []

    def _ask(self, public, images=None):
        attempts = 0
        while True:
            try:
                reply = (self.ai_client.answer(public, images=images) if images
                         else self.ai_client.answer(public))
            except AITransientServiceError:
                if attempts >= 2:
                    raise
                attempts += 1
                self.sleep(2)
                continue
            if not isinstance(reply, dict):
                raise ValueError("AI 返回的答案格式不符")
            return reply

    def _count_correct(self, test_id, paper_id, sheet, questions, answers, *, fill_sep, name):
        correct = 0
        changed = False
        for index, entry in enumerate(sheet):
            try:
                raw = self.client.exam_question(
                    test_id, paper_id, entry["questionId"], entry["version"])
            except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                raise
            except Exception as exc:
                logger.warning("回查第 %s 题失败：%s", entry.get("questionId"), exc)
                continue
            voices = (raw or {}).get("userAnswerVo") or []
            passed = bool(voices and isinstance(voices[0], dict)
                          and voices[0].get("isCorrect") == 1)
            if passed:
                correct += 1
            if self.memory is None or index >= len(questions) or index >= len(answers):
                continue
            try:
                changed |= self._remember_question(
                    questions[index][1], answers[index], raw or {}, passed, fill_sep)
            except Exception as exc:
                logger.warning("%s：更新答案记忆失败：%s", name, exc)
        if changed:
            self._save_memory(name)
        return correct

    def _remember_question(self, prepared, encoded, raw, passed, fill_sep):
        """Record what the platform marked right/wrong; returns whether memory changed."""
        changed = False
        submitted = _submitted_texts(prepared, encoded, fill_sep)
        if prepared["kind"] == 3:
            if submitted:
                self.memory.remember(prepared, submitted, passed)
                changed = True
            # After submission the platform reveals each blank's standard answer.
            blanks = sorted((option for option in raw.get("optionVos") or []
                             if isinstance(option, dict)), key=lambda option: option.get("sort") or 0)
            right = [clean_html_text(option.get("content") or "") for option in blanks]
            if (not passed and len(right) == prepared["blank_count"] and all(right)
                    and all(option.get("isCorrect") == 1 for option in blanks)):
                self.memory.remember_correct(prepared, right)
                changed = True
            return changed
        right = [clean_html_text(option.get("content") or "")
                 for option in (raw.get("optionVos") or [])
                 if isinstance(option, dict) and option.get("isCorrect") == 1]
        right = [text for text in right if text]
        if right:
            self.memory.remember_correct(prepared, right)
            changed = True
        elif submitted and passed:
            # Judgement options carry no isCorrect mark; a passing verdict is the proof.
            self.memory.remember_correct(prepared, submitted)
            changed = True
        if submitted and not passed:
            self.memory.remember(prepared, submitted, False)
            changed = True
        return changed

    def _remember_shared(self, recruit_id, stu_exam_id, exam_id, school, course_id,
                         questions, answers, name):
        if self.memory is None:
            return
        try:
            rt = self.client.shared_look_homework(recruit_id, stu_exam_id, exam_id,
                                                  school, course_id)
            rows = []
            for part in (rt or {}).get("examBase", {}).get("workExamParts") or []:
                if isinstance(part, dict):
                    rows.extend(q for q in (part.get("questionDtos") or [])
                                if isinstance(q, dict))
            if not rows:
                return
            ids = [row.get("id") for row in rows if row.get("id") not in (None, "")]
            if not ids:
                return
            info = self.client.shared_answer_info(
                recruit_id, stu_exam_id, exam_id, school, course_id, question_ids=ids) or {}
            changed = False
            for row in rows:
                if row.get("id") in (None, ""):
                    continue
                prepared = prepare_shared(dict(row, eid=row.get("eid") or row.get("id")))
                key = str(row.get("id"))
                entry = info.get(key) if isinstance(info, dict) else None
                if not isinstance(entry, dict):
                    continue
                texts = _shared_texts(prepared, entry, (row.get("questionOptions") or []))
                if not texts:
                    continue
                if str(entry.get("isCurrent")) == "1":
                    self.memory.remember_correct(prepared, texts)
                else:
                    self.memory.remember(prepared, texts, False)
                changed = True
            if changed:
                self._save_memory(name)
        except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
            raise
        except Exception as exc:
            logger.warning("%s：回查共享课作业答案失败：%s", name, exc)

    def _save_memory(self, name):
        try:
            self.memory.save()
        except Exception as exc:
            logger.warning("%s：保存答案记忆失败：%s", name, exc)

    def _result(self, name, status, message):
        result = {"paper": name, "status": status, "message": message}
        if status not in ("submitted", "preview"):
            logger.warning("%s：%s（%s）", name, message, status)
        return result

    # ---------------------------------------------------------------- 心跳

    def _start_heartbeat(self, test_id, paper_id):
        self._heartbeat_stop = threading.Event()
        self._heartbeat_error = None
        interval = self.heartbeat_interval

        def beat():
            while not self._heartbeat_stop.wait(interval):
                try:
                    self.client.exam_heartbeat(test_id, paper_id, interval)
                except (CaptchaRequired, AuthenticationExpired) as exc:
                    self._heartbeat_error = exc
                    return
                except Exception:
                    continue

        self._heartbeat_thread = threading.Thread(target=beat, daemon=True, name=child_thread_name('heartbeat'))
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    def _raise_heartbeat_error(self):
        error = self._heartbeat_error
        if error is not None:
            self._heartbeat_error = None
            raise error


# -------------------------------------------------------------------- 目录

def _knowledge_points(basic):
    points = []
    for theme in (basic or {}).get("cakeThemeList") or []:
        if isinstance(theme, dict):
            points.extend(theme.get("knowledgeList") or [])
    return [point for point in points if isinstance(point, dict)]


def catalog(client, course):
    kind = (course.get("zhs") or {}).get("kind")
    if kind == "shared":
        return catalog_shared(client, course)
    return catalog_ai(client, course)


def catalog_ai(client, course):
    """List knowledge-homework and exam papers for the AI course task picker."""
    z = course.get("zhs") or {}
    course_id = z.get("courseId")
    class_id = z.get("classId")
    entries = []
    homework_failed = exam_failed = False

    try:
        basic = client.ai_course_basic(course_id, class_id)
        for point in _knowledge_points(basic):
            knowledge_id = point.get("knowledgeId")
            knowledge_name = point.get("knowledgeName") or point.get("name") or "知识点"
            try:
                info = client.ai_homework_info(course_id, class_id, knowledge_id)
            except (CaptchaRequired, AuthenticationExpired):
                raise
            except Exception as exc:
                logger.warning("跳过知识点 %s 的作业：%s", knowledge_name, exc)
                continue
            test_id = _positive_int(info.get("examTestId"))
            paper_id = _positive_int(info.get("paperId"))
            if not test_id or not paper_id:
                logger.warning("跳过缺少试卷标识的知识点：%s", knowledge_name)
                continue
            score = info.get("highMasteryScore")
            try:
                done = float(score) > 0
            except (TypeError, ValueError):
                done = False
            entries.append({
                "id": "hw-%s-%s-%s" % (knowledge_id, test_id, paper_id),
                "name": "%s · 作业" % knowledge_name,
                "score": score,
                "_completion_status": "submitted" if done else "not_started",
                "zhs": {
                    "kind": "ai_homework",
                    "courseId": course_id,
                    "classId": class_id,
                    "knowledgeId": knowledge_id,
                    "examTestId": test_id,
                    "paperId": paper_id,
                },
            })
    except (CaptchaRequired, AuthenticationExpired):
        raise
    except Exception as exc:
        homework_failed = True
        logger.warning("读取智慧课程作业目录失败：%s", exc)

    try:
        tasks = client.ai_exam_tasks(course_id)
        for task in tasks:
            test_id = _positive_int(task.get("examTestId"))
            paper_id = _positive_int(task.get("examPaperId"))
            task_id = task.get("id")
            user_id = task.get("userId")
            if not test_id or not paper_id or task_id in (None, "") or user_id in (None, ""):
                logger.warning("跳过缺少标识的考试任务：%s", task.get("taskName") or task_id)
                continue
            item = {
                "id": "exam-%s-%s-%s" % (task_id, test_id, paper_id),
                "name": task.get("taskName") or "未命名考试",
                "startTime": task.get("startTime"),
                "endTime": task.get("endTime"),
                "zhs": {
                    "kind": "ai_exam",
                    "courseId": course_id,
                    "classId": class_id,
                    "examTestId": test_id,
                    "examPaperId": paper_id,
                    "taskId": task_id,
                    "userId": user_id,
                },
            }
            if "score" in task:
                item["score"] = task["score"]
            entries.append(item)
    except (CaptchaRequired, AuthenticationExpired):
        raise
    except Exception as exc:
        exam_failed = True
        logger.warning("读取智慧课程考试目录失败：%s", exc)

    if homework_failed and exam_failed:
        raise PlatformUnavailable("智慧树试卷目录读取失败，请稍后重试")
    return [(course, item, 1 if item["zhs"]["kind"] == "ai_homework" else 2)
            for item in entries]


def _retry_counts(row):
    """(remaining redos, total redos) from backNum/isMarking, or None if unreadable."""
    total = nonnegative_number(row.get("backNum"))
    used = nonnegative_number(row.get("isMarking"))
    if total is None or total != int(total):
        return None
    used = 0 if used is None else used
    if used != int(used):
        return None
    return max(int(total) - int(used), 0), int(total)


def _is_full_score(row):
    score = nonnegative_number(row.get("score"))
    total = nonnegative_number(row.get("totalScore"))
    return score is not None and total is not None and score >= total


def _shared_homework_items(client, recruit_id, course_id):
    rows = client.shared_homework_list(recruit_id, course_id, 1)
    rows += client.shared_homework_list(recruit_id, course_id, 2)
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stu_exam_id = row.get("id")
        exam_id = row.get("examId")
        if stu_exam_id in (None, "") or exam_id in (None, ""):
            logger.warning("跳过缺少标识的共享课作业：%s", row.get("examName"))
            continue
        row_course = row.get("courseId")
        if row_course not in (None, "") and str(row_course) != str(course_id):
            logger.warning("跳过课程不符的共享课作业：%s", row.get("examName"))
            continue
        item = {
            "id": "shw-%s-%s" % (stu_exam_id, exam_id),
            "name": row.get("examName") or "未命名作业",
            "startTime": row.get("startTime"),
            "endTime": row.get("endDate"),
            "zhs": {"kind": "shared_homework", "recruitId": recruit_id,
                    "courseId": course_id, "stuExamId": stu_exam_id, "examId": exam_id},
        }
        if "score" in row:
            item["score"] = row["score"]
        retry = _retry_counts(row)
        if retry is not None:
            item["frequency"], item["answerCount"] = retry[1], retry[0]
        state = row.get("state")
        item["_completion_status"] = "submitted" if state == 4 else "not_started"
        if state == 4:
            if _is_full_score(row):
                item["_unavailable"] = ("已满分", "已满分，无需重做。")
            elif retry is None or retry[0] <= 0:
                item["_unavailable"] = ("无重做次数", "重做次数已用完。")
            else:
                remaining = retry[0]
                item["zhs"]["redo"] = True
                item["_redo"] = True
                item["_hint"] = (
                    "重做成绩会覆盖当前成绩（当前 %s/%s），剩余重做 %d 次。"
                    % (row.get("score"), row.get("totalScore"), remaining))
        elif state not in (1, 4, 5):
            item["_unavailable"] = ("状态未知", "平台状态未知，请到网页核对。")
        items.append(item)
    return items


def _shared_exam_items(client, recruit_id, course_id, seen=()):
    rows = client.shared_exam_list(recruit_id, course_id, 1)
    rows += client.shared_exam_list(recruit_id, course_id, 0)
    # flag=0 also returns the chapter tests already listed as homework (same ids).
    items, seen = [], set(seen)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stu_exam_id = row.get("id")
        exam_id = row.get("examId")
        if stu_exam_id in (None, "") or exam_id in (None, ""):
            logger.warning("跳过缺少标识的共享课考试：%s", row.get("examName"))
            continue
        key = (str(stu_exam_id), str(exam_id))
        if key in seen:
            continue
        seen.add(key)
        item = {
            "id": "sex-%s-%s" % (stu_exam_id, exam_id),
            "name": row.get("examName") or "未命名考试",
            "startTime": row.get("startTime"),
            "endTime": row.get("endDate"),
            "zhs": {"kind": "shared_exam", "recruitId": recruit_id,
                    "courseId": course_id, "stuExamId": stu_exam_id, "examId": exam_id},
        }
        if "score" in row:
            item["score"] = row["score"]
        state = row.get("state")
        if state == 4:
            item["_completion_status"] = "submitted"
            item["_unavailable"] = ("已提交", "考试已提交。")
            items.append(item)
            continue
        if not row.get("studentStartTime") and state != 2:
            item["_unavailable"] = ("未开考", "考试需要先在网页上手动开考，再回来答题。")
        elif _positive_int(row.get("faStudentExamRemainCount")) <= 0:
            item["_unavailable"] = ("无剩余次数", "考试剩余次数不足或未知。")
        else:
            try:
                locked = client.shared_answer_lock(exam_id, recruit_id) == 1
            except (CaptchaRequired, AuthenticationExpired):
                raise
            except Exception as exc:
                logger.warning("%s：查询答案锁失败：%s", item["name"], exc)
                item["_unavailable"] = ("状态未知", "答案锁状态查询失败，请刷新重试。")
            else:
                if locked:
                    item["_unavailable"] = ("已锁定", "考试答案已锁定。")
        items.append(item)
    return items


def catalog_shared(client, course):
    """List shared-course homework and exams with their reasons for being unusable."""
    z = course.get("zhs") or {}
    recruit_id = z.get("recruitId")
    course_id = z.get("courseId")
    entries = []
    homework_failed = exam_failed = False

    client.shared_exam_sso()
    try:
        entries.extend(_shared_homework_items(client, recruit_id, course_id))
    except (CaptchaRequired, AuthenticationExpired):
        raise
    except Exception as exc:
        homework_failed = True
        logger.warning("读取共享课作业目录失败：%s", exc)

    try:
        listed = {(str(e["zhs"]["stuExamId"]), str(e["zhs"]["examId"])) for e in entries}
        entries.extend(_shared_exam_items(client, recruit_id, course_id, listed))
    except (CaptchaRequired, AuthenticationExpired):
        raise
    except Exception as exc:
        exam_failed = True
        logger.warning("读取共享课考试目录失败：%s", exc)

    if homework_failed and exam_failed:
        raise PlatformUnavailable("智慧树试卷目录读取失败，请稍后重试")
    return [(course, item, 1 if item["zhs"]["kind"] == "shared_homework" else 2)
            for item in entries]

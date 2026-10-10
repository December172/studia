"""Selected-paper workflow with local reports and verified submission outcomes."""
import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

from AIMoocMain.exam_api import ExamError, ExamServiceError, TaskUnavailableError
from AIMoocMain.export_questions import get_questions_from_paper, paper_display_name, paper_exam_id, safe_filename
from reporting import Reporter, log_callback
from ai.client import AIError, AIServiceError
from ai.concurrency import ParallelAnswers, prepared_items
from base.concurrency import validate_workers
from ai.questions import answer_record, model_question
from task_capabilities import task_availability


class AttemptClock:
    """Report only real elapsed time, at the web client's 10-second interval."""
    def __init__(self, api, course, exam_id, task_id, remaining, enabled):
        self.api, self.course, self.exam_id, self.task_id = api, course, exam_id, task_id
        self.remaining, self.enabled = remaining, enabled
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.error = None
        self.thread = None

    def start(self):
        if self.enabled:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        return self

    def _loop(self):
        previous = self.started
        while not self.stop_event.wait(10):
            now = time.monotonic()
            try:
                self.check()
                self.api.heartbeat(self.course, self.exam_id, self.task_id, int(now - previous))
                previous = now
            except ExamError as exc:
                self.error = exc
                return

    def check(self):
        if self.error:
            if isinstance(self.error, ExamError):
                raise self.error
            raise ExamError(self.error)
        if self.remaining is not None and self.elapsed >= self.remaining:
            raise ExamError("试卷剩余时间已用尽，停止交卷；请在网页核对状态")

    @property
    def elapsed(self):
        return max(0, int(time.monotonic() - self.started))

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=45)
            if self.thread.is_alive():
                self.error = ExamServiceError("计时请求未结束，停止交卷")


def report_path(root, user_id, course, exam_id):
    identity = "%s/%s/%s" % (user_id, course.get("id") or course.get("courseInfoId"), exam_id)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return Path(root) / safe_filename(course.get("courseName", "课程")) / (digest + ".json")


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temp), str(path))
    lines = ["# " + report["paper"], "", "状态：" + report["status"], "", "AI 生成答案，不是平台标准答案。", ""]
    if report.get("message"):
        lines += [report["message"], ""]
    for index, item in enumerate(report["answers"], 1):
        lines += ["## %d. %s" % (item.get("question_number", index), item.get("title", "无法解析的题目")), ""]
        for opt in item.get("options", []):
            lines.append("%s. %s" % (opt["label"], opt["text"]))
        lines += ["", ("人工答案：" if item.get("source") == "manual" else "AI 答案：") + json.dumps(item.get("answer"), ensure_ascii=False),
                  "置信度：" + str(item.get("confidence", "")),
                  item.get("explanation", ""), item.get("error", ""), ""]
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def archive_report(path, report):
    """Keep the previous completed attempt before replacing the current report."""
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
    archived = path.parent / "history" / path.stem / (digest + ".json")
    if not archived.exists():
        write_report(archived, report)


def attempt_not_draft(records, task_id):
    """Older attempts do not decide whether the current attempt can submit."""
    return any(str(record.get("status")) != "0" and
               (not (record.get("id") or record.get("taskId")) or
                str(record.get("id") or record.get("taskId")) == str(task_id))
               for record in records)


class AnswerRunner:
    def __init__(self, api, ai, output_dir="ai_results", auto_submit=False, min_confidence=0.8, output=None, clock_factory=AttemptClock, *, on_log=None, on_progress=None, manual_answer=None, max_workers=1):
        if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
            raise ValueError("置信度阈值必须在 0 到 1 之间")
        self.max_workers = validate_workers(max_workers)
        self.api, self.ai, self.output_dir = api, ai, output_dir
        self.auto_submit, self.min_confidence = auto_submit, min_confidence
        self.output = lambda message: log_callback(on_log if on_log is not None else output, message)
        self.clock_factory = clock_factory
        self.progress = Reporter(on_progress)
        self.reused = False
        self.manual_answer = manual_answer
        self.service_cooldown_until = 0.0

    def wait_for_ai(self, clock, delay):
        """Keep deadlines responsive during provider cooldowns and retries."""
        while delay > 0:
            clock.check()
            interval = min(delay, 0.25)
            time.sleep(interval)
            delay -= interval
        clock.check()

    def answer_question(self, question, clock, on_retry=None):
        """Retry only AI generation, preserving the paper and completed answers."""
        clock.check()
        cooldown = self.service_cooldown_until - time.monotonic()
        if cooldown > 0:
            self.wait_for_ai(clock, cooldown)
        for attempt in range(3):
            clock.check()
            try:
                return self.ai.answer(question)
            except AIError as exc:
                if not exc.retryable:
                    raise
                delay = max(attempt + 1, getattr(exc, 'retry_after', 0))
                if isinstance(exc, AIServiceError):
                    self.service_cooldown_until = max(self.service_cooldown_until, time.monotonic() + delay)
                if attempt == 2:
                    failure = type(exc)("单题重试 2 次后仍失败：" + str(exc))
                    if hasattr(exc, 'retry_after'):
                        failure.retry_after = exc.retry_after
                    raise failure from None
                self.output("本题重试 %s/2：%s" % (attempt + 1, exc))
                if on_retry:
                    on_retry(attempt + 1)
                self.wait_for_ai(clock, delay)

    def run_paper(self, course, item, category):
        availability = task_availability(item, category)
        if not availability.available:
            raise TaskUnavailableError(availability.reason)
        self.reused = False
        self.progress.detail("准备试卷：" + paper_display_name(item))
        self.progress.questions("读取试卷", 0)
        path = report_path(self.output_dir, self.api.user_id, course, paper_exam_id(item))
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock")
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ExamError("任务已锁定；确认没有其他程序运行并核对网页状态后，才能清理对应 .lock 文件") from None
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(str(os.getpid()))
            return self._run_paper(course, item, category)
        finally:
            lock.unlink()

    def _run_paper(self, course, item, category):
        exam_id = paper_exam_id(item)
        if not exam_id:
            raise ExamError("试卷缺少 ID")
        if not self.api.user_id or not course.get("courseId") or not (course.get("id") or course.get("courseInfoId")):
            raise ExamError("缺少账号或课程 ID")
        path = report_path(self.output_dir, self.api.user_id, course, exam_id)
        previous = {}
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise ExamError("已有报告无法读取，请先检查文件，避免重复交卷") from None
            if previous.get("status") in ("submitting", "submission_unknown"):
                self.reused = True
                self.progress.questions("交卷结果待确认", 0)
                self.output("跳过已有交卷记录：%s（%s）" % (paper_display_name(item), previous["status"]))
                return previous
            if previous.get("status") == "submitted":
                archive_report(path, previous)
        submitted_task_ids = [str(value) for value in previous.get("submitted_task_ids", []) if value]
        if previous.get("status") == "submitted" and previous.get("task_id"):
            submitted_task_ids.append(str(previous["task_id"]))
        submitted_task_ids = list(dict.fromkeys(submitted_task_ids))
        report = {"course": course.get("courseName", ""), "paper": paper_display_name(item),
                  "exam_id": exam_id, "category": category, "status": "preparing", "answers": [],
                  "auto_submit": self.auto_submit, "model": self.ai.config.model,
                  "task_id": previous.get("task_id"),
                  "submitted_task_ids": submitted_task_ids}
        clock = None
        parallel = None
        paper_opened = False
        completed, question_total = 0, None
        try:
            # The browser uses this same paper endpoint for '再次答题'. Older
            # submitted attempts must not prevent opening a fresh draft.
            records = self.api.records(exam_id)
            availability = task_availability(item, category)
            if not availability.available:
                raise TaskUnavailableError(availability.reason)
            paper = self.api.paper(exam_id)
            paper_opened = True
            task = paper.get("taskExamRecord") or {}
            if str(paper.get("tjzt")) == "1" or str(task.get("status")) != "0":
                raise ExamError("平台未提供新的可作答记录，本次无法再次作答；请检查剩余次数和开放状态")
            owner = task.get("userId") or task.get("studentId")
            if owner and str(owner) != str(self.api.user_id):
                raise ExamError("作答记录与当前账号不匹配")
            task_id = task.get("id")
            if not task_id:
                raise ExamError("未取得作答记录 ID，停止处理")
            if str(task_id) in submitted_task_ids or attempt_not_draft(records, task_id):
                raise ExamError("平台未提供新的可作答记录，本次无法再次作答；请刷新任务状态")
            if str(paper.get("typeId")) == "3" or str(paper.get("questionSettingMethod")) == "3":
                raise ExamError("附件或材料试卷暂不支持自动作答")
            raw_questions = get_questions_from_paper(paper)
            if not raw_questions:
                raise ExamError("试卷为空或结构不受支持")
            # Do not submit a parsed subset of a paper.
            count = paper.get("questionCount")
            if count is not None and int(count) != len(raw_questions):
                raise ExamError("试卷声明题数与解析题数不一致")
            remaining = paper.get("surplusSecond")
            if remaining is not None:
                remaining = int(remaining)
                if remaining <= 0:
                    raise ExamError("试卷已无剩余时间")
            report["task_id"] = str(task_id)
            report["status"] = "answering"
            write_report(path, report)
            clock = self.clock_factory(self.api, course, exam_id, task_id, remaining, self.auto_submit).start()
            items = prepared_items(raw_questions)
            entries, saved_answers = {}, {}
            failures = 0
            question_total = len(items)
            if self.max_workers > 1 and question_total > 1:
                parallel = ParallelAnswers(self.ai, [q for _, q, _ in items], self.max_workers, clock,
                                           allow_manual=self.manual_answer is not None)
                parallel.cooldown_until = self.service_cooldown_until
            self.progress.questions("AI 生成 · 等待答案", 0, question_total)
            results = parallel.answers() if parallel is not None else ((i, None) for i in range(len(items)))

            def save_answer(position, item_report):
                saved_answers[position] = item_report
                report["answers"] = [saved_answers[i] for i in sorted(saved_answers)]

            for position, generated in results:
                value, prepared, preparation_error = items[position]
                clock.check()
                stage = "第 %s 题" % (position + 1)
                self.progress.questions(stage + " · 解析题目", completed, question_total)
                item_report = {"title": str(value.get("title", "")) if isinstance(value, dict) else "无法解析的题目"}
                item_report["question_number"] = position + 1
                q = None
                try:
                    if preparation_error is not None:
                        raise preparation_error
                    q = prepared
                    item_report.update(title=q["title"], options=model_question(q)["options"])
                    self.progress.detail(report["paper"])
                    self.progress.questions(stage + " · AI 生成", completed, question_total)
                    self.output("AI 作答 %s / %s：%s" % (position + 1, question_total, q["title"][:70]))
                    if parallel is not None:
                        if isinstance(generated, BaseException):
                            raise generated
                        answer = generated
                    else:
                        answer = self.answer_question(model_question(q), clock,
                            on_retry=lambda retry: self.progress.questions(
                                stage + " · 重试 %s/2" % retry, completed, question_total))
                    self.progress.questions(stage + " · 校验答案", completed, question_total)
                    item_report.update(answer)
                    if (answer.get("source") != "manual" and
                            (not isinstance(answer.get("confidence"), (int, float)) or
                             isinstance(answer["confidence"], bool) or not math.isfinite(answer["confidence"]) or
                             not self.min_confidence <= answer["confidence"] <= 1)):
                        raise ValueError("置信度低于设置阈值，需要人工检查")
                    entry = answer_record(q, answer["answer"])
                    entries[position] = entry
                    item_report["record"] = entry
                except (ValueError, TypeError, AIError) as exc:
                    item_report["error"] = str(exc)
                    if parallel is not None and q is not None and self.manual_answer is not None:
                        save_answer(position, item_report)
                        write_report(path, report)
                        self.progress.questions(stage + " · 等待在途请求结束后补答", completed, question_total)
                        parallel.drain()
                    if isinstance(exc, AIServiceError) and not (exc.retryable and self.manual_answer is not None):
                        save_answer(position, item_report)
                        report.update(status="ai_error", message=str(exc))
                        return report
                    if parallel is not None and parallel.fatal_error is not None:
                        save_answer(position, item_report)
                        report.update(status="ai_error", message=str(parallel.fatal_error))
                        return report
                    if q is not None and self.manual_answer is not None:
                        # Persist before waiting for input, including EOF or Ctrl+C.
                        item_report["pending_manual"] = True
                        save_answer(position, item_report)
                        report.update(status="paused", message="报告已保存；重新开始时全部由 AI 作答")
                        write_report(path, report)
                        clock.check()
                        self.progress.questions(stage + " · 人工补答", completed, question_total)
                        manual = self.manual_answer(q, str(exc))
                        if manual is None:
                            return report
                        try:
                            entry = answer_record(q, manual)
                        except (ValueError, TypeError) as invalid:
                            item_report["error"] = str(invalid)
                            return report
                        item_report.update(answer=manual, confidence=None, explanation="人工补答",
                                           source="manual", record=entry)
                        item_report.pop("error", None)
                        item_report.pop("pending_manual", None)
                        entries[position] = entry
                        report.update(status="answering", message="")
                        write_report(path, report)
                        completed += 1
                        self.progress.questions("已处理题目", completed, question_total)
                        clock.check()
                        continue
                    failures += 1
                save_answer(position, item_report)
                write_report(path, report)
                completed += 1
                self.progress.questions("已处理题目", completed, question_total)
                clock.check()
            clock.stop()
            clock.check()
            if failures or not entries:
                report.update(status="needs_review", message="存在未完成或不支持的题目，仅保存本地报告，未交卷")
            elif not self.auto_submit:
                report.update(status="preview", message="答案已保存到本地，未向平台保存或提交答案")
            else:
                self.progress.questions("检查作答状态", completed, question_total)
                # Recheck this attempt, not the entire history. A browser may
                # have submitted this same draft while AI was still answering.
                if attempt_not_draft(self.api.records(exam_id), task_id):
                    raise ExamError("本轮作答状态已变化，停止交卷")
                clock.check()
                availability = task_availability(item, category)
                if not availability.available:
                    raise TaskUnavailableError(availability.reason)
                payload = {
                    "courseId": course["courseId"], "courseInfoId": course.get("id") or course.get("courseInfoId"),
                    "examId": exam_id, "examName": paper.get("name") or report["paper"],
                    "device": 1, "categoryId": category, "examTime": clock.elapsed,
                    "groupId": 0, "isLast": True, "id": task_id, "taskExamProblemRecordList": [entries[i] for i in sorted(entries)],
                }
                # Persist before the irreversible request, including on Ctrl-C or process exit.
                report["status"] = "submitting"
                write_report(path, report)
                self.progress.detail("正在交卷：" + report["paper"])
                self.progress.questions("正在交卷", completed, question_total)
                self.api.submit(payload)
                self.progress.questions("确认交卷结果", completed, question_total)
                if self.api.verify_submitted(exam_id, task_id):
                    submitted_task_ids.append(str(task_id))
                    report.update(status="submitted", message="平台作答记录已确认交卷")
                else:
                    report.update(status="submission_unknown", message="请求已发送但未确认已交卷状态，请在网页核对；不会自动重发")
        except TaskUnavailableError as exc:
            report.update(status="skipped", message=str(exc))
        except (ExamError, ValueError, TypeError, KeyError) as exc:
            state = "submission_unknown" if report["status"] == "submitting" else "error"
            report.update(status=state, message=str(exc))
            if isinstance(exc, ExamServiceError):
                report["stop_queue"] = True
        finally:
            if parallel is not None:
                parallel.close()
                self.service_cooldown_until = max(self.service_cooldown_until, parallel.cooldown_until)
            if clock:
                clock.stop()
            # A deadline crossed during history lookup must not erase an old
            # draft when no new paper was opened.
            if report["status"] != "skipped" or paper_opened:
                write_report(path, report)
            stage = {"submitted": "已交卷", "preview": "答案已生成 · 未交卷", "paused": "待补答",
                     "needs_review": "待人工检查", "skipped": "已跳过", "submission_unknown": "交卷结果待确认"}.get(
                         report["status"], "处理停止")
            self.progress.questions(stage, completed, question_total)
            report_location = "；报告 %s" % path if path.exists() else ""
            self.output("%s：%s%s" % (report["paper"], report["status"], report_location))
            if report.get("message"):
                self.output("原因：%s" % report["message"])
        return report

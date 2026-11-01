"""Selected-paper workflow with local reports and verified submission outcomes."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from AIMoocMain.exam_api import ExamError, ExamServiceError, TaskUnavailableError
from AIMoocMain.export_questions import get_questions_from_paper, paper_display_name, paper_exam_id, safe_filename
from reporting import Reporter, child_thread_name, log_callback
from ai.client import AIError, AIServiceError
from ai.concurrency import ParallelAnswers, prepared_items
from base.concurrency import validate_workers
from ai.questions import answer_record, model_question, question_fingerprint
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
            self.thread = threading.Thread(target=self._loop, daemon=True, name=child_thread_name('exam-clock'))
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
    header = "AI 生成答案，不是平台标准答案。"
    if any(item.get("source") == "manual" for item in report["answers"]):
        header = "答案由 AI 生成或人工补答，不是平台标准答案。"
    lines = ["# " + report["paper"], "", "状态：" + report["status"], "", header, ""]
    if report.get("message"):
        lines += [report["message"], ""]
    for index, item in enumerate(report["answers"], 1):
        lines += ["## %d. %s" % (item.get("question_number", index), item.get("title", "无法解析的题目")), ""]
        for opt in item.get("options", []):
            lines.append("%s. %s" % (opt["label"], opt["text"]))
        if item.get("source") == "manual":
            label = "人工答案："
        elif item.get("answer") is not None:
            label = "AI 答案："
        else:
            label = "待补答："
        lines += ["", label + json.dumps(item.get("answer"), ensure_ascii=False),
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


def reuse_identity(report, exam_id, task_id, category=None):
    """A previous report may supply answers only for this exact paper attempt."""
    if not isinstance(report, dict) or not task_id:
        return False
    if str(report.get("exam_id") or "") != str(exam_id) or str(report.get("task_id")) != str(task_id):
        return False
    # Only an unchanged task type may reuse; a missing category on either side cannot be compared.
    if category is None or report.get("category") is None:
        return True
    return str(report["category"]) == str(category)


def reusable_row(row, q, paper_id):
    """Return the validated answer of an unchanged question, or None."""
    if not isinstance(row, dict) or row.get("error") or row.get("answer") is None:
        return None
    fingerprint = row.get("question_fingerprint")
    if fingerprint:
        if fingerprint != question_fingerprint(q):
            return None
    else:
        # A plain row may speak only for its own plain choice question.
        kind = q["type_id"]
        if kind not in (1, 2) or q["paper_sub_id"] or q.get("context"):
            return None
        record = row.get("record")
        if not isinstance(record, dict) or record.get("paperSubId"):
            return None
        if str(record.get("paperId") or "") != str(paper_id):
            return None
        if str(record.get("questionNo")) != str(q["number"]):
            return None
        if str(row.get("title") or "") != q["title"]:
            return None
        options = row.get("options")
        fresh = [{"label": x["label"], "text": x["text"]} for x in q["options"]]
        if not isinstance(options, list) or options != fresh:
            return None
        sort_order = record.get("optionSort")
        # The platform may re-serialize whitespace: only the parsed value must match.
        try:
            saved_options = json.loads(sort_order) if isinstance(sort_order, str) else None
        except ValueError:
            return None
        if saved_options != q["raw_options"]:
            return None
        # A single choice carries one label, a multiple choice carries a list.
        label = row["answer"]
        if kind == 1 and not isinstance(label, str):
            return None
        if kind == 2 and not isinstance(label, list):
            return None
    try:
        answer_record(q, row["answer"])
    except (ValueError, TypeError):
        return None
    return row["answer"]


def pending_row(row, q):
    """True for a previous attempt's own failed question: an answer is still missing."""
    if not isinstance(row, dict):
        return False
    fingerprint = row.get("question_fingerprint")
    if fingerprint:
        return fingerprint == question_fingerprint(q) and bool(row.get("error") or row.get("answer") is None)
    return (row.get("answer") is None and not row.get("record") and q["type_id"] in (1, 2)
            and not q["paper_sub_id"] and not q.get("context") and row.get("title") == q["title"]
            and row.get("options") == model_question(q)["options"])


class AnswerRunner:
    def __init__(self, api, ai, output_dir="ai_results", auto_submit=False, output=None, clock_factory=AttemptClock, *, on_log=None, on_progress=None, max_workers=1, manual_answer=None, confirm_submit=None):
        self.max_workers = validate_workers(max_workers)
        self.api, self.ai, self.output_dir = api, ai, output_dir
        self.auto_submit = auto_submit
        self.output = lambda message: log_callback(on_log if on_log is not None else output, message)
        self.clock_factory = clock_factory
        self.progress = Reporter(on_progress)
        self.reused = False
        self.service_cooldown_until = 0.0
        self.manual_answer = manual_answer
        self.confirm_submit = confirm_submit

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
        completed, question_total = 0, None
        # Nothing below may touch the on-disk report until the fresh checkpoint is
        # fully prepared; the previous attempt stays readable for a later retry.
        fresh_checkpoint_ready = False
        try:
            # The browser uses this same paper endpoint for '再次答题'. Older
            # submitted attempts must not prevent opening a fresh draft.
            records = self.api.records(exam_id)
            availability = task_availability(item, category)
            if not availability.available:
                raise TaskUnavailableError(availability.reason)
            paper = self.api.paper(exam_id)
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
            items = prepared_items(raw_questions)
            report["status"] = "answering"
            entries, saved_answers = {}, {}
            question_total = len(items)

            def save_answer(position, item_report):
                saved_answers[position] = item_report
                report["answers"] = [saved_answers[i] for i in sorted(saved_answers)]

            # A previous attempt of this same task may already hold valid answers.
            # Every reusable row enters the report before the first clock or AI
            # call, so a deadline or service failure cannot lose it.
            reuse_count = 0
            manual_used = 0
            pending_positions = set()
            reusing = reuse_identity(previous, exam_id, task_id, category)
            previous_rows = previous.get("answers") if reusing else None
            cached = {}
            if isinstance(previous_rows, list):
                for index, old in enumerate(previous_rows):
                    if not isinstance(old, dict):
                        continue
                    # A legacy row may carry no number; its list index is then its position.
                    number = old.get("question_number")
                    position = number - 1 if isinstance(number, int) and not isinstance(number, bool) else index
                    if not 0 <= position < question_total or position in cached:
                        continue
                    cached[position] = old
            for position, (_, q, preparation_error) in enumerate(items):
                if preparation_error is not None or position not in cached:
                    continue
                reused = reusable_row(cached[position], q, q["paper_id"])
                if reused is None:
                    # A failed question from this same attempt is not regenerated:
                    # it is carried over as pending and handed to the human.
                    if (self.auto_submit and self.manual_answer is not None
                            and pending_row(cached[position], q)
                            and (previous.get("status") != "ai_error"
                                 or any(isinstance(row, dict) and row.get("question_number") == position + 1
                                        and row.get("question_fingerprint") == question_fingerprint(q)
                                        for row in previous.get("pending_manual") or []))):
                        pending_positions.add(position)
                        row = {**cached[position], "question_number": position + 1,
                               "title": q["title"],
                               "options": model_question(q)["options"],
                               "question_fingerprint": question_fingerprint(q)}
                        save_answer(position, row)
                        # Provenance of an established human question survives a
                        # later service failure, so a retry keeps it pending.
                        report["pending_manual"] = [existing for existing in report.get("pending_manual", [])
                                                    if existing["question_number"] != position + 1]
                        report["pending_manual"].append(
                            {"question_number": row["question_number"], "title": row["title"],
                             "reason": row.get("error") or "AI 未提供答案",
                             "question_fingerprint": row["question_fingerprint"]})
                    continue
                entry = answer_record(q, reused)
                source = "manual" if cached[position].get("source") == "manual" else "ai"
                entries[position] = entry
                manual_used += 1 if source == "manual" else 0
                item_report = {"title": q["title"], "question_number": position + 1,
                               "options": model_question(q)["options"],
                               "answer": reused, "confidence": cached[position].get("confidence"),
                               "explanation": cached[position].get("explanation", ""),
                               "record": entry, "source": source,
                               "question_fingerprint": question_fingerprint(q)}
                save_answer(position, item_report)
                reuse_count += 1
            completed = len(entries)
            if reusing:
                self.progress.detail("复用上一轮答案：" + report["paper"])
                if reuse_count:
                    self.output("复用上一轮已校验答案 %s 题" % reuse_count)
            # A new valid paper task replaces the old draft: keep the previous
            # completed attempt only now, when the fresh checkpoint is ready.
            if previous:
                archive_report(path, previous)
            fresh_checkpoint_ready = True
            write_report(path, report)
            clock = self.clock_factory(self.api, course, exam_id, task_id, remaining, self.auto_submit).start()
            if self.max_workers > 1 and question_total > 1:
                parallel = ParallelAnswers(self.ai, [None if position in entries or position in pending_positions else q
                                                     for position, (_, q, _) in enumerate(items)],
                                           self.max_workers, clock)
                parallel.cooldown_until = self.service_cooldown_until
            self.progress.questions("AI 生成 · 等待答案", completed, question_total)
            results = parallel.answers() if parallel is not None else ((i, None) for i in range(len(items)))

            for position, generated in results:
                value, prepared, preparation_error = items[position]
                if position in entries or position in pending_positions:
                    continue
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
                    item_report.update(title=q["title"], options=model_question(q)["options"],
                                       question_fingerprint=question_fingerprint(q))
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
                    entry = answer_record(q, answer["answer"])
                    entries[position] = entry
                    item_report["record"] = entry
                    item_report["source"] = "ai"
                    item_report["question_fingerprint"] = question_fingerprint(q)
                except (ValueError, TypeError, AIError) as exc:
                    item_report["error"] = str(exc)
                    if isinstance(exc, AIServiceError):
                        save_answer(position, item_report)
                        report.update(status="ai_error", message=str(exc))
                        return report
                    if parallel is not None and parallel.fatal_error is not None:
                        save_answer(position, item_report)
                        report.update(status="ai_error", message=str(parallel.fatal_error))
                        return report
                    # Unsupported or invalid questions are reported and kept in
                    # the local report; the paper is never submitted incomplete.
                save_answer(position, item_report)
                write_report(path, report)
                if position in entries:
                    # Only a question with a valid, submittable answer advances
                    # the counter; failed questions stay visible as unfinished.
                    completed += 1
                    self.progress.questions("已处理题目", completed, question_total)
                else:
                    self.progress.questions("第 %s 题 · 答案未完成" % (position + 1), completed, question_total)
                clock.check()
            clock.check()
            if reuse_count:
                pending_note = "，待人工补答 %s 题" % len(pending_positions) if pending_positions else ""
                self.output("复用上一轮已校验答案 %s 题，AI 重新生成 %s 题%s"
                            % (reuse_count, question_total - reuse_count - len(pending_positions), pending_note))
            unresolved = [position for position in range(question_total)
                          if position not in entries and items[position][2] is None]
            if unresolved and self.auto_submit and self.manual_answer is not None:
                for position in unresolved:
                    value, q, _ = items[position]
                    item_report = saved_answers[position]
                    reason = item_report.get("error") or "AI 未提供答案"
                    report["pending_manual"] = [{"question_number": row["question_number"], "title": row["title"],
                                                 "reason": row.get("error") or "AI 未提供答案",
                                                 "question_fingerprint": question_fingerprint(items[i][1])}
                                                for i, row in sorted(saved_answers.items())
                                                if i not in entries and items[i][2] is None]
                    report.update(status="paused", message="等待人工补答第 %s 题；已保存本地报告，未交卷" % (position + 1))
                    write_report(path, report)
                    self.progress.detail("人工补答：" + q["title"][:70])
                    clock.check()
                    try:
                        answer = self.manual_answer(q, reason)
                    except (EOFError, KeyboardInterrupt):
                        report.update(status="paused", message="已保存本地报告，未交卷；下次运行可继续补答")
                        write_report(path, report)
                        raise
                    if answer is None:
                        report.update(status="paused", message="已保存本地报告，未交卷；下次运行可继续补答")
                        write_report(path, report)
                        return report
                    try:
                        entry = answer_record(q, answer)
                    except (ValueError, TypeError) as exc:
                        item_report["error"] = str(exc)
                        save_answer(position, item_report)
                        write_report(path, report)
                        continue
                    item_report.pop("error", None)
                    item_report.update(answer=answer, record=entry, source="manual",
                                       question_fingerprint=question_fingerprint(q))
                    entries[position] = entry
                    completed += 1
                    manual_used += 1
                    save_answer(position, item_report)
                    report["pending_manual"] = [row for row in report["pending_manual"]
                                                if row["question_number"] != item_report["question_number"]]
                    write_report(path, report)
                    self.progress.questions("已处理题目", completed, question_total)
                    self.output("人工答案 %s / %s：%s" % (position + 1, question_total, q["title"][:70]))
                    # Persist the typed answer before a deadline that may have
                    # passed while the human was answering can abort the paper.
                    clock.check()
                if len(entries) >= question_total:
                    report.pop("pending_manual", None)
            if len(entries) < question_total:
                report.update(status="needs_review", message="存在未完成或不支持的题目，仅保存本地报告，未交卷")
            elif not self.auto_submit:
                report.update(status="preview", message="答案已保存到本地，未向平台保存或提交答案")
            else:
                if manual_used:
                    # A whole-paper submission after human input is explicit only.
                    # The attempt clock stays alive through the prompt, so time
                    # spent waiting on the human still counts against the deadline.
                    # Persist the finished draft before asking: even if the prompt
                    # is cancelled or raises, the report is not left as paused or
                    # answering with all answers already complete.
                    report.update(status="preview", message="答案已补齐，等待确认交卷；未向平台提交")
                    write_report(path, report)
                    result = self.confirm_submit(report["paper"], completed, question_total) if self.confirm_submit else False
                    if result is None:
                        # The human cancelled the prompt: end the run instead of
                        # starting the queued papers with the same state.
                        report.update(status="preview", message="已取消交卷；答案已保存到本地，队列已停止，未向平台提交")
                        report["stop_queue"] = True
                    elif not result:
                        report.update(status="preview", message="未确认交卷，答案已保存到本地，未向平台提交")
                    if not result:
                        return report
                # The request below is the point of no return; every earlier
                # deadline is still enforced against the confirmed answer.
                clock.stop()
                clock.check()
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
            # A signal or history failure before the fresh checkpoint exists must
            # leave a previous attempt's report untouched.
            if fresh_checkpoint_ready or not previous:
                write_report(path, report)
            stage = {"submitted": "已交卷", "preview": "答案已生成 · 未交卷",
                     "needs_review": "答案未完成", "skipped": "已跳过", "submission_unknown": "交卷结果待确认"}.get(
                         report["status"], "处理停止")
            self.progress.questions(stage, completed, question_total)
            report_location = "；报告 %s" % path if path.exists() else ""
            self.output("%s：%s%s" % (report["paper"], report["status"], report_location))
            if report.get("message"):
                self.output("原因：%s" % report["message"])
        return report

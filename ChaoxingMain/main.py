"""超星章节作业批量作答：课程 → 章节 → 作业。

这是本次迁移的入口，对应 chaoxing 项目 main.py 里 workid 任务的分发；视频、文档、
阅读、直播任务不在范围内。进度沿用 studia 的报告约定：每门课一个 result，每份作业
一个 unit，界面如何显示由调用方决定。
"""
import threading

from ai.client import AIServiceError
from base.auth import AuthenticationExpired, PlatformUnavailable
from ChaoxingMain.api import ChaoxingApi, ChaoxingError
from ChaoxingMain.exercise import ChaoxingExerciseRunner
from MoocMain.log import Logger
from reporting import Reporter

logger = Logger(__name__).get_log()

DONE = ("submitted", "saved")
UNIT_STATE = {"skipped": "skipped"}


def catalog(client, course):
    """这门课所有章节里的作业，给终端 / 网页的任务列表用。

    每条是 ``(course, item, category)``；item 除了任务本身，还带上作答要用的
    ``_point`` 和 ``_job_info``，这样选中的作业可以单独交给 runner.run()。
    平台不提供作业的提交状态（workid 卡片的 isPassed 不可靠），所以如实标成未知。
    """
    entries = []
    for point in client.course_points(course)["points"]:
        jobs, job_info = client.job_list(course, point)
        for job in jobs:
            if job["type"] != "workid":
                continue
            name = job.get("name") or point.get("title") or "章节作业"
            entries.append((course, dict(
                job, id=job["jobid"], name=name, _point=point, _job_info=job_info,
                _completion_status="unknown",
                _hint="平台不提供这份作业的提交状态；已完成的作业请不要再勾选"), 1))
    return entries


def course_units(client, course):
    """章节和其中的任务点，给网页课程页的单元格子用（只读）。

    非作业任务点的完成状态来自平台卡片；作业的 isPassed 不可靠，统一显示未完成。
    """
    chapters = []
    for point in client.course_points(course)["points"]:
        jobs, _ = client.job_list(course, point)
        chapters.append({"name": point.get("title") or "未命名章节", "units": [
            {"id": job["jobid"], "name": job.get("name") or job["type"],
             "kind": "work" if job["type"] == "workid" else job["type"],
             "state": "done" if job.get("is_passed") and job["type"] != "workid" else "queued"}
            for job in jobs]})
    return chapters


class ChaoxingHandler:
    def __init__(self, username="", password="", *, client=None, ai_client=None, courses=None,
                 course_ids=None, skip=None, auto_submit=False, cover_rate=0.8, delay=0.0,
                 on_progress=None, cancel_event=None, sleep=None):
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.client = client if client is not None else ChaoxingApi(username, password)
        self.ai_client = ai_client
        self.courses = courses
        self.course_ids = {str(value) for value in course_ids or ()}
        self.skip = [word for word in skip or () if word]
        self.progress = Reporter(on_progress)
        self.runner = ChaoxingExerciseRunner(
            self.client, ai_client, auto_submit=auto_submit, cover_rate=cover_rate, delay=delay,
            on_progress=on_progress, sleep=sleep, cancel_event=self.cancel_event)
        self.counts = {"submitted": 0, "saved": 0, "skipped": 0, "error": 0,
                       "cancelled": 0, "submission_unknown": 0}
        try:
            self.start_courses()
        finally:
            if client is None:
                self.client.close()

    def start_courses(self):
        try:
            rows = self.courses if self.courses is not None else self.client.course_list()
        except (AuthenticationExpired, PlatformUnavailable, ChaoxingError) as error:
            logger.error("读取超星课程失败：%s", error)
            self.progress.result("读取超星课程", "error")
            raise
        rows = [row for row in rows if self._wanted(row)]
        if not rows:
            logger.warning("没有可处理的超星课程")
            self.progress.result("读取超星课程", "skipped")
            return
        self.progress.begin("超星 · 章节作业", len(rows), "门课程")
        for course in rows:
            if self.cancel_event.is_set():
                break
            self.progress.detail(course.get("title", "课程"))
            self.progress.result(course.get("title", "课程"), self.process_course(course))

    def _wanted(self, course):
        if self.course_ids and str(course.get("courseId")) not in self.course_ids:
            return False
        title = course.get("title") or ""
        return not any(word in title for word in self.skip)

    def process_course(self, course):
        title = course.get("title", "课程")
        try:
            point_list = self.client.course_points(course)
        except (AuthenticationExpired, PlatformUnavailable):
            raise
        except ChaoxingError as error:
            logger.error("%s：读取章节失败：%s", title, error)
            return "error"
        if point_list["hasLocked"]:
            logger.info("%s：有需要完成任务点才能解锁的章节", title)
        failed = processed = False
        for point in point_list["points"]:
            if self.cancel_event.is_set():
                return "cancelled"
            try:
                jobs, job_info = self.client.job_list(course, point)
            except (AuthenticationExpired, PlatformUnavailable):
                raise
            except ChaoxingError as error:
                logger.error("%s / %s：读取任务点失败：%s", title, point.get("title", ""), error)
                failed = True
                continue
            for job in jobs:
                if job["type"] != "workid" or self.cancel_event.is_set():
                    continue
                status = self.process_work(course, point, job, job_info)
                failed |= status in ("error", "submission_unknown")
                processed |= status in DONE
        if failed:
            return "error"
        return "success" if processed else "skipped"

    def process_work(self, course, point, job, job_info):
        name = job.get("name") or point.get("title") or "章节作业"
        self.progress.unit(job["jobid"], name, "running")
        try:
            result = self.runner.run(course, point, job, job_info)
        except (AuthenticationExpired, PlatformUnavailable):
            self.progress.unit(job["jobid"], name, "failed")
            raise
        except AIServiceError:
            self.progress.unit(job["jobid"], name, "failed")
            raise
        except Exception as error:
            logger.exception("%s：作答异常", name)
            result = {"status": "error", "message": str(error)}
        status = result["status"]
        self.counts[status] = self.counts.get(status, 0) + 1
        state = "done" if status in DONE else UNIT_STATE.get(status, "failed")
        self.progress.unit(job["jobid"], name, state)
        logger.info("%s / %s：%s", course.get("title", "课程"), name, result["message"])
        return status

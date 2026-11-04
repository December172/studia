"""超星章节学习：视频、文档、阅读任务点。

迁移自 chaoxing 项目的 process_course / JobProcessor / process_chapter，保留三层调度：

- 同一门课里最多 ``max_workers`` 个章节同时处理（网页/终端的并发设置 1–8）；
- 每个章节内部的任务点再并行，最多 5 个（和源码的 ThreadPoolExecutor(5) 一致）；
- 章节失败（含视频 403）重试最多 5 次；未开放章节按 ``notopen_action`` 重试或跳过；
- 已完成章节、已通过的视频/文档直接跳过；没有任务点的空章节点一下完成；
- 视频先按"已看完"直接上报，平台不接受再按真实时间累计（speed 倍速）。

作业/测验（workid）不在这个流程里：答题由 ChaoxingMain.exercise 单独执行，
和网页/终端的「自动学习」「AI 答题」两个入口对应。
"""
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from base.auth import AuthenticationExpired, PlatformUnavailable
from base.concurrency import validate_workers
from ChaoxingMain.api import ChaoxingError
from MoocMain.log import Logger
from reporting import Reporter, child_thread_name

logger = Logger(__name__).get_log()

MAX_TRIES = 5          # 章节失败/未开放的重试次数（源码 max_tries）
TASK_WORKERS = 5       # 一个章节内同时处理的任务点数（源码 ThreadPoolExecutor(5)）
RETRY_DELAY = 30       # 给章节解锁和平台限流留出时间，等待可被取消
NOT_OPEN_ACTIONS = ("retry", "continue")


class ChapterTask:
    __slots__ = ("point", "tries")

    def __init__(self, point):
        self.point = point
        self.tries = 0


class ChaoxingStudyHandler:
    def __init__(self, *, client, course_rows, on_progress=None, max_workers=1, cancel_event=None,
                 speed=1.0, sleep=None, notopen_action="retry"):
        self.client = client
        self.course_rows = list(course_rows or [])
        self.max_workers = validate_workers(max_workers)
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        # 源码把倍速限制在 1–2 倍；超过 2 倍上报会被平台判定异常。
        self.speed = min(2.0, max(1.0, float(speed or 1.0)))
        self.sleep = sleep if sleep is not None else self.cancel_event.wait
        self.notopen_action = notopen_action if notopen_action in NOT_OPEN_ACTIONS else "retry"
        self.progress = Reporter(on_progress)
        # 当前课程已完成的 jobid；章节里先过滤掉，避免重复上报文档/视频。
        self.finished_jobs = set()
        # 与其他平台 handler 的约定一致：没有需要 UI 处理的中断时保持 None。
        self.blocked = None
        self.start_courses()

    # ------------------------------------------------------------------ 课程

    @staticmethod
    def _name(course):
        return course.get("courseName") or course.get("title") or "课程"

    def start_courses(self):
        if not self.course_rows:
            return
        self.progress.begin("学习管理", len(self.course_rows), "门课程")
        for course in self.course_rows:
            if self.cancel_event.is_set():
                break
            title = self._name(course)
            self.progress.detail(title)
            self.progress.result(title, self.process_course(course))

    def process_course(self, course):
        title = self._name(course)
        try:
            points = self.client.course_points(course)["points"]
        except (AuthenticationExpired, PlatformUnavailable):
            raise
        except ChaoxingError as error:
            logger.error("%s：读取章节失败：%s", title, error)
            return "error"
        nodes, self.finished_jobs = self._preflight(course)
        total = len(points)
        running = {}
        counts = {"completed": 0, "skipped": 0, "failed": 0}
        pending = []
        for point in points:
            counts_node = nodes.get(str(point.get("id"))) or {}
            if counts_node.get("unfinish", 0) == 0 and counts_node.get("finish", 0) > 0:
                logger.info("%s / %s：平台显示已学完 %d 个任务点，跳过", title,
                            point.get("title", ""), counts_node["finish"])
                self.progress.unit(str(point.get("id")), point.get("title") or "", "done")
                counts["skipped"] += 1
            else:
                pending.append(ChapterTask(point))
        self.progress.resources("%s · 章节" % title, counts["skipped"], total)
        fatal = None
        pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix=child_thread_name("chapter"))
        try:
            while (pending or running) and not self.cancel_event.is_set():
                while pending and len(running) < self.max_workers and not self.cancel_event.is_set():
                    task = pending.pop(0)
                    running[pool.submit(self.process_chapter, course, task.point, retry=task.tries > 0)] = task
                if not running:
                    break
                finished, _ = wait(list(running), return_when=FIRST_COMPLETED, timeout=1)
                for future in finished:
                    task = running.pop(future)
                    try:
                        result = future.result()
                    except (AuthenticationExpired, PlatformUnavailable) as error:
                        fatal = error
                        self.cancel_event.set()
                        continue
                    except Exception as error:  # 单章异常不该带走其他章节
                        logger.exception("%s / %s：章节处理异常", title, task.point.get("title", ""))
                        result = "error"
                    if result == "cancelled":
                        continue
                    if result in ("success", "skipped"):
                        counts["completed" if result == "success" else "skipped"] += 1
                    elif result == "not_open":
                        task.tries += 1
                        if self.notopen_action == "continue" or task.tries >= MAX_TRIES:
                            counts["skipped"] += 1
                            logger.warning("%s / %s：章节未开放，跳过", title, task.point.get("title", ""))
                        else:
                            pending.append(task)
                    else:
                        task.tries += 1
                        if task.tries >= MAX_TRIES:
                            counts["failed"] += 1
                            logger.error("%s / %s：重试 %d 次仍失败，跳过", title,
                                         task.point.get("title", ""), MAX_TRIES)
                        else:
                            pending.append(task)
                    finished_count = counts["completed"] + counts["skipped"] + counts["failed"]
                    self.progress.resources("%s · 章节" % title, finished_count, total)
        finally:
            pool.shutdown(wait=True)
        if fatal is not None:
            raise fatal
        if self.cancel_event.is_set():
            return "cancelled"
        if counts["failed"]:
            return "error"
        return "success" if counts["completed"] else "skipped"

    def _preflight(self, course):
        """开跑前用两份平台状态省掉无用章节：节点完成计数 + 已完成 jobid。

        取不到就退回卡片里的 isPassed，不影响原来的行为。
        """
        nodes, finished = {}, set()
        method = getattr(self.client, "course_nodes", None)
        if callable(method):
            try:
                nodes = method(course) or {}
            except (AuthenticationExpired, PlatformUnavailable):
                raise
            except ChaoxingError as error:
                logger.info("读取章节完成状态失败，按卡片状态处理：%s", error)
        method = getattr(self.client, "finished_job_ids", None)
        if callable(method):
            try:
                finished = set(method(course) or ())
            except (AuthenticationExpired, PlatformUnavailable):
                raise
            except ChaoxingError as error:
                logger.info("读取已完成任务点失败，按卡片状态处理：%s", error)
        return nodes, finished

    # ------------------------------------------------------------------ 章节

    def process_chapter(self, course, point, *, retry=False):
        if retry and not self.cancel_event.is_set():
            self.sleep(RETRY_DELAY)
        if self.cancel_event.is_set():
            return "cancelled"
        if point.get("has_finished"):
            return "skipped"
        jobs, job_info = self.client.job_list(course, point)
        if job_info.get("notOpen"):
            return "not_open"
        if jobs and self.finished_jobs:
            jobs = [job for job in jobs if job["jobid"] not in self.finished_jobs]
            if not jobs:
                logger.info("%s / %s：任务点都已完成，跳过", self._name(course),
                            point.get("title", ""))
                return "skipped"
        if not jobs:
            # 源码在 get_job_list 里顺手完成空章节；这里放在学习流程里，保证任务列表是只读的。
            self.client.empty_page(course, point)
            return "success"
        tasks = [job for job in jobs if job["type"] != "workid"]
        if not tasks:
            return "skipped"  # 只有作业的章节留给「AI 答题」
        if len(tasks) == 1:
            results = [self.process_task(course, tasks[0], job_info)]
        else:
            workers = min(TASK_WORKERS, len(tasks))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=child_thread_name("task")) as pool:
                results = [future.result() for future in
                           [pool.submit(self.process_task, course, job, job_info) for job in tasks]]
        if self.cancel_event.is_set() or "cancelled" in results:
            return "cancelled"
        return "success" if all(result in ("success", "skipped") for result in results) else "error"

    # ------------------------------------------------------------------ 任务点

    def process_task(self, course, job, job_info):
        name = job.get("name") or job["type"]
        jobid = job["jobid"]
        if self.cancel_event.is_set():
            return "cancelled"
        self.progress.unit(jobid, name, "running")
        try:
            result = self._run_task(course, job, job_info)
        except (AuthenticationExpired, PlatformUnavailable):
            self.progress.unit(jobid, name, "failed")
            raise
        except ChaoxingError as error:
            logger.error("%s：%s", name, error)
            result = "error"
        except Exception:
            logger.exception("%s：学习任务异常", name)
            result = "error"
        state = "done" if result == "success" else "skipped" if result == "skipped" else "failed"
        self.progress.unit(jobid, name, state)
        return result

    def _run_task(self, course, job, job_info):
        kind = job["type"]
        if kind == "video":
            result = self.client.study_video(course, job, speed=self.speed, cancel_event=self.cancel_event,
                                             job_info=job_info)
            if result == "skipped":
                return result  # 非任务点视频，不用再试音频
            if result not in ("success", "cancelled"):
                # 源码在视频失败后再试一次音频：有些任务点实际是音频，解码字段一样。
                audio = self.client.study_video(course, job, speed=self.speed, kind="Audio",
                                                cancel_event=self.cancel_event, job_info=job_info)
                if audio in ("success", "cancelled") or result == "forbidden":
                    result = audio
            return result
        if kind == "document":
            return self.client.study_document(course, job, job_info)
        if kind == "read":
            return self.client.study_read(course, job, job_info)
        return "skipped"

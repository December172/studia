import json
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any

from base.api_client import RequestRateLimited
from base.auth import AuthenticationExpired
from base.concurrency import INTERACTIVE_WORKERS, validate_workers
from base.study import run_resources, check_study_response
from reporting import Reporter
from MoocMain.log import Logger
from AIMoocMain.api import AIMoocApi
from AIMoocMain.activities import CourseActivities
from base.util import parse_duration, get_mp3_duration

# 作业、考试、测验在答题里做；子节点和文件夹无法展开。
SKIPPED_TYPES = ("作业", "考试", "测验", "子节点", "文件夹")


def course_units(client, course):
    """Chapters with their leaf resources and how far each one is, without studying.

    The platform takes seconds per list, so the chapter list, the completion record and
    every chapter's cells are requested at the same time (reads only)."""
    info_id, course_id = course.get("id"), course.get("courseId")
    with ThreadPoolExecutor(INTERACTIVE_WORKERS) as pool:
        records = pool.submit(client.study_record_list, info_id, course_id)
        design = client.study_design_list(info_id, course_id) or []
        cells = list(pool.map(lambda chapter: client.get_cell_list(info_id, course_id, chapter.get("id")) or [], design))
        done = set(records.result() or ())

    def leaves(nodes):
        for node in nodes:
            yield from leaves(node["children"]) if node.get("children") else (node,)

    return [{"name": chapter.get("name") or "未命名章节", "units": [{
        "id": str(node.get("id")), "name": node.get("name") or "资源", "kind": node.get("fileType") or "",
        "state": "done" if node.get("id") in done else
                 "skipped" if node.get("fileType") in SKIPPED_TYPES else "queued",
    } for node in leaves(chapter_cells)]} for chapter, chapter_cells in zip(design, cells)]


class AIMoocHandler:
    def __init__(self, jump_content: str = "", token: str = None, username: str = None, password: str = None, *, client=None, course_rows=None, on_progress=None, max_workers=1, cancel_event=None, activity_ai=None):
        """初始化 API 客户端和日志"""
        # 跳过课程列表
        self.max_workers = validate_workers(max_workers)
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.jump_list = []
        # 已完成的课程id列表
        self.study_record_list = []
        if jump_content and "#" in jump_content:
            self.jump_list = jump_content.split("#")[1:]

        # 根据是否提供token选择不同的初始化方式
        self.client = client if client is not None else AIMoocApi(token=token, username=username, password=password)
        self.course_rows = course_rows

        self.logging = Logger(__name__).get_log()
        self.progress = Reporter(on_progress)
        self.activities = (
            CourseActivities(self.client, activity_ai, cancel_event=self.cancel_event, log=self.logging,
                             on_detail=self.progress.detail)
            if activity_ai is not None else None
        )
        try:
            self.start_courses()
        finally:
            if client is None:
                self.client.close()

    def start_courses(self) -> None:
        """获取并处理课程列表"""
        try:
            if self.course_rows is None:
                courses = self.client.my_course_list(page_size=9999)
                course_rows = courses.get("rows", [])
            else:
                course_rows = self.course_rows
            total_courses = len(course_rows)

            if not course_rows:
                self.logging.warning("当前没有课程数据")
                return
            self.progress.begin("学习管理", total_courses, "门课程")
            for course in course_rows:
                if self.cancel_event.is_set():
                    break
                course_name = course.get("courseName", "未知课程")
                self.progress.resources(f"{course_name} · 正在读取资源目录", 0)
                if any(s in course.get("courseName") for s in self.jump_list):
                    self.progress.result(course_name, "skipped")
                    continue
                self._course_failed = False
                self._course_processed = False
                self.progress.detail(course_name)
                self.process_course(course)
                status = "error" if self._course_failed else "success" if self._course_processed else "skipped"
                self.progress.result(course_name, status)

        except Exception as e:
            self.logging.error(f"获取课程失败: {e}")
            self.progress.result("读取或处理课程", "error")

    def process_course(self, course: Dict[str, Any]) -> None:
        """处理单个课程"""
        course_name = course.get("courseName", "未知课程")
        teacher = course.get("displayName", "未知教师")  # 适配新字段
        course_info_name = course.get("courseInfoName", "未知课程信息")
        study_speed = course.get("studySpeed", "未知")  # 适配新字段：学习进度
        final_score = course.get("finalScore", "未知")
        course_info_id = course.get("id")
        course_id = course.get("courseId")

        self.logging.info(
            f" 课程: {course_name} ({course_info_name}) | 教师: {teacher} |  学习进度: {study_speed}% | 成绩: {final_score}"
        )

        if course_info_id:
            study_design_list = self.client.study_design_list(course_info_id, course_id)
            self.study_record_list = self.client.study_record_list(
                course_info_id, course_id
            )
            if not isinstance(self.study_record_list, (list, tuple)):
                raise RuntimeError("无法读取课程已完成记录，停止学习")
            self.study_record_list = tuple(self.study_record_list)

            def resources():
                for node in study_design_list:
                    parent_id = node.get("id")
                    cell_list = self.client.get_cell_list(course_info_id, course_id, parent_id)
                    yield from self.print_course_structure(cell_list, parent_id)
            run_resources(self, resources(), course_name)
            self.run_activities(course)
        else:
            self.logging.warning(f" 课程 {course_name} 缺少 CourseInfoId，跳过")

    def run_activities(self, course: Dict[str, Any]) -> None:
        """参与课程主题讨论并提交五星课程评价（资源处理完成后独立执行）"""
        if self.activities is None or self.cancel_event.is_set():
            return
        course_name = course.get("courseName", "未知课程")
        try:
            summary = self.activities.run_course(course)
        except (AuthenticationExpired, RequestRateLimited):
            self.cancel_event.set()
            raise
        except Exception as error:
            self._course_failed = True
            self.logging.error("课程讨论/评价处理失败: %s", error)
            return
        label = {"submitted": "已提交五星评价", "evaluated": "已评价，跳过",
                 "failed": "失败", "skipped": "跳过"}.get(summary["evaluation"], summary["evaluation"])
        self.logging.info(
            "%s · 课程讨论 参与 %s · 跳过 %s · 失败 %s · 课程评价 %s",
            course_name, summary["discussion_posted"], summary["discussion_skipped"],
            summary["discussion_failed"], label,
        )
        self._course_processed |= summary["discussion_posted"] > 0 or summary["evaluation"] == "submitted"
        self._course_failed |= summary["discussion_failed"] > 0 or summary["evaluation"] == "failed" or self.cancel_event.is_set()

    def print_course_structure(
        self, nodes: List[Dict[str, Any]], parent_id: str, indent: int = 0
    ):
        """递归打印课程结构，按原顺序产出资源参数"""
        prefix = " " * (indent * 4)  # 每层缩进 4 个空格

        for node in nodes:
            name = node.get("name", "未知")
            file_type = node.get("fileType", "未知")
            self.logging.info(f"{prefix}- {name} ({file_type})")

            children = node.get("children", [])
            if children:
                yield from self.print_course_structure(children, parent_id, indent + 1)
            else:
                yield (node, parent_id, indent)

    def handle_resource(
        self, node: Dict[str, Any], parent_id: str, indent: int = 0
    ) -> str:
        """处理最终的资源，并保存学习记录"""
        resource_name = node.get("name", "未知资源")
        self.progress.detail(resource_name)
        file_type = node.get("fileType", "未知类型")
        course_id = node.get("courseId")
        source_id = node.get("id")
        prefix = " " * ((indent + 1) * 4)  # 每层缩进 4 个空格

        self.logging.info(f"{prefix}  资源: {resource_name} | 类型: {file_type}")

        if file_type in SKIPPED_TYPES:
            self.logging.info(f"{prefix}  跳过{file_type}")
            return "skipped"

        # 已完成的课程不再处理
        if source_id in self.study_record_list:
            self.logging.info(f"{prefix}  跳过已完成的课程: {resource_name}")
            return "skipped"

        try:
            # 默认学习总数
            total_num = random.randint(1, 10)
            url_short = None
            course_content = node.get("fileUrl", "")
            if course_content and course_content.strip() != "":
                course_content = json.loads(course_content)
                url_short = course_content.get("url")
            # mp3 时长接口无法查询，本地获取
            if file_type == "audio":
                mp3_duration = get_mp3_duration(course_content.get("ossOriUrl"))
                total_num = int(mp3_duration)
            elif file_type == "ppt":
                total_num = random.randint(1, 10)
            elif file_type == "img":
                total_num = random.randint(1, 10)
            elif file_type == "图文":
                total_num = random.randint(1, 10)
            elif url_short:
                # 视频类型
                file_status_args_duration = course_content.get("duration")
                # 文件类型
                file_status_args_has_txt_file = course_content.get("has_txt_file")
                if file_status_args_duration:
                    total_num = parse_duration(file_status_args_duration)
                elif file_status_args_has_txt_file:
                    total_num = course_content.get("page_count")

            course_info_id = node.get("courseInfoId")
            study_time = total_num

            wait_time = round(random.uniform(0.5, 1.5), 2)

            self.logging.info(
                f"{prefix}  学习数: {total_num}，等待 {wait_time} 秒..."
            )

            time.sleep(wait_time)
            if self.cancel_event.is_set():
                self._course_failed = True
                return "cancelled"
            response = self.client.study_record(
                course_id,
                course_info_id,
                parent_id,
                study_time,
                source_id,
                total_num,
                total_num,
                total_num,
            )
            check_study_response(response)
            self.logging.info("学习记录已保存")
            self._course_processed = True
            return "success"
        except (AuthenticationExpired, RequestRateLimited):
            self.cancel_event.set()
            raise
        except Exception as e:
            self._course_failed = True
            self.logging.error(f"处理资源失败: {e}")
            return "error"

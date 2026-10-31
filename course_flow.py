"""Course workspace navigation; platform operations remain in Application."""
from pathlib import Path

from AIMoocMain.export_questions import QuizExportHandler, paper_exam_id
from ai.runner import report_path
from ui import Back, Cancelled

REVIEW_MINUTES = 30


def task_identity(task):
    course, item, category = task
    zhs = course.get("zhs")
    if zhs is not None:
        return (str(zhs.get("courseId")), str(zhs.get("classId", "")),
                paper_exam_id(item), category)
    return (str(course.get("id") or course.get("courseInfoId") or ""),
            str(course.get("courseId") or ""), paper_exam_id(item), category)


class CourseWorkspace:
    def __init__(self, application):
        self.app = application
        self.view = application.view
        self.store = application.store
        self.platform = 0
        self.client = None
        self.rows = []
        self.library_state = {}

    def run(self):
        # Import here so the legacy entry points and dependency injection share
        # the same client factories.
        from app import AIMoocApi, ZYKMoocApi, ZHSApi

        self.view.workspace = True
        while True:
            try:
                if self.client is None:
                    try:
                        cls = {1: ZYKMoocApi, 2: ZHSApi}.get(self.platform, AIMoocApi)
                        self.client, self.rows = self.app.client_and_courses(cls, choose=False)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        action = self.view.connection_problem(exc)
                        self.root_action(action)
                        continue
                selected = self.view.library(self.rows, self.platform, self.library_state)
                if isinstance(selected, str):
                    self.root_action(selected)
                    continue
                if selected:
                    self.course_loop(selected)
            except Back:
                continue

    def root_action(self, action):
        if action == "s":
            if self.app.manage_settings(current_platform={1: "zyk", 2: "zhs"}.get(self.platform, "mooc")):
                self.view.set_user()
                self.client, self.rows = None, []
                self.library_state.clear()
        elif action == "t":
            platform = self.view.choose_platform(self.platform)
            if platform != self.platform:
                self.view.set_user()
                self.platform = platform
                self.client, self.rows = None, []
                self.library_state.clear()
        elif action == "r":
            self.view.set_user()
            self.client = None

    def course_loop(self, courses):
        task_state = {}
        while True:
            try:
                mode = self.view.course_menu(courses, self.platform)
            except Back:
                return
            try:
                if mode == 1:
                    self.view.review_study(
                        courses, max_workers=self.store.concurrency()['study'],
                        activities=self.platform == 0,
                        notice=("逐个视频实时播放，耗时约为视频时长的三分之二；"
                                "平台限制同一账号同时只能看一个视频。") if self.platform == 2 else "")
                    result = self.execute(lambda: self.app.study(self.platform, self.client, courses))
                elif mode == 3 and self.platform == 2:
                    self.view.review_study(
                        courses, title="每日复习",
                        notice=("共享课从已看完的视频里按日期轮换，1 倍速实时重看 %d 分钟；"
                                "智慧课程会跳过。每天运行一次。" % REVIEW_MINUTES))
                    result = self.execute(lambda: self.app.study(
                        self.platform, self.client, courses, review_minutes=REVIEW_MINUTES))
                else:
                    result = self.tasks(courses, mode, task_state.setdefault(mode, {}))
                if result == 2:
                    return
            except Back:
                continue
            except (Cancelled, EOFError):
                raise
            except Exception as exc:
                self.view.begin_operation()
                self.view.pending_error = str(exc)
                if self.view.operation_result() == 2:
                    return

    def catalog_for(self, courses):
        if self.platform == 2:
            return self.app.zhs_task_catalog(self.client, courses)
        return self.app.task_catalog(self.client, courses)

    def tasks(self, courses, mode, state):
        try:
            catalog = self.catalog_for(courses)
        except Exception as exc:
            self.view.begin_operation()
            self.view.pending_error = str(exc)
            return self.view.operation_result()
        if not catalog:
            self.view.empty_tasks()
            return 1
        saved = self.store.get("export", {})
        export_options = {key: saved[key] for key in ("export_format", "quiz_mode", "include_my_answer")
                          if isinstance(saved, dict) and key in saved}
        answer_options = {"auto_submit": True}
        selected = state.get("selected", [])
        while True:
            # History/status fields can change between reads; selection belongs
            # to the task identity, not the entire response dictionary.
            selected_ids = {task_identity(task) for task in selected}
            selected = [task for task in catalog if task_identity(task) in selected_ids]
            choice = self.view.papers(catalog, selected=selected, state=state, refresh=True)
            if choice == "r":
                selected = state.get("selected", selected)
                try:
                    catalog = self.catalog_for(courses)
                except Exception as exc:
                    self.view.begin_operation()
                    self.view.pending_error = str(exc)
                    return self.view.operation_result()
                if not catalog:
                    self.view.empty_tasks()
                    return 1
                continue
            selected = choice
            state["selected"] = selected
            if not selected:
                return 1
            try:
                if mode == 3:
                    options = self.view.review_export(selected, export_options)
                    self.app.remember(lambda: self.store.set("export", options))
                    def export():
                        handler = QuizExportHandler(client=self.client, course_rows=courses,
                                                    selected_papers=selected, **options,
                                                    dev_mode=self.view.dev, on_progress=self.view.on_progress)
                        return [row[key] for row in handler.summary_rows for key in ("markdown", "docx") if row.get(key)]
                    result = self.execute(export, "导出题目")
                else:
                    config = self.app.ai_configuration()
                    answer_options["connection_status"] = self.app.ai_status_text(config)
                    auto_submit = self.view.review_ai(
                        selected, config, answer_options,
                        check_connection=lambda: self.app.test_ai_connection(answer_options.get("config")),
                        manage_models=self.app.manage_models,
                        connection_status=self.app.ai_status_text)
                    config = answer_options["config"]
                    def answer():
                        if self.platform == 2:
                            # 智慧树没有本地报告文件，执行器已记录结果与错误。
                            self.app.execute_zhs_ai(self.client, selected, config, auto_submit)
                            return []
                        results = self.app.execute_ai(self.client, selected, config, auto_submit)
                        for report in results:
                            if report.get("status") in ("submitted", "preview"):
                                continue
                            if report.get("message"):
                                self.view.log(report["message"], "warning")
                            for entry in report.get("answers", []):
                                if entry.get("error"):
                                    self.view.log(f"{report['paper']}：{entry['error']}", "warning")
                        paths = []
                        for (course, item, _), report in zip(selected, results):
                            path = report_path("ai_results", self.client.user_id, course, paper_exam_id(item)).with_suffix(".md")
                            if path.exists():
                                paths.append(str(path))
                        return paths
                    result = self.execute(answer, "AI 答题")
                state.clear()
                return result
            except Back:
                continue

    def execute(self, operation, title=None):
        self.view.begin_operation()
        paths = []
        try:
            if title:
                with self.view.activity(title):
                    paths = operation() or []
            else:
                operation()
        except KeyboardInterrupt:
            self.view.cancelled = True
            self.view.log("操作已中断；未确认的交卷结果请到平台核对。", "warning")
        except Exception as exc:
            self.view.pending_error = str(exc)
        return self.view.operation_result([str(Path(path).resolve()) for path in paths])

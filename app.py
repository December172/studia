"""Application orchestration. Menus/rendering are provided by the view (ui.py)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import time
import zlib

from MoocMain.log import Logger

from AIMoocMain.api import AIMoocApi
from AIMoocMain.main import AIMoocHandler
from AIMoocMain.exam_api import ExamAPI, ExamError, ExamServiceError, TaskUnavailableError
from AIMoocMain.export_questions import QuizExportHandler, collect_quiz_catalog, paper_display_name, paper_exam_id
from ChaoxingMain.api import ChaoxingApi, WorkUnavailable
from ChaoxingMain.exercise import ChaoxingExerciseRunner
from ChaoxingMain.login import qr_login as cx_qr_login
from ChaoxingMain.study import ChaoxingStudyHandler
from ChaoxingMain.main import catalog as cx_catalog
from ZYKMoocMain.api import ZYKMoocApi
from ZYKMoocMain.main import ZYKMoocHandler
from ZHSMain.api import CaptchaRequired, StudyTimeLimit, ZHSApi
from ZHSMain.main import ZHSHandler
import ZHSMain.answer
from ZHSMain.answer import ZHSAnswerRunner
from ZHSMain.memory import AnswerMemory
from ZHSMain.login import qr_login
from NewMoocMain.oauth_login import oauth_login
from ai.client import AIClient, AIError, AIServiceError
from ai.memory import MoocAnswerMemory
from ai.runner import AnswerRunner
from reporting import Reporter
from base.auth import AuthenticationExpired, PlatformUnavailable
from base.concurrency import INTERACTIVE_WORKERS
from settings import SettingsStore, SettingsError
from task_capabilities import task_availability
from task_metrics import completion_with_score

logger = Logger(__name__).get_log()


class Application:
    def __init__(self, view, store=None):
        self.view = view
        self.progress = Reporter(view.on_progress)
        self.store = store if store is not None else SettingsStore()
        # 显式退出后，下一次浏览器登录先走官方 /loginOut 清掉共享 cookie；
        # 成功拿到 token 才消费，失败/取消保留退出意图。
        self.browser_logout_pending = False

    def remember(self, operation):
        try:
            operation()
            return True
        except SettingsError as exc:
            self.view.note(str(exc) + "；本次改动未持久化。", "warning")
            return False

    def login(self):
        self.view.login_hint()
        while True:
            try:
                with self.view.activity("等待浏览器登录"):
                    token = oauth_login(timeout=300, force_logout=self.browser_logout_pending)
                if token:
                    self.browser_logout_pending = False
                    return "", "", token
                self.view.note("登录失败或超时。", "warning")
            except Exception as exc:
                logger.exception("OAuth 登录异常")
                self.view.note(f"登录失败：{exc}", "error")
            if not self.view.confirm("重试登录", default=True):
                from ui import Cancelled
                raise Cancelled()

    def zhs_login(self):
        from ui import Cancelled
        while True:
            self.view.note("请用智慧树 App 扫描浏览器中打开的二维码。")
            client = None
            try:
                with self.view.activity("等待智慧树扫码"):
                    client = ZHSApi()
                    if qr_login(client, on_status=lambda message: self.view.note(message)):
                        return "", "", client.access_token
                self.view.note("扫码未完成或已超时。", "warning")
            except Exception as exc:
                logger.exception("智慧树扫码登录异常")
                self.view.note(f"登录失败：{exc}", "error")
            if client is not None:
                client.close()
            if not self.view.confirm("重试扫码登录", default=True):
                raise Cancelled()

    def client_and_courses(self, api_class, login=None, *, choose=True):
        self.view.set_user()
        platform = {ZYKMoocApi: "zyk", ZHSApi: "zhs"}.get(api_class, "mooc")
        saved = self.store.session(platform) if login is None else None
        if saved:
            try:
                with self.view.activity("恢复已保存的登录状态"):
                    client = api_class(access_token=saved["access_token"])
                    rows = self.read_courses(client)
                self.view.note("已复用本机登录状态。")
                self.view.set_user(getattr(client, "user_info", None))
                return client, self.view.courses(rows) if choose else rows
            except AuthenticationExpired:
                self.store.clear_session(platform)
                self.view.note("保存的登录已过期，请重新登录；成功后自动保存。", "warning")
        if login is None and platform == "zhs":
            login = self.zhs_login
        username, password, token = (login or self.login)()
        with self.view.activity("读取课程列表"):
            client = api_class(token=token, username=username, password=password)
            if not client.access_token:
                raise PlatformUnavailable("未获取有效的平台登录状态，请重新登录或稍后重试")
            rows = self.read_courses(client)
        self.remember(lambda: self.store.save_session(platform, client))
        self.view.set_user(getattr(client, "user_info", None))
        return client, self.view.courses(rows) if choose else rows

    def cx_credentials(self):
        while True:
            username = self.view.ask("超星手机号 / 账号").strip()
            if not username:
                self.view.note("账号不能为空。", "warning")
                continue
            password = self.view.ask("超星密码", secret=True)
            if not password:
                self.view.note("密码不能为空。", "warning")
                continue
            return username, password

    def cx_login(self):
        """学习通 App 扫码优先；扫码不方便（没有浏览器、需要二次验证）时用账号密码。"""
        from ui import Cancelled
        while True:
            method = self.view.choice("超星登录", [
                (1, "学习通 App 扫码", "手机学习通扫描浏览器里的二维码"),
                (2, "账号密码登录", "扫码不方便或需要二次验证时使用"),
            ], default=1, help_text="Esc 返回 · q 退出")
            client = None
            if method == 1:
                client = ChaoxingApi()
                try:
                    with self.view.activity("等待学习通扫码"):
                        ok = cx_qr_login(client, on_status=lambda message: self.view.note(message))
                    if ok:
                        with self.view.activity("读取课程列表"):
                            rows = client.course_list()
                        self.remember(lambda: self.store.save_session("cx", client))
                        self.view.note("超星扫码登录成功。", "success")
                        return client, rows
                    self.view.note("扫码未完成或已超时。", "warning")
                except Exception as exc:
                    logger.exception("超星扫码登录异常")
                    self.view.note(f"扫码登录失败：{exc}", "error")
                if client is not None:
                    client.close()
                if not self.view.confirm("改用账号密码登录", default=True):
                    raise Cancelled()
            username, password = self.cx_credentials()
            with self.view.activity("登录超星"):
                client = ChaoxingApi(username, password)
                rows = client.course_list()
            self.remember(lambda: self.store.save_session("cx", client))
            return client, rows

    def cx_client_and_courses(self, choose=True):
        """超星登录一次后只保存平台返回的 Cookie；和网页版共用一份 session。"""
        self.view.set_user()
        saved = self.store.session("cx")
        if saved:
            try:
                with self.view.activity("恢复已保存的超星登录"):
                    client = ChaoxingApi(cookies=saved["cookies"])
                    rows = client.course_list()
                self.view.note("已复用本机超星登录。")
                return client, self.view.courses(rows) if choose else rows
            except AuthenticationExpired:
                self.store.clear_session("cx")
                self.view.note("保存的超星登录已过期，请重新扫码或输入账号密码。", "warning")
        client, rows = self.cx_login()
        return client, self.view.courses(rows) if choose else rows

    @staticmethod
    def read_courses(client):
        result = client.my_course_list(page_size=9999, strict=True)
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            raise PlatformUnavailable("课程列表读取失败，登录缓存未清除，请稍后重试")
        return result["rows"]

    def ai_configuration(self):
        config = self.store.ai_config()
        if config is not None:
            self.view.note(f"AI：{config.provider_name or '自定义供应商'} · {config.model} · {self.ai_status_text(config)}")
            return config
        config = self.manage_models()
        if config is None:
            from ui import Back
            raise Back()
        return config

    def manage_models(self):
        """One picker from settings and summaries; only saved configs can activate."""
        from ui import Back
        from ai.defaults import DEFAULT_PROVIDER_NAME
        cursor_id, message = None, ""
        while True:
            profiles = self.store.ai_profiles()
            active_id = next((profile.id for profile in profiles if profile.active), None)
            locked_ids = {profile.id for profile in profiles if profile.builtin}
            rows = []
            for profile in profiles:
                health = self.store.ai_health(profile.config)
                status = {"ok": "✓ 上次通过", "error": "× 失败"}.get(health.get("status"), "? 未测试")
                name = DEFAULT_PROVIDER_NAME if profile.builtin else profile.config.provider_name or "自定义供应商"
                rows.append((profile.id, name, profile.config.model, status))
            try:
                choice = self.view.model_picker(rows, active_id, cursor_id=cursor_id,
                                                message=message, locked_ids=locked_ids,
                                                test_connection=self.test_profile_connection)
                if choice.action == "back":
                    return self.store.ai_config()
                cursor_id = choice.profile_id
                if choice.action == "select":
                    self.store.activate_ai_profile(choice.profile_id)
                    return self.store.ai_config()
                if choice.profile_id in locked_ids:
                    raise SettingsError("默认供应商不可修改或删除")
                current = next((p.config for p in profiles if p.id == choice.profile_id), None)
                while True:
                    result = self.view.model_editor(current)
                    if result.action == "delete":
                        try:
                            if self.view.confirm("删除这份模型配置", default=False):
                                self.store.delete_ai_profile(choice.profile_id)
                                cursor_id, message = None, "已删除"
                        except Back:
                            continue
                    elif result.action == "save":
                        cursor_id = self.store.save_ai_profile(result.config, choice.profile_id)
                        message = self.test_ai_connection(result.config)
                    break
                if message:
                    self.view.note(message)
            except Back:
                continue
            except SettingsError as exc:
                message = str(exc)
                self.view.note(message, "error")

    def ai_status_text(self, config=None):
        try:
            config = config or self.store.ai_config()
            if config is None:
                return "未配置"
            health = self.store.ai_health(config)
        except SettingsError:
            return "配置无效，请重新配置"
        if not isinstance(health, dict) or not health:
            return "已配置 · 未测试"
        if health.get("status") == "ok":
            return "上次连接通过 · " + str(health.get("checked_at", "")).replace("T", " ")
        return "连接失败 · " + str(health.get("message", "请重新测试"))

    def test_profile_connection(self, profile_id):
        """Backend-only test for the picker; no rendering or profile activation."""
        from dataclasses import replace
        from MoocMain.log import redact
        try:
            config = next((p.config for p in self.store.ai_profiles() if p.id == profile_id), None)
            if config is None:
                return '连接失败：配置已不存在，请重新打开列表'
            previous = replace(config)
            try:
                AIClient(config).check_connection()
            except AIError as exc:
                reason = ' '.join(str(redact(str(exc))).split()).replace(config.api_key, '***')
                self.store.save_ai_health(config, 'error', reason)
                return '连接失败：' + reason
            self.store.save_ai_health(config, 'ok', previous_config=previous)
            return '连接通过'
        except SettingsError:
            return '测试结果无法保存，请检查配置文件权限后重试'
        except Exception:
            return '连接测试失败，请检查配置后重试'

    def verify_ai(self, config, client=None):
        from dataclasses import replace
        previous_config = replace(config)
        owns_client = client is None
        client = client or AIClient(config)
        try:
            with self.view.activity("测试 AI 连接"):
                client.check_connection()
            self.remember(lambda: self.store.save_ai_health(config, "ok", previous_config=previous_config))
            return client
        except BaseException as exc:
            if owns_client:
                client.session.close()
            if isinstance(exc, AIError):
                self.remember(lambda: self.store.save_ai_health(config, "error", str(exc)))
            raise

    def test_ai_connection(self, config=None):
        try:
            config = config or self.store.ai_config()
            if config is None:
                self.view.note("请先配置 AI 服务。", "warning")
                return "未配置"
            self.verify_ai(config)
        except (AIError, SettingsError) as exc:
            self.view.note(str(exc), "error")
        return self.ai_status_text(config)

    def study(self, version, client=None, selected=None, review_minutes=0):
        api_class, handler = {1: (ZYKMoocApi, ZYKMoocHandler),
                              2: (ZHSApi, ZHSHandler),
                              3: (ChaoxingApi, ChaoxingStudyHandler)}.get(version, (AIMoocApi, AIMoocHandler))
        if client is None:
            client, selected = (self.cx_client_and_courses() if version == 3
                                else self.client_and_courses(api_class))
        if not selected:
            self.view.cancelled = True
            return
        max_workers = self.store.concurrency()['study']
        inject = {}
        activity_ai = None
        captcha_ai = None
        if version == 0:
            # Only MOOC learning posts AI discussions and a 5-star evaluation; a
            # failure to obtain a healthy AI client aborts instead of silently
            # reporting a successful study run.
            activity_ai = self.verify_ai(self.ai_configuration())
            inject['activity_ai'] = activity_ai
        elif version == 2:
            config = self.store.ai_config()
            if config is not None:
                captcha_ai = AIClient(config)
                inject['captcha_ai'] = captcha_ai
        try:
            self.view.plan("学习管理", selected)
            if version == 2 and review_minutes:
                note = "智慧树每日复习：共享课 1 倍速重看已学视频 %d 分钟。" % review_minutes
            elif version == 3:
                note = ("超星学习：最多 %d 个章节同时处理，章节内的视频、文档、阅读任务点并行完成；"
                        "视频先尝试直接完成，不通过再按真实时间累计。" % max_workers)
            elif version == 2:
                note = ("智慧树学习：逐个视频实时播放（约 1.5 倍速），同一账号不能同时观看多个视频；"
                        "遇到验证码或当日学习时长上限会停止。")
            else:
                note = f"自动学习：最多 {max_workers} 项同时处理，完成即更新进度"
            if inject:
                note += "；并按主题自动参与课程讨论、提交五星课程评价，无需逐条确认"
            self.view.note(note)
            with self.view.activity("处理课程"):
                if review_minutes:
                    inject['review_minutes'] = review_minutes
                handler(client=client, course_rows=selected, on_progress=self.view.on_progress,
                        max_workers=max_workers, **inject)
        finally:
            if activity_ai is not None:
                activity_ai.session.close()
            if captcha_ai is not None:
                captcha_ai.session.close()

    def export(self):
        client, selected = self.client_and_courses(AIMoocApi)
        if not selected:
            self.view.cancelled = True
            return
        options = self.view.export_settings(self.store.get("export", {}))
        self.remember(lambda: self.store.set("export", options))
        selected_ids = []
        if options["quiz_mode"] == "single" and 3 in options["category_ids"]:
            with self.view.activity("读取章节测验"):
                catalog = collect_quiz_catalog(client, selected, category_id=3)
            quizzes = self.view.quizzes(catalog)
            selected_ids = [item["exam_id"] for item in quizzes]
            if not selected_ids and options["category_ids"] == [3]:
                self.view.cancelled = True
                return
        self.view.plan("题目导出", selected, options)
        with self.view.activity("导出题目"):
            return QuizExportHandler(client=client, course_rows=selected,
                                     selected_exam_ids=selected_ids, output_dir="tiku",
                                     dev_mode=self.view.dev, on_progress=self.view.on_progress, **options)

    def ai(self, version=0, login=None):
        if version != 0:
            self.view.note("AI 答题支持常规 MOOC / AI 优课。", "warning")
            return []
        client, courses = self.client_and_courses(AIMoocApi, login)
        if not courses:
            self.view.cancelled = True
            return []
        api = ExamAPI(client)
        categories = self.view.categories()
        if not categories:
            self.view.cancelled = True
            return []
        catalog = []
        with self.view.activity("读取可选任务"):
            for course in courses:
                for category in categories:
                    for item in api.catalog(course, category):
                        catalog.append((course, item, category))
        selected = self.view.papers(catalog)
        if not selected:
            self.view.cancelled = True
            return []
        config = self.ai_configuration()
        auto_submit = self.view.answer_settings()
        self.view.ai_plan(selected, auto_submit)
        return self.execute_ai(client, selected, config, auto_submit, api=api)

    def task_catalog(self, client, courses):
        api = ExamAPI(client)

        def status(exam_id):
            try:
                return api.completion_status(exam_id)
            except ExamError:
                return "unknown"

        # Read-only GETs, so they run side by side; order still follows courses/categories.
        with self.view.activity("读取课程任务"), ThreadPoolExecutor(INTERACTIVE_WORKERS) as pool:
            pairs = [(course, category) for course in courses for category in (1, 2, 3)]
            listed = [(course, item, category) for (course, category), items
                      in zip(pairs, pool.map(lambda pair: api.catalog(*pair), pairs)) for item in items]
            open_ids = list(dict.fromkeys(paper_exam_id(item) for _, item, category in listed
                                          if task_availability(item, category).available))
            statuses = dict(zip(open_ids, pool.map(status, open_ids)))
        return [(course, dict(item, _completion_status=completion_with_score(item, statuses[paper_exam_id(item)])
                              if task_availability(item, category).available else "unsupported"), category)
                for course, item, category in listed]

    def execute_ai(self, client, selected, config, auto_submit, api=None):
        max_workers = self.store.concurrency()['ai']
        # Verify with synthetic material before paper() can start an attempt.
        runner = None
        results = []
        with self.view.activity("AI 答题"):
            self.progress.begin("AI 答题", len(selected), "份试卷")
            for course, item, category in selected:
                name = paper_display_name(item)
                availability = task_availability(item, category)
                if not availability.available:
                    message = availability.reason
                    logger.warning("%s：%s", name, message)
                    results.append({"paper": name, "status": "skipped", "message": message})
                    self.progress.result(name, "skipped")
                    continue
                try:
                    if runner is None:
                        ai_client = self.verify_ai(config)
                        api = api or ExamAPI(client)
                        runner = AnswerRunner(api, ai_client, auto_submit=auto_submit,
                                              on_progress=self.view.on_progress, max_workers=max_workers,
                                              manual_answer=getattr(self.view, "manual_answer", None),
                                              confirm_submit=getattr(self.view, "confirm_submit", None),
                                              memory=MoocAnswerMemory(Path(self.store.path).parent / "mooc-answers.json"))
                    result = runner.run_paper(course, item, category)
                    results.append(result)
                except TaskUnavailableError as exc:
                    results.append({"paper": name, "status": "skipped", "message": str(exc)})
                    logger.warning("%s：%s", name, exc)
                    self.progress.result(name, "skipped")
                    continue
                except ExamError as exc:
                    result = {"paper": name, "status": "error", "message": str(exc),
                              "stop_queue": isinstance(exc, ExamServiceError)}
                    results.append(result)
                    logger.error("%s：%s", name, exc)
                status = result["status"]
                if status == "ai_error":
                    self.remember(lambda: self.store.save_ai_health(config, "error", result.get("message", "AI 服务失败")))
                self.progress.result(name, status)
                # A human pause ends the run locally: the queued papers are not
                # started and the draft stays resumable on the next selection.
                if status == "paused":
                    logger.warning("已暂停补答：%s；其余试卷未执行，可稍后继续。", name)
                    break
                if result.get("stop_queue") or status in ("submission_unknown", "submitting", "ai_error"):
                    # The message states why this run ended; do not guess a
                    # service failure when a human stopped a submission.
                    logger.warning("队列停止：%s；其余试卷未执行。", result.get("message") or "本次处理已停止")
                    break
        return results

    def cx_task_catalog(self, client, courses):
        """超星章节里的作业，和智慧树用同一种 (course, item, category) 结构。"""
        catalog = []
        with self.view.activity("读取章节作业"):
            for course in courses:
                catalog.extend(cx_catalog(client, course))
        return catalog

    def execute_cx_ai(self, client, selected, config, auto_submit):
        results = []
        ai_client = None
        with self.view.activity("AI 答题"):
            self.progress.begin("AI 答题", len(selected), "份作业")
            try:
                for course, item, category in selected:
                    name = item.get("name") or "章节作业"
                    if ai_client is None:
                        ai_client = self.verify_ai(config)
                        runner = ChaoxingExerciseRunner(client, ai_client, auto_submit=auto_submit,
                                                        on_progress=self.view.on_progress)
                    try:
                        result = runner.run(course, item["_point"], item, item["_job_info"])
                    except WorkUnavailable as exc:
                        result = {"paper": name, "status": "skipped", "message": str(exc)}
                    except AIServiceError as exc:
                        result = {"paper": name, "status": "ai_error", "message": str(exc)}
                    results.append(result)
                    status = result["status"]
                    if status == "ai_error":
                        self.remember(lambda: self.store.save_ai_health(
                            config, "error", result.get("message", "AI 服务失败")))
                    self.progress.result(name, status)
                    if status in ("ai_error", "submission_unknown"):
                        logger.warning("队列停止：%s；其余作业未执行。",
                                       result.get("message") or "本次处理已停止")
                        break
            finally:
                if ai_client is not None:
                    ai_client.session.close()
        return results

    def zhs_task_catalog(self, client, courses):
        catalog = []
        with self.view.activity("读取课程任务"):
            for course in courses:
                catalog.extend(ZHSMain.answer.catalog(client, course))
        return catalog

    def execute_zhs_ai(self, client, selected, config, auto_submit):
        results = []
        ai_client = None
        with self.view.activity("AI 答题"):
            self.progress.begin("AI 答题", len(selected), "份试卷")
            try:
                for course, item, category in selected:
                    name = paper_display_name(item)
                    if ai_client is None:
                        ai_client = self.verify_ai(config)
                        runner = ZHSAnswerRunner(
                            client, ai_client, auto_submit=auto_submit,
                            on_progress=self.view.on_progress,
                            max_workers=self.store.concurrency()["ai"],
                            memory=AnswerMemory(
                                Path(self.store.path).parent / "zhs-answers.json"))
                    try:
                        result = runner.run_paper(course, item, category)
                        results.append(result)
                    except (CaptchaRequired, StudyTimeLimit) as exc:
                        results.append({"paper": name, "status": "error", "message": str(exc)})
                        self.progress.result(name, "error")
                        logger.warning("队列停止：%s；其余试卷未执行。", exc)
                        break
                    status = result["status"]
                    if status == "ai_error":
                        self.remember(lambda: self.store.save_ai_health(
                            config, "error", result.get("message", "AI 服务失败")))
                    self.progress.result(name, status)
                    if status in ("ai_error", "submission_unknown"):
                        logger.warning("队列停止：%s；其余试卷未执行。",
                                       result.get("message") or "本次处理已停止")
                        break
            finally:
                if ai_client is not None:
                    ai_client.session.close()
        return results

    def execute_zhs_mastery(self, client, courses, config):
        ai = self.verify_ai(config)
        results = []
        try:
            with self.view.activity("掌握度学习"):
                runner = ZHSAnswerRunner(client, ai, auto_submit=True, on_progress=self.view.on_progress,
                                        max_workers=self.store.concurrency()["ai"],
                                        memory=AnswerMemory(Path(self.store.path).parent / "zhs-answers.json"))
                for course in courses:
                    result = runner.run_mastery(course)
                    results.append(result)
                    self.progress.result(course.get("courseName"), result["status"])
                    logger.info("%s：%s", course.get("courseName"), result["message"])
                    if result["status"] == "ai_error":
                        self.remember(lambda: self.store.save_ai_health(config, "error", result["message"]))
                    if result.get("stop_queue"):
                        break
        finally:
            ai.session.close()
        return results

    def _zhs_qa_courses(self, client, courses, run, label):
        """按课程打开问答区执行 run(qa, event)，返回 {ok, failed, skipped} 计数。

        run 上报 status=success/error，跳过由上层的业务返回值决定时也算在这里。
        验证码和登录问题会停止其余课程：同账号继续只会继续失败。
        """
        from ZHSMain.qa import QaBridge
        from ZHSMain.api import ZHSApiError

        totals = {"ok": 0, "failed": 0, "skipped": 0}
        counted = {"success": "ok", "error": "failed", "skipped": "skipped"}
        for row in courses:
            course = (row or {}).get("zhs") or {}
            course_id = course.get("courseId")
            # 共享课带 recruitId；智慧课程只有 classId，问答区两者取其一。
            recruit_id = course.get("recruitId") or course.get("classId")
            name = row.get("courseName") or "当前课程"
            if not course_id or not recruit_id:
                self.view.log(f"{name}：没有可作答的问答区，已跳过。", "warning")
                logger.warning("%s：没有可作答的问答区，已跳过", name)
                continue
            before = dict(totals)

            def event(kind, title, status="", _name=name):
                if kind != "result":
                    return
                if status in counted:
                    totals[counted[status]] += 1
                self.progress.result(f"{_name} · {title}", status)

            try:
                with QaBridge(client.access_token, course_id=course_id,
                              recruit_id=recruit_id) as qa:
                    run(qa, event)
            except (CaptchaRequired, AuthenticationExpired, PlatformUnavailable,
                    ZHSApiError) as exc:
                # 界面日志随动态区域消失，落一份到文件，事后才查得到原因。
                self.view.log(f"{name}：{exc}；其余课程未执行。", "warning")
                logger.warning("%s：%s；其余课程未执行", name, exc)
                break
            self.view.log(f"{name}：{label} {totals['ok'] - before['ok']} 条，"
                          f"失败 {totals['failed'] - before['failed']} 条。")
            logger.info("%s：%s %d 条，失败 %d 条", name, label,
                        totals["ok"] - before["ok"], totals["failed"] - before["failed"])
        return totals

    def execute_zhs_qa(self, client, courses, config, options):
        """问答区答题：取题 → AI 写回答 → 发布。一条失败不打断队列。"""
        from ZHSMain.qa import answer_questions

        limit = int(options.get("limit") or 10)
        state_path = Path(self.store.path).parent / "zhs-qa-answers.json"
        with self.view.activity("问答区答题"):
            self.progress.begin("问答区答题", limit * len(courses), "条")
            ai = self.verify_ai(config)
            try:
                def run(qa, event):
                    answer_questions(qa, ai, max_answers=5, limit=limit, state_path=state_path,
                                     on_event=event, sleep=_qa_pause,
                                     voice=zlib.crc32(str(getattr(client, "uuid", None) or "local").encode()))  # 同一个账号一个说话风格

                totals = self._zhs_qa_courses(client, courses, run, "发布")
            finally:
                ai.session.close()
        return {"answered": totals["ok"], "failed": totals["failed"],
                "skipped": totals["skipped"]}

    def generate_zhs_questions(self, client, courses, config, options):
        """让 AI 围绕课程里的小节写新提问；读一次问答区列表，只为避开已有的提问。"""
        from dataclasses import replace

        from ai.client import AIClient
        from ZHSMain.qa import REFERENCE_ANSWERS, QaBridge, write_questions

        count = int(options.get("count") or 1)
        row = (courses or [{}])[0] or {}
        course = row.get("zhs") or {}
        course_id = course.get("courseId")
        recruit_id = course.get("recruitId") or course.get("classId")
        name = row.get("courseName") or "这门课"
        # 推理型模型出题常要一两分钟；默认 120 秒会把正常的一次生成判成超时。
        # 接口形态已知时不再额外做连接测试——那是又一次慢请求。
        ai = AIClient(replace(config, timeout=max(int(config.timeout or 0), 300)))
        try:
            if config.api_format == "auto":
                self.verify_ai(config, client=ai)
            reference = []
            if course_id and recruit_id:
                with QaBridge(client.access_token, course_id=course_id,
                              recruit_id=recruit_id) as qa:
                    reference = [item["content"] for item in
                                 qa.questions(max_answers=REFERENCE_ANSWERS, limit=30)]
                self.view.log(f"参考 {len(reference)} 条回答较少的提问出题。" if reference
                              else "问答区还没有回答较少的提问，按课程主题出题。")
            else:
                self.view.log(f"{name}：没有可读取的问答区，按课程主题出题。", "warning")
            try:  # 每条提问围绕课程里的一节：贴着课程内容，审核才不当成无关水帖
                from ZHSMain.main import course_units
                topics = [unit["name"] for chapter in course_units(client, row) for unit in chapter.get("units", [])]
            except Exception:
                topics = []
            return write_questions(
                ai, reference, count, course_name=name, topics=topics,
                on_progress=lambda index, total: self.view.log(f"正在生成第 {index}/{total} 条提问…"))
        finally:
            ai.session.close()

    def execute_zhs_ask(self, client, courses, texts):
        """问答区提问：把准备好的正文逐条发布。"""
        from ZHSMain.qa import publish_questions

        with self.view.activity("问答区提问"):
            self.progress.begin("问答区提问", len(texts) * len(courses), "条")

            def run(qa, event):
                publish_questions(qa, texts, on_event=event, sleep=_ask_pause)

            totals = self._zhs_qa_courses(client, courses, run, "发布提问")
        return {"published": totals["ok"], "failed": totals["failed"]}

    def run(self):
        from course_flow import CourseWorkspace
        self.remember(self.store.ensure_default_model)
        return CourseWorkspace(self).run()

    def manage_concurrency(self):
        draft, message = self.store.concurrency(), ''
        while True:
            edited = self.view.concurrency_editor(draft, message=message)
            if edited is None:
                return False
            draft = edited
            try:
                self.store.save_concurrency(draft['ai'], draft['study'])
            except SettingsError as exc:
                message = str(exc)
                continue
            return True

    def manage_settings(self, current_platform="mooc"):
        from ui import Back
        message = ""
        while True:
            try:
                action = self.view.settings_menu(self.store.path, self.ai_status_text(), message=message,
                                                 concurrency=self.store.concurrency())
                message = ""
            except Back:
                return False
            if action == 0:
                return False
            if action == 1:
                try:
                    self.manage_models()
                except SettingsError as exc:
                    self.view.note(str(exc), "error")
            elif action == 2:
                if self.manage_concurrency():
                    message = '并发设置已保存，下次运行生效。'
            elif action in (3, 4, 5, 6):
                platform = {3: "mooc", 4: "zyk", 5: "zhs", 6: "cx"}[action]
                if self.remember(lambda: self.store.clear_session(platform)):
                    if platform in ("mooc", "zyk"):
                        self.browser_logout_pending = True
                    label = {"mooc": "MOOC", "zyk": "资源库", "zhs": "智慧树", "cx": "超星"}[platform]
                    if platform == current_platform:
                        self.view.set_user()
                        if platform == "cx":
                            self.view.note("已退出超星登录，下次使用超星时重新扫码或输入账号密码。", "success")
                        else:
                            self.view.note(f"已退出{label}登录，正在打开浏览器重新登录。", "success")
                        return True
                    message = f"已退出{label}登录。"
                else:
                    message = "退出失败，登录状态未更改，请重试。"


def _qa_pause(_seconds):
    """问答区两条之间随机停一会儿（和网页一样），不用 answer_questions 给的固定间隔。"""
    from ZHSMain.qa import QA_PACE
    time.sleep(random.uniform(*QA_PACE))


def _ask_pause(_seconds):
    """两条提问之间停得更久（ZHSMain.qa.ASK_PACE）：短时间连发提问会被当成刷帖。"""
    from ZHSMain.qa import ASK_PACE
    time.sleep(random.uniform(*ASK_PACE))

"""智慧树自动学习流程。"""
import base64
import random
import threading
import time
from datetime import timedelta

from base.auth import AuthenticationExpired, PlatformUnavailable
from MoocMain.log import Logger
from reporting import Reporter
from ZHSMain.api import (
    CaptchaRequired,
    StudyTimeLimit,
    ZHSApiError,
    ZHSError,
    WatchPoints,
    encode_ev,
)


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _nonneg_int(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


class ZHSHandler:
    def __init__(self, *, client, course_rows, on_progress=None, max_workers=1,
                 cancel_event=None, sleep=time.sleep, rng=None):
        self.client = client
        self.course_rows = course_rows or []
        self.progress = Reporter(on_progress)
        # 仅为与其他平台签名一致：智慧树同一账号同时看多个视频会被判定为多窗口，
        # 因此这里始终逐门课、逐个资源串行执行，max_workers 不参与调度。
        self.max_workers = max_workers
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.log = Logger(__name__).get_log()
        self.start_courses()

    def start_courses(self):
        course_rows = self.course_rows
        if not course_rows:
            self.log.warning("当前没有课程数据")
            return
        self.progress.begin("智慧树学习", len(course_rows), "门课程")
        for row in course_rows:
            if self.cancel_event.is_set():
                break
            name = row.get("courseName") or "未知课程"
            self.progress.detail(name)
            try:
                kind = (row.get("zhs") or {}).get("kind")
                if kind == "shared":
                    status = self.study_shared(row)
                elif kind == "ai":
                    status = self.study_ai(row)
                else:
                    status = "skipped"
            except (CaptchaRequired, StudyTimeLimit) as error:
                self.log.warning("%s：%s；后续课程未执行", name, error)
                self.progress.result(name, "error")
                break
            except (AuthenticationExpired, KeyboardInterrupt):
                raise
            except Exception as error:
                self.log.error("%s：学习失败：%s", name, error)
                self.progress.result(name, "error")
                continue
            self.progress.result(name, status)

    # ---------------------------------------------------------------- 共享课

    def study_shared(self, row):
        course = row.get("courseName") or "未知课程"
        z = row["zhs"]
        secret = z["secret"]
        self.client.shared_gologin(secret)
        info = self.client.shared_course(secret) or {}
        recruit_id = info["recruitId"]
        report_course_id = (info.get("courseInfo") or {}).get("courseId")
        data = self.client.shared_videos(secret) or {}
        cc_course_id = data["courseId"]
        videos = self._shared_videos(data)
        if not videos:
            self.log.warning("%s：没有可学习的视频", course)
            return "skipped"

        lesson_ids = [video["lessonId"] for video in videos]
        video_ids = [video["id"] for video in videos if video["id"]]
        states = self.client.shared_study_info(lesson_ids, video_ids, recruit_id) or {}
        self.client.shared_last_video(recruit_id)

        for video in videos:
            watch, played = self._shared_state(states, video)
            video["watchState"] = watch
            video["studyTotalTime"] = played

        ctx = {
            "course": course,
            "recruit_id": recruit_id,
            "cc_course_id": cc_course_id,
            "report_course_id": report_course_id,
        }
        total = len(videos)
        success = skipped = failed = 0
        self.progress.resources(f"{course} · 读取资源 · 成功 0 · 跳过 0 · 失败 0", 0, total)
        for video in videos:
            if self.cancel_event.is_set():
                raise KeyboardInterrupt
            name = video["name"]
            if video["watchState"] == 1:
                skipped += 1
            else:
                try:
                    self.play_shared(ctx, video)
                    success += 1
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                    raise
                except Exception as error:
                    failed += 1
                    self.log.error("%s · %s：学习失败：%s", course, name, error)
            completed = success + skipped + failed
            self.progress.resources(
                f"{course} · {name} · 成功 {success} · 跳过 {skipped} · 失败 {failed}",
                completed, total,
            )

        states = self.client.shared_study_info(lesson_ids, video_ids, recruit_id) or {}
        ready = sum(1 for video in videos if self._shared_state(states, video)[0] == 1)
        self.log.info("%s：服务端确认 %d/%d 个视频已完成", course, ready, total)
        return "success" if ready == total and not failed else "error"

    @staticmethod
    def _shared_videos(data):
        videos = []
        for chapter in data.get("videoChapterDtos") or []:
            for lesson in chapter.get("videoLessons") or []:
                smalls = lesson.get("videoSmallLessons") or []
                if not smalls and lesson.get("videoId"):
                    videos.append({
                        "videoId": lesson.get("videoId"),
                        "id": 0,
                        "name": lesson.get("name") or "未命名视频",
                        "lessonId": lesson.get("id"),
                        "chapterId": chapter.get("id"),
                        "videoSec": lesson.get("videoSec"),
                    })
                    continue
                for small in smalls:
                    videos.append({
                        "videoId": small.get("videoId"),
                        "id": small.get("id") or 0,
                        "name": small.get("name") or lesson.get("name") or "未命名视频",
                        "lessonId": small.get("lessonId") or lesson.get("id"),
                        "chapterId": chapter.get("id"),
                        "videoSec": small.get("videoSec"),
                    })
        return videos

    @staticmethod
    def _shared_state(states, video):
        if video["id"] > 0:
            item = (states.get("lv") or {}).get(str(video["id"])) or {}
        else:
            item = (states.get("lesson") or {}).get(str(video["lessonId"])) or {}
        watch = item.get("watchState")
        if watch not in (0, 1):
            watch = None
        return watch, _nonneg_int(item.get("studyTotalTime"))

    def play_shared(self, ctx, video):
        course = ctx["course"]
        recruit_id = ctx["recruit_id"]
        cc_course_id = ctx["cc_course_id"]
        lesson_id = video["lessonId"]
        video_id = video["id"]
        play_id = video["videoId"]
        video_sec = _to_int(video.get("videoSec"))
        end = max(video_sec, 1)

        def prelearning():
            data = self.client.shared_prelearning({
                "ccCourseId": cc_course_id,
                "chapterId": video["chapterId"],
                "isApply": 1,
                "lessonId": lesson_id,
                "lessonVideoId": video_id,
                "recruitId": recruit_id,
                "videoId": play_id,
            }) or {}
            dto = data.get("studiedLessonDto") or {}
            token = base64.b64encode(str(dto.get("id", 0)).encode("ascii")).decode("ascii")
            return token, _nonneg_int(dto.get("studyTotalTime"))

        token, server_time = prelearning()
        state = {
            "played": max(_nonneg_int(video.get("studyTotalTime")), server_time),
            "token": token,
            "last_submit": 0,
        }
        state["last_submit"] = state["played"]
        wp = WatchPoints(int(state["played"]))

        data = self.client.shared_question_points({
            "lessonId": lesson_id,
            "lessonVideoId": video_id,
            "recruitId": recruit_id,
            "courseId": cc_course_id,
        }) or {}
        points = [
            point for point in (data.get("questionPoint") or [])
            if _to_int(point.get("timeSec")) > state["played"]
        ]
        points.sort(key=lambda point: _to_int(point.get("timeSec")))
        if points:
            end = max(end, _to_int(points[-1].get("timeSec")))

        def build_payload(played, last_submit, token_value):
            wp.add(played)
            ev = encode_ev([
                recruit_id, lesson_id, video_id, play_id, video["chapterId"],
                "0", int(played - last_submit), int(played),
                str(timedelta(seconds=min(video_sec, int(played)))),
                (self.client.uuid or "") + "zhs",
            ])
            return {"ewssw": wp.text(), "sdsew": ev, "zwsds": token_value}

        def send(payload):
            self.client.shared_save_progress(dict(payload, courseId=ctx["report_course_id"]))

        def report():
            payload = build_payload(state["played"], state["last_submit"], state["token"])
            self.client.shared_save_cache(payload)
            sent_payload = payload
            try:
                send(payload)
            except (CaptchaRequired, AuthenticationExpired):
                raise
            except PlatformUnavailable as error:
                # A network hiccup is retried on the next tick with the unsent delta.
                self.log.warning("%s · %s：进度上报失败：%s", course, video["name"], error)
                return False
            except ZHSApiError as error:
                if error.code == -9:
                    raise StudyTimeLimit("学习时长已达平台上限") from error
                if error.code == -10:
                    self.sleep(3)
                    token, _ = prelearning()
                    state["token"] = token
                    sent_payload = dict(payload, zwsds=token)
                elif error.code == -8:
                    token, server_time = prelearning()
                    state["token"] = token
                    if server_time > state["played"]:
                        state["played"] = server_time
                        sent_payload = build_payload(
                            state["played"], state["last_submit"], token
                        )
                    else:
                        sent_payload = dict(payload, zwsds=token)
                else:
                    self.log.warning("%s · %s：进度上报失败：%s", course, video["name"], error)
                    return False
                try:
                    send(sent_payload)
                except (CaptchaRequired, AuthenticationExpired):
                    raise
                except PlatformUnavailable as retry:
                    self.log.warning("%s · %s：进度上报失败：%s", course, video["name"], retry)
                    return False
                except ZHSApiError as retry:
                    if retry.code == -9:
                        raise StudyTimeLimit("学习时长已达平台上限") from retry
                    self.log.warning("%s · %s：进度上报失败：%s", course, video["name"], retry)
                    return False
            state["last_submit"] = state["played"]
            wp.reset(int(state["played"]))
            return True

        # 预检：delta=0 的上报会提前暴露验证码和时长上限。
        report()
        if state["played"] >= end:
            return

        threading.Thread(
            target=self.client.open_video, kwargs={"video_id": play_id}, daemon=True
        ).start()
        self.client.shared_courseware(play_id)

        speed = 1.5 + self.rng.uniform(-0.15, 0.15)
        interval = 30 + self.rng.randint(-5, 5)
        elapsed = 0
        paused_until = 0
        force = False
        failures = 0
        while True:
            self.sleep(1)
            if self.cancel_event.is_set():
                raise KeyboardInterrupt
            elapsed += 1
            if elapsed % 5 == 0:
                self.progress.detail(
                    f"{course} · {video['name']} · "
                    f"{self._mmss(state['played'])}/{self._mmss(end)}"
                )
            if elapsed <= paused_until:
                continue
            if self.rng.random() < 0.0025:
                paused_until = elapsed + 60
                continue
            state["played"] = min(state["played"] + speed, end)
            if elapsed % 2 == 0:
                wp.add(state["played"])
            while points and state["played"] >= _to_int(points[0].get("timeSec")):
                self._answer_popup(ctx, video, points.pop(0))
                paused_until = elapsed + 2
                force = True
            if elapsed % interval == 0 or state["played"] >= end or force:
                if report():
                    failures = 0
                    force = False
                    speed = 1.5 + self.rng.uniform(-0.15, 0.15)
                    interval = 30 + self.rng.randint(-5, 5)
                else:
                    failures += 1
                    if failures >= 5:
                        raise ZHSError("连续 5 次进度上报失败，停止学习")
            if state["played"] >= end and state["last_submit"] >= end and not force:
                break
        self.sleep(1 + self.rng.random())

    def _answer_popup(self, ctx, video, point):
        try:
            data = self.client.shared_popup_question({
                "lessonId": video["lessonId"],
                "lessonVideoId": video["id"],
                "questionIds": point.get("questionIds"),
            }) or {}
            items = data.get("lessonTestQuestionUseInterfaceDtos") or []
            test = (items[0] or {}).get("testQuestion") or {} if items else {}
            answer = ",".join(
                str(option.get("id"))
                for option in test.get("questionOptions") or []
                if str(option.get("result")) == "1"
            )
            self.client.shared_save_popup({
                "courseId": ctx["cc_course_id"],
                "recruitId": ctx["recruit_id"],
                "testQuestionId": test.get("questionId", test.get("id")),
                "isCurrent": "1",
                "lessonId": video["lessonId"],
                "lessonVideoId": video["id"],
                "answer": answer,
                "testType": 0,
            })
        except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
            raise
        except Exception as error:
            self.log.warning("%s · 弹题处理失败：%s", ctx["course"], error)

    @staticmethod
    def _mmss(seconds):
        seconds = int(seconds)
        return "%02d:%02d" % (seconds // 60, seconds % 60)

    # ---------------------------------------------------------------- 智慧课程

    def study_ai(self, row):
        course = row.get("courseName") or "未知课程"
        z = row["zhs"]
        course_id = z["courseId"]
        class_id = z["classId"]
        basic = self.client.ai_course_basic(course_id, class_id) or {}
        knowledge_points = []
        for theme in basic.get("cakeThemeList") or []:
            knowledge_points.extend(theme.get("knowledgeList") or [])

        groups = []
        total = 0
        for point in knowledge_points:
            items = self.client.ai_resources(course_id, class_id, point["knowledgeId"]) or []
            groups.append((point, items))
            total += len(items)
        if total == 0:
            self.log.warning("%s：没有可学习的资源", course)
            return "skipped"

        self.progress.resources(f"{course} · 读取资源 · 成功 0 · 跳过 0 · 失败 0", 0, total)
        success = skipped = failed = 0
        for point, items in groups:
            for item in items:
                if self.cancel_event.is_set():
                    raise KeyboardInterrupt
                detail = item.get("resourcesDetail") or {}
                name = detail.get("resourcesName") or item.get("resourcesName") or "资源"
                self.progress.detail(f"{course} · {name}")
                if _to_int(item.get("studyStatus")) == 1:
                    skipped += 1
                else:
                    self.sleep(self.rng.uniform(0.2, 2))
                    try:
                        if self._is_video(detail, item):
                            self.play_ai(
                                course_id, class_id, point["knowledgeId"],
                                detail["resourcesFileId"], _to_int(item.get("schedule")),
                                course, name,
                            )
                        else:
                            self.client.ai_complete(
                                course_id, class_id, point["knowledgeId"],
                                detail["resourcesUid"],
                            )
                        success += 1
                    except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                        raise
                    except Exception as error:
                        failed += 1
                        self.log.error("%s · %s：学习失败：%s", course, name, error)
                completed = success + skipped + failed
                self.progress.resources(
                    f"{course} · {name} · 成功 {success} · 跳过 {skipped} · 失败 {failed}",
                    completed, total,
                )

        ready = 0
        all_total = 0
        for point, _ in groups:
            items = self.client.ai_resources(course_id, class_id, point["knowledgeId"]) or []
            all_total += len(items)
            ready += sum(1 for item in items if _to_int(item.get("studyStatus")) == 1)
        self.log.info("%s：服务端确认 %d/%d 项资源已完成", course, ready, all_total)
        return "success" if ready == all_total and not failed else "error"

    @staticmethod
    def _is_video(detail, item):
        pair = (
            _to_int(detail.get("resourcesType", item.get("resourcesType"))),
            _to_int(detail.get("resourcesDistributeType", item.get("resourcesDistributeType"))),
        )
        return pair in ((1, 3), (2, 2))

    def play_ai(self, course_id, class_id, knowledge_id, file_id, schedule,
                course="", name=""):
        length = self.client.ai_video_length(course_id, class_id, file_id)
        if length <= 0:
            raise ZHSError("无法获取视频时长")
        position = int(length * schedule / 100)
        threading.Thread(
            target=self.client.open_video, kwargs={"file_id": file_id}, daemon=True
        ).start()
        if position >= length:
            self.client.ai_report(course_id, class_id, file_id, knowledge_id, length)
            return
        while True:
            if self.cancel_event.is_set():
                raise KeyboardInterrupt
            position = min(int(round(position + 1.5 * 2)), length)
            attempts = 0
            while True:
                try:
                    self.client.ai_report(course_id, class_id, file_id, knowledge_id, position)
                    break
                except (CaptchaRequired, StudyTimeLimit, AuthenticationExpired, KeyboardInterrupt):
                    raise
                except Exception as error:
                    attempts += 1
                    if attempts >= 3:
                        raise ZHSError("智慧课程进度上报失败：%s" % error) from error
                    self.sleep(1)
            self.sleep(2)
            self.progress.detail(f"{course} · {name} · {position}/{length}")
            if position == length:
                break

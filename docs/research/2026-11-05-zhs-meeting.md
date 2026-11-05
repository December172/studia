# 智慧树见面课回放核对

2026-11-05 对学生端页面和官方回放脚本进行核对。代码入口为 `ZHSMain/meeting.py`，终端与网页共用同一处理器。

- 学生列表页：`https://stuonline.zhihuishu.com/stuonline/teachMeeting/stuListV2?recruitId=…`。先访问该页以建立 stuonline 会话，再 POST `/stuonline/json/teachMeeting/listTeachMeetingStu?examV2=ss`；字段为 `recruitId`、`page.pageNo`（从 0 开始）、`page.pageSize`，返回 `maps` 和 `mapCount`。
- `taskType=1/4` 的课提供直播/回放；`taskStatus=3` 表示已结束。回放地址来自 `replayCourseUrl`，视频标识来自 `videoId`，不根据课程编号猜回放地址。
- 官方回放页：[vod_room.html](https://lc.zhihuishu.com/live/vod_room.html)，逻辑见 [vod_room.js](https://lc.zhihuishu.com/live/scripts/video/vod_room/vod_room.js?v=1.6)。`checkLiveHomeAuth` 使用 JSONP，字段为 `liveCourseId`、`courseId`、`recruitId`、当前 Cookie 中的 `userId`。`signInStatus=2` 是服务端签到完成状态；`courseEndStatus=1` 表示仍在学习期限内，`openStatus=2` 或 `courseLeaveStatus=1` 允许通过线上观看签到。
- 播放器把每个视频的秒数分别向上取整到分钟，再拼成整节课的观看分钟编号。续看必须按这个规则找到首个缺失分钟，不能把各段视频的秒数简单相加。
- 播放页通过 `getWatchHistory` 读取历史，通过 `setWatchHistorySecret` 保存观看记录；页面还维护动态请求签名。实现直接使用官方播放器及其原有回调，按 1 倍速真实播放，不自行构造这些写入。每 30 秒独立回查签到状态，页面显示 100% 本身不作为完成依据。
- 旧回放页的 `Map.js` 覆盖全局 `Map`，会使 Playwright 的 `wait_for_function` 抛出 `this._engines.set is not a function`，还会让 `evaluate` 返回的对象丢失为 `None`。初始化以普通 `evaluate` 读取布尔值；播放元数据、状态和续播参数全部通过 JSON 文本跨越浏览器边界，不修改平台的 `Map`。新增真实 Chromium 离线回归覆盖开始播放、续播参数及状态读取；未安装 Chromium 的环境明确跳过该项。

验证：指定课程的列表有 4 节，3 节服务端已签到、1 节未开始；真实运行处理器确认跳过前三节、保留后一节待处理，没有新播放或进度写入。另对已签到的一节完成了官方回放页初始化和观看历史加载检查，没有启动播放。跨视频断点、服务端确认、取消、平台拒绝及播放停滞使用离线测试覆盖。由于当前可观看的三节均已签到，本次没有验证从未签到到签到完成的真实播放过程。

本功能只处理回放观看签到。官方[见面课说明](https://www.zhihuishu.com/supportService/page/stu/qaDetail/jianmianke.html?questionId=1606)将现场表现、随堂测验与考勤区分，现场签到、讨论和实践任务不能由回放观看替代。

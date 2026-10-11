# 新版考试接口核对记录

核对日期：2026-10-10。以下是静态阅读公开网页资源得到的协议记录，不代表真实账号端到端验证。

## 来源

- 上游 PR #198，提交 `a276abd0627f44bc433a4b8d8bc74dc1e44c09ac`，提供列表、试卷与历史记录接口及导出逻辑。
- [平台答题页面脚本](https://ai.icve.com.cn/js/chunk-01472d86.1791543916907.js)：`getData`、`submitData`、`updateExamTime_Met`。
- [平台题型组件](https://ai.icve.com.cn/js/chunk-32fe7516.1791543916907.js)：选项 `SortOrder` 和判断题 `1/0` 的含义。
- [平台请求拦截器](https://ai.icve.com.cn/app~970f9218.1791543916907.js)：`/course/exam/record` 请求体编码。
- [平台 API 模块](https://ai.icve.com.cn/js/app~06837ae4.1791543916907.js)：交卷 POST 方法。

网页构建更新后这些资源地址可能失效。没有把第三方压缩脚本或账号数据复制进仓库。

## 请求

基础地址：`https://ai.icve.com.cn/prod-api`，授权使用当前账号 Bearer token。

| 操作 | 方法与路径 |
| --- | --- |
| 列表 | GET `/course/exam/record/getExamListByStudent` |
| 当前账号历史记录 | GET `/course/exam/record/getStudentRecordByExamId` |
| 进入试卷 | GET `/course/exam/paper?id=...&groupId=0` |
| 上报真实作答时间 | POST `/course/exam/record/updateExamTime` |
| 提交整份试卷 | POST `/course/exam/record` |

进入试卷可能创建草稿并启动倒计时，不能视为无副作用查询。程序先查本账号作答历史，然后进入试卷；历史已交卷记录不阻止重新作答。只检查返回的当前作答 ID 是否仍为草稿，提交前也只复查本轮 ID，避免把旧记录误当成本轮已交卷。

2026-10-10 的实际读取确认：试卷接口可以直接返回含 `questions`、`taskExamRecord` 的对象，没有 `code/data` 外层。仅 GET 试卷接口接受这种经过结构校验的响应；显式错误码和提交接口仍必须严格校验，不能把缺少 `code` 一律当成功。

网页交卷载荷包含 `courseId`、`courseInfoId`、`examId`、`examName`、`device=1`、`categoryId`、`examTime`、`groupId=0`、`isLast=true`、`id=taskExamRecord.id`、`taskExamProblemRecordList`。

每条答案使用 `questionNo`、`paperId`、`answer`；子题另有 `paperSubId`，`questionNo` 是父子零基索引数组的 JSON 字符串。选择题还携带原始选项 JSON `optionSort`。单选答案是原始 `SortOrder`，多选用逗号连接；判断题正确为 `1`、错误为 `0`；填空为字符串数组的 JSON。

请求体不是普通 JSON：网页采用 `btoa(encodeURIComponent(JSON.stringify(payload)))`，Content-Type 为 `application/json;charset=UTF-8`。Python 实现必须匹配 encodeURIComponent 保留字符集。

没有假设 `isLast=false` 就是可靠的远端草稿接口。本项目“保存”指本地检查点与答案报告；自动交卷一次性发送完整答案。

## 结果和失败

HTTP 成功且业务 `code=200` 后，另查同一 task ID 的作答记录，只有 `status=2` 才标记 `submitted`。查询可短暂重试，交卷 POST 不自动重试。

每轮提交前写入 `submitting` 检查点；超时、异常、进程中断或未确认状态均阻止自动重发同一份交卷请求。已确认 `submitted` 的报告不再阻止用户再次选择任务：旧报告先归档到 `history/`，新一轮获取平台提供的草稿 ID，重新生成答案。文件路径按账号、课程和任务隔离。平台状态与本地锁共同防止重复运行；不保证跨电脑实例的并发互斥。

`AttemptClock` 在真实经过 10 秒后才上报这段时间。它不会按目标时长高速累加，也不尝试跳过服务器最低作答时间或其他提交限制。有限时试卷在本地已用尽剩余时间后停止写入。

## 尚未验证

- 用户自选服务的实际模型可用性与答案质量。
- 当前账号各题型的真实交卷结果和平台时间限制。
- 附件题、音频/图片/公式、匹配题、完形填空等非支持题型。
- 未公开或变化后的平台响应结构。未知结构会停止处理，不当作空列表或提交成功。

## 任务列表成绩与剩余次数

2026-10-10 补充核对官网公开列表脚本：

- `https://ai.icve.com.cn/js/chunk-0a9feddc.1791543916907.js`（考试）
- `https://ai.icve.com.cn/js/chunk-73d3dd02.1791543916907.js`（测验）
- `https://ai.icve.com.cn/js/chunk-c13dfc0a.1791543916907.js`（作业）

三者直接展示列表接口 `rows`：`score` 为官网当前成绩，`scoreSetType=1` 显示“取最高”，其他值显示“取最新”。助手直接显示 `score`，不从历史记录自行挑选分数；数值 0 是有效成绩，空值为未出分（列表 `status=1` 时为待批阅），字段缺失或格式异常为未知。

官网“剩余答题次数”直接显示 `answerCount/frequency`，`frequency=0` 显示“不限”。因此 `answerCount` 已是剩余次数，不能从 `frequency` 再减一次，也不能通过作答历史条数推断剩余量。次数为平台提供的额度，不代表当前开放时间、批阅状态等条件允许重答。这些字段只用于显示；重新答题仍由进入试卷接口判定是否可用。

2026-10-10 经授权读取真实作业目录，三份已提交作业的 `answerCount` 均为 `-1`；结合用户核实的无重答机会状态，作业剩余栏将该值显示为 `x`。缺失字段仍显示 `?`，不据此推断次数用完。同日一份真实测验交卷后，目录返回 `status=3`、`score=92.5`，剩余次数从 9999 减至 9998；历史记录返回 `status=2` 和相同成绩。目录状态与历史记录状态不可混用。

## 再次作答（2026-10-10）

公开作业、考试、测验列表脚本中，目录 `status=3` 的“再次答题”和 `status=0` 的“开始答题”均进入相同的 `preview-exam` 路由，答题页仍调用 GET `/course/exam/paper?id=...&groupId=0`。没有额外猜测“重置试卷”接口。

平台返回 `tjzt=1`、当前记录非草稿，或历史中同一个作答 ID 已结束时，显示“平台未提供新的可作答记录”，不再笼统显示“已跳过”。本地报告也保留已确认交卷的 ID，避免平台返回过期草稿时重复提交同一轮；中途读取失败不会清除这些 ID。以前其他作答 ID 的交卷记录不影响本轮。每次启动答题都重新由 AI 生成全部答案，不复用历史答案；仅本次运行内保留单题重试后的进度。协议核对和离线测试未实际消耗真实账号的答题次数。

# 自动课程讨论和评价 Implementation Plan

> 执行方式：用户指定 Codex CLI，deepseek-v4-1-flash、high；最多两轮修改，复杂部分和未解决问题由主代理接手。仅本地开发，不提交、不推送。

**Goal:** MOOC 自动学习覆盖课程活动主题讨论（AI 文本最多 20 字）和课程评价（固定五星，AI 评语）。
**Architecture:** 在现有 Application.study / AIMoocHandler 流程复用平台客户端、AIClient、进度事件。资源库不调用这些 MOOC 专属接口；平台请求、字段和完成状态须根据当前公开前端核实。
**Tech Stack:** Python >=3.9，requests，现有 unittest，无新增依赖。
**Spec:** 用户本次请求和补充“默认5星，评语都ai写”。

## Global Constraints

- 保留现有 README.md、.gitignore 的用户改动；只改与本任务有关内容，不重构。
- 不提交、不推送，不实际发送讨论、评价或学习写请求；全部写入验证使用离线替身。
- 讨论按主题生成，不超过 20 个字符；评价评语按课程由 AI 生成，非空且不超过平台 500 字上限。
- 复用现有 AI 配置和协议兼容，平台令牌不进入 AI 提示；外部课程/主题文本作为数据，不能执行其嵌入指令。
- 完成状态读取失败不得当作未完成；仅开放且未参与讨论可提交；已评价跳过；写入失败或结果未知不自动重发。

## Review Focus

- 目录分页、重复主题、已参与、未开始/截止边界不漏检或重复发帖。
- AI 非字符串、空白、HTML 或超长输出不能产生无效写入。
- 平台失败、鉴权、限流、取消、响应不完整不得计为成功。
- MOOC 已学完资源仍须处理讨论/评价；资源库和既有调用保持兼容。
- 配置、连接探测、进度事件与终端摘要必须反映实际自动提交内容。

### Task 1: 核实协议并实现自动活动

Files: AIMoocMain/api.py、AIMoocMain/main.py、app.py，按需一个活动模块；ai/client.py 仅在复用需要时最小修改；tests/test_course_activities.py。

- [x] 核实公开前端的讨论列表、详情、回复和评价读取/提交端点及字段，在计划末尾或开发说明记录来源。
- [x] 先写离线测试，断言实际生成的请求、20 字上限、五星 AI 评价、完成跳过、开放限制、失败无重发；运行并看到因功能缺失而失败。
- [x] 最小实现并接到自动学习，复用 AIClient.answer 的问答生成或其现有协议路径，避免引入第二套 HTTP/配置。
- [x] 运行新增测试和相关学习/流程测试，通过后执行全套 unittest。

### Task 2: 接入说明和验收

Files: ui.py 或现有摘要处、README.md；尽量只改必要行。

- [x] 自动学习摘要展示讨论和五星 AI 评价；用户启动自动学习即授权这些任务，不额外逐条确认。
- [x] 主代理审查第一轮 diff，必要时第二轮定点修复；最多两轮后主代理接手。
- [x] `.venv/bin/python -m unittest discover -s tests` 和 `git diff --check` 通过；报告未进行线上写入验证的限制。

## Baseline

2026-10-15：600 tests / 86.388s / OK。工作目录 main，现有未提交改动 README.md、.gitignore。按用户“本地开发”原地开发，保留这些改动。

## 公开前端协议证据（2026-10-15）

- [学生讨论列表](https://ai.icve.com.cn/js/chunk-05da9316.1791971321936.js)：discussType=4，queryUser=2；joinStatus=0 未参与、1 已参与；rows/total 分页。startTime/endTime 控制开放时间。
- [学生讨论详情](https://ai.icve.com.cn/js/chunk-7f3bd172.1791971321936.js)：详情带 title/content；顶级回复携带 courseInfoId、courseId、discussId、content、typeId=1。回复行 userId 表示作者。
- [平台 API 模块 abb7](https://ai.icve.com.cn/js/app~06837ae4.1791971321936.js)：GET /course/courseInfoDiscuss/list、GET /course/courseInfoDiscuss/{id}、GET /course/courseInfoReply/list、POST /course/courseInfoReply/add；GET /course/courseInfoEvaluation/getEvaluation、POST /course/courseInfoEvaluation/add。
- [评价组件](https://ai.icve.com.cn/app~970f9218.1791971321936.js)：读取参数 courseInfoId；提交 courseInfoId/courseId/star/content，star 默认5，content 上限500。data=null 表示无现有评价，已有评价只读。

上述为公开前端静态核对；本任务不使用真实课程写入验证。

## 执行与验证

- CLI 均明确设置 deepseek-v4-1-flash / model_reasoning_effort=high：第一轮完成初始接入（616 tests OK）；主代理审查后接手复杂活动状态处理，第二轮只完成入口、接口和摘要（100 项相关测试 OK）。没有第三轮 CLI 修改。
- 主代理补充真实 API 封装到应用入口的离线端到端检查，以及北京时间、取消、到期、目录不完整、重复主题、缺失评价状态、AI 故障和未知写入结果的回归检查，观察失败后实现修复。
- 最终 `.venv/bin/python -m unittest discover -s tests`：631 tests / 87.010s / OK。
- `git -c core.whitespace=cr-at-eol diff --check` 通过（原文件 main.py 使用 CRLF）；变更模块 compileall 通过。
- 保留用户原有 README.md、.gitignore 改动；所有代码留在本地工作目录，未提交、未推送；没有对真实课程发送讨论、评价或学习写入。

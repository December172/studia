# Studia · 课程学习与 AI 答题助手

智慧职教和智慧树的终端工具：自动学习课程、AI 答题并交卷、导出题目与答案。

[使用指南](docs/USAGE.md) · [开发与目录说明](docs/DEVELOPMENT.md) · [接口说明](docs/EXAM_PROTOCOL.md) · 维护者 [yijunjie07](https://github.com/yijunjie07)

## 功能

| 功能 | 说明 |
| --- | --- |
| 自动学习 | 完成课程学习任务；MOOC / AI 优课还会自动参与主题讨论、提交五星课程评价 |
| AI 答题 | AI 作答，全部题目通过校验后自动交卷；也可只生成本地答案 |
| 导出题目 | 保存题目和已有答案为 Markdown / Word，不调用 AI |

- 支持 **MOOC / AI 优课**（`ai.icve.com.cn`）全部功能；**资源库**只支持自动学习。
- **智慧树**（共享课、智慧课程）支持自动学习，也支持 AI 答题（共享课的作业和考试、智慧课程的知识点作业和考试）；用智慧树 App 扫码登录，课程列表按 `t` 切换平台。
- 任务列表识别开放时间，未开始、已截止和附件作业不可勾选。
- AI 答题和自动学习最多 8 项并行，单份试卷失败会记录原因并继续下一份。

完整说明见 [使用指南](docs/USAGE.md)。

## 快速开始

先安装 [Git](https://git-scm.com/downloads) 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，重新打开终端（Windows 用 PowerShell），然后执行：

```bash
git clone https://github.com/yijunjie07/studia.git
cd studia
uv sync --locked --python 3.12
uv run StartWork.py
```

uv 会自动准备 Python 3.12 和依赖，不需要手动建虚拟环境。启动后在浏览器完成平台登录，再回到终端操作；之后会自动恢复登录。

**以后启动**：在项目目录执行 `uv run StartWork.py`。执行过 `uv sync` 后，macOS / Linux 也可以直接用 `.venv/bin/python StartWork.py`。

**更新**：先退出程序，再执行：

```bash
git pull --ff-only
uv sync --locked
uv run StartWork.py
```

源码运行时显示 `pyproject.toml` 里的版本号，不会自动下载更新。

## 操作

流程：**选课程 → 选功能 → 选任务 → 确认摘要 → 运行 → 查看结果**。所有页面用同一套按键：

| 按键 | 作用 |
| --- | --- |
| `↑` `↓` / `PageUp` `PageDown` | 移动 / 翻页 |
| `Enter` | 选择或确认当前项（多选列表没有勾选时，直接选当前任务） |
| `空格` | 勾选任务（多选列表；顶部 `All` 全选） |
| `/` | 搜索 |
| 页面上标出的数字或字母 | 直接执行，例如课程列表的 `s` 设置、`t` 切换平台、`r` 刷新 |
| `Tab` / `Shift+Tab` | 在列表和按钮之间切换 |
| `Esc` | 返回上一步 |
| `q` / `Ctrl+C` | 退出 |

AI 答不出的题会在终端列出题目和选项，等你手动补答。补答过的试卷交卷前会再确认一次：`y` 交卷、`n` 不交卷并继续下一份（默认）、`p` 暂停全部。纯 AI 完成的试卷不会询问。

不登录也能预览界面：`uv run python -m tools.ui_demo --native`。

## AI 模型

内置**默认供应商**，新用户不用填 Key，直接使用项目共享额度（可用性以剩余额度为准）。

要用自己的服务：课程列表按 `s` → **模型配置** → **＋ 添加模型**，填写名称、接口地址（如 `https://服务商地址/v1`）、模型名和 API Key，选 **保存并测试**。程序会自动识别 Chat Completions 或 Responses 接口。可以保存多份配置，答题摘要里按 `m` 随时切换。

## 文件在哪里

| 内容 | 位置 |
| --- | --- |
| 导出的题目与平台答案 | `tiku/<课程名>/` |
| AI 答案与执行报告 | `ai_results/<课程名>/` |
| 课程结构导出 | `exports/structure/` |
| 本机配置（登录令牌、API Key、并发等） | `~/.config/icve-ai-assistant/settings.json`（macOS / Linux） |

以上文件和日志都已被 Git 忽略。配置文件是**未加密**的 JSON（macOS / Linux 权限为 `600`），里面有登录令牌和 Key，不要分享。

## 注意

- 不保证答案正确，也不保证完成全部课程要求。含图片、音频、附件的题目需要人工处理。
- 每次进入 AI 答题，默认都是“答题并自动交卷”，可以在执行摘要里按 `s` 改为只生成答案。
- 打开试卷可能会在平台上创建作答记录或开始计时。

## 来源

部分早期代码源自 [11273/mooc-work-answer](https://github.com/11273/mooc-work-answer)（含 tooplick 的题目导出贡献）；上游未声明许可证，再分发前请确认授权。

## 友情链接

- [LINUX DO](https://linux.do) - 真诚、友善、团结、专业，共建你我引以为荣之社区

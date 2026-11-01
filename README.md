# Studia · 课程学习与 AI 答题助手

智慧职教和智慧树的学习工具：自动学习课程、AI 答题并交卷、导出题目与答案。有终端版和网页版，也可以部署到服务器给多人用。

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
- 网页版和终端版共用登录、模型和答案记忆；网页版还能按章节看每个学习单元的进度。

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

**网页版**：执行 `uv run StartWork.py --web`，会在浏览器打开图形界面，支持 MOOC 和智慧树：课程页按章节显示每个学习单元的进度，运行时底部有进度条，可展开看日志。详见[使用指南](docs/USAGE.md#网页版)。

**服务器部署（Docker）**：给多人共用，有账号、邀请码和管理页，每人的数据分开加密存放。

```bash
mkdir studia && cd studia
curl -fsSLO https://raw.githubusercontent.com/yijunjie07/studia/main/docker-compose.yml
STUDIA_PUBLIC_URL=http://服务器IP:8080 docker compose up -d
docker compose logs studia   # 日志里有首次设置用的「设置码」
```

打开地址，第一次进入会让你填设置码并创建管理员账号。详见[使用指南](docs/USAGE.md#docker-部署)。

**以后启动**：在项目目录执行 `uv run StartWork.py`。执行过 `uv sync` 后，macOS / Linux 也可以直接用 `.venv/bin/python StartWork.py`。

**更新**：先退出程序，再执行：

```bash
git pull --ff-only
uv sync --locked
uv run StartWork.py
```

项目不再发布打包好的版本，都从源码运行；版本号见 `pyproject.toml`。

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

要用自己的服务：网页版在 **设置 → AI 模型 → ＋ 添加模型**；终端版在课程列表按 `s` → **模型配置** → **＋ 添加模型**，填写名称、接口地址（如 `https://服务商地址/v1`）、模型名和 API Key，选 **保存并测试**。程序会自动识别 Chat Completions 或 Responses 接口。可以保存多份配置，答题摘要里按 `m` 随时切换。

## 文件在哪里

| 内容 | 位置 |
| --- | --- |
| 导出的题目与平台答案 | `tiku/<课程名>/` |
| AI 答案与执行报告 | `ai_results/<课程名>/` |
| 课程结构导出 | `exports/structure/` |
| 本机配置（登录令牌、API Key、并发等） | `~/.config/icve-ai-assistant/settings.json`（macOS / Linux） |
| 答案记忆 | 与本机配置同目录：智慧树 `zhs-answers.json`，MOOC `mooc-answers.json` |
| 服务器模式 | 数据目录（Docker 是 `studia-data` 卷）：`studia.db` 是账号和全站共享的答案记忆，`users/<用户 ID>/` 是各人数据（登录信息和 Key 加密存放） |

以上文件和日志都已被 Git 忽略。本机模式的配置文件是**未加密**的 JSON（macOS / Linux 权限为 `600`），里面有登录令牌和 Key，不要分享。

## 注意

- 不保证答案正确，也不保证完成全部课程要求。智慧树智慧课程作业里的图片会发给模型识别，其他含图片、音频、附件的题目仍需人工处理。
- 终端版每次进入 AI 答题，默认都是“答题并自动交卷”，可以在执行摘要里按 `s` 改为只生成答案；网页版交卷要按两次确认。
- 打开试卷可能会在平台上创建作答记录或开始计时。

## 来源

部分早期代码源自 [11273/mooc-work-answer](https://github.com/11273/mooc-work-answer)（含 tooplick 的题目导出贡献）；上游未声明许可证，再分发前请确认授权。

## 友情链接

- [LINUX DO](https://linux.do) - 真诚、友善、团结、专业，共建你我引以为荣之社区

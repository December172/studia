# 自用集成与发布流程

本 fork（`December172/studia`）采用一个长期自用集成分支 `dev`。2026-11-06 从当时的 `main`（上游 `103cade`）建立，沿用已有集成版本的自有功能，并纳入功能分支的最新提交。

## 分支职责

| 分支 | 职责 |
| --- | --- |
| `upstream/main` | 上游维护者的发布线 |
| `main` | 跟踪上游，按 fast-forward 同步，不承载自用功能提交 |
| `dev` | 唯一长期自用集成分支；生产从这里选择通过验证的提交 |
| `feat/*`、`fix/*` | 单项开发与上游 PR；需要时合入 `dev` |
| 已有 `test/*`、`release/*` | 保留历史，后续集成和发布以 `dev` 为准 |

`dev` 保留普通合并历史，后续通过 `git merge upstream/main` 吸收更新。不要将 `dev` reset 为上游或强推重写它，也不要从单一功能分支直接制作完整生产版本。

## 当前集成内容

- 北京时间 23:00–07:00 的全用户夜间暂停：上游 [#95](https://github.com/yijunjie07/studia/pull/95) 已关闭，自用策略在 `dev` 保留。当前行为、代价和证据边界见[研究记录](research/2026-11-06-nightly-study-evidence.md)。
- 智慧树共享课掌握度练习和共享课见面课回放。
- 管理员共享 AI 模型及对应的模型状态处理。
- 尚未上游合并的 AI 响应诊断、问答生成相关修复。
- 已合入上游的超星已有答案保留与图片题支持（#90/#91），直接继承自 `main`，继续吸收维护者的后续修订。

## 同步上游

在干净的 checkout 中执行。`dev` 已在另一个 worktree 打开时，用 `git worktree list` 定位并进入那个 worktree。

```bash
git fetch upstream
git fetch origin

git switch main
git merge --ff-only upstream/main
git push origin main

git switch dev
git merge --ff-only origin/dev
git merge --no-edit upstream/main
```

`main` 无法 fast-forward 时先检查差异，不强推。合并 `dev` 发生冲突时，逐处保留上游修复和仍需保留的自有行为，解决后运行验证再提交；只要 PR 内容已经上游合并，就沿用上游实现，避免重新叠加旧补丁。

```bash
python -m unittest discover -s tests
node --check web/static/studia.js
git push origin dev
```

如果 `main` 尚未 checkout 到 worktree，通常直接在自己的普通 clone 中执行上述流程即可。本机主工作区仍有未提交改动时，先保留并核对它们，使用独立 worktree 完成集成，不覆盖正在进行的工作。

## 新功能与上游贡献

给上游的单项 PR 从 `upstream/main` 建立干净分支；将同一功能分支合入 `dev` 做组合验证。避免直接以整个 `dev` 向上游提交某一个功能，否则会混入其他自用策略。已关闭的 #95 不自动重开；有足够证据后再讨论针对性调整。

## 镜像与生产

仓库的 Publish image 工作流对 `main`、`dev` 发布 amd64/arm64 镜像：

| 镜像标签 | 用途 |
| --- | --- |
| `ghcr.io/december172/studia:latest` | 本 fork 的 `main` |
| `ghcr.io/december172/studia:dev` | 跟踪自用集成分支的候选镜像 |
| `ghcr.io/december172/studia:<完整提交 SHA>` | 固定某次提交；生产使用此标签并记录镜像 digest |

推送后，单元测试／Docker 冒烟与镜像发布分别在 Actions 中运行。候选标签存在不等于验证完成；生产只选择 Tests 和 Publish image 均成功的同一 SHA。发布镜像不会自动更新 VPS。

Python 3.9 的兼容性测试固定在 `ubuntu-24.04`：当前 `ubuntu-latest` 为 26.04，setup-python 尚无相应的 3.9 安装包。同一分支的镜像发布采用并发组，取消旧构建，避免过时构建最后完成时覆盖候选标签。

上线时修改现有 Compose 的镜像引用，先停稳并备份整个 `/data`，保留原数据卷、主密钥和实际网络设置，再用 `docker compose up` 重建并验证。完整的私人部署地址、凭据、备份和运行日志留在仓库外。生产回滚到上一个已验证的 `dev` SHA；若回退到不含夜间暂停的旧版，夜间保持停止，07:00 后再启动。

`dev` 的镜像不会发布到上游维护者的 GHCR 命名空间。首次发布的包是否可公开拉取，以 GitHub Packages 实际权限为准；私有包使用自己的登录凭据，不将凭据写入 Compose、文档或 Git。

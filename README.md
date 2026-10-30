# 即梦 MCP

**你负责脑洞，Agent 负责开工。**

灵感来了，还得先翻半小时命令行？提示词已经够费脑子，参数说明书就交给 Agent 吧。

即梦 MCP 把即梦的画布、图片、视频、音频，以及部分主体和短剧工作室能力，接到你常用的 AI Agent。连接后，你用日常语言交代需求，Agent 负责查参数、提交任务、跟踪进度和取回素材。

## 折腾这些，是为了少折腾

从想法到成品，常常要在几套命令之间来回切换：找参数、查任务、记编号、下载文件。我们把这些琐事整理成一套 MCP 工具，让创作流程能在对话里接着往下走。

- **少背命令，多想创意。** Agent 按需查参数，再调用工具。参数不用背成绕口令。
- **一个入口，把流程接起来。** 画布操作、媒体生成、主体管理和故事板都有统一的调用方式，省去在几套 CLI 之间反复切换。
- **任务有下落，进度有地方查。** 提交记录和队列保存在本机；对话断了，也能找回原任务继续查询。重试时复用原提交记录，减少重复开工。
- **积分怎么花，你说了算。** 先预览操作，再正式执行；生成额度由你设置。灵感可以放飞，钱包先系好安全带。
- **素材拿得到，后续接得上。** 已有受管素材可以下载、校验并导出到本机，方便交给下一步制作。
- **用自己的账号，在自己的电脑开工。** 授权后直接连接本机 Agent，省去搭建公共 MCP 服务器这一关。

目前提供 **10 个 MCP 工具、122 项业务动作**。具体功能和开发进度见 [功能清单](docs/features.md)。

## 三步上手

准备 **Python 3.12+、Git** 和一个支持 MCP 的 Agent。macOS、Linux 可以使用；Windows 请在 **WSL2** 中安装。

### 1. 安装

已安装 uv 时，可以直接运行下面的命令；也可以展开下方说明，从源码安装：

```sh
uv tool install 'git+https://github.com/zsn2003/jimeng-mcp.git'
jimeng-mcp install-cli all
```

第二条命令会下载两套官方 CLI，并自动校验文件。安装完可以运行 `jimeng-mcp doctor` 看看是否就绪。

<details>
<summary>习惯从源码安装？展开这里</summary>

```sh
git clone https://github.com/zsn2003/jimeng-mcp.git
cd jimeng-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/jimeng-mcp install-cli all
```

后面的命令把 `jimeng-mcp` 换成 `.venv/bin/jimeng-mcp` 即可。

</details>

### 2. 登录即梦

先连接画布与基础创作能力：

```sh
jimeng-mcp auth login canvas
```

终端会给出授权网址和验证码。打开网址完成授权，再回到终端运行：

```sh
jimeng-mcp auth complete canvas
```

需要多关键帧、创作会话和任务历史时，再连接创作 CLI：

```sh
jimeng-mcp auth login plugin
# 同样打开输出的网址，完成授权后运行：
jimeng-mcp auth complete plugin
```

最后运行 `jimeng-mcp doctor --check-auth` 检查登录状态。个人主体库和部分网页工作室功能还有一个独立的网页登录入口，按需查看 [登录指南](docs/authentication.md#网页工作室)。

### 3. 接上 Agent

生成适合你安装位置的 MCP 配置：

```sh
jimeng-mcp client-config
```

将输出加入 Agent 的 MCP 配置中。它使用 **stdio** 连接：Agent 在同一电脑、同一系统用户下启动这个工具。

使用 Codex CLI 时，也可以直接添加：

```sh
codex mcp add jimeng -- /absolute/path/to/jimeng-mcp serve
```

把示例路径换成 `command -v jimeng-mcp` 的输出；源码安装则填写 `.venv/bin/jimeng-mcp` 的绝对路径。

<details>
<summary>通用 MCP 配置示例</summary>

```json
{
  "mcpServers": {
    "jimeng": {
      "command": "/absolute/path/to/jimeng-mcp",
      "args": ["serve"]
    }
  }
}
```

连接需要客户端支持本地 stdio MCP。

</details>

## 接好以后，可以这样开口

> 看看我的即梦账号状态，以及现在能用哪些功能。

> 列出这个画布里的节点，帮我整理一下有哪些图片和视频。

> 按这个提示词准备一张 16:9 海报，先给我看生成参数，确认后再提交。

> 查一下刚才那个任务，完成后把可下载的素材保存下来。

你描述目标，Agent 通过 MCP 操作即梦。自然语言理解由你连接的 Agent 完成。

## 准备正式开工：设个积分额度

刚安装时，生成额度为 **0**，写操作默认先预览。这样可以先把账号和流程跑通。

准备生成时，设置单次操作的积分上限，例如：

```sh
jimeng-mcp config --max-credits 20
```

重启 Agent 中的 MCP 服务后生效，再让 Agent 正式执行。这里的 20 是**支持额度控制的单次操作上限**，实际积分和模型以即梦账号为准。

<details>
<summary>更多操作设置</summary>

```sh
# 只查询，暂停真实修改
jimeng-mcp config --read-only

# 恢复写入能力
jimeng-mcp config --allow-writes

# 视频流程优先做样片
jimeng-mcp config --video-policy sample_first

# 开放无法设置数字上限的网页、多关键帧等操作
jimeng-mcp config --max-credits unlimited
```

`unlimited` 会取消本工具的数字额度限制，实际调用仍消耗账号积分，请按自己的预算选择。

写操作预览使用 `dryRun:true`，正式执行使用 `dryRun:false`；永久删除另需确认具体对象。设置保存在本机 `settings.json`，每次修改后重启 MCP 服务。

</details>

## 几个常见问题

**所有即梦功能都接好了吗？** 目前功能目录有 124 项，其中 108 项已有完整或部分接入，16 项仍待开发。[功能清单](docs/features.md)列出了当前进度。

**登录一次就够了吗？** 画布 CLI、创作 CLI 和网页工作室分别管理登录。按使用的功能连接对应入口即可，详见 [登录指南](docs/authentication.md)。

**任务卡住了怎么办？** 让 Agent 查询原任务。遇到结果不明的状态，先核对它在即梦上的进度，再决定下一步；这样能减少重复生成。

**能同时接两个 Agent 吗？** 一个数据目录同时运行一个服务进程。切换客户端时先关闭原服务；独立用途可以通过 `--data-dir` 使用不同目录。

**现在测到哪一步了？** 已完成 406 项离线测试，以及安装包、GitHub 标签安装和官方 CLI 安装检查。真实账号的模型与生成结果由用户实际调用确认；平台测试详情在 [验证记录](docs/verification.md)。

## 给开发者

<details>
<summary>10 个工具，分别负责什么</summary>

| 工具 | 用途 |
| --- | --- |
| `capabilities` | 查看可调用动作和当前配置 |
| `studio_catalog` | 查看功能覆盖与开发进度 |
| `action_schema` | 获取某个动作的参数说明 |
| `submit_job` / `submit_job_json` | 提交并保存任务 |
| `get_job` | 查询任务和业务结果 |
| `list_jobs` / `queue_summary` | 找回任务、查看排队情况 |
| `cancel_job` | 取消尚未开始执行的任务 |
| `artifact_manifest` | 校验受管素材并导出本地文件 |

常用顺序：`capabilities` → `action_schema` → `submit_job` → `get_job`。重试同一请求时保留原 `idempotencyKey` 和完整参数。

判断执行结果时，结合 `operationOutcome`、资源状态和实际文件一起看。`COMPLETED` 表示工具调用已结束，媒体生成可能还需要继续查询。

</details>

```sh
python3 scripts/test_all.py
python3 scripts/build_release.py
```

运行时使用 Python 标准库。发布脚本需要 uv，会生成 wheel、源码包和 `SHA256SUMS`，并清理源码归档中的本机用户元数据。GitHub Actions 配置可参考 [工作流模板](docs/github-actions-example.yml)。

欢迎通过 Issue 分享问题和建议。提供版本、操作步骤和错误码，就很有帮助；账号凭据留在本机，详情见 [安全说明](SECURITY.md)。

---

独立社区项目，基于官方 CLI 与部分网页接口适配。即梦及相关商标归各自权利人所有。代码采用 [MIT 许可](LICENSE)，第三方说明见 [NOTICE](NOTICE)。
